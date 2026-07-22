"""Adaptadores CLI headless (D5): el debate cubierto por suscripciones.

Implementan el mismo Protocol AgentAdapter que los adaptadores API, pero
delegan cada turno a un CLI de agente (`claude -p` / `gemini -p`) vía
subprocess. Motivo: las suscripciones de consumidor (Claude Pro/Max, Google
AI Pro) cubren los CLI pero no las claves API (DISENO.md §11 D5).

Diferencias con el camino API (invariantes en DISENO.md §11, decisión D5):
  - El ToolRegistry local NO se usa: las herramientas son las del propio CLI,
    restringidas a SOLO LECTURA vía flags. Es una garantía más débil que el
    sandbox propio de read_file.
  - El cwd del subprocess es la raíz del repo del debate, para que las
    lecturas relativas del CLI caigan dentro del proyecto.
"""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import threading
import time

from ..tools.registry import ToolRegistry
from .base import (
    AgentCancelledError,
    AgentError,
    DeltaCb,
    SessionLimitError,
    TransientProviderError,
    TurnUsage,
)


class CliAdapterError(AgentError):
    """Fallo de CLI sin clasificación conocida (no reintentable)."""


_TRANSITORIO_RE = re.compile(
    r"(?i)\b(503|429|529|resource_exhausted|unavailable|overloaded|high demand|rate limit)\b"
)
_LIMITE_SESION_RE = re.compile(r"(?i)session limit")
_RESETS_RE = re.compile(r"(?i)resets\s+([^\n·]+)")

# Tope corto para cerrar un stream: tras el EOF de stdout, cuánto esperar a que
# el proceso muera (o el hilo de stderr drene) antes de matar el grupo. El texto
# final ya está parseado a esta altura; es solo higiene de cierre.
_ESPERA_CIERRE = 5


def clasificar_fallo(detalle: str, prefijo: str) -> AgentError:
    """Mapea el texto de error de un CLI a la taxonomía (plan de resiliencia).

    Heurística textual: es el único dato disponible en los CLIs de texto
    plano. Sin match → CliAdapterError genérico, que el orquestador NO
    reintenta (mejor abortar informando que reintentar a ciegas).
    """
    if _LIMITE_SESION_RE.search(detalle):
        m = _RESETS_RE.search(detalle)
        resets = m.group(1).strip() if m else None
        return SessionLimitError(f"{prefijo}: {detalle}", resets_at=resets)
    if _TRANSITORIO_RE.search(detalle):
        return TransientProviderError(f"{prefijo}: {detalle}")
    return CliAdapterError(f"{prefijo}: {detalle}")


def env_suscripcion() -> dict[str, str]:
    """Entorno para el subprocess SIN credenciales API heredables.

    Nuestro proceso carga las claves API desde .env para los backends api; si
    un CLI las hereda, les da precedencia sobre su login de suscripción y
    factura (o falla) contra la clave/proyecto. Verificado en real con
    ANTHROPIC_API_KEY (D5) y aplica igual a las variables de Google. Se
    quitan para que cada CLI use su propia sesión.
    """
    return {
        k: v
        for k, v in os.environ.items()
        if k not in (
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
            "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_CLOUD_PROJECT",
        )
    }


def _run(
    argv: list[str], cwd: str, timeout: int, name: str, cancel_event=None
) -> subprocess.CompletedProcess:
    """Corre el CLI. Con `cancel_event` (objeto con .is_set()) el turno es
    INTERRUMPIBLE: si el vocero cancela, se mata el subprocess al instante en
    vez de esperar minutos a que termine. Sin evento, se comporta como antes.
    """
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            # stdin cerrado (EOF inmediato): un CLI que sondee la entrada al no
            # hallar flags interactivos se quedaría esperando el terminal
            # heredado — el "no responde" hasta reventar el timeout.
            stdin=subprocess.DEVNULL,
            text=True, env=env_suscripcion(),
            # Grupo de procesos propio: al cancelar/timeout matamos el árbol
            # entero (el CLI puede lanzar hijos; matar solo el padre los deja
            # huérfanos reteniendo los pipes → communicate colgaría).
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise CliAdapterError(
            f"No se encontró el binario '{argv[0]}'. ¿Está instalado el CLI de {name}?"
        ) from exc

    inicio = time.monotonic()
    while True:
        try:
            # communicate lee stdout/stderr en paralelo (sin deadlock por pipe
            # lleno) y, tras TimeoutExpired, reintentar no pierde salida.
            out, err = proc.communicate(timeout=0.5)
            return subprocess.CompletedProcess(argv, proc.returncode, out, err)
        except subprocess.TimeoutExpired:
            if cancel_event is not None and cancel_event.is_set():
                _matar_grupo(proc)
                raise AgentCancelledError(f"{name}: turno cancelado por el vocero.")
            if time.monotonic() - inicio > timeout:
                _matar_grupo(proc)
                raise CliAdapterError(f"El CLI de {name} superó el timeout de {timeout}s.")


def _terminar_grupo(proc: subprocess.Popen) -> None:
    """Mata el árbol de procesos del CLI (grupo). NO drena los pipes: úsalo
    cuando otro hilo está leyendo stdout (camino streaming) — llamar aquí a
    `communicate` chocaría con ese lector. El lector verá EOF y terminará solo.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()  # el grupo ya murió, o no aplica: mata al menos el padre


def _matar_grupo(proc: subprocess.Popen) -> None:
    """Mata el árbol de procesos del CLI (grupo) y drena los pipes.

    Camino por turnos (sin lector concurrente): tras matar, `communicate`
    recoge lo que quedó en los pipes sin riesgo de deadlock.
    """
    _terminar_grupo(proc)
    try:
        proc.communicate(timeout=5)
    except (subprocess.TimeoutExpired, ValueError):
        pass


def _run_stream(
    argv: list[str],
    cwd: str,
    timeout: int,
    name: str,
    on_delta: DeltaCb | None,
    cancel_event=None,
) -> tuple[int, dict | None, str]:
    """Corre un CLI con salida `stream-json` (JSONL) leyéndola incrementalmente.

    Emite cada `text_delta` por `on_delta` a medida que llega y devuelve
    `(returncode, mensaje 'result' | None, stderr)`. El mensaje `result` trae
    los mismos campos que `--output-format json` (`result`, `is_error`,
    `api_error_status`, `usage`, `total_cost_usd`), así que el parseo posterior
    no cambia respecto al camino por turnos.

    Un hilo lector consume stdout (y otro stderr, para no bloquear el proceso
    si llena su pipe). Por eso, al cancelar/timeout se usa `_terminar_grupo`
    (mata sin `communicate`): drenar con `communicate` chocaría con el lector.
    Interrumpible: el hilo principal chequea `cancel_event` entre líneas y en
    cada silencio de 0.5 s, y mata el subprocess en vuelo.
    """
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,  # mismo motivo que en _run
            text=True, env=env_suscripcion(), start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise CliAdapterError(
            f"No se encontró el binario '{argv[0]}'. ¿Está instalado el CLI de {name}?"
        ) from exc

    lineas: queue.Queue = queue.Queue()
    _FIN = object()

    def _leer_stdout() -> None:
        try:
            for linea in proc.stdout:
                lineas.put(linea)
        finally:
            lineas.put(_FIN)

    err_partes: list[str] = []

    def _leer_stderr() -> None:
        try:
            for linea in proc.stderr:
                err_partes.append(linea)
        except (ValueError, OSError):  # pipe cerrado al matar el grupo
            pass

    hilo_out = threading.Thread(target=_leer_stdout, daemon=True)
    hilo_err = threading.Thread(target=_leer_stderr, daemon=True)
    hilo_out.start()
    hilo_err.start()

    def _cancelado_o_timeout() -> None:
        if cancel_event is not None and cancel_event.is_set():
            _terminar_grupo(proc)
            raise AgentCancelledError(f"{name}: turno cancelado por el vocero.")
        if time.monotonic() - inicio > timeout:
            _terminar_grupo(proc)
            raise CliAdapterError(f"El CLI de {name} superó el timeout de {timeout}s.")

    inicio = time.monotonic()
    resultado: dict | None = None
    while True:
        try:
            item = lineas.get(timeout=0.5)
        except queue.Empty:
            _cancelado_o_timeout()
            continue
        if item is _FIN:
            break
        _cancelado_o_timeout()  # streams veloces no pasan por el Empty de arriba
        linea = item.strip()
        if not linea:
            continue
        try:
            msg = json.loads(linea)
        except ValueError:
            continue  # línea no-JSON (un log suelto del CLI): se ignora
        tipo = msg.get("type")
        if tipo == "result":
            resultado = msg
        elif tipo == "stream_event" and on_delta is not None:
            evento = msg.get("event") or {}
            if evento.get("type") == "content_block_delta":
                delta = evento.get("delta") or {}
                if delta.get("type") == "text_delta":
                    texto = delta.get("text") or ""
                    if texto:
                        on_delta(texto)

    # El _FIN dice que stdout llegó a EOF, no que el proceso murió: un hijo que
    # cierra stdout pero sigue vivo colgaría `proc.wait()` para siempre, justo
    # tras haber honrado timeout/cancel todo el lazo. Esperar con tope y, si no
    # cierra, matar el grupo. El `resultado` ya está capturado (el lector puso
    # _FIN tras drenar todo stdout), así que matar aquí no pierde salida.
    try:
        proc.wait(timeout=_ESPERA_CIERRE)
    except subprocess.TimeoutExpired:
        _terminar_grupo(proc)
        proc.wait()
    # Drenar el hilo de stderr antes de leer err_partes, o el diagnóstico del
    # fallo sale truncado (el hilo puede no haber terminado de anexar sus líneas).
    hilo_err.join(timeout=_ESPERA_CIERRE)
    return proc.returncode, resultado, "".join(err_partes)


class ClaudeCliAdapter:
    """Turno de debate vía Claude Code headless (`claude -p`).

    Usa `--output-format stream-json` para emitir los tokens a medida que
    llegan (streaming, `on_delta`) y captar el texto final y las métricas del
    turno (`usage`, `total_cost_usd`) del mensaje `result`, mapeadas a
    TurnUsage en `last_usage`. Por turno, nunca acumuladas aquí (plan §13: la
    totalización es del orquestador; el estado acumulativo era el antipatrón).

    Es el único adaptador con streaming hoy (`soporta_streaming`): el mensaje
    muda por turno era el dolor de UX que D6 dejó pendiente hasta la web (M7).
    """

    soporta_streaming = True

    def __init__(self, binary: str = "claude", cwd: str = ".", timeout: int = 600) -> None:
        self.name = "claude"
        self.binary = binary
        self.cwd = cwd
        self.timeout = timeout
        self.last_usage: TurnUsage | None = None
        # Señal de cancelación (la fija el orquestador); None = no cancelable.
        self.cancel_event = None
        # Callback de deltas (lo fija el orquestador); None = no emitir deltas.
        self.on_delta: DeltaCb | None = None

    def build_argv(self, system: str, prompt: str) -> list[str]:
        # Solo herramientas de lectura: el debate nunca escribe (D5).
        # stream-json + partial messages = tokens en vivo; `-p` con stream-json
        # exige `--verbose` (lo pide el propio CLI).
        return [
            self.binary,
            "-p", prompt,
            "--append-system-prompt", system,
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--allowedTools", "Read,Glob,Grep",
        ]

    def converse(self, system: str, prompt: str, registry: ToolRegistry) -> str:
        self.last_usage = None
        returncode, data, err = _run_stream(
            self.build_argv(system, prompt), self.cwd, self.timeout,
            "Claude Code", self.on_delta, self.cancel_event,
        )
        # El mensaje 'result' es la autoridad del turno: si llegó, el turno
        # cerró. Un returncode distinto de 0 puede venir de la limpieza del
        # stream (un hijo colgado que hubo que matar tras capturar el result),
        # y ese código de higiene NO debe invalidar un turno ya completo. Por
        # eso se mira el result antes que el returncode.
        if data is None:
            detalle = (err or "").strip()[:500]
            if returncode not in (0, None):
                raise clasificar_fallo(detalle, f"claude -p salió con código {returncode}")
            raise CliAdapterError(
                "La salida stream-json de claude -p no incluyó un mensaje 'result'."
                + (f" stderr: {detalle}" if detalle else "")
            )
        if data.get("is_error"):
            # El JSON trae la clasificación fina: texto del error + status API.
            detalle = str(data.get("result", ""))
            status = data.get("api_error_status")
            if isinstance(status, int) and (status == 429 or status >= 500):
                detalle = f"{detalle} [{status}]"
            raise clasificar_fallo(detalle, "claude -p reportó error")

        texto = str(data.get("result", "")).strip()
        if not texto:
            # Mismo criterio que el camino de texto plano: un turno sin texto
            # no es un turno. Aquí el JSON no marcó error, así que el diagnóstico
            # útil está en stderr si lo hubo.
            detalle = (err or "").strip()[:500]
            raise clasificar_fallo(
                detalle or "el mensaje 'result' llegó vacío.",
                "claude -p terminó sin producir respuesta",
            )

        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        cost = data.get("total_cost_usd")
        self.last_usage = TurnUsage(
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cache_read_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
            cache_creation_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
            cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
        )
        return texto


class PlainCliAdapter:
    """CLI headless de texto plano (M8): `<binary> -p "<prompt>"` → stdout.

    Para CLIs sin flag de system prompt: las instrucciones de rol se anteponen
    al prompt. Sin escritura: no se pasan modos de auto-aprobación; en modo
    print las herramientas que exigen confirmación quedan denegadas y las de
    lectura funcionan. No reportan métricas por stdout (last_usage = None).
    """

    def __init__(
        self,
        name: str,
        binary: str,
        cwd: str = ".",
        timeout: int = 600,
        extra_args: list[str] | None = None,
    ) -> None:
        self.name = name
        self.binary = binary
        self.cwd = cwd
        self.timeout = timeout
        self.extra_args = list(extra_args or [])
        self.last_usage: TurnUsage | None = None
        self.cancel_event = None  # la fija el orquestador; None = no cancelable

    def build_argv(self, system: str, prompt: str) -> list[str]:
        combinado = f"INSTRUCCIONES DE SISTEMA (tu rol):\n{system}\n\n{prompt}"
        return [self.binary, "-p", combinado, *self.extra_args]

    def converse(self, system: str, prompt: str, registry: ToolRegistry) -> str:
        self.last_usage = None
        proc = _run(self.build_argv(system, prompt), self.cwd, self.timeout,
                    self.name, self.cancel_event)
        if proc.returncode != 0:
            detalle = (proc.stderr or proc.stdout).strip()[:500]
            raise clasificar_fallo(
                detalle, f"{self.binary} -p salió con código {proc.returncode}"
            )
        salida = proc.stdout.strip()
        if not salida:
            # Éxito aparente (código 0) SIN respuesta: no es un turno, es un
            # fallo mudo. Verificado en real con `agy`, que sale 0 y no imprime
            # nada cuando una herramienta suya se auto-deniega en headless; el
            # porqué solo viaja en stderr, que aquí se descartaba por venir el
            # código en 0. Aceptarlo como turno dejaba a un agente MUDO en el
            # debate: rondas y síntesis se completaban con un solo participante
            # y el vocero pagaba un debate que nunca ocurrió.
            detalle = (proc.stderr or "").strip()[:500]
            raise clasificar_fallo(
                detalle or "no imprimió nada en stdout ni en stderr.",
                f"{self.binary} -p terminó sin producir respuesta",
            )
        return salida


class GeminiCliAdapter(PlainCliAdapter):
    """Gemini CLI headless (`gemini -p`)."""

    def __init__(self, binary: str = "gemini", cwd: str = ".", timeout: int = 600) -> None:
        super().__init__("gemini", binary, cwd, timeout)


class AntigravityCliAdapter(PlainCliAdapter):
    """Antigravity headless (`agy -p`), el CLI agéntico de Google.

    Sin `model` usa el default configurado en el propio agy (p. ej.
    "Gemini 3.1 Pro (High)") — el motivo de su entrada al roster (D7).
    """

    def __init__(
        self,
        binary: str = "agy",
        cwd: str = ".",
        timeout: int = 1500,
        model: str | None = None,
    ) -> None:
        # Los modelos Pro en tareas ancladas a código real pueden tardar >10
        # min por turno (verificado en real). El --print-timeout interno de
        # agy (default 5m) debe acompañar al timeout del subprocess.
        extra = ["--print-timeout", f"{max(60, timeout - 60)}s"]
        if model:
            extra += ["--model", model]
        super().__init__("antigravity", binary, cwd, timeout, extra)


class KimiCliAdapter(PlainCliAdapter):
    """Kimi CLI headless (`kimi -p`), de Moonshot — diversidad de familia."""

    def __init__(self, binary: str = "kimi", cwd: str = ".", timeout: int = 600) -> None:
        super().__init__("kimi", binary, cwd, timeout, ["--output-format", "text"])

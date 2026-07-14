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
import re
import subprocess

from ..tools.registry import ToolRegistry
from .base import AgentError, SessionLimitError, TransientProviderError, TurnUsage


class CliAdapterError(AgentError):
    """Fallo de CLI sin clasificación conocida (no reintentable)."""


_TRANSITORIO_RE = re.compile(
    r"(?i)\b(503|429|529|resource_exhausted|unavailable|overloaded|high demand|rate limit)\b"
)
_LIMITE_SESION_RE = re.compile(r"(?i)session limit")
_RESETS_RE = re.compile(r"(?i)resets\s+([^\n·]+)")


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


def _run(argv: list[str], cwd: str, timeout: int, name: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            env=env_suscripcion(),
        )
    except FileNotFoundError as exc:
        raise CliAdapterError(
            f"No se encontró el binario '{argv[0]}'. ¿Está instalado el CLI de {name}?"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CliAdapterError(
            f"El CLI de {name} superó el timeout de {timeout}s."
        ) from exc


class ClaudeCliAdapter:
    """Turno de debate vía Claude Code headless (`claude -p`).

    Usa `--output-format json` para captar el texto final y las métricas del
    turno (`usage`, `total_cost_usd`), mapeadas a TurnUsage en `last_usage`.
    Por turno, nunca acumuladas aquí (plan §13: la totalización es del
    orquestador; el estado acumulativo en el adaptador era el antipatrón).
    """

    def __init__(self, binary: str = "claude", cwd: str = ".", timeout: int = 600) -> None:
        self.name = "claude"
        self.binary = binary
        self.cwd = cwd
        self.timeout = timeout
        self.last_usage: TurnUsage | None = None

    def build_argv(self, system: str, prompt: str) -> list[str]:
        # Solo herramientas de lectura: el debate nunca escribe (D5).
        return [
            self.binary,
            "-p", prompt,
            "--append-system-prompt", system,
            "--output-format", "json",
            "--allowedTools", "Read,Glob,Grep",
        ]

    def converse(self, system: str, prompt: str, registry: ToolRegistry) -> str:
        self.last_usage = None
        proc = _run(self.build_argv(system, prompt), self.cwd, self.timeout, "Claude Code")
        if proc.returncode != 0:
            detalle = (proc.stderr or proc.stdout).strip()[:500]
            raise clasificar_fallo(detalle, f"claude -p salió con código {proc.returncode}")
        try:
            data = json.loads(proc.stdout)
        except ValueError as exc:
            raise CliAdapterError(
                f"Salida no-JSON de claude -p: {proc.stdout.strip()[:200]}"
            ) from exc
        if data.get("is_error"):
            # El JSON trae la clasificación fina: texto del error + status API.
            detalle = str(data.get("result", ""))
            status = data.get("api_error_status")
            if isinstance(status, int) and (status == 429 or status >= 500):
                detalle = f"{detalle} [{status}]"
            raise clasificar_fallo(detalle, "claude -p reportó error")

        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        cost = data.get("total_cost_usd")
        self.last_usage = TurnUsage(
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cache_read_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
            cache_creation_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
            cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
        )
        return str(data.get("result", "")).strip()


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

    def build_argv(self, system: str, prompt: str) -> list[str]:
        combinado = f"INSTRUCCIONES DE SISTEMA (tu rol):\n{system}\n\n{prompt}"
        return [self.binary, "-p", combinado, *self.extra_args]

    def converse(self, system: str, prompt: str, registry: ToolRegistry) -> str:
        self.last_usage = None
        proc = _run(self.build_argv(system, prompt), self.cwd, self.timeout, self.name)
        if proc.returncode != 0:
            detalle = (proc.stderr or proc.stdout).strip()[:500]
            raise clasificar_fallo(
                detalle, f"{self.binary} -p salió con código {proc.returncode}"
            )
        return proc.stdout.strip()


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
        timeout: int = 600,
        model: str | None = None,
    ) -> None:
        extra = ["--model", model] if model else []
        super().__init__("antigravity", binary, cwd, timeout, extra)


class KimiCliAdapter(PlainCliAdapter):
    """Kimi CLI headless (`kimi -p`), de Moonshot — diversidad de familia."""

    def __init__(self, binary: str = "kimi", cwd: str = ".", timeout: int = 600) -> None:
        super().__init__("kimi", binary, cwd, timeout, ["--output-format", "text"])

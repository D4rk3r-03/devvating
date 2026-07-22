"""Fase 4 — Ejecución del plan aprobado (M3).

Estrategia híbrida (DISENO.md sección 4.4): el debate se hace por API, pero la
ejecución se delega a un agente de consola headless (`claude -p`), que ya trae
herramientas de entorno probadas. El ejecutor añade la envoltura de seguridad:
  - Corre en una RAMA (git como red de seguridad).
  - Por defecto solo permite EDITAR archivos; los comandos (Bash) requieren
    opt-in explícito del vocero (freno destructivo, D2).
  - Al terminar, muestra el DIFF para que el vocero revise y decida.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Protocol, runtime_checkable

from . import gitutil
from .adapters.cli import env_suscripcion


class ExecutorError(RuntimeError):
    pass


@dataclass
class ExecutionPlan:
    text: str
    title: str = "plan"
    # Preguntas de las decisiones cruciales que el vocero aún no resolvió. Si no
    # está vacío, el gate bloquea la ejecución: un plan con una ambigüedad
    # crucial abierta no está cerrado, y aplicarlo hace que el ejecutor la
    # resuelva en silencio (el bucle que motivó esta feature).
    decisiones_pendientes: list[str] = field(default_factory=list)


def decisiones_crucial_sin_resolver(decisiones) -> list[str]:
    """Preguntas de las decisiones crucial que faltan por resolver — la
    condición del gate. Acepta dicts (del transcript) o dataclasses Decision,
    para ser la única verdad que compartan el Executor y el Hub."""
    preguntas: list[str] = []
    for d in decisiones or []:
        if isinstance(d, dict):
            crucial, resuelta, pregunta = d.get("crucial"), d.get("resuelta"), d.get("pregunta", "")
        else:
            crucial = getattr(d, "crucial", False)
            resuelta = getattr(d, "resuelta", False)
            pregunta = getattr(d, "pregunta", "")
        if crucial and not resuelta:
            preguntas.append(str(pregunta))
    return preguntas


@dataclass
class ExecutionOutcome:
    branch: str
    backend: str
    returncode: int
    backend_output: str
    diff: str
    changed_files: list[str] = field(default_factory=list)
    allow_commands: bool = False
    # Rama previa a crear la de ejecución: a dónde volver si el vocero descarta.
    base_branch: str = ""
    # Worktree DESECHABLE donde se aplicó el plan (D9 paso 2): el commit y el
    # descarte operan ahí, no sobre el árbol del vocero.
    worktree: str = ""
    # Fase 5 (M9) — verificación tras aplicar el plan. verify_command vacío =
    # no se pidió verificación (comportamiento clásico, sin cambios).
    verify_command: str = ""
    verify_returncode: int | None = None
    verify_output: str = ""
    # True si el comando falló y se intentó UNA corrección acotada (1
    # iteración del mismo backend, con la salida del fallo como contexto).
    # verify_returncode/verify_output quedan con el resultado tras corregir.
    verify_corrected: bool = False


EventCb = Callable[[str, str], None]


def base_worktrees() -> str:
    """Directorio raíz de los worktrees de ejecución.

    Único lugar que resuelve esta ruta: la usan el Executor para crearlos y
    `limpiar`/Hub para recogerlos, y si divergen la limpieza mira donde no es.
    `DEVVATING_WORKTREE_DIR` la redirige (la suite la apunta a un tmp_path por
    test; en real sirve para sacarlos de un /tmp pequeño o volátil).
    """
    return os.environ.get("DEVVATING_WORKTREE_DIR") or os.path.join(
        tempfile.gettempdir(), "devvating-worktrees"
    )


def _exec_prompt(plan: ExecutionPlan) -> str:
    return (
        "Aplica el siguiente plan aprobado en este repositorio. Realiza ÚNICAMENTE "
        "los cambios que describe el plan; no añadas mejoras ni refactors extra. "
        "Si algo del plan es ambiguo, elige la interpretación más conservadora.\n\n"
        f"PLAN:\n{plan.text}"
    )


def _correction_prompt(plan: ExecutionPlan, verify_command: str, verify_output: str) -> str:
    return (
        "El plan que acabas de aplicar en este repositorio NO pasó la "
        "verificación del proyecto. Corrige el código para que la verificación "
        "pase, sin desviarte del plan original ni añadir cambios fuera de su "
        "alcance.\n\n"
        f"PLAN ORIGINAL:\n{plan.text}\n\n"
        f"COMANDO DE VERIFICACIÓN: {verify_command}\n\n"
        f"SALIDA DEL FALLO:\n{verify_output}"
    )


@runtime_checkable
class HeadlessBackend(Protocol):
    name: str

    def run(self, prompt: str, cwd: str, allow_commands: bool) -> tuple[int, str]:
        """Ejecuta el agente headless en cwd; devuelve (returncode, salida)."""
        ...


class ClaudeCodeBackend:
    """Delega en Claude Code en modo headless (`claude -p`).

    D8 — separación de modelos por fase: la ejecución usa un modelo EJECUTOR
    (default "sonnet": el alias del CLI resuelve al Sonnet vigente — 5 hoy,
    4.x si el plan no lo trae) y los modelos de más razonamiento quedan para
    el debate. Configurable con DEVVATING_EXEC_MODEL o el flag --model.
    """

    name = "claude-code"

    def __init__(self, binary: str = "claude", model: str | None = None) -> None:
        self.binary = binary
        self.model = model or os.environ.get("DEVVATING_EXEC_MODEL", "sonnet")

    def build_argv(self, prompt: str, allow_commands: bool) -> list[str]:
        # Sin comandos: auto-acepta ediciones de archivo, nada de Bash -> nada que
        # confirmar en headless. Con comandos: opt-in explícito y peligroso.
        argv = [self.binary, "-p", prompt, "--model", self.model]
        if allow_commands:
            argv += ["--dangerously-skip-permissions"]
        else:
            argv += ["--permission-mode", "acceptEdits",
                     "--allowedTools", "Read,Edit,Write"]
        return argv

    def run(self, prompt: str, cwd: str, allow_commands: bool) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                self.build_argv(prompt, allow_commands),
                cwd=cwd,
                capture_output=True,
                text=True,
                # stdin cerrado: sin él, el CLI hereda el terminal y puede
                # quedarse esperando entrada (mismo blindaje que adapters/cli).
                stdin=subprocess.DEVNULL,
                # Sin ANTHROPIC_API_KEY heredada: el CLI debe usar el login de
                # suscripción, no facturar contra la clave del backend api (D5).
                env=env_suscripcion(),
            )
        except FileNotFoundError as exc:
            raise ExecutorError(
                f"No se encontró el binario '{self.binary}'. ¿Está instalado Claude Code?"
            ) from exc
        return proc.returncode, (proc.stdout + proc.stderr)


class Executor:
    def __init__(
        self, repo: str, backend: HeadlessBackend, on_event: EventCb | None = None
    ) -> None:
        self.repo = repo
        self.backend = backend
        self._on_event = on_event or (lambda *_: None)

    def _default_branch(self, title: str) -> str:
        slug = "".join(c if c.isalnum() else "-" for c in title.lower())[:40].strip("-")
        return f"devvating/{slug or 'plan'}-{time.strftime('%Y%m%d-%H%M%S')}"

    def _worktree_path(self, branch: str) -> str:
        # En el temp del sistema, NO bajo .git/: un worktree dentro de .git
        # confunde a `claude -p` (trata .git como interno y no escribe ahí,
        # verificado en real). El dir final lo crea `git worktree add`.
        base = base_worktrees()
        os.makedirs(base, exist_ok=True)
        slug = branch.replace("/", "-")
        # Sufijo único (no timestamp): dos ejecuciones en el mismo segundo
        # colisionarían el path, y `git worktree add` exige que no exista.
        return os.path.join(base, f"{slug}-{uuid.uuid4().hex[:8]}")

    def execute(
        self,
        plan: ExecutionPlan,
        *,
        allow_commands: bool = False,
        branch: str | None = None,
        verify_command: str | None = None,
        allow_open_decisions: bool = False,
    ) -> ExecutionOutcome:
        if not gitutil.is_git_repo(self.repo):
            raise ExecutorError(
                f"'{self.repo}' no es un repositorio git. Inicialízalo con `git init`."
            )
        # `git init` a secas no basta: el worktree se ramifica desde HEAD y sin
        # commits nace VACÍO. El agente entraba a un directorio sin un solo
        # archivo del proyecto y "aplicaba" el plan sobre la nada — sin error,
        # porque git crea el worktree igual. Verificado en real.
        if not gitutil.tiene_commits(self.repo):
            raise ExecutorError(
                f"El repositorio '{self.repo}' no tiene ningún commit todavía. "
                "El plan se aplica en un worktree ramificado desde HEAD, que sin "
                "commits nace vacío: el agente no vería tus archivos. Haz el "
                "commit inicial y reintenta:\n"
                f"    git -C {self.repo} add -A && "
                f"git -C {self.repo} commit -m \"Estado inicial\""
            )
        # Gate de decisiones (una sola verdad; el Hub la traduce a 422): no
        # ejecutar un plan con una decisión crucial abierta. Va antes de crear
        # el worktree, así no deja ninguno colgado.
        if plan.decisiones_pendientes and not allow_open_decisions:
            preguntas = "; ".join(p for p in plan.decisiones_pendientes if p)
            raise ExecutorError(
                "El plan tiene decisiones cruciales sin resolver; ciérralas antes "
                "de ejecutar (si no, el ejecutor aplicaría la ambigüedad en "
                f"silencio). Pendientes: {preguntas}. Resuélvelas y cierra el plan, "
                "o fuerza bajo tu riesgo (--allow-open-decisions)."
            )

        base_branch = gitutil.current_branch(self.repo)
        branch = branch or self._default_branch(plan.title)
        self._on_event("rama", branch)
        # Aislamiento por worktree (D9 paso 2): el agente escribe en un dir
        # desechable ramificado de HEAD, nunca en el árbol del vocero. Por eso
        # no exige árbol limpio: sus cambios sin confirmar no contaminan el diff.
        worktree = self._worktree_path(branch)
        gitutil.add_worktree(self.repo, branch, worktree)

        # Marcador "en curso" ANTES de lanzar el backend (decisión D2 del
        # vocero: lo escribe el Executor, no el consumidor de la cola del Hub).
        # Si el proceso muere a mitad, el sidecar se queda sin `returncode` y
        # quien rehidrate sabe que la ejecución no terminó, en vez de suponer
        # que salió bien.
        gitutil.escribir_sidecar(worktree, {
            "estado": "en_curso",
            "rama": branch,
            "rama_base": base_branch,
            "backend": self.backend.name,
            "titulo": plan.title,
            "iniciado": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })

        self._on_event("ejecutando", self.backend.name)
        code, output = self.backend.run(_exec_prompt(plan), worktree, allow_commands)

        gitutil.stage_all(worktree)
        diff = gitutil.staged_diff(worktree)
        changed = gitutil.staged_changed_files(worktree)
        self._on_event("diff_listo", str(len(changed)))

        verify_returncode: int | None = None
        verify_output = ""
        verify_corrected = False
        if verify_command:
            self._on_event("verificando", verify_command)
            verify_returncode, verify_output = self._run_verify(verify_command, worktree)
            if verify_returncode != 0:
                self._on_event("verificacion_fallida", str(verify_returncode))
                # Mini-ronda de corrección acotada: UNA sola iteración, mismo
                # ejecutor, con la salida del fallo como contexto. No se
                # reintenta más allá de esto — el reporte honesto es el punto.
                self._on_event("corrigiendo", self.backend.name)
                self.backend.run(
                    _correction_prompt(plan, verify_command, verify_output),
                    worktree,
                    allow_commands,
                )
                verify_corrected = True
                gitutil.stage_all(worktree)
                diff = gitutil.staged_diff(worktree)
                changed = gitutil.staged_changed_files(worktree)
                verify_returncode, verify_output = self._run_verify(verify_command, worktree)
                self._on_event("verificacion_reintentada", str(verify_returncode))
            else:
                self._on_event("verificacion_ok", "")

        # Sidecar definitivo: ya se sabe cómo terminó. `returncode` es el dato
        # que git no puede dar y que decide si el Hub deja commitear.
        gitutil.escribir_sidecar(worktree, {
            "estado": "terminado",
            "rama": branch,
            "rama_base": base_branch,
            "backend": self.backend.name,
            "titulo": plan.title,
            "returncode": code,
            "verify_returncode": verify_returncode,
            "terminado": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })

        return ExecutionOutcome(
            branch=branch,
            backend=self.backend.name,
            returncode=code,
            backend_output=output,
            diff=diff,
            changed_files=changed,
            allow_commands=allow_commands,
            base_branch=base_branch,
            worktree=worktree,
            verify_command=verify_command or "",
            verify_returncode=verify_returncode,
            verify_output=verify_output,
            verify_corrected=verify_corrected,
        )

    def _run_verify(self, command: str, cwd: str) -> tuple[int, str]:
        # Corre en el worktree: es donde viven los cambios del plan.
        proc = subprocess.run(
            command, shell=True, cwd=cwd, capture_output=True, text=True
        )
        return proc.returncode, proc.stdout + proc.stderr

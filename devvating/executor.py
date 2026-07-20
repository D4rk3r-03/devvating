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
import time
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

    def execute(
        self,
        plan: ExecutionPlan,
        *,
        allow_commands: bool = False,
        branch: str | None = None,
        require_clean: bool = True,
        verify_command: str | None = None,
    ) -> ExecutionOutcome:
        if not gitutil.is_git_repo(self.repo):
            raise ExecutorError(
                f"'{self.repo}' no es un repositorio git. Inicialízalo con `git init`."
            )
        if require_clean and not gitutil.is_clean(self.repo):
            raise ExecutorError(
                "El árbol de trabajo tiene cambios sin confirmar. Haz commit o "
                "stash antes de ejecutar (así el diff refleja solo los cambios del plan)."
            )

        base_branch = gitutil.current_branch(self.repo)
        branch = branch or self._default_branch(plan.title)
        self._on_event("rama", branch)
        gitutil.create_branch(self.repo, branch)

        self._on_event("ejecutando", self.backend.name)
        code, output = self.backend.run(_exec_prompt(plan), self.repo, allow_commands)

        gitutil.stage_all(self.repo)
        diff = gitutil.staged_diff(self.repo)
        changed = gitutil.staged_changed_files(self.repo)
        self._on_event("diff_listo", str(len(changed)))

        verify_returncode: int | None = None
        verify_output = ""
        verify_corrected = False
        if verify_command:
            self._on_event("verificando", verify_command)
            verify_returncode, verify_output = self._run_verify(verify_command)
            if verify_returncode != 0:
                self._on_event("verificacion_fallida", str(verify_returncode))
                # Mini-ronda de corrección acotada: UNA sola iteración, mismo
                # ejecutor, con la salida del fallo como contexto. No se
                # reintenta más allá de esto — el reporte honesto es el punto.
                self._on_event("corrigiendo", self.backend.name)
                self.backend.run(
                    _correction_prompt(plan, verify_command, verify_output),
                    self.repo,
                    allow_commands,
                )
                verify_corrected = True
                gitutil.stage_all(self.repo)
                diff = gitutil.staged_diff(self.repo)
                changed = gitutil.staged_changed_files(self.repo)
                verify_returncode, verify_output = self._run_verify(verify_command)
                self._on_event("verificacion_reintentada", str(verify_returncode))
            else:
                self._on_event("verificacion_ok", "")

        return ExecutionOutcome(
            branch=branch,
            backend=self.backend.name,
            returncode=code,
            backend_output=output,
            diff=diff,
            changed_files=changed,
            allow_commands=allow_commands,
            base_branch=base_branch,
            verify_command=verify_command or "",
            verify_returncode=verify_returncode,
            verify_output=verify_output,
            verify_corrected=verify_corrected,
        )

    def _run_verify(self, command: str) -> tuple[int, str]:
        proc = subprocess.run(
            command, shell=True, cwd=self.repo, capture_output=True, text=True
        )
        return proc.returncode, proc.stdout + proc.stderr

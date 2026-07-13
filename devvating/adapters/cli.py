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
import subprocess

from ..tools.registry import ToolRegistry
from .base import TurnUsage


class CliAdapterError(RuntimeError):
    pass


def env_suscripcion() -> dict[str, str]:
    """Entorno para el subprocess SIN credenciales API de Anthropic.

    Nuestro proceso carga ANTHROPIC_API_KEY desde .env para el backend api;
    si el CLI la hereda, le da precedencia sobre el login de suscripción y
    factura (o falla) contra la clave. Se quita para que `claude -p` use la
    suscripción — que es la razón de ser del backend cli (D5).
    """
    return {
        k: v
        for k, v in os.environ.items()
        if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
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
            raise CliAdapterError(f"claude -p salió con código {proc.returncode}: {detalle}")
        try:
            data = json.loads(proc.stdout)
        except ValueError as exc:
            raise CliAdapterError(
                f"Salida no-JSON de claude -p: {proc.stdout.strip()[:200]}"
            ) from exc
        if data.get("is_error"):
            raise CliAdapterError(f"claude -p reportó error: {data.get('result', '')}")

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


class GeminiCliAdapter:
    """Turno de debate vía Gemini CLI headless (`gemini -p`).

    El CLI de Gemini no tiene flag de system prompt en headless, así que las
    instrucciones de rol se anteponen al prompt. Sus herramientas de solo
    lectura (leer/buscar archivos) están permitidas por defecto sin
    aprobación; nada de escritura sin modos explícitos que aquí no se pasan.
    """

    def __init__(self, binary: str = "gemini", cwd: str = ".", timeout: int = 600) -> None:
        self.name = "gemini"
        self.binary = binary
        self.cwd = cwd
        self.timeout = timeout
        # El CLI de Gemini en headless no reporta métricas por stdout.
        self.last_usage: TurnUsage | None = None

    def build_argv(self, system: str, prompt: str) -> list[str]:
        combinado = f"INSTRUCCIONES DE SISTEMA (tu rol):\n{system}\n\n{prompt}"
        return [self.binary, "-p", combinado]

    def converse(self, system: str, prompt: str, registry: ToolRegistry) -> str:
        self.last_usage = None
        proc = _run(self.build_argv(system, prompt), self.cwd, self.timeout, "Gemini")
        if proc.returncode != 0:
            detalle = (proc.stderr or proc.stdout).strip()[:500]
            raise CliAdapterError(f"gemini -p salió con código {proc.returncode}: {detalle}")
        return proc.stdout.strip()

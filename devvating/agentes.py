"""Banco de agentes plugable (M8, D7): fábrica por nombre de roster.

El orquestador debate con CUALQUIER par de adaptadores que cumplan el
Protocol; lo único que estaba rígido era la construcción del par
claude+gemini. Este módulo lo abre: cada entrada del roster es un nombre
estable ("claude-cli", "antigravity", "kimi"…) que produce un adaptador
listo. El par se elige con `--agentes a,b`, con `"agentes": [...]` en
`.devvating.json`, o cae al par clásico según los backends legados.
"""

from __future__ import annotations

import os

from .adapters.base import AgentAdapter
from .adapters.claude import ClaudeAdapter
from .adapters.cli import (
    AntigravityCliAdapter,
    ClaudeCliAdapter,
    GeminiCliAdapter,
    KimiCliAdapter,
)
from .adapters.gemini import GeminiAdapter
from .config import Config


def _claude_api(cfg: Config, repo: str) -> AgentAdapter:
    cfg.require_anthropic()
    return ClaudeAdapter(cfg.anthropic_api_key, cfg.claude_model, cfg.max_tool_iterations)


def _gemini_api(cfg: Config, repo: str) -> AgentAdapter:
    cfg.require_gemini()
    return GeminiAdapter(cfg.gemini_api_key, cfg.gemini_model, cfg.max_tool_iterations)


def _timeout_cli(defecto: int) -> int:
    """Timeout de los adaptadores CLI, sobreescribible con DEVVATING_CLI_TIMEOUT."""
    try:
        return int(os.environ["DEVVATING_CLI_TIMEOUT"])
    except (KeyError, ValueError):
        return defecto


ROSTER = {
    "claude-api": _claude_api,
    "claude-cli": lambda cfg, repo: ClaudeCliAdapter(cwd=repo, timeout=_timeout_cli(600)),
    "gemini-api": _gemini_api,
    "gemini-cli": lambda cfg, repo: GeminiCliAdapter(cwd=repo, timeout=_timeout_cli(600)),
    "antigravity": lambda cfg, repo: AntigravityCliAdapter(cwd=repo, timeout=_timeout_cli(1500)),
    "kimi": lambda cfg, repo: KimiCliAdapter(cwd=repo, timeout=_timeout_cli(600)),
}

# Alias de comodidad → nombre canónico del roster.
ALIAS = {
    "agy": "antigravity",
    "claude": "claude-cli",
    "gemini": "gemini-api",
}


def nombres() -> list[str]:
    return sorted(ROSTER)


def crear(nombre: str, cfg: Config, repo: str) -> AgentAdapter:
    canonico = ALIAS.get(nombre.strip().lower(), nombre.strip().lower())
    fabrica = ROSTER.get(canonico)
    if fabrica is None:
        raise ValueError(
            f"Agente desconocido: '{nombre}'. Roster: {', '.join(nombres())} "
            f"(alias: {', '.join(sorted(ALIAS))})."
        )
    return fabrica(cfg, repo)


def par(dos_nombres: list[str], cfg: Config, repo: str) -> tuple[AgentAdapter, AgentAdapter]:
    """Crea el par de debatientes, validando que sean exactamente 2 y distintos.

    Los nombres de adaptador (agent.name) deben diferir: el orquestador y el
    transcript indexan las posturas por ese nombre.
    """
    if len(dos_nombres) != 2:
        raise ValueError(
            f"Un debate necesita exactamente 2 agentes; recibí {len(dos_nombres)}: "
            f"{', '.join(dos_nombres) or '(ninguno)'}."
        )
    a = crear(dos_nombres[0], cfg, repo)
    b = crear(dos_nombres[1], cfg, repo)
    if a.name == b.name:
        raise ValueError(
            f"Los dos agentes resuelven al mismo nombre '{a.name}' "
            f"({dos_nombres[0]} y {dos_nombres[1]}); el debate necesita "
            "identidades distintas."
        )
    return a, b

"""Config de proyecto para los defaults del debate (`.devvating.json`).

Separada de `config.Config` (que guarda secretos + modelos desde el entorno):
aquí van los defaults de comportamiento del debate, versionables en el repo.
Precedencia final (la resuelve cada CLI): flags CLI > esta config > defaults.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

CONFIG_FILE = ".devvating.json"

BACKENDS = ("api", "cli")


def _backend(value: object) -> str:
    """Coerción tolerante: cualquier valor que no sea api/cli cae a 'api'."""
    return value if value in BACKENDS else "api"


@dataclass
class ProjectConfig:
    rounds: int = 2
    deep_mode: bool = False
    auto_rotate: bool = True
    repo: str = "."
    files: str = ""
    # D5: backend por agente — "api" (SDK + Tool Runtime propio, requiere
    # créditos) o "cli" (agente headless, cubierto por suscripción).
    claude_backend: str = "api"
    gemini_backend: str = "api"

    @classmethod
    def load(cls, start: str = ".") -> "ProjectConfig":
        path = os.path.join(start, CONFIG_FILE)
        if not os.path.isfile(path):
            return cls()
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            backends = data.get("backends") or {}
            return cls(
                rounds=int(data.get("rounds", 2)),
                deep_mode=bool(data.get("deep_mode", False)),
                auto_rotate=bool(data.get("auto_rotate", True)),
                repo=str(data.get("repo", ".")),
                files=str(data.get("files", "")),
                claude_backend=_backend(backends.get("claude", "api")),
                gemini_backend=_backend(backends.get("gemini", "api")),
            )
        except (OSError, ValueError, TypeError, AttributeError):
            return cls()

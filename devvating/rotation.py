"""Rotación de roles entre temas (D3, capa 1).

Estado persistente por proyecto: cuántos debates se han corrido. De ahí se
deriva quién sintetiza, alternando entre temas para no sesgar siempre al mismo
modelo hacia el mismo papel a largo plazo.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass

STATE_DIR = ".devvating"
STATE_FILE = "state.json"


@dataclass
class RotationState:
    debates: int = 0

    def synthesizer_index(self) -> int:
        # Alterna 0/1 (claude/gemini) según el número de debates previos.
        return self.debates % 2

    def advanced(self) -> "RotationState":
        return RotationState(self.debates + 1)


def _path(repo: str) -> str:
    return os.path.join(repo, STATE_DIR, STATE_FILE)


def load(repo: str) -> RotationState:
    try:
        with open(_path(repo), encoding="utf-8") as fh:
            data = json.load(fh)
        return RotationState(int(data.get("debates", 0)))
    except (OSError, ValueError, TypeError):
        return RotationState()


def save(repo: str, state: RotationState) -> None:
    os.makedirs(os.path.join(repo, STATE_DIR), exist_ok=True)
    with open(_path(repo), "w", encoding="utf-8") as fh:
        json.dump(asdict(state), fh)

"""Fixtures compartidas: adaptadores stub y repos temporales.

Los stubs implementan el Protocol AgentAdapter sin tocar ninguna API, para
poder verificar el flujo del orquestador y del ejecutor en local y en CI.
"""

from __future__ import annotations

import subprocess

import pytest

from devvating.adapters.base import TurnUsage
from devvating.tools.registry import ToolRegistry


class StubAdapter:
    """Adaptador falso: devuelve respuestas pre-programadas en orden.

    Registra cada llamada (system, prompt) para poder afirmar sobre el flujo
    (p. ej. que en la apertura a ciegas nadie ve la postura del otro).
    `usages` (opcional) fabrica un TurnUsage sintético por turno, en orden,
    para probar el contador de tokens (§13) sin claves API. Si una respuesta
    programada es una Exception, se LANZA en ese turno — para probar la
    resiliencia (reintentos, aborto, volcado parcial) sin red.
    """

    def __init__(
        self,
        name: str,
        respuestas: list[str],
        usages: list[TurnUsage | None] | None = None,
    ) -> None:
        self.name = name
        self._respuestas = list(respuestas)
        self._usages = list(usages) if usages else []
        self.last_usage: TurnUsage | None = None
        self.llamadas: list[tuple[str, str]] = []

    def converse(self, system: str, prompt: str, registry: ToolRegistry) -> str:
        self.llamadas.append((system, prompt))
        self.last_usage = self._usages.pop(0) if self._usages else None
        if not self._respuestas:
            return f"[{self.name}: sin respuestas programadas]"
        respuesta = self._respuestas.pop(0)
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta


@pytest.fixture
def git_repo(tmp_path):
    """Repo git real, limpio y con un commit inicial."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=True
        )

    git("init", "-b", "main")
    git("config", "user.email", "test@test")
    git("config", "user.name", "Test")
    (repo / "hola.txt").write_text("hola\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "inicial")
    return repo

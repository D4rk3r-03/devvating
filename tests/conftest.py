"""Fixtures compartidas: adaptadores stub y repos temporales.

Los stubs implementan el Protocol AgentAdapter sin tocar ninguna API, para
poder verificar el flujo del orquestador y del ejecutor en local y en CI.
"""

from __future__ import annotations

import os
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


@pytest.fixture(scope="session", autouse=True)
def registro_de_sesion_aislado(tmp_path_factory):
    """Red de seguridad de SESIÓN para el índice global (D13).

    El aislamiento por test no basta: varios tests del Hub lanzan el debate en
    un hilo daemon que puede terminar DESPUÉS del teardown, cuando monkeypatch
    ya restauró el entorno — y entonces daría de alta en el `~/.devvating` del
    usuario. Verificado en real: aparecieron debates llamados `test_*` en su
    historial. Esta capa dura toda la sesión, así que un hilo rezagado sigue
    escribiendo en un temporal.
    """
    d = tmp_path_factory.mktemp("registro-sesion")
    previo = os.environ.get("DEVVATING_REGISTRO_DIR")
    os.environ["DEVVATING_REGISTRO_DIR"] = str(d)
    yield
    if previo is None:
        os.environ.pop("DEVVATING_REGISTRO_DIR", None)
    else:
        os.environ["DEVVATING_REGISTRO_DIR"] = previo


@pytest.fixture(autouse=True)
def worktrees_aislados(tmp_path, monkeypatch):
    """Confina los worktrees de la ejecución al tmp_path del test.

    Sin esto, `Executor` los crea bajo el temp del SISTEMA y solo se limpian
    si el test cierra el ciclo (commit o descartar) — que casi ninguno hace,
    porque prueban otra cosa. El resultado era basura acumulada en
    /tmp/devvating-worktrees (65 directorios encontrados en una auditoría).
    Es autouse a propósito: un test futuro que ejecute un plan queda cubierto
    sin acordarse de nada, y pytest borra el tmp_path por él.
    """
    monkeypatch.setenv("DEVVATING_WORKTREE_DIR", str(tmp_path / "worktrees"))
    # Y el índice global (D13) al mismo tmp_path: guardar un transcript da de
    # alta en él, así que sin esto la suite escribiría en ~/.devvating del
    # usuario y su historial real se llenaría de debates de prueba.
    monkeypatch.setenv("DEVVATING_REGISTRO_DIR", str(tmp_path / "registro"))


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

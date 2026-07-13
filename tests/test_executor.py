"""Ejecutor (fase 4): rama, diff, guardas de seguridad y argv del backend."""

from __future__ import annotations

import pytest

from devvating import gitutil
from devvating.executor import (
    ClaudeCodeBackend,
    ExecutionPlan,
    Executor,
    ExecutorError,
)

PLAN = ExecutionPlan(text="Añade una línea a hola.txt", title="demo plan")


class WriterBackend:
    """Backend stub: simula al agente headless escribiendo en el repo."""

    name = "stub"

    def run(self, prompt: str, cwd: str, allow_commands: bool) -> tuple[int, str]:
        import pathlib

        p = pathlib.Path(cwd)
        (p / "hola.txt").write_text("hola\nmundo\n", encoding="utf-8")
        (p / "nuevo.txt").write_text("nuevo\n", encoding="utf-8")
        return 0, "ok"


class TestExecutor:
    def test_crea_rama_y_capta_diff_de_los_cambios(self, git_repo):
        ex = Executor(str(git_repo), WriterBackend())
        out = ex.execute(PLAN, branch="devvating/test")
        assert out.branch == "devvating/test"
        assert gitutil.current_branch(str(git_repo)) == "devvating/test"
        assert sorted(out.changed_files) == ["hola.txt", "nuevo.txt"]
        assert "mundo" in out.diff and out.returncode == 0

    def test_nombre_de_rama_por_defecto_con_slug_y_fecha(self, git_repo):
        ex = Executor(str(git_repo), WriterBackend())
        out = ex.execute(PLAN)
        assert out.branch.startswith("devvating/demo-plan-")

    def test_rechaza_directorio_que_no_es_repo(self, tmp_path):
        ex = Executor(str(tmp_path), WriterBackend())
        with pytest.raises(ExecutorError, match="no es un repositorio"):
            ex.execute(PLAN)

    def test_rechaza_arbol_de_trabajo_sucio(self, git_repo):
        (git_repo / "hola.txt").write_text("modificado\n", encoding="utf-8")
        ex = Executor(str(git_repo), WriterBackend())
        with pytest.raises(ExecutorError, match="sin confirmar"):
            ex.execute(PLAN)

    def test_no_commitea_nada(self, git_repo):
        ex = Executor(str(git_repo), WriterBackend())
        ex.execute(PLAN)
        # Los cambios quedan en staging, no confirmados.
        assert gitutil.staged_changed_files(str(git_repo))


class TestClaudeCodeBackendArgv:
    def test_sin_comandos_limita_herramientas_a_edicion(self):
        argv = ClaudeCodeBackend().build_argv("plan", allow_commands=False)
        assert "--permission-mode" in argv and "acceptEdits" in argv
        assert "Read,Edit,Write" in argv
        assert "--dangerously-skip-permissions" not in argv

    def test_con_comandos_requiere_el_flag_peligroso(self):
        argv = ClaudeCodeBackend().build_argv("plan", allow_commands=True)
        assert "--dangerously-skip-permissions" in argv
        assert "--allowedTools" not in argv

    def test_no_hereda_la_clave_api_al_subprocess(self, tmp_path, monkeypatch):
        # Mismo fix D5 que en los adaptadores CLI: el ejecutor debe correr
        # `claude -p` con el login de suscripción, no con la clave heredada.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-falsa")
        fake = tmp_path / "claude"
        fake.write_text(
            '#!/bin/bash\nif [ -n "$ANTHROPIC_API_KEY" ]; then echo con-clave; '
            "else echo sin-clave; fi\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        code, output = ClaudeCodeBackend(binary=str(fake)).run(
            "plan", str(tmp_path), allow_commands=False
        )
        assert code == 0 and "sin-clave" in output

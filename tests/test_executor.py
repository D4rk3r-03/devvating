"""Ejecutor (fase 4): rama, diff, guardas de seguridad y argv del backend."""

from __future__ import annotations

from pathlib import Path

import pytest

from devvating import gitutil
from devvating.executor import (
    ClaudeCodeBackend,
    ExecutionPlan,
    Executor,
    ExecutorError,
    decisiones_crucial_sin_resolver,
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
    def test_crea_worktree_aislado_y_capta_diff(self, git_repo):
        ex = Executor(str(git_repo), WriterBackend())
        out = ex.execute(PLAN, branch="devvating/test")
        assert out.branch == "devvating/test"
        # El repo del vocero NO cambia de rama: el plan se aplicó en el worktree.
        assert gitutil.current_branch(str(git_repo)) == "main"
        assert out.worktree and Path(out.worktree).is_dir()
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

    def test_no_toca_el_arbol_del_vocero_aunque_este_sucio(self, git_repo):
        # Antes exigía árbol limpio; ahora el worktree aísla, así que ejecuta
        # igual con cambios sin confirmar del vocero y NO los pisa (D9 paso 2).
        (git_repo / "hola.txt").write_text("trabajo del vocero\n", encoding="utf-8")
        ex = Executor(str(git_repo), WriterBackend())
        out = ex.execute(PLAN)
        assert out.changed_files  # ejecutó pese al árbol sucio
        assert (git_repo / "hola.txt").read_text(encoding="utf-8") == "trabajo del vocero\n"

    def test_no_commitea_nada(self, git_repo):
        ex = Executor(str(git_repo), WriterBackend())
        out = ex.execute(PLAN)
        # Los cambios quedan en staging DEL WORKTREE, sin commitear.
        assert gitutil.staged_changed_files(out.worktree)


class VerifyBackend:
    """Backend stub: la 2ª llamada (corrección) arregla el archivo si `corrige`."""

    name = "stub-verify"

    def __init__(self, corrige: bool = True) -> None:
        self.llamadas = 0
        self.corrige = corrige

    def run(self, prompt: str, cwd: str, allow_commands: bool) -> tuple[int, str]:
        import pathlib

        self.llamadas += 1
        p = pathlib.Path(cwd)
        (p / "hola.txt").write_text("hola\nmundo\n", encoding="utf-8")
        if self.llamadas >= 2 and self.corrige:
            (p / "arreglado.txt").write_text("ok\n", encoding="utf-8")
        return 0, "ok"


class TestDecisionesCrucialSinResolver:
    def test_lee_dicts_del_transcript(self):
        decisiones = [
            {"pregunta": "¿A o B?", "crucial": True, "resuelta": False},
            {"pregunta": "resuelta", "crucial": True, "resuelta": True},
            {"pregunta": "no crucial", "crucial": False},
        ]
        assert decisiones_crucial_sin_resolver(decisiones) == ["¿A o B?"]

    def test_none_o_vacio_no_falla(self):
        assert decisiones_crucial_sin_resolver(None) == []
        assert decisiones_crucial_sin_resolver([]) == []


class TestGateDecisiones:
    def _plan_con_pendiente(self):
        return ExecutionPlan(
            text="plan", title="demo", decisiones_pendientes=["¿A o B?"]
        )

    def test_bloquea_ejecucion_con_decision_crucial_abierta(self, git_repo):
        ex = Executor(str(git_repo), WriterBackend())
        with pytest.raises(ExecutorError, match="decisiones cruciales sin resolver"):
            ex.execute(self._plan_con_pendiente())
        # No dejó rama colgada: sigue en la base, nada en staging.
        assert gitutil.current_branch(str(git_repo)) == "main"
        assert not gitutil.staged_changed_files(str(git_repo))

    def test_override_permite_forzar(self, git_repo):
        ex = Executor(str(git_repo), WriterBackend())
        out = ex.execute(self._plan_con_pendiente(), allow_open_decisions=True)
        assert out.returncode == 0 and out.changed_files

    def test_plan_sin_pendientes_no_se_bloquea(self, git_repo):
        ex = Executor(str(git_repo), WriterBackend())
        out = ex.execute(PLAN)  # decisiones_pendientes vacío por defecto
        assert out.changed_files


class TestVerificacion:
    """Fase 5 (M9): comando de verificación opcional tras aplicar el plan."""

    def test_sin_verify_command_no_verifica(self, git_repo):
        backend = VerifyBackend()
        out = Executor(str(git_repo), backend).execute(PLAN)
        assert out.verify_command == "" and out.verify_returncode is None
        assert not out.verify_corrected and backend.llamadas == 1

    def test_verificacion_pasa_sin_correccion(self, git_repo):
        backend = VerifyBackend()
        out = Executor(str(git_repo), backend).execute(
            PLAN, verify_command="test -f hola.txt"
        )
        assert out.verify_command == "test -f hola.txt"
        assert out.verify_returncode == 0 and not out.verify_corrected
        assert backend.llamadas == 1  # sin corrección: una sola pasada

    def test_verificacion_falla_dispara_una_correccion_que_arregla(self, git_repo):
        backend = VerifyBackend(corrige=True)
        out = Executor(str(git_repo), backend).execute(
            PLAN, verify_command="test -f arreglado.txt"
        )
        assert out.verify_corrected
        assert backend.llamadas == 2  # ejecución original + 1 corrección
        assert out.verify_returncode == 0  # la corrección sí arregló
        assert "arreglado.txt" in out.changed_files  # el diff refleja la corrección

    def test_correccion_que_no_arregla_reporta_el_fallo_honestamente(self, git_repo):
        backend = VerifyBackend(corrige=False)
        out = Executor(str(git_repo), backend).execute(
            PLAN, verify_command="test -f arreglado.txt"
        )
        assert out.verify_corrected
        assert backend.llamadas == 2  # tope de 1 corrección: no reintenta más
        assert out.verify_returncode != 0  # honesto: sigue fallando


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

    def test_modelo_ejecutor_por_defecto_es_sonnet(self, monkeypatch):
        # D8: razonamiento para debatir, sonnet para ejecutar.
        monkeypatch.delenv("DEVVATING_EXEC_MODEL", raising=False)
        argv = ClaudeCodeBackend().build_argv("plan", allow_commands=False)
        i = argv.index("--model")
        assert argv[i + 1] == "sonnet"

    def test_modelo_ejecutor_configurable(self, monkeypatch):
        monkeypatch.setenv("DEVVATING_EXEC_MODEL", "claude-sonnet-4-5")
        assert ClaudeCodeBackend().model == "claude-sonnet-4-5"
        # El argumento explícito gana sobre el entorno.
        assert ClaudeCodeBackend(model="opus").model == "opus"

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

    def test_no_hereda_stdin_al_subprocess(self, tmp_path):
        # Mismo blindaje que adapters/cli: sin stdin cerrado, el CLI hereda el
        # terminal y puede quedarse esperando entrada en medio de la ejecución.
        fake = tmp_path / "claude"
        fake.write_text(
            "#!/bin/bash\n"
            'if [ -e /proc/self/fd/0 ] && [ "$(readlink /proc/self/fd/0)" = "/dev/null" ]; '
            "then echo stdin-nulo; else echo stdin-heredado; fi\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        code, output = ClaudeCodeBackend(binary=str(fake)).run(
            "plan", str(tmp_path), allow_commands=False
        )
        assert code == 0 and "stdin-nulo" in output

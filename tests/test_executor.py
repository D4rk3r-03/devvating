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

    def test_worktree_respeta_el_directorio_base_configurado(self, git_repo, tmp_path):
        # DEVVATING_WORKTREE_DIR redirige la base. Es lo que impide que la
        # suite siembre el /tmp del sistema (fixture autouse worktrees_aislados)
        # y permite al vocero moverla si su temp es pequeño o volátil.
        base = tmp_path / "worktrees"  # el mismo que fija el fixture autouse
        out = Executor(str(git_repo), WriterBackend()).execute(PLAN)
        assert Path(out.worktree).parent == base
        assert (Path(out.worktree) / "hola.txt").is_file()  # el plan se aplicó ahí

    def test_la_suite_no_escribe_en_el_temp_del_sistema(self, git_repo):
        # Regresión de la fuga: 65 worktrees de tests quedaron acumulados en
        # /tmp/devvating-worktrees porque los tests no cierran el ciclo
        # commit/descartar, que es quien los limpia en producción.
        import tempfile

        del_sistema = Path(tempfile.gettempdir()) / "devvating-worktrees"
        antes = set(del_sistema.iterdir()) if del_sistema.is_dir() else set()
        Executor(str(git_repo), WriterBackend()).execute(PLAN)
        despues = set(del_sistema.iterdir()) if del_sistema.is_dir() else set()
        assert antes == despues

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


class TestRepoSinCommits:
    """`git init` a secas no basta: el worktree se ramifica desde HEAD y sin
    commits nace VACÍO. El agente aplicaba el plan sobre un directorio sin un
    solo archivo del proyecto, y nada fallaba — git crea el worktree igual."""

    @pytest.fixture
    def repo_vacio(self, tmp_path):
        import subprocess

        repo = tmp_path / "recien-iniciado"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
        (repo / "documento.md").write_text("contenido real\n", encoding="utf-8")
        return repo

    def test_tiene_commits_distingue_init_de_repo_usable(self, repo_vacio, git_repo):
        assert gitutil.tiene_commits(str(repo_vacio)) is False
        assert gitutil.tiene_commits(str(git_repo)) is True

    def test_ejecutar_falla_con_instrucciones_en_vez_de_worktree_vacio(self, repo_vacio):
        ex = Executor(str(repo_vacio), WriterBackend())
        with pytest.raises(ExecutorError) as info:
            ex.execute(PLAN)
        mensaje = str(info.value)
        assert "no tiene ningún commit" in mensaje
        # Accionable, no solo un diagnóstico: lleva el comando a copiar.
        assert "add -A" in mensaje and 'commit -m "Estado inicial"' in mensaje

    def test_no_deja_worktree_colgando_al_rechazar(self, repo_vacio):
        ex = Executor(str(repo_vacio), WriterBackend())
        with pytest.raises(ExecutorError):
            ex.execute(PLAN)
        # El gate va ANTES de crear nada: ni worktrees ni ramas huérfanas.
        assert gitutil.list_worktrees(str(repo_vacio)) == []

    def test_tras_el_commit_inicial_ejecuta_normal(self, repo_vacio):
        import subprocess

        subprocess.run(["git", "add", "-A"], cwd=repo_vacio, check=True)
        subprocess.run(["git", "commit", "-qm", "inicial"], cwd=repo_vacio, check=True)
        out = Executor(str(repo_vacio), WriterBackend()).execute(PLAN)
        # El worktree ya NO nace vacío: trae los archivos del proyecto.
        assert (Path(out.worktree) / "documento.md").is_file()
        assert out.changed_files


class TestSidecarDeEjecucion:
    """Metadatos que git no puede saber (el returncode, sobre todo), para que
    una ejecución sobreviva a un reinicio del Hub. Decisiones D1/D2 del vocero
    (2026-07-22): viven en el directorio ADMINISTRATIVO del worktree y los
    escribe el Executor."""

    def test_el_sidecar_no_contamina_el_diff_del_vocero(self, git_repo):
        # La regresión que motivó D1: dentro del árbol, `stage_all` (add -A) lo
        # metería en el staging, en el diff que revisa el vocero y en el commit.
        out = Executor(str(git_repo), WriterBackend()).execute(PLAN)
        assert sorted(out.changed_files) == ["hola.txt", "nuevo.txt"]
        assert "devvating-ejecucion" not in out.diff

    def test_guarda_returncode_y_rama_base_al_terminar(self, git_repo):
        out = Executor(str(git_repo), WriterBackend()).execute(PLAN)
        side = gitutil.leer_sidecar(out.worktree)
        assert side["estado"] == "terminado"
        assert side["returncode"] == 0
        assert side["rama_base"] == "main"   # git no puede deducirla después
        assert side["rama"] == out.branch

    def test_marcador_en_curso_antes_de_lanzar_el_backend(self, git_repo):
        # Si el proceso muere a mitad, el sidecar no tiene returncode y quien
        # rehidrate sabe que NO terminó, en vez de suponer que salió bien.
        visto = {}

        class BackendQueEspia:
            name = "espia"

            def run(self, prompt, cwd, allow_commands):
                visto.update(gitutil.leer_sidecar(cwd) or {})
                return 0, "ok"

        Executor(str(git_repo), BackendQueEspia()).execute(PLAN)
        assert visto["estado"] == "en_curso"
        assert "returncode" not in visto

    def test_se_va_con_el_worktree(self, git_repo):
        # La virtud que defendía la opción in-tree se conserva igual: el
        # directorio administrativo lo borra `git worktree remove`.
        out = Executor(str(git_repo), WriterBackend()).execute(PLAN)
        gitdir = gitutil.gitdir_de_worktree(out.worktree)
        assert Path(gitdir, "devvating-ejecucion.json").is_file()
        gitutil.remove_worktree(str(git_repo), out.worktree)
        assert not Path(gitdir).exists()

    def test_leer_sidecar_sin_worktree_da_none(self, git_repo, tmp_path):
        assert gitutil.leer_sidecar(str(tmp_path)) is None
        assert gitutil.gitdir_de_worktree(str(git_repo)) is None  # no es worktree

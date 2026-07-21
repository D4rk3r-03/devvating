"""gitutil: commit y descarte de la rama de ejecución (fase 4).

Usan el fixture git_repo (repo real, limpio, con un commit en 'main').
"""

from __future__ import annotations

import os
import subprocess
import tempfile

import pytest

from devvating import gitutil


def _worktree(git_repo, branch="devvating/x"):
    """Crea un worktree desechable (en el temp del sistema) y devuelve su ruta."""
    path = os.path.join(
        tempfile.mkdtemp(prefix="dv-wt-test-"), branch.replace("/", "-")
    )
    return gitutil.add_worktree(str(git_repo), branch, path)


class TestCommit:
    def test_commitea_lo_staged_y_devuelve_sha(self, git_repo):
        gitutil.create_branch(str(git_repo), "devvating/x")
        (git_repo / "nuevo.py").write_text("print('hola')\n", encoding="utf-8")
        gitutil.stage_all(str(git_repo))

        sha = gitutil.commit(str(git_repo), "feat: archivo nuevo")

        assert sha and len(sha) >= 7
        assert gitutil.is_clean(str(git_repo))  # ya no queda nada en staging
        assert gitutil.current_branch(str(git_repo)) == "devvating/x"

    def test_mensaje_vacio_falla(self, git_repo):
        with pytest.raises(RuntimeError, match="no puede estar vacío"):
            gitutil.commit(str(git_repo), "   ")


class TestListado:
    def test_lista_solo_ramas_devvating_con_asunto(self, git_repo):
        # Una rama devvating con commit, y una rama ajena que NO debe aparecer.
        gitutil.create_branch(str(git_repo), "devvating/uno")
        (git_repo / "a.py").write_text("x\n", encoding="utf-8")
        gitutil.stage_all(str(git_repo))
        gitutil.commit(str(git_repo), "feat: rama uno")
        gitutil.checkout(str(git_repo), "main")
        gitutil.create_branch(str(git_repo), "feature/ajena")
        gitutil.checkout(str(git_repo), "main")

        ramas = gitutil.list_branches(str(git_repo))

        nombres = [r["nombre"] for r in ramas]
        assert "devvating/uno" in nombres
        assert "feature/ajena" not in nombres  # fuera del prefijo
        uno = next(r for r in ramas if r["nombre"] == "devvating/uno")
        assert uno["asunto"] == "feat: rama uno" and uno["sha"]


class TestDescarte:
    def test_descartar_vuelve_a_la_base_y_borra_la_rama(self, git_repo):
        gitutil.create_branch(str(git_repo), "devvating/x")
        (git_repo / "nuevo.py").write_text("basura\n", encoding="utf-8")
        gitutil.stage_all(str(git_repo))

        gitutil.discard_branch(str(git_repo), "main", "devvating/x")

        assert gitutil.current_branch(str(git_repo)) == "main"
        assert not (git_repo / "nuevo.py").exists()  # los cambios se tiraron
        assert gitutil.is_clean(str(git_repo))
        # La rama de ejecución ya no existe.
        import subprocess
        ramas = subprocess.run(
            ["git", "branch"], cwd=git_repo, capture_output=True, text=True
        ).stdout
        assert "devvating/x" not in ramas


class TestWorktree:
    def test_add_worktree_aisla_del_arbol_del_vocero(self, git_repo):
        # Cambio sin confirmar en el árbol del vocero: el worktree no lo ve.
        (git_repo / "hola.txt").write_text("trabajo del vocero\n", encoding="utf-8")
        wt = _worktree(git_repo)
        assert os.path.isdir(wt)
        # El worktree sale de HEAD (hola.txt = "hola\n"), no del árbol sucio.
        assert (git_repo / "hola.txt").read_text(encoding="utf-8") == "trabajo del vocero\n"
        with open(os.path.join(wt, "hola.txt"), encoding="utf-8") as f:
            assert f.read() == "hola\n"
        # El repo del vocero sigue en main; la rama vive en el worktree.
        assert gitutil.current_branch(str(git_repo)) == "main"

    def test_commit_en_worktree_no_toca_el_arbol_del_vocero(self, git_repo):
        (git_repo / "hola.txt").write_text("trabajo del vocero\n", encoding="utf-8")
        wt = _worktree(git_repo)
        with open(os.path.join(wt, "nuevo.py"), "w", encoding="utf-8") as f:
            f.write("x\n")
        gitutil.stage_all(wt)
        sha = gitutil.commit(wt, "feat: en el worktree")
        assert sha
        # El commit está en la rama; el árbol del vocero, intacto.
        log = subprocess.run(["git", "log", "--oneline", "devvating/x"],
                             cwd=git_repo, capture_output=True, text=True).stdout
        assert "feat: en el worktree" in log
        assert (git_repo / "hola.txt").read_text(encoding="utf-8") == "trabajo del vocero\n"

    def test_discard_worktree_quita_todo_sin_reset_del_arbol(self, git_repo):
        (git_repo / "hola.txt").write_text("trabajo del vocero\n", encoding="utf-8")
        wt = _worktree(git_repo)
        gitutil.discard_worktree(str(git_repo), wt, "devvating/x")
        assert not os.path.isdir(wt)                       # worktree quitado
        assert "devvating/x" not in subprocess.run(
            ["git", "branch"], cwd=git_repo, capture_output=True, text=True).stdout
        # NUNCA se reseteó el árbol del vocero (a diferencia de discard_branch).
        assert (git_repo / "hola.txt").read_text(encoding="utf-8") == "trabajo del vocero\n"

    def test_delete_branch_quita_el_worktree_colgado(self, git_repo):
        # Rama con worktree vivo (ni commiteada ni descartada): borrar la rama
        # debe quitar antes el worktree, o git -D fallaría.
        wt = _worktree(git_repo)
        gitutil.delete_branch(str(git_repo), "devvating/x")
        assert not os.path.isdir(wt)
        assert "devvating/x" not in subprocess.run(
            ["git", "branch"], cwd=git_repo, capture_output=True, text=True).stdout

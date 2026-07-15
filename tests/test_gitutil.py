"""gitutil: commit y descarte de la rama de ejecución (fase 4).

Usan el fixture git_repo (repo real, limpio, con un commit en 'main').
"""

from __future__ import annotations

import pytest

from devvating import gitutil


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

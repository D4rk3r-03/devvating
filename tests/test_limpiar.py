"""`devvating limpiar`: recoge los worktrees de ejecución que quedaron colgando.

La invariante que protegen estos tests: quitar un worktree NO borra su rama ni
sus commits, así que lo único perdible es lo que esté sin commitear — y eso
solo se toca con --forzar explícito.
"""

from __future__ import annotations

import os
import subprocess

from devvating import gitutil, limpiar


def _worktree(git_repo, branch: str, sucio: bool = False, base=None) -> str:
    """Crea un worktree de ejecución; `sucio` le deja cambios sin commitear.

    La ruta cuelga del tmp_path del test (no de `tempfile.mkdtemp`): estos
    tests no pueden reintroducir la misma fuga que arreglan.
    """
    raiz = base if base is not None else git_repo.parent / "wt-test"
    path = os.path.join(str(raiz), branch.replace("/", "-"))
    gitutil.add_worktree(str(git_repo), branch, path)
    if sucio:
        with open(os.path.join(path, "trabajo.txt"), "w", encoding="utf-8") as fh:
            fh.write("trabajo sin commitear\n")
        gitutil.stage_all(path)
    return path


class TestListado:
    def test_lista_solo_worktrees_de_ejecucion_con_su_estado(self, git_repo):
        limpio = _worktree(git_repo, "devvating/limpio")
        sucio = _worktree(git_repo, "devvating/sucio", sucio=True)
        wts = gitutil.list_worktrees(str(git_repo))
        por_rama = {w["rama"]: w for w in wts}
        # El worktree principal (el del vocero, rama 'main') no entra.
        assert set(por_rama) == {"devvating/limpio", "devvating/sucio"}
        assert por_rama["devvating/limpio"]["tiene_cambios"] is False
        assert por_rama["devvating/sucio"]["tiene_cambios"] is True
        assert por_rama["devvating/limpio"]["path"] == limpio
        assert por_rama["devvating/sucio"]["path"] == sucio


class TestLimpieza:
    def test_retira_los_limpios_y_conserva_la_rama(self, git_repo):
        path = _worktree(git_repo, "devvating/limpio")
        code = limpiar.main(["--repo", str(git_repo), "--yes"])
        assert code == 0
        assert not os.path.isdir(path)
        # La rama sobrevive: quitar el worktree no borra historia.
        assert "devvating/limpio" in subprocess.run(
            ["git", "branch"], cwd=git_repo, capture_output=True, text=True
        ).stdout

    def test_no_toca_los_que_tienen_cambios_sin_commitear(self, git_repo):
        path = _worktree(git_repo, "devvating/sucio", sucio=True)
        code = limpiar.main(["--repo", str(git_repo), "--yes"])
        assert code == 0
        assert os.path.isdir(path)  # protegido: tiene trabajo perdible
        assert os.path.isfile(os.path.join(path, "trabajo.txt"))

    def test_forzar_si_los_retira(self, git_repo):
        path = _worktree(git_repo, "devvating/sucio", sucio=True)
        code = limpiar.main(["--repo", str(git_repo), "--yes", "--forzar"])
        assert code == 0
        assert not os.path.isdir(path)

    def test_sin_confirmacion_no_borra_nada(self, git_repo, monkeypatch):
        path = _worktree(git_repo, "devvating/limpio")
        monkeypatch.setattr("rich.console.Console.input", lambda self, *a, **k: "n")
        assert limpiar.main(["--repo", str(git_repo)]) == 0
        assert os.path.isdir(path)

    def test_filtro_por_dias_salta_los_recientes(self, git_repo):
        path = _worktree(git_repo, "devvating/reciente")
        # Recién creado: con --dias 7 no debe entrar en la limpieza.
        assert limpiar.main(["--repo", str(git_repo), "--yes", "--dias", "7"]) == 0
        assert os.path.isdir(path)

    def test_sin_worktrees_no_falla(self, git_repo):
        assert limpiar.main(["--repo", str(git_repo), "--yes"]) == 0

    def test_rechaza_directorio_que_no_es_repo(self, tmp_path):
        assert limpiar.main(["--repo", str(tmp_path), "--yes"]) == 1

    def test_poda_registros_de_worktrees_ya_borrados(self, git_repo):
        path = _worktree(git_repo, "devvating/zombie")
        # Borrar el dir a mano deja el registro zombie en git: limpiar lo poda.
        subprocess.run(["rm", "-rf", path], check=True)
        assert limpiar.main(["--repo", str(git_repo), "--yes"]) == 0
        assert gitutil.list_worktrees(str(git_repo)) == []


class TestHuerfanos:
    """Directorios cuyo repo padre desapareció: ningún repo vivo los ve, así
    que `git worktree prune` no los alcanza. Es como se acumularon 65."""

    def _huerfano(self, base, gitdir_inexistente="/tmp/repo-que-ya-no-existe/.git"):
        d = base / "devvating-viejo-abc123"
        d.mkdir(parents=True)
        (d / ".git").write_text(f"gitdir: {gitdir_inexistente}\n", encoding="utf-8")
        (d / "resto.txt").write_text("basura\n", encoding="utf-8")
        return d

    def test_detecta_solo_los_de_repo_desaparecido(self, tmp_path, git_repo):
        base = tmp_path / "base"
        huerfano = self._huerfano(base)
        # Uno vivo (su gitdir existe) NO debe considerarse huérfano.
        vivo = base / "devvating-vivo-def456"
        vivo.mkdir()
        (vivo / ".git").write_text(f"gitdir: {git_repo}/.git\n", encoding="utf-8")
        encontrados = gitutil.worktrees_huerfanos(str(base))
        assert encontrados == [str(huerfano)]

    def test_base_inexistente_no_falla(self, tmp_path):
        assert gitutil.worktrees_huerfanos(str(tmp_path / "no-existe")) == []

    def test_limpiar_los_borra(self, tmp_path, git_repo, monkeypatch):
        base = tmp_path / "base"
        huerfano = self._huerfano(base)
        monkeypatch.setenv("DEVVATING_WORKTREE_DIR", str(base))
        assert limpiar.main(["--repo", str(git_repo), "--yes"]) == 0
        assert not huerfano.exists()

    def test_se_limpian_aunque_el_repo_no_tenga_worktrees(self, tmp_path, git_repo, monkeypatch):
        # Los huérfanos no dependen del repo: el flujo no debe saltárselos
        # por no haber nada registrado que limpiar.
        base = tmp_path / "base"
        huerfano = self._huerfano(base)
        monkeypatch.setenv("DEVVATING_WORKTREE_DIR", str(base))
        assert gitutil.list_worktrees(str(git_repo)) == []  # nada registrado
        limpiar.main(["--repo", str(git_repo), "--yes"])
        assert not huerfano.exists()

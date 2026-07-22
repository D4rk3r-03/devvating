"""Utilidades de git para la fase de ejecución (envoltura fina sobre subprocess).

Git es la red de seguridad de la fase 4 (DISENO.md sección 8): se trabaja en una
rama y se muestra el diff antes de que el vocero decida hacer commit o descartar.
"""

from __future__ import annotations

import os
import subprocess


def _run(args: list[str], repo: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True
    )


def is_git_repo(repo: str) -> bool:
    r = _run(["rev-parse", "--is-inside-work-tree"], repo)
    return r.returncode == 0 and r.stdout.strip() == "true"


def current_branch(repo: str) -> str:
    return _run(["rev-parse", "--abbrev-ref", "HEAD"], repo).stdout.strip()


def is_clean(repo: str) -> bool:
    return _run(["status", "--porcelain"], repo).stdout.strip() == ""


def create_branch(repo: str, name: str) -> str:
    r = _run(["checkout", "-b", name], repo)
    if r.returncode != 0:
        raise RuntimeError(f"No se pudo crear la rama '{name}': {r.stderr.strip()}")
    return name


def add_worktree(repo: str, branch: str, path: str) -> str:
    """Crea un worktree DESECHABLE en `path` con una rama nueva `branch` desde
    HEAD. Aísla la ejecución (D9 paso 2): el árbol de trabajo del vocero no se
    toca, así que un agente que aborta a medias nunca lo ensucia."""
    _run(["worktree", "prune"], repo)  # limpia registros de worktrees ya borrados
    r = _run(["worktree", "add", "-b", branch, path], repo)
    if r.returncode != 0:
        raise RuntimeError(
            f"No se pudo crear el worktree para '{branch}': {r.stderr.strip()}"
        )
    return path


def remove_worktree(repo: str, path: str) -> None:
    """Quita el worktree (forzado: puede tener cambios sin commitear) y poda."""
    _run(["worktree", "remove", "--force", path], repo)
    _run(["worktree", "prune"], repo)


def _worktree_de_rama(repo: str, branch: str) -> str | None:
    """Ruta del worktree que tiene `branch` chequeada, si alguno la tiene."""
    out = _run(["worktree", "list", "--porcelain"], repo).stdout
    actual: str | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            actual = line[len("worktree "):].strip()
        elif line.strip() == f"branch refs/heads/{branch}":
            return actual
    return None


def prune_worktrees(repo: str) -> None:
    """Descarta los registros de worktrees cuyo directorio ya no existe."""
    _run(["worktree", "prune"], repo)


def worktrees_huerfanos(base: str) -> list[str]:
    """Directorios de `base` cuyo repositorio padre ya no existe.

    Un worktree apunta a su repo por el archivo `.git` (`gitdir: <ruta>`). Si
    esa ruta desapareció (repo borrado, tmp_path de una corrida de tests), el
    directorio es basura irrecuperable: su rama vivía en el repo que ya no
    está, así que no hay nada que rescatar ni ningún repo desde el que
    `git worktree prune` pueda verlo.
    """
    if not os.path.isdir(base):
        return []
    huerfanos = []
    for nombre in os.listdir(base):
        ruta = os.path.join(base, nombre)
        marcador = os.path.join(ruta, ".git")
        if not os.path.isdir(ruta) or not os.path.isfile(marcador):
            continue
        try:
            with open(marcador, encoding="utf-8") as fh:
                contenido = fh.read().strip()
        except OSError:
            continue
        if not contenido.startswith("gitdir:"):
            continue
        gitdir = contenido[len("gitdir:"):].strip()
        if not os.path.exists(gitdir):
            huerfanos.append(ruta)
    return huerfanos


def worktree_tiene_cambios(path: str) -> bool:
    """True si el worktree tiene algo sin commitear (staged o en el árbol).

    Es el criterio EXACTO de seguridad para limpiarlo: quitar un worktree no
    borra su rama ni sus commits —esos sobreviven en el repo—, así que lo
    único que se pierde al removerlo es justamente lo que no está commiteado.
    """
    return _run(["status", "--porcelain"], path).stdout.strip() != ""


def list_worktrees(repo: str, prefix: str = "devvating/") -> list[dict]:
    """Worktrees de ejecución registrados, con lo necesario para decidir.

    Devuelve por cada uno: `path`, `rama`, `existe` (el dir sigue en disco) y
    `tiene_cambios` (trabajo sin commitear que se perdería al quitarlo). El
    worktree principal (el del vocero) queda fuera: solo se listan los de
    ramas bajo `prefix`.
    """
    out = _run(["worktree", "list", "--porcelain"], repo).stdout
    worktrees: list[dict] = []
    actual: dict | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            actual = {"path": line[len("worktree "):].strip(), "rama": ""}
        elif line.startswith("branch ") and actual is not None:
            actual["rama"] = line[len("branch "):].strip().removeprefix("refs/heads/")
        elif not line.strip() and actual is not None:
            worktrees.append(actual)
            actual = None
    if actual is not None:
        worktrees.append(actual)

    resultado = []
    for wt in worktrees:
        if not wt["rama"].startswith(prefix):
            continue
        existe = os.path.isdir(wt["path"])
        resultado.append({
            **wt,
            "existe": existe,
            "tiene_cambios": worktree_tiene_cambios(wt["path"]) if existe else False,
        })
    return resultado


def stage_all(repo: str) -> None:
    _run(["add", "-A"], repo)


def staged_diff(repo: str) -> str:
    return _run(["diff", "--cached", "--no-color"], repo).stdout


def staged_changed_files(repo: str) -> list[str]:
    out = _run(["diff", "--cached", "--name-only"], repo).stdout
    return [line for line in out.splitlines() if line]


def commit(repo: str, message: str) -> str:
    """Commitea lo que esté en staging; devuelve el sha corto.

    Decisión del vocero (D2, fase 4): el commit NUNCA es automático — lo
    dispara el humano. Esta función es el músculo; el gatillo vive en la UI/CLI.
    """
    if not message.strip():
        raise RuntimeError("El mensaje de commit no puede estar vacío.")
    r = _run(["commit", "-m", message], repo)
    if r.returncode != 0:
        raise RuntimeError(f"No se pudo commitear: {(r.stderr or r.stdout).strip()}")
    return _run(["rev-parse", "--short", "HEAD"], repo).stdout.strip()


def checkout(repo: str, branch: str) -> None:
    r = _run(["checkout", branch], repo)
    if r.returncode != 0:
        raise RuntimeError(f"No se pudo cambiar a '{branch}': {r.stderr.strip()}")


def delete_branch(repo: str, branch: str) -> None:
    # Si la rama aún tiene un worktree colgando (ejecución ni commiteada ni
    # descartada), git -D fallaría: se quita el worktree primero.
    wt = _worktree_de_rama(repo, branch)
    if wt:
        remove_worktree(repo, wt)
    r = _run(["branch", "-D", branch], repo)
    if r.returncode != 0:
        raise RuntimeError(f"No se pudo borrar la rama '{branch}': {r.stderr.strip()}")


def list_branches(repo: str, prefix: str = "devvating/") -> list[dict]:
    """Ramas bajo <prefix>, más recientes primero, con sha/fecha/asunto.

    Historial de ejecuciones para el Hub: cada debate ejecutado deja una rama
    devvating/<slug>-<fecha> que se acumula hasta que el vocero la fusione o
    descarte. Esto las lista para revisarlas y limpiarlas.
    """
    fmt = "%(refname:short)%09%(objectname:short)%09%(committerdate:iso8601)%09%(contents:subject)"
    out = _run(
        ["for-each-ref", "--sort=-committerdate", f"--format={fmt}",
         f"refs/heads/{prefix}"],
        repo,
    ).stdout
    ramas = []
    for line in out.splitlines():
        partes = line.split("\t")
        if len(partes) < 3:
            continue
        ramas.append({
            "nombre": partes[0],
            "sha": partes[1],
            "fecha": partes[2],
            "asunto": partes[3] if len(partes) > 3 else "",
        })
    return ramas


def discard_worktree(repo: str, worktree_path: str, branch: str) -> None:
    """Descarta una ejecución aislada: quita el worktree y borra su rama.

    A diferencia del viejo `discard_branch`, NUNCA toca el árbol de trabajo del
    vocero (no hay `reset --hard` sobre el árbol vivo — ese era el peligro
    activo que motivó el aislamiento por worktree, D9 paso 2).
    """
    if worktree_path:
        remove_worktree(repo, worktree_path)
    delete_branch(repo, branch)


def discard_branch(repo: str, base_branch: str, branch: str) -> None:
    """Descarta una rama de ejecución del árbol compartido (camino legado).

    Tira los cambios (staged y en el árbol), regresa a la base y borra la rama.
    Se conserva para ramas creadas sin worktree; el camino aislado usa
    `discard_worktree`, que no toca el árbol del vocero.
    """
    _run(["reset", "--hard"], repo)  # descarta staged + árbol de trabajo
    checkout(repo, base_branch)
    delete_branch(repo, branch)

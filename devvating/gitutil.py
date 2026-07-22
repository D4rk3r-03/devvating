"""Utilidades de git para la fase de ejecución (envoltura fina sobre subprocess).

Git es la red de seguridad de la fase 4 (DISENO.md sección 8): se trabaja en una
rama y se muestra el diff antes de que el vocero decida hacer commit o descartar.
"""

from __future__ import annotations

import json
import os
import subprocess


def _run(args: list[str], repo: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True
    )


def is_git_repo(repo: str) -> bool:
    r = _run(["rev-parse", "--is-inside-work-tree"], repo)
    return r.returncode == 0 and r.stdout.strip() == "true"


def tiene_commits(repo: str) -> bool:
    """True si el repo tiene al menos un commit (HEAD resoluble).

    Un `git init` recién hecho es un repo válido pero sin HEAD, y un worktree
    se ramifica DESDE HEAD: sin él nace vacío, sin ninguno de los archivos del
    proyecto. El agente trabajaría sobre la nada sin que nada fallara.
    """
    return _run(["rev-parse", "--verify", "HEAD"], repo).returncode == 0


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


# Metadatos de la ejecución que git NO puede saber (el returncode del backend,
# sobre todo: es lo que bloquea commitear un plan roto). Viven en el directorio
# ADMINISTRATIVO del worktree (`<repo>/.git/worktrees/<n>/`) y no dentro de su
# árbol —decisión D1 del vocero, 2026-07-22—: ahí `git add -A` no los ve, así
# que no contaminan el diff que revisa el vocero, y aun así `git worktree
# remove` se los lleva consigo, que era la virtud de ponerlos dentro.
_SIDECAR = "devvating-ejecucion.json"


def gitdir_de_worktree(path: str) -> str | None:
    """Directorio administrativo de un worktree, o None si no lo es.

    En un worktree, `.git` es un ARCHIVO con `gitdir: <ruta>` en vez de un
    directorio. Es el mismo parseo que usa `worktrees_huerfanos`.
    """
    marcador = os.path.join(path, ".git")
    if not os.path.isfile(marcador):
        return None
    try:
        with open(marcador, encoding="utf-8") as fh:
            contenido = fh.read().strip()
    except OSError:
        return None
    if not contenido.startswith("gitdir:"):
        return None
    return contenido[len("gitdir:"):].strip()


def escribir_sidecar(worktree: str, datos: dict) -> bool:
    """Guarda los metadatos de la ejecución. False si no se pudo (no fatal)."""
    gitdir = gitdir_de_worktree(worktree)
    if gitdir is None or not os.path.isdir(gitdir):
        return False
    try:
        with open(os.path.join(gitdir, _SIDECAR), "w", encoding="utf-8") as fh:
            json.dump(datos, fh, ensure_ascii=False, indent=2)
    except OSError:
        return False
    return True


def leer_sidecar(worktree: str) -> dict | None:
    """Metadatos de la ejecución, o None si no hay (o están ilegibles).

    None significa "no sé cómo terminó", y el llamador debe degradar de forma
    conservadora: sin returncode no se ofrece commitear.
    """
    gitdir = gitdir_de_worktree(worktree)
    if gitdir is None:
        return None
    try:
        with open(os.path.join(gitdir, _SIDECAR), encoding="utf-8") as fh:
            datos = json.load(fh)
    except (OSError, ValueError):
        return None
    return datos if isinstance(datos, dict) else None


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
        if not os.path.isdir(ruta):
            continue
        gitdir = gitdir_de_worktree(ruta)
        if gitdir is not None and not os.path.exists(gitdir):
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


# Rutas que casi nunca deben entrar en un commit: secretos y artefactos. Si
# están presentes y no hay `.gitignore`, `init_inicial` se niega en vez de
# commitearlas — el primer commit de un proyecto es para siempre aunque luego
# se borre el archivo (verificado: un `add -A` ciego mete el .env con su clave
# y `git show` la recupera).
_IGNORABLES = (".env", ".venv", "venv", "node_modules", "__pycache__", ".direnv")


def es_raiz_de_repo(ruta: str) -> bool:
    """True si `ruta` es la RAÍZ de un repositorio, no solo algo dentro de uno.

    `is_git_repo` busca hacia arriba, así que devuelve True para `<repo>/src`.
    Para descubrir proyectos hace falta la distinción: si no, cada carpeta
    interna de un repo aparece como un proyecto registrable (verificado en
    real: salían `docs`, `tests`, `src`… de cada repositorio).
    """
    toplevel = _run(["rev-parse", "--show-toplevel"], ruta).stdout.strip()
    return bool(toplevel) and os.path.realpath(toplevel) == os.path.realpath(ruta)


def esta_anidado_en_repo(ruta: str) -> bool:
    """True si algún ancestro de `ruta` ya es un repositorio git.

    Inicializar dentro de otro repo crearía historias solapadas y los worktrees
    de la ejecución saldrían del árbol equivocado.
    """
    actual = os.path.dirname(os.path.abspath(ruta))
    while True:
        if os.path.isdir(os.path.join(actual, ".git")):
            return True
        padre = os.path.dirname(actual)
        if padre == actual:
            return False
        actual = padre


def init_inicial(ruta: str, mensaje: str = "Estado inicial") -> str:
    """`git init` + primer commit en un solo paso. Devuelve el sha corto.

    Va junto a propósito: el ejecutor exige repo git CON al menos un commit
    (`tiene_commits`), así que un `init` a secas dejaría el proyecto igual de
    inservible para la fase 4.

    Se niega —con un error accionable— antes que hacer algo dudoso:
      - directorio vacío: no hay nada que debatir, y para commitear habría que
        inventar contenido del proyecto;
      - ya es repo, o está dentro de otro;
      - contiene secretos/artefactos típicos sin un `.gitignore` que los
        excluya. Aquí NO se genera el `.gitignore`: decidir qué se versiona en
        un proyecto ajeno es del vocero, no del Hub.
    """
    ruta = os.path.abspath(ruta)
    if not os.path.isdir(ruta):
        raise RuntimeError(f"'{ruta}' no es un directorio.")
    if os.path.isdir(os.path.join(ruta, ".git")):
        raise RuntimeError(f"'{ruta}' ya es un repositorio git.")
    if esta_anidado_en_repo(ruta):
        raise RuntimeError(
            f"'{ruta}' está dentro de otro repositorio git. Inicializarlo ahí "
            "solaparía historias y la ejecución trabajaría sobre el árbol "
            "equivocado."
        )
    contenido = [n for n in os.listdir(ruta) if not n.startswith(".git")]
    if not contenido:
        raise RuntimeError(
            f"'{ruta}' está vacío: no hay nada que debatir ni que commitear. "
            "Añade el material del proyecto y reintenta."
        )
    if not os.path.isfile(os.path.join(ruta, ".gitignore")):
        presentes = [n for n in _IGNORABLES if os.path.exists(os.path.join(ruta, n))]
        if presentes:
            raise RuntimeError(
                f"'{ruta}' contiene {', '.join(presentes)} y no tiene .gitignore. "
                "El primer commit se los llevaría —y un secreto commiteado queda "
                "en la historia aunque después borres el archivo—. Crea un "
                ".gitignore que los excluya y reintenta."
            )

    r = _run(["init", "-q"], ruta)
    if r.returncode != 0:
        raise RuntimeError(f"No se pudo inicializar: {(r.stderr or r.stdout).strip()}")
    stage_all(ruta)
    if not staged_changed_files(ruta):
        raise RuntimeError(
            f"Tras `git add -A` no quedó nada por commitear en '{ruta}' "
            "(¿todo su contenido está ignorado?)."
        )
    return commit(ruta, mensaje)


def merge(repo: str, branch: str) -> str:
    """Fusiona `branch` en la rama actual. Devuelve el resumen de git.

    Es la ÚNICA operación del Hub que escribe en la rama de trabajo del vocero
    (todo lo demás vive en ramas `devvating/` o en worktrees desechables), así
    que ante cualquier problema deshace: un conflicto dispara `merge --abort` y
    el árbol queda exactamente como estaba, con el error explicado. Nunca deja
    a medias un merge que el vocero tendría que resolver a mano desde la web,
    donde no hay herramientas para hacerlo.
    """
    r = _run(["merge", "--no-edit", branch], repo)
    if r.returncode != 0:
        _run(["merge", "--abort"], repo)
        detalle = (r.stdout + r.stderr).strip()
        raise RuntimeError(
            f"No se pudo fusionar '{branch}' (se deshizo, tu rama quedó intacta): "
            f"{detalle}"
        )
    return r.stdout.strip()


def ramas_sin_fusionar(repo: str, prefix: str = "devvating/") -> list[str]:
    """Ramas de ejecución cuyo trabajo aún no está en la rama actual.

    Es lo que queda pendiente de tu decisión tras commitear en la rama: el
    merge a la rama de trabajo sigue siendo manual, así que estas son las que
    tienen algo que nadie ha recogido.
    """
    # `--format` va ANTES de `--no-merged`: al revés, git lo toma como el
    # commit opcional de esa opción y aborta con "nombre de objeto mal formado".
    out = _run(["branch", "--format=%(refname:short)", "--no-merged"], repo).stdout
    return [l.strip() for l in out.splitlines() if l.strip().startswith(prefix)]


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

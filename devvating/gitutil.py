"""Utilidades de git para la fase de ejecución (envoltura fina sobre subprocess).

Git es la red de seguridad de la fase 4 (DISENO.md sección 8): se trabaja en una
rama y se muestra el diff antes de que el vocero decida hacer commit o descartar.
"""

from __future__ import annotations

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


def stage_all(repo: str) -> None:
    _run(["add", "-A"], repo)


def staged_diff(repo: str) -> str:
    return _run(["diff", "--cached", "--no-color"], repo).stdout


def staged_changed_files(repo: str) -> list[str]:
    out = _run(["diff", "--cached", "--name-only"], repo).stdout
    return [line for line in out.splitlines() if line]

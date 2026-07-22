"""Limpieza de los worktrees de ejecución que quedaron colgando.

La fase 4 aísla cada ejecución en un worktree desechable (D9 paso 2) que se
retira cuando el vocero cierra el ciclo: commit o descartar. Si no cierra
ninguno de los dos —mira el diff y se va, o ejecuta desde la CLI, que no
tiene botones—, el worktree se queda. Este subcomando es el recolector
explícito de esa basura.

Uso:
    devvating limpiar [--repo .] [--dias N] [--forzar] [--yes]

Criterio de seguridad: quitar un worktree NO borra su rama ni sus commits
(sobreviven en el repo), así que lo único que se pierde es lo que esté sin
commitear. Por eso los worktrees con cambios sin commitear se saltan y se
reportan; borrarlos exige --forzar. Las RAMAS nunca se tocan aquí: se listan
en el Hub (/api/ramas) y se borran ahí o con `git branch -D`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import time

from rich.console import Console

from . import gitutil
from .executor import base_worktrees


def _dias_desde(path: str) -> float:
    """Antigüedad del worktree en días, por la mtime de su directorio."""
    try:
        return (time.time() - os.path.getmtime(path)) / 86400
    except OSError:
        return 0.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="devvating limpiar",
        description="Retira los worktrees de ejecución que quedaron colgando.",
    )
    parser.add_argument("--repo", default=".", help="Raíz del repositorio.")
    parser.add_argument(
        "--dias", type=float, default=0.0,
        help="Solo los más antiguos que N días (default: todos).",
    )
    parser.add_argument(
        "--forzar", action="store_true",
        help="Incluye los que tienen cambios SIN COMMITEAR. Esos cambios se "
             "pierden (la rama y sus commits no). Opt-in explícito.",
    )
    parser.add_argument("--yes", action="store_true", help="Omite la confirmación.")
    args = parser.parse_args(argv)

    console = Console()

    if not gitutil.is_git_repo(args.repo):
        console.print(f"[red]'{args.repo}' no es un repositorio git.[/red]")
        return 1

    _limpiar_registrados(console, args)
    # Siempre después de los registrados, y pase lo que pase con ellos: los
    # huérfanos no dependen de este repo (ningún repo vivo los ve).
    _limpiar_huerfanos(console, confirmar=not args.yes)
    return 0


def _limpiar_registrados(console: Console, args) -> None:
    """Worktrees que el repo todavía conoce (`git worktree list`)."""
    # Poda previa: los registros zombie (git los conoce, el dir ya no existe)
    # desaparecen solos y no hay que preguntar por ellos.
    gitutil.prune_worktrees(args.repo)

    worktrees = gitutil.list_worktrees(args.repo)
    if args.dias:
        worktrees = [w for w in worktrees if _dias_desde(w["path"]) >= args.dias]

    if not worktrees:
        console.print("[green]No hay worktrees de ejecución registrados que limpiar.[/green]")
        return

    limpiables = [w for w in worktrees if not w["tiene_cambios"] or args.forzar]
    protegidos = [w for w in worktrees if w["tiene_cambios"] and not args.forzar]

    console.rule("[bold]DEVVATING · Limpieza de worktrees")
    for w in limpiables:
        marca = "[red](con cambios sin commitear)[/red] " if w["tiene_cambios"] else ""
        console.print(
            f"  • {marca}{w['rama']} [dim]({_dias_desde(w['path']):.1f} días)[/dim]"
        )
    for w in protegidos:
        console.print(
            f"  [yellow]⚠ se conserva:[/yellow] {w['rama']} "
            f"[dim]— tiene cambios sin commitear[/dim]"
        )

    if not limpiables:
        console.print(
            "\n[yellow]Todos tienen trabajo sin commitear; no se tocó ninguno.[/yellow]\n"
            "[dim]Revísalos y commitea lo que valga, o usa --forzar para "
            "descartar esos cambios (las ramas y sus commits sobreviven).[/dim]"
        )
        return

    if args.forzar and any(w["tiene_cambios"] for w in limpiables):
        console.print(
            "\n[bold red]⚠ --forzar: se perderán cambios sin commitear de los "
            "worktrees marcados en rojo.[/bold red]"
        )

    if not args.yes:
        resp = console.input(
            f"\n[bold yellow]Vocero[/bold yellow] · ¿Retirar {len(limpiables)} "
            "worktree(s)? (y/N): "
        ).strip().lower()
        if resp not in ("y", "s", "yes", "si", "sí"):
            console.print("Cancelado por el vocero.")
            return

    for w in limpiables:
        gitutil.remove_worktree(args.repo, w["path"])
    console.print(
        f"\n[green]✓ {len(limpiables)} worktree(s) retirados.[/green] "
        "[dim]Las ramas siguen ahí; bórralas desde el Hub o con `git branch -D`.[/dim]"
    )


def _limpiar_huerfanos(console: Console, confirmar: bool) -> int:
    """Borra los directorios cuyo repo padre ya no existe (basura pura).

    Van aparte del flujo principal porque ningún repo vivo los ve: no salen en
    `git worktree list` ni los poda `git worktree prune`. Sin esto quedan para
    siempre — es como se acumularon 65 en una auditoría.
    """
    base = base_worktrees()
    huerfanos = gitutil.worktrees_huerfanos(base)
    if not huerfanos:
        return 0
    console.print(
        f"\n[bold]Huérfanos en {base}[/bold] "
        "[dim](su repositorio ya no existe: nada que rescatar)[/dim]"
    )
    for h in huerfanos:
        console.print(f"  • {os.path.basename(h)}")
    if confirmar:
        resp = console.input(
            f"\n[bold yellow]Vocero[/bold yellow] · ¿Borrar {len(huerfanos)} "
            "directorio(s) huérfano(s)? (y/N): "
        ).strip().lower()
        if resp not in ("y", "s", "yes", "si", "sí"):
            console.print("Huérfanos conservados.")
            return 0
    for h in huerfanos:
        shutil.rmtree(h, ignore_errors=True)
    console.print(f"[green]✓ {len(huerfanos)} huérfano(s) borrados.[/green]")
    return len(huerfanos)


if __name__ == "__main__":
    raise SystemExit(main())

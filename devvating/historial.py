"""Vista global de todo lo que devvating ha debatido en esta máquina (D13).

Uso:
    devvating historial [--limite 30] [--repo .] [--pendientes]
    devvating historial --reindexar [--repo a --repo b ...]

Los transcripts viven junto a su repo; esto solo los indexa para poder verlos
juntos. Si el índice se pierde o queda viejo, `--reindexar` lo reconstruye
entero desde los propios transcripts — es caché, no fuente.
"""

from __future__ import annotations

import argparse
import os

from rich import box
from rich.console import Console
from rich.table import Table

from . import registro


def _tema_corto(tema: str) -> str:
    """Una línea legible del tema.

    Los prompts reales son largos y los de una ronda de cierre traen pegado el
    bloque "DECISIONES YA TOMADAS...", que aquí es ruido: en una tabla lo útil
    es reconocer el debate, no releerlo.
    """
    corte = tema.split("DECISIONES YA TOMADAS")[0]
    return " ".join(corte.split())[:110] or "(sin tema)"


def _estado_legible(fila: dict) -> str:
    if fila["parcial"]:
        return "[yellow]a medias[/yellow]"
    if fila["decisiones_abiertas"]:
        return f"[magenta]{fila['decisiones_abiertas']} decisión·es[/magenta]"
    if fila["convergio"]:
        return "[green]convergido[/green]"
    return "[dim]sin acuerdo[/dim]"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="devvating historial",
        description="Todo lo que devvating ha debatido, de todos tus proyectos.",
    )
    parser.add_argument("--limite", type=int, default=30, help="Cuántos mostrar.")
    parser.add_argument(
        "--repo", action="append", default=None,
        help="Filtra por repo; con --reindexar, qué repos escanear (repetible).",
    )
    parser.add_argument(
        "--reindexar", action="store_true",
        help="Reconstruye el índice desde los transcripts de los repos dados.",
    )
    parser.add_argument(
        "--pendientes", action="store_true",
        help="Solo los que piden algo: decisiones abiertas o debates a medias.",
    )
    parser.add_argument(
        "--limpiar", action="store_true",
        help="Olvida las entradas cuyo transcript ya no está en disco.",
    )
    args = parser.parse_args(argv)

    console = Console()

    if args.reindexar:
        repos = args.repo or ["."]
        indexados, saltados = registro.reindexar(repos)
        console.print(
            f"[green]✓ {indexados} debate(s) indexados[/green]"
            + (f" · [yellow]{saltados} ilegible(s) saltado(s)[/yellow]" if saltados else "")
        )
        console.print(f"[dim]{registro.ruta_db()}[/dim]")
        return 0

    if args.limpiar:
        n = registro.olvidar_inexistentes()
        console.print(f"[green]✓ {n} entrada(s) olvidada(s)[/green] (transcript ya no existe).")
        return 0

    # Con --repo (uno) se filtra; el índice guarda rutas absolutas resueltas.
    filas = registro.listar(limite=args.limite,
                            repo=args.repo[0] if args.repo else None)
    if args.pendientes:
        filas = [f for f in filas if f["parcial"] or f["decisiones_abiertas"]]

    if not filas:
        console.print(
            "[yellow]El índice está vacío.[/yellow] Los debates se indexan al "
            "guardarse; para los anteriores:\n"
            "[dim]  devvating historial --reindexar --repo /ruta/al/proyecto[/dim]"
        )
        return 0

    # Rich no reparte bien entre columnas `no_wrap` (cada una pide ancho
    # infinito y se aplastan entre sí; con datos reales se vio primero el tema
    # colapsado a cero y luego comiéndose las demás). Así que el ancho del tema
    # se calcula: lo que sobre tras las fijas, con un mínimo legible.
    FIJAS = 12 + 13 + 13 + 5          # fecha, proyecto, estado, coste
    ancho_tema = max(24, console.width - FIJAS - 12)  # 12 ≈ padding y bordes

    tabla = Table(title="DEVVATING · historial de debates", box=box.SIMPLE,
                  padding=(0, 1))
    tabla.add_column("fecha", style="dim", no_wrap=True, width=12)
    tabla.add_column("proyecto", style="cyan", no_wrap=True,
                     width=13, overflow="ellipsis")
    tabla.add_column("tema", overflow="ellipsis", no_wrap=True, width=ancho_tema)
    tabla.add_column("estado", no_wrap=True, width=13)
    tabla.add_column("$", justify="right", no_wrap=True, width=5)

    for f in filas:
        costo = f"{f['coste']:.2f}" if f["coste"] is not None else "—"
        marca = "" if f["existe"] else " [red]✗[/red]"
        fecha = f["fecha"]  # AAAAMMDD-HHMMSS
        tabla.add_row(
            f"{fecha[4:6]}-{fecha[6:8]} {fecha[9:11]}:{fecha[11:13]}",
            os.path.basename(f["repo"]) + marca,
            _tema_corto(f["tema"]),
            _estado_legible(f),
            costo,
        )
    console.print(tabla)

    total = sum(f["coste"] or 0 for f in filas)
    pendientes = [f for f in filas if f["parcial"] or f["decisiones_abiertas"]]
    console.print(
        f"[dim]{len(filas)} debate(s) · ${total:.2f} en total"
        + (f" · {len(pendientes)} esperando algo de ti" if pendientes else "")
        + "[/dim]"
    )
    if any(not f["existe"] for f in filas):
        console.print(
            "[dim]✗ = el transcript ya no está en disco; "
            "`devvating historial --limpiar` los olvida.[/dim]"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

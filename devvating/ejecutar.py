"""M3 — Ejecución del plan aprobado desde la consola.

Uso:
    # Plan desde la síntesis de un debate:
    python -m devvating.ejecutar --repo /ruta/proyecto --from-transcript transcripts/xxx.json

    # Plan desde un archivo de texto/markdown:
    python -m devvating.ejecutar --repo /ruta/proyecto --plan-file plan.md

Opciones:
    --branch NOMBRE     Nombre de la rama (por defecto devvating/<slug>-<fecha>).
    --allow-commands    Permite al ejecutor correr comandos (Bash). PELIGROSO.
    --yes               Omite la confirmación de aprobación (fase 3).

Flujo: muestra el plan → el vocero aprueba → crea rama → ejecuta headless →
muestra el diff. Nada se confirma (commit) automáticamente.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax

from .appconfig import ProjectConfig
from .auditor import crear_auditor
from .executor import (
    ClaudeCodeBackend,
    Executor,
    ExecutionPlan,
    ExecutorError,
    decisiones_crucial_sin_resolver,
)


def _load_plan(args: argparse.Namespace) -> ExecutionPlan:
    if args.from_transcript:
        data = json.loads(Path(args.from_transcript).read_text(encoding="utf-8"))
        text = data.get("synthesis", "").strip()
        if not text:
            raise ExecutorError("El transcript no contiene una síntesis.")
        title = data.get("topic", {}).get("prompt", "plan")
        return ExecutionPlan(
            text=text, title=title,
            decisiones_pendientes=decisiones_crucial_sin_resolver(data.get("decisiones")),
        )
    if args.plan_file:
        return ExecutionPlan(
            text=Path(args.plan_file).read_text(encoding="utf-8").strip(),
            title=Path(args.plan_file).stem,
        )
    raise ExecutorError("Indica --from-transcript o --plan-file.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="devvating ejecutar", description="Ejecuta un plan aprobado.")
    parser.add_argument("--repo", required=True, help="Raíz del repositorio git objetivo.")
    parser.add_argument("--from-transcript", help="Transcript de debate (usa su síntesis).")
    parser.add_argument("--plan-file", help="Archivo de texto/markdown con el plan.")
    parser.add_argument("--branch", help="Nombre de la rama a crear.")
    parser.add_argument(
        "--model", default=None,
        help="Modelo del agente ejecutor (default: sonnet o DEVVATING_EXEC_MODEL; "
             "D8: los modelos de razonamiento se reservan para el debate).",
    )
    parser.add_argument(
        "--allow-commands",
        action="store_true",
        help="Permite ejecutar comandos (Bash). Salta permisos — PELIGROSO.",
    )
    parser.add_argument("--yes", action="store_true", help="Omite la confirmación.")
    parser.add_argument(
        "--allow-open-decisions",
        action="store_true",
        help="Fuerza la ejecución aunque el plan tenga decisiones cruciales sin "
             "resolver. Opt-in explícito (como --allow-commands): normalmente el "
             "vocero cierra las decisiones antes de ejecutar.",
    )
    parser.add_argument(
        "--verificar",
        action="store_true",
        help="Fase 5 (M9): tras aplicar el plan, corre el comando de "
             "'verificacion' de .devvating.json; si falla, intenta UNA "
             "corrección acotada y reporta el resultado. El comando viene del "
             "repo objetivo: exige confirmación aparte de --yes — PELIGROSO.",
    )
    parser.add_argument(
        "--auditar",
        action="store_true",
        help="Fase 5 (D16): tras aplicar el plan, un agente auditor "
             "(read-only, nombre en 'auditoria' de .devvating.json) revisa si "
             "el diff hace lo que el plan pidió. Un veredicto 'desviado' NO "
             "detiene la CLI, pero se reporta y bloquearía el commit en el Hub.",
    )
    args = parser.parse_args(argv)

    console = Console()

    try:
        plan = _load_plan(args)
    except (ExecutorError, OSError, json.JSONDecodeError) as exc:
        console.print(f"[red]No se pudo cargar el plan: {exc}[/red]")
        return 1

    backend = ClaudeCodeBackend(model=args.model)

    console.rule("[bold]DEVVATING · Ejecución (M3)")
    console.print(f"[dim]modelo ejecutor: {backend.model}[/dim]")
    console.print(Panel(Markdown(plan.text), title="[green]Plan a ejecutar", border_style="green"))

    # Gate de decisiones: aviso claro antes de que el vocero apruebe.
    if plan.decisiones_pendientes:
        pendientes = "\n".join(f"  • {p}" for p in plan.decisiones_pendientes)
        if args.allow_open_decisions:
            console.print(
                "[bold red]⚠ --allow-open-decisions: el plan tiene decisiones "
                "cruciales SIN resolver y se ejecutará igual (el ejecutor "
                f"resolverá la ambigüedad en silencio):\n{pendientes}[/bold red]"
            )
        else:
            console.print(
                "[red]El plan tiene decisiones cruciales sin resolver; ciérralas "
                f"antes de ejecutar:\n{pendientes}\n[/red]"
                "[dim]Resuélvelas (aún no hay UI de cierre en CLI) o fuerza con "
                "--allow-open-decisions bajo tu riesgo.[/dim]"
            )
            return 1

    # Fase 3 — arbitraje del vocero.
    if args.allow_commands:
        console.print(
            "[bold red]⚠ --allow-commands: el ejecutor podrá correr comandos "
            "arbitrarios (saltando permisos). Se corre en una rama, pero revisa "
            "el diff con cuidado.[/bold red]"
        )
    if not args.yes:
        resp = console.input(
            "[bold yellow]Vocero[/bold yellow] · ¿Aprobar y ejecutar este plan? (y/N): "
        ).strip().lower()
        if resp not in ("y", "s", "yes", "si", "sí"):
            console.print("Cancelado por el vocero.")
            return 0

    # M9 — verificación (fase 5): comando configurado en el REPO OBJETIVO, así
    # que --verificar por sí solo no basta — exige confirmación explícita y
    # aparte de --yes (mismo régimen que --allow-commands, protocolo 5). Un
    # repo hostil no puede disparar ejecución remota disfrazada de config.
    verify_command: str | None = None
    if args.verificar:
        pc = ProjectConfig.load(args.repo)
        if not pc.verificacion:
            console.print(
                "[yellow]--verificar activado, pero '.devvating.json' no trae "
                "'verificacion'; se omite.[/yellow]"
            )
        else:
            console.print(Panel(
                pc.verificacion, title="[bold red]Comando de verificación (.devvating.json)",
                border_style="red",
            ))
            console.print(
                "[bold red]⚠ Viene del repositorio objetivo y se correrá tal "
                "cual tras aplicar el plan (y tras la corrección, si falla). "
                "Revísalo con cuidado.[/bold red]"
            )
            resp = console.input(
                "[bold yellow]Vocero[/bold yellow] · ¿Confirmas correr este "
                "comando de verificación? (y/N): "
            ).strip().lower()
            if resp in ("y", "s", "yes", "si", "sí"):
                verify_command = pc.verificacion
            else:
                console.print("Verificación omitida por el vocero.")

    # Auditoría (fase 5, D16): read-only, así que no exige el ritual de
    # confirmación de --verificar (que sí corre un comando del repo). El agente
    # sale de 'auditoria' en .devvating.json; sin él, --auditar se omite.
    auditor_backend = None
    if args.auditar:
        pc_aud = ProjectConfig.load(args.repo)
        if not pc_aud.auditoria:
            console.print(
                "[yellow]--auditar activado, pero '.devvating.json' no trae "
                "'auditoria'; se omite.[/yellow]"
            )
        else:
            try:
                auditor_backend = crear_auditor(pc_aud.auditoria)
            except ValueError as exc:
                console.print(f"[red]{exc}[/red]")
                return 1

    executor = Executor(
        args.repo,
        backend,
        on_event=lambda ev, val: console.print(f"[dim]· {ev}: {val}[/dim]"),
    )

    try:
        outcome = executor.execute(
            plan, allow_commands=args.allow_commands, branch=args.branch,
            verify_command=verify_command,
            auditor_backend=auditor_backend,
            allow_open_decisions=args.allow_open_decisions,
        )
    except ExecutorError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    console.rule(f"[bold]Resultado · rama {outcome.branch}")
    if outcome.returncode != 0:
        console.print(f"[yellow]El backend salió con código {outcome.returncode}.[/yellow]")

    if not outcome.changed_files:
        console.print("[yellow]El ejecutor no produjo cambios.[/yellow]")
    else:
        console.print(f"[bold]Archivos cambiados ({len(outcome.changed_files)}):[/bold]")
        for f in outcome.changed_files:
            console.print(f"  • {f}")
        console.print("\n[bold]Diff:[/bold]")
        console.print(Syntax(outcome.diff, "diff", theme="ansi_dark", word_wrap=True))

    if outcome.verify_command:
        console.rule("[bold]Verificación (fase 5)")
        if outcome.verify_corrected:
            console.print(
                "[yellow]Falló en el primer intento; se corrió UNA corrección "
                "acotada.[/yellow]"
            )
        if outcome.verify_returncode == 0:
            console.print("[green]✓ Verificación OK.[/green]")
        else:
            console.print(
                f"[red]✗ Verificación sigue fallando (código "
                f"{outcome.verify_returncode}). Reporte honesto: el plan no "
                "quedó verde.[/red]"
            )
            console.print(Syntax(
                outcome.verify_output, "text", theme="ansi_dark", word_wrap=True
            ))

    if outcome.auditoria:
        console.rule("[bold]Auditoría de correspondencia (fase 5)")
        veredicto = outcome.auditoria.get("veredicto", "desconocido")
        if veredicto == "conforme":
            console.print("[green]✓ Auditor: el diff corresponde al plan.[/green]")
        elif veredicto == "desviado":
            console.print(
                "[red]✗ Auditor: la ejecución se DESVIÓ del plan. "
                f"{outcome.auditoria.get('resumen', '')}[/red]"
            )
            for h in outcome.auditoria.get("no_pedido", []):
                marca = "" if h.get("cita_localizada") else " [dim](cita no localizada en el diff)[/dim]"
                console.print(f"  • No pedido: {h.get('que', '')}{marca}")
            for h in outcome.auditoria.get("omitido", []):
                console.print(f"  • Omitido: {h.get('que', '')}")
            console.print(
                "[dim]La CLI no detiene por esto; en el Hub este veredicto "
                "bloquearía el commit salvo forzar.[/dim]"
            )
        else:
            console.print(
                "[yellow]Auditor sin veredicto legible: no se pudo auditar "
                "(no bloquea).[/yellow]"
            )

    # El plan se aplicó en un worktree aislado, no en el árbol del vocero: las
    # instrucciones tienen que apuntar ahí, o el vocero busca sus cambios donde
    # no están y deja el worktree colgando (lo recoge `devvating limpiar`).
    console.print(
        f"\n[bold]Vocero:[/bold] los cambios están en [cyan]{outcome.worktree}[/cyan] "
        f"(rama [cyan]{outcome.branch}[/cyan]), en staging y sin commitear.\n"
        f"  Conservar:  git -C {outcome.worktree} commit -m \"…\"\n"
        f"  Descartar:  git worktree remove --force {outcome.worktree} && "
        f"git branch -D {outcome.branch}\n"
        "[dim]Los worktrees que queden colgando se recogen con `devvating limpiar`.[/dim]"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

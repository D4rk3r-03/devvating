"""Debate completo (N rondas) desde la consola.

Uso:
    devvating debate "tu tema"  [--files "a.py, b.py"] [--repo .]
        [--rounds N] [--synthesizer <agente>|auto] [--agentes a,b]
        [--sesgos audaz,cauto] [--profundo] [--interactivo]
    devvating debate --resume transcripts/<x>.partial.json  [--agentes a,b]
        (reanuda un debate interrumpido sin repetir los turnos ya pagados)

Los defaults (rondas, modo profundo, repo, files, rotación) se toman de
`.devvating.json` si existe; los flags CLI tienen prioridad. Con
`--synthesizer auto` (por defecto) el sintetizador rota entre debates (D3).

Auto-debate (mismo agente dos veces, p. ej. `--agentes claude-cli,claude-cli`):
el par se desambigua a `claude#1`/`claude#2` y, sin `--sesgos` explícitos, se
les asigna el par de inclinaciones por defecto (audaz/cauto) para que no sea un
eco puro. La divergencia real la aporta el sesgo, no la identidad separada.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from . import agentes as banco
from . import registro
from . import roles
from . import rotation
from .adapters.base import AgentAdapter, SessionLimitError, TurnUsage
from .appconfig import ProjectConfig
from .config import Config
from .orchestrator import DebateAbortedError, DebateSession, DebateTopic, Orchestrator, Turn

_COLORS = {"claude": "cyan", "gemini": "magenta", "antigravity": "blue", "kimi": "green"}


def _color(agente: str) -> str:
    """Color del agente, tolerando el sufijo '#n' de un auto-debate."""
    return _COLORS.get(agente.split("#")[0], "white")


def _save_transcript(session: DebateSession, repo_root: str, parcial: bool = False) -> Path:
    out_dir = Path(repo_root) / "transcripts"
    out_dir.mkdir(exist_ok=True)
    slug = "-".join(session.topic.prompt.lower().split()[:6]) or "debate"
    slug = "".join(c for c in slug if c.isalnum() or c == "-")[:60]
    sufijo = ".partial.json" if parcial else ".json"
    path = out_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}{sufijo}"
    path.write_text(
        json.dumps(asdict(session), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # Alta en el índice global (D13). Va DESPUÉS de escribir el transcript y
    # nunca levanta: el índice es una comodidad reconstruible, así que un fallo
    # suyo no puede costar un debate ya pagado. Este es el punto único por el
    # que pasan la CLI y el Hub, así que basta con engancharlo aquí.
    registro.registrar(path, repo_root)
    return path


def make_agent(provider: str, backend: str, cfg: Config, repo: str) -> AgentAdapter:
    """Compatibilidad D5: par clásico proveedor+backend, resuelto vía roster."""
    return banco.crear(f"{provider}-{backend}", cfg, repo)


def _load_partial_session(path: str) -> DebateSession:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    topic = DebateTopic(
        prompt=data["topic"]["prompt"],
        context_hint=data["topic"].get("context_hint", "")
    )
    
    turns = []
    for td in data.get("turns", []):
        usage = None
        if td.get("usage"):
            usage = TurnUsage(**td["usage"])
        turns.append(Turn(
            round=td["round"],
            phase=td["phase"],
            agent=td["agent"],
            text=td["text"],
            verdict=td.get("verdict"),
            usage=usage
        ))
    
    return DebateSession(
        topic=topic,
        turns=turns,
        rounds_run=data.get("rounds_run", 0),
        converged=data.get("converged", False),
        converged_round=data.get("converged_round"),
        deep_mode=data.get("deep_mode", False),
        synthesis=data.get("synthesis", ""),
        synthesizer=data.get("synthesizer", "")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="devvating debate", description="Debate multi-agente.")
    parser.add_argument("tema", nargs="?", default=None, help="El tema a debatir (problema, mejora, decisión).")
    parser.add_argument("--resume", default=None, help="Ruta a un archivo .partial.json para reanudar.")
    parser.add_argument("--files", default=None, help="Pista de archivos relevantes.")
    parser.add_argument("--repo", default=None, help="Raíz del repositorio.")
    parser.add_argument("--rounds", type=int, default=None, help="Tope de rondas de réplica.")
    parser.add_argument(
        "--synthesizer",
        default="auto",
        help="Quién sintetiza: nombre de agente del par, o 'auto' (rota entre debates).",
    )
    parser.add_argument(
        "--agentes",
        default=None,
        help=(
            "Par de debatientes del roster, separados por coma (D7). "
            f"Roster: {', '.join(banco.nombres())}. Ej: --agentes antigravity,claude-cli"
        ),
    )
    parser.add_argument(
        "--sesgos",
        default=None,
        help=(
            "Inclinación por agente, separadas por coma, para romper el eco de un "
            f"auto-debate. Perfiles: {', '.join(roles.SESGOS)}. Ej: --sesgos audaz,cauto"
        ),
    )
    parser.add_argument("--profundo", action="store_true", help="Ronda de inversión (~2x coste).")
    parser.add_argument("--interactivo", action="store_true", help="Nota del vocero por ronda.")
    parser.add_argument(
        "--claude-backend", choices=["api", "cli"], default=None,
        help="Backend de Claude: api (SDK, créditos) o cli (claude -p, suscripción).",
    )
    parser.add_argument(
        "--gemini-backend", choices=["api", "cli"], default=None,
        help="Backend de Gemini: api (SDK) o cli (gemini -p, suscripción).",
    )
    args = parser.parse_args(argv)

    if not args.tema and not args.resume:
        parser.error("Debes proveer un tema o usar --resume para reanudar un debate previo.")

    old_session = None
    if args.resume:
        old_session = _load_partial_session(args.resume)
        args.tema = old_session.topic.prompt
        if old_session.topic.context_hint and not args.files:
            args.files = old_session.topic.context_hint
        args.profundo = old_session.deep_mode

    console = Console()
    cfg = Config.from_env()

    # Precedencia: flag CLI > .devvating.json > default.
    pc = ProjectConfig.load(".")
    repo = args.repo if args.repo is not None else pc.repo
    files = args.files if args.files is not None else pc.files
    rounds = args.rounds if args.rounds is not None else pc.rounds
    deep = args.profundo or pc.deep_mode
    claude_backend = args.claude_backend or pc.claude_backend
    gemini_backend = args.gemini_backend or pc.gemini_backend

    # Par de debatientes (D7): --agentes > config > par clásico D5.
    if args.agentes:
        nombres_par = [n.strip() for n in args.agentes.split(",") if n.strip()]
    elif pc.agentes:
        nombres_par = pc.agentes
    else:
        nombres_par = [f"claude-{claude_backend}", f"gemini-{gemini_backend}"]
    try:
        agente_a, agente_b = banco.par(nombres_par, cfg, repo)
    except (ValueError, RuntimeError) as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    # Sesgos por agente (rompen el eco de un auto-debate). --sesgos > config;
    # si es auto-debate sin sesgos, se aplica el par por defecto (audaz/cauto)
    # para que 'claude vs claude' no sea un eco puro. En un par de familias
    # distintas sin sesgos, se mantiene el comportamiento clásico (None).
    autodebate = banco.es_autodebate(agente_a, agente_b)
    if args.sesgos is not None:
        nombres_sesgos = [s.strip() for s in args.sesgos.split(",") if s.strip()]
    else:
        nombres_sesgos = list(pc.sesgos)
    try:
        biases, etiqueta_sesgos = roles.resolver_biases(nombres_sesgos, autodebate)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    # En un auto-debate exigimos al menos 2 rondas antes de honrar la
    # convergencia: evita que un eco declare acuerdo en la primera réplica.
    # Clamp a `rounds` para no romper si el vocero forzó --rounds 1.
    min_rounds = min(2, rounds) if autodebate else 1

    # Resolución del sintetizador (rotación automática, D3 capa 1).
    auto_rotate = args.synthesizer == "auto" and pc.auto_rotate
    if auto_rotate:
        state = rotation.load(repo)
        synth_index = state.synthesizer_index()
    elif args.synthesizer == agente_b.name:
        synth_index = 1
    elif args.synthesizer in (agente_a.name, "auto"):
        synth_index = 0  # auto con rotación desactivada cae al primero
    else:
        console.print(
            f"[red]Sintetizador '{args.synthesizer}' no está en el par: "
            f"{agente_a.name}, {agente_b.name} (o usa 'auto').[/red]"
        )
        return 1

    # Heartbeat (M6a): spinner con agente, etapa y cronómetro durante cada
    # turno, usando los pares *_inicio/*_fin que el orquestador ya emite.
    latido: dict = {"activo": None}

    def _detener_latido() -> None:
        if latido["activo"] is not None:
            latido["activo"].stop()
            latido["activo"] = None

    def _iniciar_latido(agente: str, fase: str) -> None:
        _detener_latido()
        color = _color(agente)
        prog = Progress(
            SpinnerColumn(),
            TextColumn(f"[{color}]{agente}[/{color}] · {fase}…"),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        )
        prog.start()
        prog.add_task("", total=None)
        latido["activo"] = prog

    def on_event(evento: str, agente: str, texto: str | None) -> None:
        if evento == "ronda":
            _detener_latido()
            console.rule(f"[bold]{agente}")
            return
        if evento == "convergencia":
            _detener_latido()
            console.print(f"[bold green]✓ Convergencia en {agente}[/bold green]")
            return
        if evento == "reintento":
            console.print(f"[yellow]⟳ {agente}: {texto}[/yellow]")
            return
        color = _color(agente)
        if evento.endswith("_inicio"):
            _iniciar_latido(agente, evento.removesuffix("_inicio"))
        elif evento.endswith("_fin") and texto is not None:
            _detener_latido()
            fase = evento.removesuffix("_fin")
            console.print(
                Panel(Markdown(texto), title=f"[{color}]{agente} — {fase}[/{color}]",
                      border_style=color)
            )

    def on_intervention(ronda: int) -> str | None:
        if not args.interactivo:
            return None
        nota = console.input(
            f"[bold yellow]Vocero[/bold yellow] · nota antes de la ronda {ronda} "
            "(Enter para omitir): "
        ).strip()
        return nota or None

    orch = Orchestrator(
        agente_a, agente_b, repo_root=repo, on_event=on_event, biases=biases
    )
    topic = DebateTopic(prompt=args.tema, context_hint=files)

    console.rule("[bold]DEVVATING · Debate")
    console.print(f"[bold]Tema:[/bold] {args.tema}")
    console.print(
        f"[dim]rondas≤{rounds} · profundo={deep} · "
        f"sintetizador={'auto' if auto_rotate else args.synthesizer} · "
        f"agentes: {agente_a.name} vs {agente_b.name}"
        f"{f' · sesgos: {etiqueta_sesgos}' if etiqueta_sesgos else ''}[/dim]\n"
    )

    try:
        session = orch.run(
            topic,
            max_rounds=rounds,
            min_rounds=min_rounds,
            synthesizer_index=synth_index,
            deep_mode=deep,
            on_intervention=on_intervention,
            old_session=old_session,
        )
    except DebateAbortedError as exc:
        # Amabilidad (plan de resiliencia): nada de traceback — mensaje humano
        # y los turnos ya pagados a salvo en un transcript parcial.
        console.print(f"\n[bold red]Debate interrumpido:[/bold red] {exc.causa}")
        if isinstance(exc.causa, SessionLimitError) and exc.causa.resets_at:
            console.print(
                f"[yellow]La cuota de suscripción se reinicia a las "
                f"{exc.causa.resets_at} — relanza el debate entonces.[/yellow]"
            )
        if exc.session.turns:
            parcial = _save_transcript(exc.session, repo, parcial=True)
            console.print(
                f"[dim]{len(exc.session.turns)} turno(s) completados guardados en "
                f"{parcial} (nada se perdió).[/dim]"
            )
        return 1
    finally:
        _detener_latido()

    # Avanzar la rotación solo si se usó y el debate terminó.
    if auto_rotate:
        rotation.save(repo, state.advanced())

    estado = (
        f"convergieron en la ronda {session.converged_round}"
        if session.converged
        else f"sin convergencia tras {session.rounds_run} ronda(s)"
    )
    console.rule(f"[bold green]Síntesis (por {session.synthesizer}) · {estado}")
    console.print(Markdown(session.synthesis))

    # Resumen de uso y costo (§13): totales por agente + global, del transcript.
    if session.usage_totals:
        partes = []
        for nombre, u in session.usage_totals.items():
            costo = f", ${u.cost_usd:.4f}" if u.cost_usd is not None else ", costo desconocido"
            partes.append(f"{nombre}: {u.input_tokens} entrada / {u.output_tokens} salida{costo}")
        console.print(f"[dim]Uso · {' · '.join(partes)}[/dim]")

    path = _save_transcript(session, repo)
    console.print(f"\n[dim]Transcript guardado en {path}[/dim]")
    console.print(f"[dim]Reporte navegable: devvating reporte {path}[/dim]")
    console.print("\n[bold]Vocero:[/bold] revisa la síntesis y decide el camino a seguir.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

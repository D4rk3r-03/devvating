"""Debate completo (N rondas) desde la consola.

Uso:
    devvating debate "tu tema"  [--files "a.py, b.py"] [--repo .]
        [--rounds N] [--synthesizer claude|gemini|auto]
        [--profundo] [--interactivo]

Los defaults (rondas, modo profundo, repo, files, rotación) se toman de
`.devvating.json` si existe; los flags CLI tienen prioridad. Con
`--synthesizer auto` (por defecto) el sintetizador rota entre debates (D3).
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

from . import rotation
from .adapters.base import AgentAdapter
from .adapters.claude import ClaudeAdapter
from .adapters.cli import ClaudeCliAdapter, GeminiCliAdapter
from .adapters.gemini import GeminiAdapter
from .appconfig import ProjectConfig
from .config import Config
from .orchestrator import DebateSession, DebateTopic, Orchestrator

_COLORS = {"claude": "cyan", "gemini": "magenta"}


def _save_transcript(session: DebateSession, repo_root: str) -> Path:
    out_dir = Path(repo_root) / "transcripts"
    out_dir.mkdir(exist_ok=True)
    slug = "-".join(session.topic.prompt.lower().split()[:6]) or "debate"
    slug = "".join(c for c in slug if c.isalnum() or c == "-")[:60]
    path = out_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}.json"
    path.write_text(
        json.dumps(asdict(session), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def make_agent(provider: str, backend: str, cfg: Config, repo: str) -> AgentAdapter:
    """Fábrica de agentes (D5): API con SDK o CLI headless, por proveedor.

    Solo exige la clave API cuando el backend realmente la usa.
    """
    if provider == "claude":
        if backend == "cli":
            return ClaudeCliAdapter(cwd=repo)
        cfg.require_anthropic()
        return ClaudeAdapter(cfg.anthropic_api_key, cfg.claude_model, cfg.max_tool_iterations)
    if backend == "cli":
        return GeminiCliAdapter(cwd=repo)
    cfg.require_gemini()
    return GeminiAdapter(cfg.gemini_api_key, cfg.gemini_model, cfg.max_tool_iterations)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="devvating debate", description="Debate multi-agente.")
    parser.add_argument("tema", help="El tema a debatir (problema, mejora, decisión).")
    parser.add_argument("--files", default=None, help="Pista de archivos relevantes.")
    parser.add_argument("--repo", default=None, help="Raíz del repositorio.")
    parser.add_argument("--rounds", type=int, default=None, help="Tope de rondas de réplica.")
    parser.add_argument(
        "--synthesizer",
        choices=["claude", "gemini", "auto"],
        default="auto",
        help="Quién sintetiza. 'auto' rota entre debates.",
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

    # Resolución del sintetizador (rotación automática, D3 capa 1).
    auto_rotate = args.synthesizer == "auto" and pc.auto_rotate
    if auto_rotate:
        state = rotation.load(repo)
        synth_index = state.synthesizer_index()
    elif args.synthesizer == "gemini":
        synth_index = 1
    else:  # claude, o auto con rotación desactivada
        synth_index = 0

    claude = make_agent("claude", claude_backend, cfg, repo)
    gemini = make_agent("gemini", gemini_backend, cfg, repo)

    # Heartbeat (M6a): spinner con agente, etapa y cronómetro durante cada
    # turno, usando los pares *_inicio/*_fin que el orquestador ya emite.
    latido: dict = {"activo": None}

    def _detener_latido() -> None:
        if latido["activo"] is not None:
            latido["activo"].stop()
            latido["activo"] = None

    def _iniciar_latido(agente: str, fase: str) -> None:
        _detener_latido()
        color = _COLORS.get(agente, "white")
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
        color = _COLORS.get(agente, "white")
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

    orch = Orchestrator(claude, gemini, repo_root=repo, on_event=on_event)
    topic = DebateTopic(prompt=args.tema, context_hint=files)

    console.rule("[bold]DEVVATING · Debate")
    console.print(f"[bold]Tema:[/bold] {args.tema}")
    console.print(
        f"[dim]rondas≤{rounds} · profundo={deep} · "
        f"sintetizador={'auto' if auto_rotate else args.synthesizer} · "
        f"backends: claude={claude_backend}, gemini={gemini_backend}[/dim]\n"
    )

    try:
        session = orch.run(
            topic,
            max_rounds=rounds,
            synthesizer_index=synth_index,
            deep_mode=deep,
            on_intervention=on_intervention,
        )
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

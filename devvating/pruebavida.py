"""M0 — Prueba de vida.

Verifica que Claude y Gemini responden y que el bucle de herramientas
funciona: se les pide describir un archivo del repo, que solo pueden conocer
leyéndolo (read_file por API, o las herramientas de lectura del CLI en
backend cli). Ver DISENO.md, hito M0 y decisión D5.

Uso:
    devvating pruebavida [archivo] [--claude-backend api|cli] [--gemini-backend api|cli]

El backend api requiere la clave correspondiente en el entorno o en .env;
el backend cli requiere el CLI del agente instalado y con sesión iniciada.
"""

from __future__ import annotations

import argparse

from rich.console import Console

from .appconfig import ProjectConfig
from .config import Config
from .tools.readonly import make_read_file
from .tools.registry import ToolRegistry

SYSTEM = (
    "Eres un agente en una sala de debate sobre código. Dispones de la "
    "herramienta read_file para leer archivos del repositorio. Responde en "
    "español, de forma breve."
)


def build_registry(cfg: Config) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(make_read_file(cfg.repo_root))
    return registry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="devvating pruebavida", description="Prueba de vida de los adaptadores."
    )
    parser.add_argument("archivo", nargs="?", default="DISENO.md")
    parser.add_argument("--claude-backend", choices=["api", "cli"], default=None)
    parser.add_argument("--gemini-backend", choices=["api", "cli"], default=None)
    args = parser.parse_args(argv)

    console = Console()
    cfg = Config.from_env()
    pc = ProjectConfig.load(".")
    claude_backend = args.claude_backend or pc.claude_backend
    gemini_backend = args.gemini_backend or pc.gemini_backend

    prompt = (
        f"Lee el archivo '{args.archivo}' del repositorio y dime en 2 o 3 frases "
        f"de qué trata. Debes leerlo con tu herramienta de lectura de archivos."
    )

    registry = build_registry(cfg)

    # Import aquí para evitar coste al ayudar (--help) y mantener el módulo liviano.
    from .debate import make_agent

    for provider, backend in (("claude", claude_backend), ("gemini", gemini_backend)):
        console.rule(f"[bold]{provider} · backend {backend}")
        try:
            adapter = make_agent(provider, backend, cfg, cfg.repo_root)
            answer = adapter.converse(SYSTEM, prompt, registry)
            console.print(answer)
        except Exception as exc:  # noqa: BLE001 — prueba de vida: mostrar el fallo.
            console.print(f"[red]Fallo con {provider} ({backend}): {exc}[/red]")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""M0 — Prueba de vida.

Verifica que Claude y Gemini responden y que el bucle de herramientas
funciona: se les pide describir un archivo del repo, que solo pueden conocer
leyéndolo (read_file por API, o las herramientas de lectura del CLI en
backend cli). Ver DISENO.md, hito M0 y decisión D5.

Uso:
    devvating pruebavida [archivo] [--claude-backend api|cli] [--gemini-backend api|cli]
    devvating pruebavida --agentes antigravity,kimi   # agentes puntuales del roster
    devvating pruebavida --roster-cli                 # smoke test: TODOS los adaptadores CLI

El backend api requiere la clave correspondiente en el entorno o en .env;
el backend cli requiere el CLI del agente instalado y con sesión iniciada.

`--roster-cli` es el smoke test documentado contra roturas de flags/formatos
de los CLIs de terceros (claude-cli, gemini-cli, antigravity, kimi): cada uno
trae su propio binario que puede cambiar de versión sin aviso.
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


def _elegir_objetivos(
    args: argparse.Namespace, roster_nombres: list[str], claude_backend: str, gemini_backend: str
) -> list[str]:
    """Resuelve qué agentes probar: --agentes > --roster-cli > par por defecto."""
    if args.agentes:
        return [n.strip() for n in args.agentes.split(",") if n.strip()]
    if args.roster_cli:
        return [n for n in roster_nombres if not n.endswith("-api")]
    return [f"claude-{claude_backend}", f"gemini-{gemini_backend}"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="devvating pruebavida", description="Prueba de vida de los adaptadores."
    )
    parser.add_argument("archivo", nargs="?", default="DISENO.md")
    parser.add_argument("--claude-backend", choices=["api", "cli"], default=None)
    parser.add_argument("--gemini-backend", choices=["api", "cli"], default=None)
    parser.add_argument(
        "--agentes", default=None,
        help="Agentes del roster a probar, separados por coma (D7). Ej: antigravity,kimi",
    )
    parser.add_argument(
        "--roster-cli", action="store_true",
        help="Prueba TODOS los adaptadores CLI del roster (claude-cli, gemini-cli, "
             "antigravity, kimi): smoke test documentado contra roturas de flags/"
             "formatos de los CLIs de terceros. Ignora --agentes y los --*-backend.",
    )
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
    from . import agentes as banco

    objetivos = _elegir_objetivos(args, banco.nombres(), claude_backend, gemini_backend)

    for nombre in objetivos:
        console.rule(f"[bold]{nombre}")
        try:
            adapter = banco.crear(nombre, cfg, cfg.repo_root)
            answer = adapter.converse(SYSTEM, prompt, registry)
            console.print(answer)
        except Exception as exc:  # noqa: BLE001 — prueba de vida: mostrar el fallo.
            console.print(f"[red]Fallo con {nombre}: {exc}[/red]")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Configuración del sistema: claves de API, modelos y límites.

Carga desde variables de entorno (y desde un archivo .env si existe).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    anthropic_api_key: str
    gemini_api_key: str
    claude_model: str = "claude-opus-4-8"
    # gemini-2.5-pro no tiene cuota en el tier gratuito (límite 0) y las
    # cuentas nuevas ya no acceden a 2.5-flash; 3.5-flash sí responde.
    gemini_model: str = "gemini-3.5-flash"
    # Raíz del repositorio sobre la que operan las herramientas de lectura.
    repo_root: str = "."
    # Tope de iteraciones del bucle de tool use (corta divagación y gasto).
    max_tool_iterations: int = 8

    @classmethod
    def from_env(cls) -> "Config":
        # Carga .env si python-dotenv está disponible; es opcional.
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:
            pass

        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
        gemini_key = os.environ.get("GEMINI_API_KEY", "")

        return cls(
            anthropic_api_key=anthropic_key,
            gemini_api_key=gemini_key,
            claude_model=os.environ.get("DEVVATING_CLAUDE_MODEL", "claude-opus-4-8"),
            gemini_model=os.environ.get("DEVVATING_GEMINI_MODEL", "gemini-3.5-flash"),
            repo_root=os.environ.get("DEVVATING_REPO_ROOT", "."),
            max_tool_iterations=int(
                os.environ.get("DEVVATING_MAX_TOOL_ITERATIONS", "8")
            ),
        )

    def require_anthropic(self) -> None:
        if not self.anthropic_api_key:
            raise RuntimeError(
                "Falta ANTHROPIC_API_KEY. Copia .env.example a .env y rellénala."
            )

    def require_gemini(self) -> None:
        if not self.gemini_api_key:
            raise RuntimeError(
                "Falta GEMINI_API_KEY. Copia .env.example a .env y rellénala."
            )

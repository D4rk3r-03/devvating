"""Registro de herramientas con niveles de permiso.

El modelo NO toca el disco: en cada turno pide una llamada a herramienta y este
registro la ejecuta localmente (Tool Runtime). Ver DISENO.md secciones 4.1 y 4.3.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable


class Permission(Enum):
    """Nivel de permiso de una herramienta.

    En la fase de DEBATE solo se exponen herramientas READONLY. Las WRITE se
    reservan a la fase de EJECUCIÓN, y solo tras aprobación del vocero.
    """

    READONLY = "readonly"
    WRITE = "write"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    # JSON Schema del input (subconjunto compatible con Claude y Gemini).
    input_schema: dict
    permission: Permission
    # Ejecuta la herramienta con el input ya parseado y devuelve texto.
    handler: Callable[[dict], str]


class ToolRegistry:
    """Contenedor de herramientas disponibles para un turno del agente."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"Herramienta duplicada: {spec.name}")
        self._tools[spec.name] = spec

    def specs(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def execute(self, name: str, tool_input: dict) -> str:
        spec = self._tools.get(name)
        if spec is None:
            return f"Error: herramienta desconocida '{name}'."
        try:
            return spec.handler(tool_input)
        except Exception as exc:  # noqa: BLE001 — se reporta al modelo, no se propaga.
            return f"Error ejecutando '{name}': {exc}"

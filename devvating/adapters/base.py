"""Interfaz común de los adaptadores de agente.

`converse`: dado un system prompt, un mensaje del usuario y un registro de
herramientas, el adaptador ejecuta el turno completo y devuelve el texto final.

`TurnUsage` (plan del debate 2026-07-12, DISENO.md §13): métricas del ÚLTIMO
turno, expuestas en el accessor `last_usage` — nunca acumuladas en el
adaptador, para no perder granularidad por turno. La firma de `converse` no
cambia; el orquestador copia `last_usage` a cada `Turn` tras la llamada.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..tools.registry import ToolRegistry


@dataclass
class TurnUsage:
    """Tokens y costo de un turno. `cost_usd` es None si no hay tarifa conocida."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float | None = None

    def __add__(self, other: "TurnUsage") -> "TurnUsage":
        if self.cost_usd is None and other.cost_usd is None:
            costo = None
        else:
            costo = (self.cost_usd or 0.0) + (other.cost_usd or 0.0)
        return TurnUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
            cost_usd=costo,
        )


@runtime_checkable
class AgentAdapter(Protocol):
    name: str
    # Uso del último turno; None si el backend no reporta métricas.
    last_usage: TurnUsage | None

    def converse(self, system: str, prompt: str, registry: ToolRegistry) -> str:
        """Ejecuta un turno con tool use y devuelve la respuesta final en texto."""
        ...

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


# --- Taxonomía de fallos (plan del debate de resiliencia, 2026-07-13) --------
# Los adaptadores CLASIFICAN lanzando estos tipos; el REINTENTO vive en el
# orquestador. Todo lo que no se pueda clasificar queda como AgentError plano
# (no reintentable por defecto).


class AgentError(RuntimeError):
    """Fallo de un agente. Base de la taxonomía; no reintentable por defecto."""


class TransientProviderError(AgentError):
    """Fallo transitorio del proveedor (503/429 momentáneo): vale reintentar."""


class SessionLimitError(AgentError):
    """Cuota por ventana de tiempo agotada: no se cura con backoff corto.

    `resets_at` lleva la hora de reinicio si el proveedor la reportó.
    """

    def __init__(self, mensaje: str, resets_at: str | None = None) -> None:
        super().__init__(mensaje)
        self.resets_at = resets_at


class AgentCancelledError(AgentError):
    """El vocero canceló el debate mientras el turno estaba en vuelo.

    No es un fallo: el adaptador mató su propio subprocess al ver la señal de
    cancelación. No reintentable; el orquestador lo convierte en corte limpio
    con transcript parcial (reanudable).
    """


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

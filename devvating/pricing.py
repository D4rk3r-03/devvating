"""Tabla de precios por modelo, FUERA de los adaptadores (plan §13, D. vocero).

Solo aplica al camino API: el backend CLI ya reporta su costo calculado.
Cuando el modelo no tiene tarifa conocida se devuelve None — el costo queda
como "desconocido" en el transcript en vez de inventar una estimación.

Precios en USD por millón de tokens (input, output), vigentes 2026-06.
El caché pondera sobre la tarifa de input: lectura 0.1x, escritura 1.25x.
Actualizar aquí cuando el proveedor cambie precios; los adaptadores no se tocan.
"""

from __future__ import annotations

from .adapters.base import TurnUsage

_PRECIOS_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-fable-5": (10.00, 50.00),
    # Gemini: sin tarifa registrada (tier gratuito / precios no confirmados)
    # → estimate_cost devuelve None y el costo queda como desconocido.
}

_CACHE_READ_FACTOR = 0.1
_CACHE_WRITE_FACTOR = 1.25


def _tarifa(model: str) -> tuple[float, float] | None:
    if model in _PRECIOS_MTOK:
        return _PRECIOS_MTOK[model]
    # Tolerar IDs con sufijo de fecha (p. ej. claude-haiku-4-5-20251001).
    for alias, tarifa in _PRECIOS_MTOK.items():
        if model.startswith(alias):
            return tarifa
    return None


def estimate_cost(model: str, usage: TurnUsage) -> float | None:
    """Costo estimado en USD del turno, o None si el modelo no tiene tarifa."""
    tarifa = _tarifa(model)
    if tarifa is None:
        return None
    entrada, salida = tarifa
    return (
        usage.input_tokens * entrada
        + usage.cache_read_tokens * entrada * _CACHE_READ_FACTOR
        + usage.cache_creation_tokens * entrada * _CACHE_WRITE_FACTOR
        + usage.output_tokens * salida
    ) / 1_000_000

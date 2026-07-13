"""Adaptador de Claude (SDK oficial `anthropic`) con bucle de tool use manual.

El modelo razona en la nube y, cuando pide una herramienta, el Tool Runtime
local la ejecuta y le devuelve el resultado. Ver DISENO.md 4.1.
"""

from __future__ import annotations

from dataclasses import replace

import anthropic

from ..tools.registry import ToolRegistry
from .base import TurnUsage


def _usage_de_respuesta(u) -> TurnUsage:
    return TurnUsage(
        input_tokens=u.input_tokens or 0,
        output_tokens=u.output_tokens or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_creation_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
    )


class ClaudeAdapter:
    def __init__(self, api_key: str, model: str, max_iterations: int = 8) -> None:
        self.name = "claude"
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_iterations = max_iterations
        self.last_usage: TurnUsage | None = None

    def _tools_payload(self, registry: ToolRegistry) -> list[dict]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "input_schema": spec.input_schema,
            }
            for spec in registry.specs()
        ]

    def _cerrar_turno(self, total: TurnUsage) -> None:
        # El costo se calcula con la tabla externa (pricing.py, plan §13).
        from .. import pricing

        self.last_usage = replace(total, cost_usd=pricing.estimate_cost(self._model, total))

    def converse(self, system: str, prompt: str, registry: ToolRegistry) -> str:
        tools = self._tools_payload(registry)
        messages: list[dict] = [{"role": "user", "content": prompt}]
        self.last_usage = None
        total = TurnUsage()

        for _ in range(self._max_iterations):
            response = self._client.messages.create(
                model=self._model,
                max_tokens=8000,
                thinking={"type": "adaptive"},
                system=system,
                tools=tools,
                messages=messages,
            )
            total = total + _usage_de_respuesta(response.usage)

            # Ejecutar cualquier herramienta que Claude haya pedido.
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = registry.execute(block.name, dict(block.input))
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        }
                    )

            if response.stop_reason != "tool_use":
                # Turno terminado: devolver el texto final.
                self._cerrar_turno(total)
                return "".join(
                    b.text for b in response.content if b.type == "text"
                ).strip()

            # Continuar el bucle: eco de la respuesta + resultados de herramientas.
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

        self._cerrar_turno(total)
        return "[Claude alcanzó el tope de iteraciones de herramientas.]"

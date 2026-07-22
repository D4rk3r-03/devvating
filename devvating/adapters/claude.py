"""Adaptador de Claude (SDK oficial `anthropic`) con bucle de tool use manual.

El modelo razona en la nube y, cuando pide una herramienta, el Tool Runtime
local la ejecuta y le devuelve el resultado. Ver DISENO.md 4.1.
"""

from __future__ import annotations

from dataclasses import replace

import anthropic

from ..tools.registry import ToolRegistry
from .base import TransientProviderError, TurnUsage


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

        # Prefijo estable = tools + system (orden de render tools → system →
        # messages). Un breakpoint de caché en el último bloque system cachea
        # AMBOS juntos: se reescribe una vez y se relee a ~0.1x en cada
        # iteración del bucle de herramientas y en cada turno con el mismo rol.
        # Ojo (cautela del debate): si el prefijo no supera el mínimo cacheable
        # del modelo (~4096 tokens en Opus 4.8) no cachea en silencio; se
        # verifica con cache_read_tokens en TurnUsage, ya instrumentado.
        system_cacheado = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]

        # Breakpoint MÓVIL sobre el historial: donde el prefijo estable (system
        # ~500 tokens) casi nunca supera el mínimo cacheable, los tool_results
        # sí — traen contenidos de archivos (read_file) que se reenvían en cada
        # iteración del bucle. Marcamos el tool_results más reciente para que la
        # siguiente iteración relea todo el prefijo (system+tools+mensajes) a
        # ~0.1x, y quitamos la marca del anterior para no pasar del tope de 4
        # breakpoints. Total vivo: 1 (system) + 1 (este) = 2.
        cache_previo: dict | None = None

        for _ in range(self._max_iterations):
            try:
                response = self._client.messages.create(
                    model=self._model,
                    max_tokens=8000,
                    thinking={"type": "adaptive"},
                    system=system_cacheado,
                    tools=tools,
                    messages=messages,
                )
            except (
                anthropic.RateLimitError,
                anthropic.InternalServerError,
                anthropic.APIConnectionError,
            ) as exc:
                # 429/5xx/red: transitorio — el orquestador decide reintentar.
                raise TransientProviderError(f"API de Claude: {exc}") from exc
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
                texto = "".join(
                    b.text for b in response.content if b.type == "text"
                ).strip()
                if not texto:
                    # Turno sin texto (p. ej. solo bloques de thinking, o corte
                    # por max_tokens antes de emitir nada): no es una postura.
                    # Se trata como transitorio porque reintentar suele curarlo.
                    raise TransientProviderError(
                        "API de Claude: turno sin texto "
                        f"(stop_reason={response.stop_reason})."
                    )
                self._cerrar_turno(total)
                return texto

            # Continuar el bucle: eco de la respuesta + resultados de herramientas.
            messages.append({"role": "assistant", "content": response.content})
            if tool_results:
                if cache_previo is not None:
                    cache_previo.pop("cache_control", None)
                tool_results[-1]["cache_control"] = {"type": "ephemeral"}
                cache_previo = tool_results[-1]
            messages.append({"role": "user", "content": tool_results})

        self._cerrar_turno(total)
        return "[Claude alcanzó el tope de iteraciones de herramientas.]"

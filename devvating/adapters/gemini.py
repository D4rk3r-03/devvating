"""Adaptador de Gemini (SDK `google-genai`) con function calling manual.

Simétrico al de Claude: el modelo pide funciones y el Tool Runtime local las
ejecuta. Mantiene el control explícito del bucle (no usa la ejecución
automática del SDK) para poder auditar y limitar las llamadas.
"""

from __future__ import annotations

from dataclasses import replace

from google import genai
from google.genai import types

from ..tools.registry import ToolRegistry
from .base import TurnUsage


def _usage_de_respuesta(response) -> TurnUsage:
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return TurnUsage()
    # thoughts_token_count: tokens de razonamiento en modelos con thinking;
    # cuentan como salida facturada.
    return TurnUsage(
        input_tokens=meta.prompt_token_count or 0,
        output_tokens=(meta.candidates_token_count or 0)
        + (getattr(meta, "thoughts_token_count", 0) or 0),
        cache_read_tokens=getattr(meta, "cached_content_token_count", 0) or 0,
    )


class GeminiAdapter:
    def __init__(self, api_key: str, model: str, max_iterations: int = 8) -> None:
        self.name = "gemini"
        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._max_iterations = max_iterations
        self.last_usage: TurnUsage | None = None

    def _tools_payload(self, registry: ToolRegistry) -> list[types.Tool]:
        declarations = [
            types.FunctionDeclaration(
                name=spec.name,
                description=spec.description,
                parameters=spec.input_schema,
            )
            for spec in registry.specs()
        ]
        return [types.Tool(function_declarations=declarations)]

    def _cerrar_turno(self, total: TurnUsage) -> None:
        from .. import pricing

        self.last_usage = replace(total, cost_usd=pricing.estimate_cost(self._model, total))

    def converse(self, system: str, prompt: str, registry: ToolRegistry) -> str:
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=self._tools_payload(registry),
        )
        contents: list[types.Content] = [
            types.Content(role="user", parts=[types.Part(text=prompt)])
        ]
        self.last_usage = None
        total = TurnUsage()

        for _ in range(self._max_iterations):
            response = self._client.models.generate_content(
                model=self._model,
                contents=contents,
                config=config,
            )
            total = total + _usage_de_respuesta(response)
            candidate = response.candidates[0]
            parts = candidate.content.parts or []

            fn_calls = [p.function_call for p in parts if p.function_call]
            if not fn_calls:
                self._cerrar_turno(total)
                return (response.text or "").strip()

            # Eco del turno del modelo, luego los resultados de las funciones.
            contents.append(candidate.content)
            result_parts = []
            for fc in fn_calls:
                result = registry.execute(fc.name, dict(fc.args or {}))
                result_parts.append(
                    types.Part.from_function_response(
                        name=fc.name, response={"result": result}
                    )
                )
            contents.append(types.Content(role="user", parts=result_parts))

        self._cerrar_turno(total)
        return "[Gemini alcanzó el tope de iteraciones de herramientas.]"

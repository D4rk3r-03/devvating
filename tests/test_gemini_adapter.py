"""Adaptador Gemini (camino API): las respuestas malformadas del SDK quedan
CLASIFICADAS según la taxonomía de base.py, nunca escapan como
TypeError/AttributeError sin clasificar (que tiraban el debate sin transcript
parcial). Sin red: se inyecta un cliente falso, como en test_claude_adapter.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from devvating.adapters.base import AgentError, TransientProviderError
from devvating.adapters.gemini import GeminiAdapter
from devvating.tools.registry import ToolRegistry

REG = ToolRegistry()


def _adapter_con(*respuestas) -> GeminiAdapter:
    adapter = GeminiAdapter(api_key="k", model="gemini-x")
    cola = list(respuestas)

    def generate_content(**kwargs):
        r = cola.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    adapter._client = SimpleNamespace(
        models=SimpleNamespace(generate_content=generate_content)
    )
    return adapter


def _parte_texto(texto: str) -> SimpleNamespace:
    return SimpleNamespace(text=texto, function_call=None)


def _respuesta_texto(texto: str) -> SimpleNamespace:
    candidate = SimpleNamespace(
        content=SimpleNamespace(parts=[_parte_texto(texto)]), finish_reason="STOP"
    )
    return SimpleNamespace(candidates=[candidate], usage_metadata=None)


class TestRespuestasMalformadas:
    def test_texto_normal_sigue_funcionando(self):
        adapter = _adapter_con(_respuesta_texto("postura"))
        assert adapter.converse("ROL", "p", REG) == "postura"

    def test_sin_candidatos_es_transitorio(self):
        # La API devuelve candidates=None cuando bloquea el prompt; indexar a
        # ciegas era un TypeError sin clasificar.
        respuesta = SimpleNamespace(candidates=None, usage_metadata=None)
        adapter = _adapter_con(respuesta)
        with pytest.raises(TransientProviderError, match="sin candidatos"):
            adapter.converse("ROL", "p", REG)

    def test_candidatos_vacios_es_transitorio(self):
        respuesta = SimpleNamespace(candidates=[], usage_metadata=None)
        adapter = _adapter_con(respuesta)
        with pytest.raises(TransientProviderError):
            adapter.converse("ROL", "p", REG)

    def test_content_none_es_transitorio_y_reporta_finish_reason(self):
        # SAFETY / MALFORMED_FUNCTION_CALL dejan content=None; era un
        # AttributeError sin clasificar (visto al sintetizar prompts grandes).
        candidate = SimpleNamespace(content=None, finish_reason="MALFORMED_FUNCTION_CALL")
        respuesta = SimpleNamespace(candidates=[candidate], usage_metadata=None)
        adapter = _adapter_con(respuesta)
        with pytest.raises(TransientProviderError, match="MALFORMED_FUNCTION_CALL"):
            adapter.converse("ROL", "p", REG)

    def test_turno_sin_texto_ni_herramientas_es_transitorio(self):
        candidate = SimpleNamespace(
            content=SimpleNamespace(parts=[]), finish_reason="STOP"
        )
        respuesta = SimpleNamespace(candidates=[candidate], usage_metadata=None)
        adapter = _adapter_con(respuesta)
        with pytest.raises(TransientProviderError, match="sin texto"):
            adapter.converse("ROL", "p", REG)


class TestClasificacionAPIError:
    def _api_error(self, codigo: int):
        from google.genai import errors as genai_errors

        exc = genai_errors.APIError.__new__(genai_errors.APIError)
        exc.code = codigo
        return exc

    def test_5xx_es_transitorio(self):
        adapter = _adapter_con(self._api_error(503))
        with pytest.raises(TransientProviderError):
            adapter.converse("ROL", "p", REG)

    def test_4xx_no_transitorio_queda_como_agent_error(self):
        # Protocolo 3b: todo fallo sin clasificar debe ser AgentError (no
        # reintentable), nunca la excepción cruda del SDK.
        adapter = _adapter_con(self._api_error(400))
        with pytest.raises(AgentError) as info:
            adapter.converse("ROL", "p", REG)
        assert not isinstance(info.value, TransientProviderError)

"""Adaptador Claude (camino API): prompt caching del prefijo tools+system.

El modelo nunca toca disco; aquí solo verificamos que la petición se arma
bien (cache_control en el prefijo estable) y que las métricas de caché de la
respuesta caen en TurnUsage. Sin red: se inyecta un cliente falso.
"""

from __future__ import annotations

from types import SimpleNamespace

from devvating.adapters.claude import ClaudeAdapter
from devvating.tools.registry import ToolRegistry


def _usage():
    return SimpleNamespace(
        input_tokens=10, output_tokens=5,
        cache_read_input_tokens=1234, cache_creation_input_tokens=42,
    )


class _FakeMessages:
    def __init__(self, respuestas):
        self._respuestas = list(respuestas)
        self.llamadas: list[dict] = []

    def create(self, **kwargs):
        # Copia superficial de messages: el adaptador muta cache_control tras
        # cada create, así que hay que fotografiar el estado de ESTA llamada.
        import copy
        self.llamadas.append({**kwargs, "messages": copy.deepcopy(kwargs["messages"])})
        return self._respuestas.pop(0)


def _respuesta_final(texto: str):
    bloque = SimpleNamespace(type="text", text=texto)
    return SimpleNamespace(content=[bloque], stop_reason="end_turn", usage=_usage())


def _respuesta_tool(nombre: str, tid: str):
    bloque = SimpleNamespace(type="tool_use", name=nombre, id=tid, input={"path": "a.py"})
    return SimpleNamespace(content=[bloque], stop_reason="tool_use", usage=_usage())


def _adapter_con(*respuestas) -> tuple[ClaudeAdapter, _FakeMessages]:
    adapter = ClaudeAdapter(api_key="k", model="claude-opus-4-8")
    fake = _FakeMessages(respuestas)
    adapter._client = SimpleNamespace(messages=fake)
    return adapter, fake


class TestPromptCaching:
    def test_system_lleva_cache_control(self):
        adapter, fake = _adapter_con(_respuesta_final("postura"))
        out = adapter.converse("ROL DEL AGENTE", "prompt", ToolRegistry())
        assert out == "postura"
        # El system se envía como bloque estructurado con breakpoint de caché,
        # que cachea tools+system juntos (orden de render tools → system).
        system = fake.llamadas[0]["system"]
        assert isinstance(system, list) and len(system) == 1
        assert system[0]["text"] == "ROL DEL AGENTE"
        assert system[0]["cache_control"] == {"type": "ephemeral"}

    def test_metricas_de_cache_llegan_a_turnusage(self):
        adapter, _ = _adapter_con(_respuesta_final("x"))
        adapter.converse("ROL", "p", ToolRegistry())
        u = adapter.last_usage
        assert u is not None
        assert u.cache_read_tokens == 1234 and u.cache_creation_tokens == 42

    def test_breakpoint_movil_sobre_tool_results(self):
        # Dos rondas de herramienta y luego cierre: el tool_results de cada
        # ronda debe llevar cache_control en la llamada que lo reenvía, y solo
        # el MÁS RECIENTE — la marca del anterior se retira (tope de 4).
        adapter, fake = _adapter_con(
            _respuesta_tool("read_file", "t1"),
            _respuesta_tool("read_file", "t2"),
            _respuesta_final("veredicto"),
        )
        out = adapter.converse("ROL", "prompt", ToolRegistry())
        assert out == "veredicto"

        def tool_results_de(msgs):
            return [m["content"] for m in msgs
                    if m["role"] == "user" and isinstance(m["content"], list)]

        # 2ª llamada: reenvía el 1er tool_results, que lleva el breakpoint.
        tr2 = tool_results_de(fake.llamadas[1]["messages"])
        assert tr2[-1][-1].get("cache_control") == {"type": "ephemeral"}

        # 3ª llamada: el breakpoint se movió al 2º; el 1º ya no lo tiene.
        tr3 = tool_results_de(fake.llamadas[2]["messages"])
        assert "cache_control" not in tr3[0][-1]
        assert tr3[1][-1].get("cache_control") == {"type": "ephemeral"}


class TestTurnoSinTexto:
    def test_respuesta_sin_texto_es_transitoria(self):
        # Un turno que termina sin texto (solo thinking, o corte antes de
        # emitir) no es una postura: aceptarlo metía a un agente mudo en el
        # debate. Mismo criterio que los adaptadores CLI.
        from devvating.adapters.base import TransientProviderError
        import pytest

        vacio = SimpleNamespace(content=[], stop_reason="end_turn", usage=_usage())
        adapter, _ = _adapter_con(vacio)
        with pytest.raises(TransientProviderError, match="sin texto"):
            adapter.converse("ROL", "p", ToolRegistry())

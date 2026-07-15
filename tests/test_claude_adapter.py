"""Adaptador Claude (camino API): prompt caching del prefijo tools+system.

El modelo nunca toca disco; aquí solo verificamos que la petición se arma
bien (cache_control en el prefijo estable) y que las métricas de caché de la
respuesta caen en TurnUsage. Sin red: se inyecta un cliente falso.
"""

from __future__ import annotations

from types import SimpleNamespace

from devvating.adapters.claude import ClaudeAdapter
from devvating.tools.registry import ToolRegistry


class _FakeMessages:
    def __init__(self, respuesta):
        self._respuesta = respuesta
        self.llamadas: list[dict] = []

    def create(self, **kwargs):
        self.llamadas.append(kwargs)
        return self._respuesta


def _respuesta_final(texto: str):
    usage = SimpleNamespace(
        input_tokens=10, output_tokens=5,
        cache_read_input_tokens=1234, cache_creation_input_tokens=42,
    )
    bloque = SimpleNamespace(type="text", text=texto)
    return SimpleNamespace(content=[bloque], stop_reason="end_turn", usage=usage)


def _adapter_con(respuesta) -> tuple[ClaudeAdapter, _FakeMessages]:
    adapter = ClaudeAdapter(api_key="k", model="claude-opus-4-8")
    fake = _FakeMessages(respuesta)
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

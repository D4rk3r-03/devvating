"""Contador de tokens y costos (§13): TurnUsage, pricing y totalización."""

from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from devvating import pricing
from devvating.adapters.base import TurnUsage
from devvating.orchestrator import DebateTopic, Orchestrator
from tests.conftest import StubAdapter


class TestTurnUsage:
    def test_suma_campo_a_campo(self):
        a = TurnUsage(100, 20, 5, 3, 0.5)
        b = TurnUsage(50, 10, 1, 2, 0.25)
        c = a + b
        assert (c.input_tokens, c.output_tokens) == (150, 30)
        assert (c.cache_read_tokens, c.cache_creation_tokens) == (6, 5)
        assert c.cost_usd == pytest.approx(0.75)

    def test_costo_none_mas_none_sigue_siendo_none(self):
        assert (TurnUsage() + TurnUsage()).cost_usd is None

    def test_costo_conocido_mas_desconocido_conserva_el_conocido(self):
        c = TurnUsage(cost_usd=1.0) + TurnUsage(cost_usd=None)
        assert c.cost_usd == pytest.approx(1.0)


class TestPricing:
    def test_modelo_conocido_calcula_entrada_y_salida(self):
        # opus-4-8: $5 entrada / $25 salida por MTok.
        u = TurnUsage(input_tokens=1_000_000, output_tokens=1_000_000)
        assert pricing.estimate_cost("claude-opus-4-8", u) == pytest.approx(30.0)

    def test_cache_pondera_sobre_la_tarifa_de_entrada(self):
        # lectura 0.1x, escritura 1.25x sobre $5/MTok.
        u = TurnUsage(cache_read_tokens=1_000_000, cache_creation_tokens=1_000_000)
        assert pricing.estimate_cost("claude-opus-4-8", u) == pytest.approx(0.5 + 6.25)

    def test_sufijo_de_fecha_cae_al_alias(self):
        u = TurnUsage(input_tokens=1_000_000)
        assert pricing.estimate_cost("claude-haiku-4-5-20251001", u) == pytest.approx(1.0)

    def test_modelo_sin_tarifa_devuelve_none(self):
        u = TurnUsage(input_tokens=1_000_000)
        assert pricing.estimate_cost("gemini-3.5-flash", u) is None
        assert pricing.estimate_cost("modelo-inventado", u) is None


class TestTotalizacionEnDebate:
    def _run(self):
        # Apertura + 1 réplica + síntesis (a sintetiza): a=3 turnos, b=2.
        a = StubAdapter(
            "claude",
            ["A0", 'A1 {"convergencia": true}', "síntesis"],
            usages=[
                TurnUsage(100, 10, cost_usd=0.01),
                TurnUsage(200, 20, cost_usd=0.02),
                TurnUsage(300, 30, cost_usd=0.03),
            ],
        )
        b = StubAdapter(
            "gemini",
            ["B0", 'B1 {"convergencia": true}'],
            usages=[TurnUsage(50, 5), TurnUsage(60, 6)],  # sin costo (tarifa desconocida)
        )
        orch = Orchestrator(a, b, repo_root=".")
        return orch.run(DebateTopic(prompt="tema"), max_rounds=1, synthesizer_index=0)

    def test_cada_turno_lleva_su_usage(self):
        s = self._run()
        assert all(t.usage is not None for t in s.turns)
        sintesis = next(t for t in s.turns if t.phase == "sintesis")
        assert sintesis.usage.input_tokens == 300

    def test_totales_por_agente_y_global(self):
        s = self._run()
        assert s.usage_totals["claude"].input_tokens == 600
        assert s.usage_totals["claude"].cost_usd == pytest.approx(0.06)
        assert s.usage_totals["gemini"].input_tokens == 110
        assert s.usage_totals["gemini"].cost_usd is None
        total = s.usage_totals["total"]
        assert total.input_tokens == 710 and total.output_tokens == 71
        # El total conserva el costo conocido aunque un agente no tenga tarifa.
        assert total.cost_usd == pytest.approx(0.06)

    def test_agentes_sin_metricas_no_generan_totales_fantasma(self):
        a = StubAdapter("claude", ["A0", 'A1 {"convergencia": true}', "s"])
        b = StubAdapter("gemini", ["B0", 'B1 {"convergencia": true}'])
        s = Orchestrator(a, b, repo_root=".").run(
            DebateTopic(prompt="t"), max_rounds=1
        )
        assert s.usage_totals == {}
        assert all(t.usage is None for t in s.turns)

    def test_usage_se_serializa_en_el_transcript(self):
        s = self._run()
        data = json.loads(json.dumps(asdict(s), ensure_ascii=False))
        assert data["usage_totals"]["total"]["input_tokens"] == 710
        assert data["turns"][0]["usage"]["input_tokens"] == 100

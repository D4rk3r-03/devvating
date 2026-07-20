"""Reanudación de debates (--resume): fast-forward de turnos ya pagados."""

from __future__ import annotations

import json

import pytest

from devvating.adapters.base import TurnUsage
from devvating.debate import _load_partial_session, _save_transcript
from devvating.orchestrator import DebateSession, DebateTopic, Orchestrator, Turn
from tests.conftest import StubAdapter

TOPIC = DebateTopic(prompt="¿tema reanudado?", context_hint="a.py")


def _parcial_hasta_ronda_1() -> DebateSession:
    """Sesión parcial: apertura completa + ronda 1 completa (sin convergencia)."""
    return DebateSession(
        topic=TOPIC,
        turns=[
            Turn(0, "propuesta", "claude", "A0-viejo"),
            Turn(0, "propuesta", "gemini", "B0-viejo"),
            Turn(1, "replica", "claude", "A1-viejo", "no"),
            Turn(1, "replica", "gemini", "B1-viejo", "no"),
        ],
        rounds_run=1,
    )


class TestResumeEnOrquestador:
    def test_no_repite_turnos_pagados_y_continua_donde_quedo(self):
        # Solo se programan los turnos que FALTAN: réplicas r2 + síntesis.
        a = StubAdapter("claude", ['A2 {"convergencia": true}', "síntesis"])
        b = StubAdapter("gemini", ['B2 {"convergencia": true}'])
        orch = Orchestrator(a, b, repo_root=".", sleep=lambda _: None)
        s = orch.run(TOPIC, max_rounds=2, old_session=_parcial_hasta_ronda_1())

        assert len(a.llamadas) == 2 and len(b.llamadas) == 1  # cero re-pagos
        assert s.converged and s.converged_round == 2
        assert s.synthesis == "síntesis"
        # Los turnos viejos están en la sesión nueva, en orden.
        assert [t.text for t in s.turns[:4]] == [
            "A0-viejo", "B0-viejo", "A1-viejo", "B1-viejo",
        ]

    def test_las_replicas_nuevas_ven_las_posturas_cacheadas(self):
        a = StubAdapter("claude", ['A2 {"convergencia": true}', "síntesis"])
        b = StubAdapter("gemini", ['B2 {"convergencia": true}'])
        orch = Orchestrator(a, b, repo_root=".", sleep=lambda _: None)
        orch.run(TOPIC, max_rounds=2, old_session=_parcial_hasta_ronda_1())
        # La primera llamada real de 'a' es su réplica de r2: debe traer la
        # postura r1 del rival cargada del parcial, no re-preguntada.
        assert "B1-viejo" in a.llamadas[0][1]

    def test_ronda_a_medias_solo_completa_al_agente_faltante(self):
        parcial = _parcial_hasta_ronda_1()
        parcial.turns.append(Turn(2, "replica", "claude", "A2-viejo", "si"))
        a = StubAdapter("claude", ["síntesis"])  # su r2 ya está pagada
        b = StubAdapter("gemini", ['B2 {"convergencia": true}'])
        orch = Orchestrator(a, b, repo_root=".", sleep=lambda _: None)
        s = orch.run(TOPIC, max_rounds=2, old_session=parcial)
        assert len(b.llamadas) == 1 and len(a.llamadas) == 1  # solo lo faltante
        assert s.converged  # el veredicto cacheado 'si' de a cuenta

    def test_sin_old_session_el_flujo_es_el_de_siempre(self):
        a = StubAdapter("claude", ["A0", 'A1 {"convergencia": true}', "síntesis"])
        b = StubAdapter("gemini", ["B0", 'B1 {"convergencia": true}'])
        s = Orchestrator(a, b, repo_root=".").run(TOPIC, max_rounds=1)
        assert len(a.llamadas) == 3 and s.converged


class TestCargaDelParcial:
    def test_roundtrip_guardar_y_cargar(self, tmp_path):
        s = _parcial_hasta_ronda_1()
        s.turns[0].usage = TurnUsage(input_tokens=10, output_tokens=5, cost_usd=0.01)
        path = _save_transcript(s, str(tmp_path), parcial=True)
        cargada = _load_partial_session(str(path))
        assert cargada.topic == TOPIC
        assert [t.text for t in cargada.turns] == [t.text for t in s.turns]
        assert cargada.turns[2].verdict == "no"
        assert cargada.turns[0].usage.cost_usd == pytest.approx(0.01)

    def test_json_invalido_revienta_con_error_de_datos(self, tmp_path):
        malo = tmp_path / "roto.partial.json"
        malo.write_text("{sin json", encoding="utf-8")
        with pytest.raises(ValueError):
            _load_partial_session(str(malo))


class TestValidacionCli:
    def test_sin_tema_ni_resume_es_error_de_uso(self, capsys):
        from devvating.debate import main

        with pytest.raises(SystemExit):
            main([])
        assert "resume" in capsys.readouterr().err.lower()

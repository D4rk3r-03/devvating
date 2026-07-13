"""Orquestador: apertura a ciegas, convergencia, intervención, inversión."""

from __future__ import annotations

from devvating.orchestrator import DebateTopic, Orchestrator, _parse_verdict
from tests.conftest import StubAdapter

TOPIC = DebateTopic(prompt="¿Refactor X?", context_hint="a.py")


class TestParseVerdict:
    def test_extrae_si_con_y_sin_acento(self):
        assert _parse_verdict("texto [CONVERGENCIA: SÍ]")[1] == "si"
        assert _parse_verdict("texto [convergencia: si]")[1] == "si"

    def test_extrae_no_y_limpia_el_texto(self):
        clean, verdict = _parse_verdict("postura firme\n[CONVERGENCIA: NO]")
        assert verdict == "no"
        assert "[CONVERGENCIA" not in clean and clean == "postura firme"

    def test_sin_marca_devuelve_none(self):
        assert _parse_verdict("sin veredicto")[1] is None


def _run(a_resp, b_resp, **kwargs):
    a = StubAdapter("claude", a_resp)
    b = StubAdapter("gemini", b_resp)
    orch = Orchestrator(a, b, repo_root=".")
    session = orch.run(TOPIC, **kwargs)
    return a, b, session


class TestFlujo:
    def test_apertura_a_ciegas_no_expone_la_postura_del_otro(self):
        a, b, _ = _run(
            ["postura A", "réplica A [CONVERGENCIA: SÍ]", "síntesis"],
            ["postura B", "réplica B [CONVERGENCIA: SÍ]"],
            max_rounds=1,
        )
        # En el primer prompt de cada agente no aparece la propuesta ajena.
        assert "postura B" not in a.llamadas[0][1]
        assert "postura A" not in b.llamadas[0][1]
        # En la réplica sí.
        assert "postura B" in a.llamadas[1][1]
        assert "postura A" in b.llamadas[1][1]

    def test_corte_temprano_si_ambos_convergen(self):
        _, _, s = _run(
            ["A0", "A1 [CONVERGENCIA: SÍ]", "síntesis"],
            ["B0", "B1 [CONVERGENCIA: SÍ]"],
            max_rounds=3,
        )
        assert s.converged and s.converged_round == 1 and s.rounds_run == 1

    def test_sin_consenso_agota_las_rondas(self):
        _, _, s = _run(
            ["A0", "A1 [CONVERGENCIA: NO]", "A2 [CONVERGENCIA: NO]", "síntesis"],
            ["B0", "B1 [CONVERGENCIA: SÍ]", "B2 [CONVERGENCIA: SÍ]"],
            max_rounds=2,
        )
        assert not s.converged and s.rounds_run == 2

    def test_sintesis_rotativa_por_indice(self):
        _, b, s = _run(
            ["A0", "A1 [CONVERGENCIA: SÍ]"],
            ["B0", "B1 [CONVERGENCIA: SÍ]", "síntesis de B"],
            max_rounds=1,
            synthesizer_index=1,
        )
        assert s.synthesizer == "gemini" and s.synthesis == "síntesis de B"
        # La síntesis recibe la transcripción con ambos lados.
        assert "A1" in b.llamadas[-1][1] and "B1" in b.llamadas[-1][1]

    def test_intervencion_del_vocero_llega_en_la_ronda_correcta(self):
        notas = {2: "ojo con el rendimiento"}
        a = StubAdapter(
            "claude", ["A0", "A1 [CONVERGENCIA: NO]", "A2 [CONVERGENCIA: SÍ]", "síntesis"]
        )
        b = StubAdapter(
            "gemini", ["B0", "B1 [CONVERGENCIA: NO]", "B2 [CONVERGENCIA: SÍ]"]
        )
        orch = Orchestrator(a, b, repo_root=".")
        orch.run(TOPIC, max_rounds=2, on_intervention=lambda r: notas.get(r))
        # Llamadas: 0=propuesta, 1=ronda 1, 2=ronda 2.
        assert "ojo con el rendimiento" not in a.llamadas[1][1]
        assert "ojo con el rendimiento" in a.llamadas[2][1]
        assert "ojo con el rendimiento" in b.llamadas[2][1]

    def test_modo_profundo_agrega_ronda_de_inversion(self):
        _, _, s = _run(
            ["A0", "A1 [CONVERGENCIA: SÍ]", "steelman A", "síntesis"],
            ["B0", "B1 [CONVERGENCIA: SÍ]", "steelman B"],
            max_rounds=1,
            deep_mode=True,
        )
        fases = [t.phase for t in s.turns]
        assert fases.count("inversion") == 2 and s.deep_mode

    def test_turnos_serializables_y_ordenados(self):
        _, _, s = _run(
            ["A0", "A1 [CONVERGENCIA: SÍ]", "síntesis"],
            ["B0", "B1 [CONVERGENCIA: SÍ]"],
            max_rounds=1,
        )
        assert [t.phase for t in s.turns] == [
            "propuesta", "propuesta", "replica", "replica", "sintesis",
        ]
        assert all(t.verdict == "si" for t in s.turns if t.phase == "replica")

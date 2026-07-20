"""Orquestador: apertura a ciegas, convergencia, intervención, inversión."""

from __future__ import annotations

import threading

import pytest

from devvating import roles
from devvating.orchestrator import (
    DebateCancelledError,
    DebateTopic,
    Orchestrator,
    _parse_verdict,
)
from tests.conftest import StubAdapter

TOPIC = DebateTopic(prompt="¿Refactor X?", context_hint="a.py")


class TestParseVerdict:
    def test_extrae_si(self):
        assert _parse_verdict('texto {"convergencia": true}')[1] == "si"

    def test_extrae_no_y_limpia_el_texto(self):
        clean, verdict = _parse_verdict('postura firme\n{"convergencia": false}')
        assert verdict == "no"
        assert '"convergencia"' not in clean and clean == "postura firme"

    def test_tolera_espacios_y_mayusculas_en_el_booleano(self):
        assert _parse_verdict('texto { "convergencia" :  TRUE }')[1] == "si"

    def test_sin_marca_devuelve_none(self):
        assert _parse_verdict("sin veredicto")[1] is None

    def test_marca_vieja_de_corchetes_ya_no_se_reconoce(self):
        # Fallback seguro: un bloque mal formado o el formato viejo no cuentan
        # como convergencia — el debate sigue de largo en vez de romper.
        assert _parse_verdict("texto [CONVERGENCIA: SÍ]")[1] is None


def _run(a_resp, b_resp, **kwargs):
    a = StubAdapter("claude", a_resp)
    b = StubAdapter("gemini", b_resp)
    orch = Orchestrator(a, b, repo_root=".")
    session = orch.run(TOPIC, **kwargs)
    return a, b, session


class TestFlujo:
    def test_apertura_a_ciegas_no_expone_la_postura_del_otro(self):
        a, b, _ = _run(
            ["postura A", 'réplica A {"convergencia": true}', "síntesis"],
            ["postura B", 'réplica B {"convergencia": true}'],
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
            ["A0", 'A1 {"convergencia": true}', "síntesis"],
            ["B0", 'B1 {"convergencia": true}'],
            max_rounds=3,
        )
        assert s.converged and s.converged_round == 1 and s.rounds_run == 1

    def test_sin_consenso_agota_las_rondas(self):
        _, _, s = _run(
            ["A0", 'A1 {"convergencia": false}', 'A2 {"convergencia": false}', "síntesis"],
            ["B0", 'B1 {"convergencia": true}', 'B2 {"convergencia": true}'],
            max_rounds=2,
        )
        assert not s.converged and s.rounds_run == 2

    def test_sintesis_rotativa_por_indice(self):
        _, b, s = _run(
            ["A0", 'A1 {"convergencia": true}'],
            ["B0", 'B1 {"convergencia": true}', "síntesis de B"],
            max_rounds=1,
            synthesizer_index=1,
        )
        assert s.synthesizer == "gemini" and s.synthesis == "síntesis de B"
        # La síntesis recibe la transcripción con ambos lados.
        assert "A1" in b.llamadas[-1][1] and "B1" in b.llamadas[-1][1]

    def test_intervencion_del_vocero_llega_en_la_ronda_correcta(self):
        notas = {2: "ojo con el rendimiento"}
        a = StubAdapter(
            "claude", ["A0", 'A1 {"convergencia": false}', 'A2 {"convergencia": true}', "síntesis"]
        )
        b = StubAdapter(
            "gemini", ["B0", 'B1 {"convergencia": false}', 'B2 {"convergencia": true}']
        )
        orch = Orchestrator(a, b, repo_root=".")
        orch.run(TOPIC, max_rounds=2, on_intervention=lambda r: notas.get(r))
        # Llamadas: 0=propuesta, 1=ronda 1, 2=ronda 2.
        assert "ojo con el rendimiento" not in a.llamadas[1][1]
        assert "ojo con el rendimiento" in a.llamadas[2][1]
        assert "ojo con el rendimiento" in b.llamadas[2][1]

    def test_modo_profundo_agrega_ronda_de_inversion(self):
        _, _, s = _run(
            ["A0", 'A1 {"convergencia": true}', "steelman A", "síntesis"],
            ["B0", 'B1 {"convergencia": true}', "steelman B"],
            max_rounds=1,
            deep_mode=True,
        )
        fases = [t.phase for t in s.turns]
        assert fases.count("inversion") == 2 and s.deep_mode

    def test_turnos_serializables_y_ordenados(self):
        _, _, s = _run(
            ["A0", 'A1 {"convergencia": true}', "síntesis"],
            ["B0", 'B1 {"convergencia": true}'],
            max_rounds=1,
        )
        assert [t.phase for t in s.turns] == [
            "propuesta", "propuesta", "replica", "replica", "sintesis",
        ]
        assert all(t.verdict == "si" for t in s.turns if t.phase == "replica")


class TestConSesgo:
    def test_neutral_devuelve_el_rol_intacto(self):
        assert roles.con_sesgo(roles.PROPONENTE, "") == roles.PROPONENTE
        assert roles.con_sesgo(roles.PROPONENTE, roles.SESGOS["neutral"]) == roles.PROPONENTE

    def test_sesgo_se_anexa_al_rol(self):
        compuesto = roles.con_sesgo(roles.REPLICA, roles.SESGOS["audaz"])
        assert compuesto.startswith(roles.REPLICA)
        assert roles.SESGOS["audaz"] in compuesto


class TestSesgos:
    def test_sesgo_en_propuesta_y_replica_no_en_sintesis(self):
        # El sesgo colorea propuesta y réplica; la síntesis debe ser neutral.
        a = StubAdapter("claude", ["A0", 'A1 {"convergencia": true}', "síntesis"])
        b = StubAdapter("gemini", ["B0", 'B1 {"convergencia": true}'])
        orch = Orchestrator(a, b, repo_root=".", biases=["SESGO_A", "SESGO_B"])
        orch.run(TOPIC, max_rounds=1, synthesizer_index=0)
        assert "SESGO_A" in a.llamadas[0][0]      # propuesta
        assert "SESGO_A" in a.llamadas[1][0]      # réplica
        assert "SESGO_A" not in a.llamadas[2][0]  # síntesis, neutral
        assert "SESGO_B" in b.llamadas[0][0] and "SESGO_B" in b.llamadas[1][0]

    def test_inversion_no_recibe_sesgo(self):
        # La inversión (steelman) ya invierte por diseño: no debe llevar sesgo.
        a = StubAdapter("claude", ["A0", 'A1 {"convergencia": true}', "steelman A", "síntesis"])
        b = StubAdapter("gemini", ["B0", 'B1 {"convergencia": true}', "steelman B"])
        orch = Orchestrator(a, b, repo_root=".", biases=["SESGO_A", "SESGO_B"])
        orch.run(TOPIC, max_rounds=1, deep_mode=True, synthesizer_index=0)
        # a: 0 propuesta, 1 réplica, 2 inversión, 3 síntesis.
        assert "SESGO_A" not in a.llamadas[2][0]

    def test_sin_sesgo_es_comportamiento_clasico(self):
        a, _, _ = _run(
            ["A0", 'A1 {"convergencia": true}', "síntesis"],
            ["B0", 'B1 {"convergencia": true}'],
            max_rounds=1,
        )
        assert a.llamadas[0][0] == roles.PROPONENTE  # rol puro, sin añadidos

    def test_biases_de_largo_incorrecto_falla(self):
        a, b = StubAdapter("claude", []), StubAdapter("gemini", [])
        with pytest.raises(ValueError, match="una por agente"):
            Orchestrator(a, b, biases=["solo-uno"])


class TestMinRounds:
    def test_convergencia_temprana_se_ignora_bajo_min_rounds(self):
        # Ambos declaran SÍ en la ronda 1, pero min_rounds=2 (auto-debate)
        # obliga a seguir: el eco no puede cerrar el debate en la primera réplica.
        _, _, s = _run(
            ["A0", 'A1 {"convergencia": true}', 'A2 {"convergencia": true}', "síntesis"],
            ["B0", 'B1 {"convergencia": true}', 'B2 {"convergencia": true}'],
            max_rounds=2,
            min_rounds=2,
        )
        assert s.converged and s.converged_round == 2 and s.rounds_run == 2

    def test_default_permite_corte_en_ronda_1(self):
        # min_rounds=1 (default, debate clásico): la convergencia en ronda 1 vale.
        _, _, s = _run(
            ["A0", 'A1 {"convergencia": true}', "síntesis"],
            ["B0", 'B1 {"convergencia": true}'],
            max_rounds=3,
        )
        assert s.converged and s.converged_round == 1


class TestCancelacion:
    def test_cancel_preseteado_corta_antes_del_primer_turno(self):
        ev = threading.Event()
        ev.set()  # ya cancelado antes de empezar
        a = StubAdapter("claude", ["A0"])
        b = StubAdapter("gemini", ["B0"])
        orch = Orchestrator(a, b, repo_root=".")
        with pytest.raises(DebateCancelledError) as ei:
            orch.run(TOPIC, max_rounds=1, cancel_event=ev)
        assert ei.value.session.turns == []  # no se corrió ni un turno
        assert a.llamadas == []

    def test_cancel_a_mitad_conserva_turnos_previos(self):
        ev = threading.Event()

        class CancelaTrasResponder(StubAdapter):
            def converse(self, system, prompt, registry):
                out = super().converse(system, prompt, registry)
                ev.set()  # el orquestador lo ve antes del siguiente turno
                return out

        a = CancelaTrasResponder("claude", ["A0", 'A1 {"convergencia": true}', "s"])
        b = StubAdapter("gemini", ["B0", 'B1 {"convergencia": true}'])
        orch = Orchestrator(a, b, repo_root=".")
        with pytest.raises(DebateCancelledError) as ei:
            orch.run(TOPIC, max_rounds=1, cancel_event=ev)
        # a hizo su propuesta; el corte cae antes del turno de b. Turno a salvo.
        turns = ei.value.session.turns
        assert len(turns) == 1 and turns[0].agent == "claude"
        assert turns[0].phase == "propuesta"


class StreamingStub(StubAdapter):
    """Stub que declara streaming y emite deltas por `on_delta` en cada turno.

    Simula el contrato de `ClaudeCliAdapter` sin subprocess: parte cada
    respuesta en fragmentos y los emite, tal como el adaptador real reenvía los
    text_delta del stream-json.
    """

    soporta_streaming = True

    def __init__(self, name, respuestas, usages=None):
        super().__init__(name, respuestas, usages)
        self.on_delta = None

    def converse(self, system, prompt, registry):
        out = super().converse(system, prompt, registry)
        if self.on_delta is not None:
            for palabra in out.split(" "):
                self.on_delta(palabra + " ")
        return out


class TestStreaming:
    def test_orquestador_fija_on_delta_solo_en_los_que_soportan(self):
        a = StreamingStub("claude", ["postura A", 'r {"convergencia": true}', "síntesis"])
        b = StubAdapter("gemini", ["postura B", 'r {"convergencia": true}'])
        orch = Orchestrator(a, b, repo_root=".")
        orch.run(TOPIC, max_rounds=1)
        assert callable(a.on_delta)  # se lo fijó el orquestador
        assert not hasattr(b, "on_delta")  # al no soportar, no se toca

    def test_los_deltas_llegan_como_eventos_delta_a_la_ui(self):
        eventos = []
        a = StreamingStub("claude", ["postura A", 'r {"convergencia": true}', "síntesis"])
        b = StubAdapter("gemini", ["postura B", 'r {"convergencia": true}'])
        orch = Orchestrator(
            a, b, repo_root=".",
            on_event=lambda ev, ag, tx: eventos.append((ev, ag, tx)),
        )
        orch.run(TOPIC, max_rounds=1)
        deltas = [(ag, tx) for ev, ag, tx in eventos if ev == "delta"]
        # Solo claude (el que soporta) emite deltas, y reconstruyen su texto.
        assert deltas and all(ag == "claude" for ag, _ in deltas)
        assert "".join(tx for _, tx in deltas).startswith("postura A")

    def test_el_retorno_completo_no_depende_del_streaming(self):
        # El orquestador es ciego al streaming: la síntesis y la convergencia
        # salen del retorno completo de converse, no de los deltas.
        a = StreamingStub("claude", ["A0", 'A1 {"convergencia": true}', "síntesis final"])
        b = StreamingStub("gemini", ["B0", 'B1 {"convergencia": true}'])
        orch = Orchestrator(a, b, repo_root=".")
        s = orch.run(TOPIC, max_rounds=1)
        assert s.converged and s.synthesis == "síntesis final"

"""Resiliencia (plan del debate 2026-07-13): clasificación, backoff y aborto."""

from __future__ import annotations

import json

import pytest

from devvating.adapters.base import SessionLimitError, TransientProviderError
from devvating.adapters.cli import (
    ClaudeCliAdapter,
    CliAdapterError,
    KimiCliAdapter,
    clasificar_fallo,
)
from devvating.debate import _save_transcript
from devvating.orchestrator import DebateAbortedError, DebateTopic, Orchestrator
from devvating.tools.registry import ToolRegistry
from tests.conftest import StubAdapter

REG = ToolRegistry()
TOPIC = DebateTopic(prompt="tema")


@pytest.fixture
def fake_bin(tmp_path):
    def make(name: str, script: str) -> str:
        path = tmp_path / name
        path.write_text(f"#!/bin/bash\n{script}\n", encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    return make


class TestClasificacion:
    def test_limite_de_sesion_extrae_hora_de_reset(self):
        exc = clasificar_fallo(
            "You've hit your session limit · resets 1:50pm (America/Bogota)", "x"
        )
        assert isinstance(exc, SessionLimitError)
        assert "1:50pm" in exc.resets_at

    def test_transitorios_por_texto(self):
        for detalle in ("503 UNAVAILABLE", "high demand", "RESOURCE_EXHAUSTED",
                        "429 rate limit", "Overloaded"):
            assert isinstance(clasificar_fallo(detalle, "x"), TransientProviderError)

    def test_desconocido_es_generico_no_reintentable(self):
        exc = clasificar_fallo("algo raro pasó", "x")
        assert isinstance(exc, CliAdapterError)
        assert not isinstance(exc, TransientProviderError)

    def test_claude_cli_json_con_limite_de_sesion(self, fake_bin, tmp_path):
        # La clasificación sale del mensaje 'result' del stream-json.
        out = json.dumps({
            "type": "result",
            "result": "Session limit reached · resets 2:30pm",
            "is_error": True, "api_error_status": 429,
        })
        adapter = ClaudeCliAdapter(binary=fake_bin("claude", f"echo '{out}'"), cwd=str(tmp_path))
        with pytest.raises(SessionLimitError) as info:
            adapter.converse("S", "P", REG)
        assert "2:30pm" in info.value.resets_at

    def test_claude_cli_json_con_5xx_es_transitorio(self, fake_bin, tmp_path):
        out = json.dumps({"type": "result", "result": "server error",
                          "is_error": True, "api_error_status": 529})
        adapter = ClaudeCliAdapter(binary=fake_bin("claude", f"echo '{out}'"), cwd=str(tmp_path))
        with pytest.raises(TransientProviderError):
            adapter.converse("S", "P", REG)

    def test_plain_cli_stderr_transitorio(self, fake_bin, tmp_path):
        script = "echo 'RESOURCE_EXHAUSTED: retry later' >&2; exit 1"
        adapter = KimiCliAdapter(binary=fake_bin("kimi", script), cwd=str(tmp_path))
        with pytest.raises(TransientProviderError):
            adapter.converse("S", "P", REG)


def _orq(a, b, esperas=(1, 2)):
    dormido: list[float] = []
    orch = Orchestrator(a, b, repo_root=".", retry_waits=esperas,
                        sleep=dormido.append)
    return orch, dormido


class TestBackoffEnOrquestador:
    def test_transitorio_se_reintenta_y_el_debate_termina(self):
        a = StubAdapter("claude", [
            TransientProviderError("503"), TransientProviderError("503"),
            "A0", 'A1 {"convergencia": true}', "síntesis",
        ])
        b = StubAdapter("gemini", ["B0", 'B1 {"convergencia": true}'])
        orch, dormido = _orq(a, b)
        s = orch.run(TOPIC, max_rounds=1)
        assert s.synthesis == "síntesis"
        assert dormido == [1, 2]  # backoff según retry_waits
        assert len(a.llamadas) == 5  # 2 fallidos + 3 turnos reales

    def test_transitorios_agotados_abortan_con_sesion_parcial(self):
        a = StubAdapter("claude", ["A0"])
        b = StubAdapter("gemini", [
            TransientProviderError("503"), TransientProviderError("503"),
            TransientProviderError("503"),
        ])
        orch, dormido = _orq(a, b, esperas=(1, 2))
        with pytest.raises(DebateAbortedError) as info:
            orch.run(TOPIC, max_rounds=1)
        # La apertura de 'a' ya pagada viene en la sesión parcial, con totales.
        assert [t.agent for t in info.value.session.turns] == ["claude"]
        assert isinstance(info.value.causa, TransientProviderError)
        assert dormido == [1, 2]

    def test_limite_de_sesion_aborta_sin_reintentar(self):
        a = StubAdapter("claude", [SessionLimitError("límite", resets_at="1:50pm")])
        b = StubAdapter("gemini", ["B0"])
        orch, dormido = _orq(a, b)
        with pytest.raises(DebateAbortedError) as info:
            orch.run(TOPIC, max_rounds=1)
        assert dormido == []  # cero esperas: esta clase no se cura con backoff
        assert info.value.causa.resets_at == "1:50pm"

    def test_fallo_desconocido_aborta_sin_reintentar(self):
        a = StubAdapter("claude", [CliAdapterError("raro")])
        b = StubAdapter("gemini", ["B0"])
        orch, dormido = _orq(a, b)
        with pytest.raises(DebateAbortedError):
            orch.run(TOPIC, max_rounds=1)
        assert dormido == []

    def test_emite_eventos_de_reintento(self):
        eventos: list[tuple[str, str]] = []
        a = StubAdapter("claude", [TransientProviderError("503"), "A0",
                                   'A1 {"convergencia": true}', "síntesis"])
        b = StubAdapter("gemini", ["B0", 'B1 {"convergencia": true}'])
        orch = Orchestrator(a, b, repo_root=".", retry_waits=(1,),
                            sleep=lambda _: None,
                            on_event=lambda ev, ag, tx: eventos.append((ev, ag)))
        orch.run(TOPIC, max_rounds=1)
        assert ("reintento", "claude") in eventos


class TestVolcadoParcial:
    def test_transcript_parcial_lleva_sufijo_propio(self, tmp_path):
        from devvating.orchestrator import DebateSession, Turn

        s = DebateSession(topic=TOPIC, turns=[Turn(0, "propuesta", "claude", "A0")])
        path = _save_transcript(s, str(tmp_path), parcial=True)
        assert path.name.endswith(".partial.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["turns"][0]["text"] == "A0"

"""Banco de agentes (M8, D7): roster, alias, validación y nuevos adaptadores."""

from __future__ import annotations

import json

import pytest

from devvating import agentes as banco
from devvating.adapters.cli import AntigravityCliAdapter, KimiCliAdapter
from devvating.appconfig import ProjectConfig
from devvating.config import Config
from devvating.tools.registry import ToolRegistry

CFG = Config(anthropic_api_key="k1", gemini_api_key="k2")
REG = ToolRegistry()


@pytest.fixture
def fake_bin(tmp_path):
    def make(name: str, script: str) -> str:
        path = tmp_path / name
        path.write_text(f"#!/bin/bash\n{script}\n", encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    return make


class TestRoster:
    def test_crea_cada_entrada_del_roster(self, tmp_path):
        for nombre in banco.nombres():
            adapter = banco.crear(nombre, CFG, str(tmp_path))
            assert adapter.name  # cumple el Protocol

    def test_alias_agy_resuelve_a_antigravity(self, tmp_path):
        adapter = banco.crear("agy", CFG, str(tmp_path))
        assert isinstance(adapter, AntigravityCliAdapter)
        assert adapter.name == "antigravity"

    def test_nombre_desconocido_lista_el_roster(self, tmp_path):
        with pytest.raises(ValueError, match="Roster:"):
            banco.crear("skynet", CFG, str(tmp_path))

    def test_backend_api_sin_clave_falla_claro(self, tmp_path):
        vacio = Config(anthropic_api_key="", gemini_api_key="")
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            banco.crear("claude-api", vacio, str(tmp_path))

    def test_par_exige_exactamente_dos(self, tmp_path):
        with pytest.raises(ValueError, match="exactamente 2"):
            banco.par(["kimi"], CFG, str(tmp_path))
        with pytest.raises(ValueError, match="exactamente 2"):
            banco.par(["kimi", "claude-cli", "gemini-api"], CFG, str(tmp_path))

    def test_par_autodebate_desambigua_identidades(self, tmp_path):
        # Mismo agente dos veces (auto-debate): en vez de rechazar, se
        # desambiguan los nombres para que el transcript no colisione.
        a, b = banco.par(["claude-cli", "claude-cli"], CFG, str(tmp_path))
        assert (a.name, b.name) == ("claude#1", "claude#2")
        assert a.name != b.name
        assert banco.es_autodebate(a, b)

    def test_par_familias_distintas_no_es_autodebate(self, tmp_path):
        a, b = banco.par(["antigravity", "kimi"], CFG, str(tmp_path))
        assert not banco.es_autodebate(a, b)

    def test_par_valido_cruzando_familias(self, tmp_path):
        a, b = banco.par(["antigravity", "kimi"], CFG, str(tmp_path))
        assert (a.name, b.name) == ("antigravity", "kimi")


class TestNuevosAdaptadores:
    def test_kimi_argv_headless_texto(self):
        argv = KimiCliAdapter().build_argv("SYS", "PROMPT")
        assert argv[0] == "kimi" and argv[1] == "-p"
        assert "SYS" in argv[2] and "PROMPT" in argv[2]  # system antepuesto
        assert argv[-2:] == ["--output-format", "text"]

    def test_antigravity_sin_modelo_usa_el_default_del_cli(self):
        argv = AntigravityCliAdapter().build_argv("S", "P")
        assert argv[0] == "agy" and "--model" not in argv
        assert "--dangerously-skip-permissions" not in argv

    def test_antigravity_con_modelo_explicito(self):
        argv = AntigravityCliAdapter(model="Gemini 3.1 Pro (High)").build_argv("S", "P")
        i = argv.index("--model")
        assert argv[i + 1] == "Gemini 3.1 Pro (High)"

    def test_antigravity_alinea_su_print_timeout_interno(self):
        adapter = AntigravityCliAdapter(timeout=1500)
        argv = adapter.build_argv("S", "P")
        i = argv.index("--print-timeout")
        assert argv[i + 1] == "1440s"  # timeout - 60s de margen

    def test_timeout_cli_sobreescribible_por_entorno(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEVVATING_CLI_TIMEOUT", "99")
        assert banco.crear("kimi", CFG, str(tmp_path)).timeout == 99
        monkeypatch.setenv("DEVVATING_CLI_TIMEOUT", "no-numero")
        assert banco.crear("kimi", CFG, str(tmp_path)).timeout == 600  # cae al default

    def test_kimi_converse_devuelve_stdout(self, fake_bin, tmp_path):
        adapter = KimiCliAdapter(binary=fake_bin("kimi", 'echo "postura de kimi"'),
                                 cwd=str(tmp_path))
        assert adapter.converse("S", "P", REG) == "postura de kimi"
        assert adapter.last_usage is None

    def test_cancelacion_mata_el_subprocess_sin_esperar(self, fake_bin, tmp_path):
        import threading
        import time
        from devvating.adapters.base import AgentCancelledError

        ev = threading.Event()
        ev.set()  # cancelación ya pedida
        adapter = KimiCliAdapter(
            binary=fake_bin("kimi", 'sleep 5; echo "tarde"'), cwd=str(tmp_path))
        adapter.cancel_event = ev
        t0 = time.monotonic()
        with pytest.raises(AgentCancelledError):
            adapter.converse("S", "P", REG)
        assert time.monotonic() - t0 < 3  # no esperó los 5s del sleep

    def test_no_hereda_claves_google_al_subprocess(self, fake_bin, tmp_path, monkeypatch):
        # Trampa gemela a la de ANTHROPIC_API_KEY: si el CLI de Google ve
        # GEMINI_API_KEY o un proyecto GCP, desvía la facturación del login.
        monkeypatch.setenv("GEMINI_API_KEY", "clave-falsa")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "sentinel-falso")
        script = (
            'if [ -n "$GEMINI_API_KEY" ] || [ -n "$GOOGLE_CLOUD_PROJECT" ]; then '
            'echo "con clave"; else echo "sin clave"; fi'
        )
        adapter = AntigravityCliAdapter(binary=fake_bin("agy", script), cwd=str(tmp_path))
        assert adapter.converse("S", "P", REG) == "sin clave"


class TestConfigAgentes:
    def test_lee_lista_de_agentes(self, tmp_path):
        (tmp_path / ".devvating.json").write_text(
            json.dumps({"agentes": ["antigravity", "claude-cli"]}), encoding="utf-8"
        )
        pc = ProjectConfig.load(str(tmp_path))
        assert pc.agentes == ["antigravity", "claude-cli"]

    def test_agentes_invalidos_caen_a_vacio(self, tmp_path):
        (tmp_path / ".devvating.json").write_text(
            json.dumps({"agentes": "no-una-lista"}), encoding="utf-8"
        )
        assert ProjectConfig.load(str(tmp_path)).agentes == []
        (tmp_path / ".devvating.json").write_text(
            json.dumps({"agentes": [1, None, "kimi"]}), encoding="utf-8"
        )
        assert ProjectConfig.load(str(tmp_path)).agentes == ["kimi"]

    def test_lee_lista_de_sesgos(self, tmp_path):
        (tmp_path / ".devvating.json").write_text(
            json.dumps({"sesgos": ["audaz", "cauto"]}), encoding="utf-8"
        )
        assert ProjectConfig.load(str(tmp_path)).sesgos == ["audaz", "cauto"]

    def test_sesgos_invalidos_caen_a_vacio(self, tmp_path):
        (tmp_path / ".devvating.json").write_text(
            json.dumps({"sesgos": "no-una-lista"}), encoding="utf-8"
        )
        assert ProjectConfig.load(str(tmp_path)).sesgos == []

"""Adaptadores CLI (D5): argv de solo lectura, parseo y manejo de errores.

Se prueban contra binarios falsos (scripts bash en tmp) — sin CLIs reales.
"""

from __future__ import annotations

import json

import pytest

from devvating.adapters.cli import ClaudeCliAdapter, CliAdapterError, GeminiCliAdapter
from devvating.appconfig import ProjectConfig
from devvating.tools.registry import ToolRegistry


@pytest.fixture
def fake_bin(tmp_path):
    """Crea un binario falso ejecutable y devuelve su ruta."""

    def make(name: str, script: str) -> str:
        path = tmp_path / name
        path.write_text(f"#!/bin/bash\n{script}\n", encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    return make


REG = ToolRegistry()


class TestClaudeCliAdapter:
    def test_argv_es_headless_json_y_solo_lectura(self):
        argv = ClaudeCliAdapter().build_argv("SYS", "PROMPT")
        assert argv[:3] == ["claude", "-p", "PROMPT"]
        assert "--append-system-prompt" in argv and "SYS" in argv
        assert "--output-format" in argv and "json" in argv
        i = argv.index("--allowedTools")
        assert argv[i + 1] == "Read,Glob,Grep"
        # Jamás los flags de escritura/peligro de la fase de ejecución.
        assert "--dangerously-skip-permissions" not in argv
        assert "acceptEdits" not in argv

    def test_parsea_result_y_usage_por_turno_sin_acumular(self, fake_bin, tmp_path):
        # Plan §13: last_usage es POR TURNO; la totalización es del orquestador.
        out = json.dumps(
            {"result": "postura de claude", "is_error": False,
             "total_cost_usd": 0.0125,
             "usage": {"input_tokens": 100, "output_tokens": 42,
                       "cache_read_input_tokens": 7}}
        )
        binary = fake_bin("claude", f"echo '{out}'")
        adapter = ClaudeCliAdapter(binary=binary, cwd=str(tmp_path))
        assert adapter.converse("SYS", "P", REG) == "postura de claude"
        assert adapter.converse("SYS", "P", REG) == "postura de claude"
        u = adapter.last_usage
        assert u.input_tokens == 100 and u.output_tokens == 42
        assert u.cache_read_tokens == 7
        assert u.cost_usd == pytest.approx(0.0125)  # el del turno, no 0.025

    def test_sin_usage_en_el_json_da_turnusage_con_ceros(self, fake_bin, tmp_path):
        out = json.dumps({"result": "ok", "is_error": False})
        adapter = ClaudeCliAdapter(binary=fake_bin("claude", f"echo '{out}'"), cwd=str(tmp_path))
        adapter.converse("SYS", "P", REG)
        assert adapter.last_usage.input_tokens == 0
        assert adapter.last_usage.cost_usd is None

    def test_codigo_de_salida_no_cero_levanta_error(self, fake_bin, tmp_path):
        binary = fake_bin("claude", "echo 'algo falló' >&2; exit 3")
        adapter = ClaudeCliAdapter(binary=binary, cwd=str(tmp_path))
        with pytest.raises(CliAdapterError, match="código 3"):
            adapter.converse("SYS", "P", REG)

    def test_is_error_del_cli_levanta_error(self, fake_bin, tmp_path):
        out = json.dumps({"result": "sin sesión", "is_error": True})
        binary = fake_bin("claude", f"echo '{out}'")
        adapter = ClaudeCliAdapter(binary=binary, cwd=str(tmp_path))
        with pytest.raises(CliAdapterError, match="reportó error"):
            adapter.converse("SYS", "P", REG)

    def test_salida_no_json_levanta_error(self, fake_bin, tmp_path):
        binary = fake_bin("claude", "echo 'texto plano'")
        adapter = ClaudeCliAdapter(binary=binary, cwd=str(tmp_path))
        with pytest.raises(CliAdapterError, match="no-JSON"):
            adapter.converse("SYS", "P", REG)

    def test_binario_inexistente_da_mensaje_claro(self, tmp_path):
        adapter = ClaudeCliAdapter(binary=str(tmp_path / "no-existe"), cwd=str(tmp_path))
        with pytest.raises(CliAdapterError, match="No se encontró el binario"):
            adapter.converse("SYS", "P", REG)

    def test_no_hereda_la_clave_api_al_subprocess(self, fake_bin, tmp_path, monkeypatch):
        # Trampa de precedencia: si el CLI ve ANTHROPIC_API_KEY, factura contra
        # la clave en vez de usar la suscripción. El adaptador debe quitarla.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-falsa")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token-falso")
        script = (
            'if [ -n "$ANTHROPIC_API_KEY" ] || [ -n "$ANTHROPIC_AUTH_TOKEN" ]; then '
            "echo '{\"result\": \"con clave\", \"is_error\": false}'; "
            "else echo '{\"result\": \"sin clave\", \"is_error\": false}'; fi"
        )
        adapter = ClaudeCliAdapter(binary=fake_bin("claude", script), cwd=str(tmp_path))
        assert adapter.converse("SYS", "P", REG) == "sin clave"


class TestGeminiCliAdapter:
    def test_incluye_el_system_en_el_prompt(self, fake_bin, tmp_path):
        # El script vuelca sus argumentos a un archivo y responde por stdout.
        dump = tmp_path / "args.txt"
        binary = fake_bin("gemini", f'printf "%s\\n" "$@" > {dump}; echo "respuesta"')
        adapter = GeminiCliAdapter(binary=binary, cwd=str(tmp_path))
        assert adapter.converse("ERES RÉPLICA", "el tema", REG) == "respuesta"
        args = dump.read_text(encoding="utf-8")
        assert "ERES RÉPLICA" in args and "el tema" in args
        assert args.startswith("-p")

    def test_codigo_de_salida_no_cero_levanta_error(self, fake_bin, tmp_path):
        binary = fake_bin("gemini", "exit 1")
        adapter = GeminiCliAdapter(binary=binary, cwd=str(tmp_path))
        with pytest.raises(CliAdapterError, match="código 1"):
            adapter.converse("SYS", "P", REG)


class TestBackendsEnConfig:
    def test_defaults_son_api(self, tmp_path):
        pc = ProjectConfig.load(str(tmp_path))
        assert pc.claude_backend == "api" and pc.gemini_backend == "api"

    def test_lee_backends_del_archivo(self, tmp_path):
        (tmp_path / ".devvating.json").write_text(
            json.dumps({"backends": {"claude": "cli", "gemini": "api"}}),
            encoding="utf-8",
        )
        pc = ProjectConfig.load(str(tmp_path))
        assert pc.claude_backend == "cli" and pc.gemini_backend == "api"

    def test_valor_invalido_cae_a_api(self, tmp_path):
        (tmp_path / ".devvating.json").write_text(
            json.dumps({"backends": {"claude": "magia", "gemini": None}}),
            encoding="utf-8",
        )
        pc = ProjectConfig.load(str(tmp_path))
        assert pc.claude_backend == "api" and pc.gemini_backend == "api"


class TestFactory:
    def test_backend_cli_no_exige_claves(self, tmp_path):
        from devvating.config import Config
        from devvating.debate import make_agent

        cfg = Config(anthropic_api_key="", gemini_api_key="")
        claude = make_agent("claude", "cli", cfg, str(tmp_path))
        gemini = make_agent("gemini", "cli", cfg, str(tmp_path))
        assert claude.name == "claude" and isinstance(claude, ClaudeCliAdapter)
        assert gemini.name == "gemini" and isinstance(gemini, GeminiCliAdapter)
        assert claude.cwd == str(tmp_path)

    def test_backend_api_sin_clave_falla_claro(self, tmp_path):
        from devvating.config import Config
        from devvating.debate import make_agent

        cfg = Config(anthropic_api_key="", gemini_api_key="")
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            make_agent("claude", "api", cfg, str(tmp_path))

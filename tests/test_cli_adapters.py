"""Adaptadores CLI (D5): argv de solo lectura, parseo y manejo de errores.

Se prueban contra binarios falsos (scripts bash en tmp) — sin CLIs reales.
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from devvating.adapters.base import AgentCancelledError
from devvating.adapters.cli import ClaudeCliAdapter, CliAdapterError, GeminiCliAdapter
from devvating.appconfig import ProjectConfig
from devvating.tools.registry import ToolRegistry


def _delta(texto: str) -> str:
    """Una línea stream-json de tipo content_block_delta (como emite el CLI real)."""
    return json.dumps({
        "type": "stream_event",
        "event": {"type": "content_block_delta", "index": 0,
                  "delta": {"type": "text_delta", "text": texto}},
    })


def _result(**campos) -> str:
    """La línea final 'result' del stream-json, con los campos que se pasen."""
    base = {"type": "result", "subtype": "success", "is_error": False}
    base.update(campos)
    return json.dumps(base)


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
    def test_argv_es_headless_stream_json_y_solo_lectura(self):
        argv = ClaudeCliAdapter().build_argv("SYS", "PROMPT")
        assert argv[:3] == ["claude", "-p", "PROMPT"]
        assert "--append-system-prompt" in argv and "SYS" in argv
        # stream-json para emitir tokens en vivo; partial-messages trae los
        # deltas y `-p` con stream-json exige --verbose (lo pide el CLI).
        i = argv.index("--output-format")
        assert argv[i + 1] == "stream-json"
        assert "--include-partial-messages" in argv
        assert "--verbose" in argv
        j = argv.index("--allowedTools")
        assert argv[j + 1] == "Read,Glob,Grep"
        # Jamás los flags de escritura/peligro de la fase de ejecución.
        assert "--dangerously-skip-permissions" not in argv
        assert "acceptEdits" not in argv

    def test_soporta_streaming(self):
        # El front lee esta capacidad para decidir entre "en vivo" y "sin soporte".
        assert ClaudeCliAdapter.soporta_streaming is True

    def test_parsea_result_y_usage_por_turno_sin_acumular(self, fake_bin, tmp_path):
        # Plan §13: last_usage es POR TURNO; la totalización es del orquestador.
        # Los datos salen del mensaje 'result' del stream, no de la última línea.
        out = _result(result="postura de claude", total_cost_usd=0.0125,
                      usage={"input_tokens": 100, "output_tokens": 42,
                             "cache_read_input_tokens": 7})
        binary = fake_bin("claude", f"echo '{out}'")
        adapter = ClaudeCliAdapter(binary=binary, cwd=str(tmp_path))
        assert adapter.converse("SYS", "P", REG) == "postura de claude"
        assert adapter.converse("SYS", "P", REG) == "postura de claude"
        u = adapter.last_usage
        assert u.input_tokens == 100 and u.output_tokens == 42
        assert u.cache_read_tokens == 7
        assert u.cost_usd == pytest.approx(0.0125)  # el del turno, no 0.025

    def test_emite_los_deltas_por_on_delta_y_devuelve_el_result(self, fake_bin, tmp_path):
        # Streaming: cada text_delta llega por on_delta a medida que se lee el
        # stream; el retorno de converse sigue siendo el texto COMPLETO (el
        # orquestador es ciego al streaming).
        script = "\n".join([
            f"echo '{_delta('Hola')}'",
            f"echo '{_delta(', ')}'",
            f"echo '{_delta('mundo')}'",
            f"echo '{_result(result='Hola, mundo')}'",
        ])
        adapter = ClaudeCliAdapter(binary=fake_bin("claude", script), cwd=str(tmp_path))
        recibidos: list[str] = []
        adapter.on_delta = recibidos.append
        assert adapter.converse("SYS", "P", REG) == "Hola, mundo"
        assert recibidos == ["Hola", ", ", "mundo"]

    def test_sin_on_delta_no_falla(self, fake_bin, tmp_path):
        # Sin callback fijado (on_delta=None por defecto) los deltas se ignoran.
        script = f"echo '{_delta('x')}'\necho '{_result(result='ok')}'"
        adapter = ClaudeCliAdapter(binary=fake_bin("claude", script), cwd=str(tmp_path))
        assert adapter.converse("SYS", "P", REG) == "ok"

    def test_sin_usage_en_el_result_da_turnusage_con_ceros(self, fake_bin, tmp_path):
        adapter = ClaudeCliAdapter(
            binary=fake_bin("claude", f"echo '{_result(result='ok')}'"), cwd=str(tmp_path)
        )
        adapter.converse("SYS", "P", REG)
        assert adapter.last_usage.input_tokens == 0
        assert adapter.last_usage.cost_usd is None

    def test_codigo_de_salida_no_cero_levanta_error(self, fake_bin, tmp_path):
        binary = fake_bin("claude", "echo 'algo falló' >&2; exit 3")
        adapter = ClaudeCliAdapter(binary=binary, cwd=str(tmp_path))
        with pytest.raises(CliAdapterError, match="código 3"):
            adapter.converse("SYS", "P", REG)

    def test_is_error_del_cli_levanta_error(self, fake_bin, tmp_path):
        out = _result(result="sin sesión", is_error=True)
        binary = fake_bin("claude", f"echo '{out}'")
        adapter = ClaudeCliAdapter(binary=binary, cwd=str(tmp_path))
        with pytest.raises(CliAdapterError, match="reportó error"):
            adapter.converse("SYS", "P", REG)

    def test_stream_sin_mensaje_result_levanta_error(self, fake_bin, tmp_path):
        # Solo deltas, sin 'result': el turno no cerró bien → error claro.
        binary = fake_bin("claude", f"echo '{_delta('a medias')}'")
        adapter = ClaudeCliAdapter(binary=binary, cwd=str(tmp_path))
        with pytest.raises(CliAdapterError, match="no incluyó un mensaje 'result'"):
            adapter.converse("SYS", "P", REG)

    def test_lineas_no_json_se_ignoran(self, fake_bin, tmp_path):
        # El CLI puede colar un log suelto no-JSON; no debe romper el parseo.
        script = f"echo 'log suelto del CLI'\necho '{_result(result='ok')}'"
        adapter = ClaudeCliAdapter(binary=fake_bin("claude", script), cwd=str(tmp_path))
        assert adapter.converse("SYS", "P", REG) == "ok"

    def test_binario_inexistente_da_mensaje_claro(self, tmp_path):
        adapter = ClaudeCliAdapter(binary=str(tmp_path / "no-existe"), cwd=str(tmp_path))
        with pytest.raises(CliAdapterError, match="No se encontró el binario"):
            adapter.converse("SYS", "P", REG)

    def test_hijo_que_cierra_stdout_sin_morir_no_cuelga(self, fake_bin, tmp_path, monkeypatch):
        # Regresión: `proc.wait()` sin tope colgaba para siempre si el hijo
        # cerraba stdout (EOF → _FIN) pero seguía vivo. Ahora se espera un tope
        # corto y, si no muere, se mata el grupo; el result ya está capturado.
        monkeypatch.setattr("devvating.adapters.cli._ESPERA_CIERRE", 0.3)
        script = f"echo '{_result(result='pese al hijo colgado')}'\nexec 1>&-\nsleep 30"
        adapter = ClaudeCliAdapter(binary=fake_bin("claude", script), cwd=str(tmp_path))
        inicio = time.monotonic()
        assert adapter.converse("SYS", "P", REG) == "pese al hijo colgado"
        assert time.monotonic() - inicio < 10  # no esperó los 30s del sleep

    def test_stderr_completo_en_el_fallo(self, fake_bin, tmp_path):
        # Regresión: leer err_partes sin drenar el hilo de stderr truncaba el
        # diagnóstico justo en el caso de fallo. Debe llegar entero.
        script = (
            "echo 'linea de error 1' >&2\n"
            "echo 'linea de error 2' >&2\n"
            "echo 'ultima linea con el detalle clave' >&2\n"
            "exit 7"
        )
        adapter = ClaudeCliAdapter(binary=fake_bin("claude", script), cwd=str(tmp_path))
        with pytest.raises(CliAdapterError) as info:
            adapter.converse("SYS", "P", REG)
        assert "código 7" in str(info.value)
        assert "ultima linea con el detalle clave" in str(info.value)

    def test_cancelacion_a_mitad_de_stream_mata_el_subprocess(self, fake_bin, tmp_path):
        # El cambio a lecturas incrementales alteró la mecánica de _matar_grupo
        # (ya no drena con communicate porque un hilo lee stdout). Regresión: al
        # cancelar con el stream en vuelo, el subprocess muere pronto — no se
        # espera al timeout ni cuelga el lector.
        binary = fake_bin("claude", f"echo '{_delta('arranco')}'; sleep 30")
        adapter = ClaudeCliAdapter(binary=binary, cwd=str(tmp_path), timeout=600)
        cancel = threading.Event()
        adapter.cancel_event = cancel
        threading.Timer(0.5, cancel.set).start()
        inicio = time.monotonic()
        with pytest.raises(AgentCancelledError, match="cancelado por el vocero"):
            adapter.converse("SYS", "P", REG)
        assert time.monotonic() - inicio < 10  # no esperó los 30s del sleep

    def test_no_hereda_la_clave_api_al_subprocess(self, fake_bin, tmp_path, monkeypatch):
        # Trampa de precedencia: si el CLI ve ANTHROPIC_API_KEY, factura contra
        # la clave en vez de usar la suscripción. El adaptador debe quitarla —
        # se re-verifica con la invocación nueva (stream-json).
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-falsa")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token-falso")
        con = _result(result="con clave")
        sin = _result(result="sin clave")
        script = (
            'if [ -n "$ANTHROPIC_API_KEY" ] || [ -n "$ANTHROPIC_AUTH_TOKEN" ]; then '
            f"echo '{con}'; else echo '{sin}'; fi"
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


class TestStdinCerrado:
    """Los subprocess de CLI corren con stdin=DEVNULL: un CLI que sondee la
    entrada (agy/gemini sin flags interactivos) heredaría el terminal y se
    quedaría "sin responder" hasta reventar el timeout del turno."""

    def _espiar_popen(self, monkeypatch):
        import subprocess

        capturado: dict = {}
        original = subprocess.Popen

        def espia(*args, **kwargs):
            capturado.update(kwargs)
            return original(*args, **kwargs)

        monkeypatch.setattr("devvating.adapters.cli.subprocess.Popen", espia)
        return capturado

    def test_camino_por_turnos_no_hereda_stdin(self, fake_bin, tmp_path, monkeypatch):
        import subprocess

        capturado = self._espiar_popen(monkeypatch)
        binario = fake_bin("gem", 'echo "hola"')
        out = GeminiCliAdapter(binary=binario, cwd=str(tmp_path)).converse("S", "P", REG)
        assert out == "hola"
        assert capturado.get("stdin") == subprocess.DEVNULL

    def test_camino_streaming_no_hereda_stdin(self, fake_bin, tmp_path, monkeypatch):
        import subprocess

        capturado = self._espiar_popen(monkeypatch)
        binario = fake_bin("cla", f"echo '{_result(result='ok')}'")
        out = ClaudeCliAdapter(binary=binario, cwd=str(tmp_path)).converse("S", "P", REG)
        assert out == "ok"
        assert capturado.get("stdin") == subprocess.DEVNULL


class TestTurnoVacio:
    """Un CLI que sale con código 0 pero sin imprimir nada NO es un turno.

    Verificado en real con `agy`: cuando una herramienta suya se auto-deniega
    en headless, sale 0 y no imprime nada; el porqué solo viaja en stderr.
    Aceptarlo dejaba un agente MUDO debatiendo — rondas y síntesis completas
    con un solo participante real.
    """

    def test_plain_cli_sin_salida_falla_con_el_stderr(self, fake_bin, tmp_path):
        motivo = "no output produced — a tool required the command permission"
        binario = fake_bin("mudo", f'echo "{motivo}" >&2; exit 0')
        with pytest.raises(CliAdapterError) as info:
            GeminiCliAdapter(binary=binario, cwd=str(tmp_path)).converse("S", "P", REG)
        # El diagnóstico del CLI llega al vocero en vez de perderse.
        assert "command permission" in str(info.value)
        assert "sin producir respuesta" in str(info.value)

    def test_plain_cli_sin_salida_ni_stderr_igual_falla(self, fake_bin, tmp_path):
        binario = fake_bin("mudo2", "exit 0")
        with pytest.raises(CliAdapterError, match="no imprimió nada"):
            GeminiCliAdapter(binary=binario, cwd=str(tmp_path)).converse("S", "P", REG)

    def test_plain_cli_solo_espacios_cuenta_como_vacio(self, fake_bin, tmp_path):
        binario = fake_bin("blanco", 'printf "  \\n\\n"; exit 0')
        with pytest.raises(CliAdapterError):
            GeminiCliAdapter(binary=binario, cwd=str(tmp_path)).converse("S", "P", REG)

    def test_claude_cli_result_vacio_falla(self, fake_bin, tmp_path):
        binario = fake_bin("cla-mudo", f"echo '{_result(result='')}'")
        with pytest.raises(CliAdapterError, match="vacío"):
            ClaudeCliAdapter(binary=binario, cwd=str(tmp_path)).converse("S", "P", REG)

    def test_respuesta_normal_sigue_pasando(self, fake_bin, tmp_path):
        binario = fake_bin("ok", 'echo "mi postura"')
        out = GeminiCliAdapter(binary=binario, cwd=str(tmp_path)).converse("S", "P", REG)
        assert out == "mi postura"


class TestAntigravityArgv:
    """agy IGNORA el cwd del subprocess: sin --add-dir trabaja en su scratch y
    responde desde memoria en vez de leer el repo del debate (verificado en
    real: describía OTRO proyecto). El flag sostiene el anclaje al código."""

    def test_ancla_el_repo_con_add_dir(self, tmp_path):
        from devvating.adapters.cli import AntigravityCliAdapter

        argv = AntigravityCliAdapter(cwd=str(tmp_path)).build_argv("SYS", "PROMPT")
        i = argv.index("--add-dir")
        assert argv[i + 1] == str(tmp_path)

    def test_add_dir_es_ruta_absoluta(self, monkeypatch, tmp_path):
        # El subprocess hereda cwd, pero agy resuelve rutas contra SU scratch:
        # una relativa apuntaría a un directorio que no existe para él.
        from devvating.adapters.cli import AntigravityCliAdapter

        monkeypatch.chdir(tmp_path)
        argv = AntigravityCliAdapter(cwd=".").build_argv("SYS", "PROMPT")
        i = argv.index("--add-dir")
        assert argv[i + 1] == str(tmp_path)
        assert os.path.isabs(argv[i + 1])

    def test_conserva_print_timeout_acorde_al_timeout(self, tmp_path):
        from devvating.adapters.cli import AntigravityCliAdapter

        argv = AntigravityCliAdapter(cwd=str(tmp_path), timeout=600).build_argv("S", "P")
        i = argv.index("--print-timeout")
        assert argv[i + 1] == "540s"

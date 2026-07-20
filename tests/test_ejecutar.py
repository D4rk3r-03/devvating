"""CLI `devvating ejecutar`: la confirmación de consola para --verificar.

M9: el comando de 'verificacion' de .devvating.json viene del REPO OBJETIVO,
así que --verificar (y --yes) no bastan por sí solos — exigen una
confirmación de consola aparte (ejecutar.py:105-134). Sin este test, esa
defensa vive solo en código y comentario.
"""

from __future__ import annotations

import json

from rich.console import Console

from devvating import ejecutar
from devvating.executor import ExecutionOutcome


class _StubBackend:
    name = "stub-backend"

    def __init__(self, model: str | None = None) -> None:
        self.model = model or "sonnet"


class _StubExecutor:
    """Reemplaza al Executor real: no toca el repo ni corre subprocesos."""

    ultima_instancia: "_StubExecutor | None" = None

    def __init__(self, repo: str, backend, on_event=None) -> None:
        self.repo = repo
        self.backend = backend
        self.verify_command_recibido: str | None = "__no_llamado__"
        _StubExecutor.ultima_instancia = self

    def execute(self, plan, *, allow_commands=False, branch=None, verify_command=None,
                allow_open_decisions=False):
        self.verify_command_recibido = verify_command
        self.allow_open_decisions_recibido = allow_open_decisions
        return ExecutionOutcome(
            branch=branch or "devvating/test",
            backend=self.backend.name,
            returncode=0,
            backend_output="",
            diff="",
            changed_files=[],
        )


def _preparar(monkeypatch, respuestas: list[str]):
    """Aísla ejecutar.main(): backend y Executor stub, consola con respuestas fijas."""
    monkeypatch.setattr(ejecutar, "ClaudeCodeBackend", _StubBackend)
    monkeypatch.setattr(ejecutar, "Executor", _StubExecutor)
    _StubExecutor.ultima_instancia = None
    cola = iter(respuestas)
    monkeypatch.setattr(Console, "input", lambda self, *a, **k: next(cola))


def _plan_file(tmp_path):
    p = tmp_path / "plan.md"
    p.write_text("Añade una línea a hola.txt", encoding="utf-8")
    return p


def _con_verificacion(git_repo, comando: str = "pytest -q"):
    (git_repo / ".devvating.json").write_text(
        json.dumps({"verificacion": comando}), encoding="utf-8"
    )


def _transcript_con_decision(tmp_path, crucial=True):
    p = tmp_path / "t.json"
    p.write_text(json.dumps({
        "topic": {"prompt": "tema"}, "synthesis": "un plan",
        "decisiones": [{"pregunta": "¿A o B?", "crucial": crucial, "resuelta": False}],
    }), encoding="utf-8")
    return p


class TestGateDecisionesCLI:
    def test_bloquea_sin_flag_y_no_ejecuta(self, tmp_path, monkeypatch):
        _preparar(monkeypatch, respuestas=[])  # ni siquiera debe llegar a aprobar
        rc = ejecutar.main(
            ["--repo", str(tmp_path), "--from-transcript", str(_transcript_con_decision(tmp_path))]
        )
        assert rc == 1
        assert _StubExecutor.ultima_instancia is None  # nunca se ejecutó

    def test_con_flag_fuerza_y_pasa_allow_open_decisions(self, tmp_path, monkeypatch):
        _preparar(monkeypatch, respuestas=[])
        rc = ejecutar.main(
            ["--repo", str(tmp_path), "--from-transcript",
             str(_transcript_con_decision(tmp_path)), "--yes", "--allow-open-decisions"]
        )
        assert rc == 0
        assert _StubExecutor.ultima_instancia.allow_open_decisions_recibido is True

    def test_decision_no_crucial_no_bloquea(self, tmp_path, monkeypatch):
        _preparar(monkeypatch, respuestas=[])
        rc = ejecutar.main(
            ["--repo", str(tmp_path), "--yes", "--from-transcript",
             str(_transcript_con_decision(tmp_path, crucial=False))]
        )
        assert rc == 0 and _StubExecutor.ultima_instancia is not None


class TestConfirmacionDeVerificar:
    def test_yes_no_basta_para_verificar_exige_confirmacion_aparte(
        self, git_repo, tmp_path, monkeypatch
    ):
        _con_verificacion(git_repo)
        _preparar(monkeypatch, respuestas=["n"])  # rechaza SOLO la verificación
        rc = ejecutar.main(
            ["--repo", str(git_repo), "--plan-file", str(_plan_file(tmp_path)),
             "--yes", "--verificar"]
        )
        assert rc == 0
        assert _StubExecutor.ultima_instancia.verify_command_recibido is None

    def test_confirmando_aparte_pasa_el_comando_del_config(
        self, git_repo, tmp_path, monkeypatch
    ):
        _con_verificacion(git_repo, comando="pytest -q")
        _preparar(monkeypatch, respuestas=["y"])
        rc = ejecutar.main(
            ["--repo", str(git_repo), "--plan-file", str(_plan_file(tmp_path)),
             "--yes", "--verificar"]
        )
        assert rc == 0
        assert _StubExecutor.ultima_instancia.verify_command_recibido == "pytest -q"

    def test_aprobar_el_plan_no_implica_aprobar_la_verificacion(
        self, git_repo, tmp_path, monkeypatch
    ):
        _con_verificacion(git_repo)
        # Sin --yes: primera respuesta aprueba el plan (fase 3), segunda
        # rechaza la verificación (fase 5) — son preguntas distintas.
        _preparar(monkeypatch, respuestas=["y", "n"])
        rc = ejecutar.main(
            ["--repo", str(git_repo), "--plan-file", str(_plan_file(tmp_path)),
             "--verificar"]
        )
        assert rc == 0
        assert _StubExecutor.ultima_instancia.verify_command_recibido is None

    def test_sin_verificacion_en_config_no_pregunta_nada(
        self, git_repo, tmp_path, monkeypatch
    ):
        # Sin .devvating.json: no hay 'verificacion' que confirmar.
        _preparar(monkeypatch, respuestas=[])  # cualquier input() sería un error
        rc = ejecutar.main(
            ["--repo", str(git_repo), "--plan-file", str(_plan_file(tmp_path)),
             "--yes", "--verificar"]
        )
        assert rc == 0
        assert _StubExecutor.ultima_instancia.verify_command_recibido is None

    def test_sin_flag_verificar_no_pregunta_nada(self, git_repo, tmp_path, monkeypatch):
        _con_verificacion(git_repo)
        _preparar(monkeypatch, respuestas=[])  # sin --verificar no debe preguntar
        rc = ejecutar.main(
            ["--repo", str(git_repo), "--plan-file", str(_plan_file(tmp_path)), "--yes"]
        )
        assert rc == 0
        assert _StubExecutor.ultima_instancia.verify_command_recibido is None

"""Auditor de correspondencia (fase 5, D16): parser, veredicto y gate.

El auditor es la segunda línea tras `executor.correspondencia`: un agente de
roster en solo lectura que, con la carga de la prueba invertida, señala lo que
se hizo sin pedirlo y lo que se omitió. Fallback acordado: NO bloquear (JSON
roto es culpa del auditor, no evidencia de desvío)."""

from __future__ import annotations

from pathlib import Path

import pytest

from devvating.auditor import (
    Auditoria,
    ClaudeAuditBackend,
    auditar,
    bloquea,
    crear_auditor,
    parse_auditoria,
)
from devvating.executor import Executor, ExecutionPlan
from devvating import gitutil

PLAN = ExecutionPlan(text="Añade una línea a hola.txt", title="demo plan")

BLOQUE_DESVIADO = (
    'Revisé el diff.\n'
    '{"auditoria":{"veredicto":"desviado","no_pedido":[{"que":"tocó un log sin '
    'relación","cita":"«+mock_logicapp_datadog.log»"}],"omitido":[],'
    '"resumen":"tocó un .log que el plan no menciona"}}'
)
BLOQUE_CONFORME = (
    '{"auditoria":{"veredicto":"conforme","no_pedido":[],"omitido":[],'
    '"resumen":"el diff hace lo que el plan pidió"}}'
)


class TestParseAuditoria:
    def test_extrae_bloque_bien_formado_de_entre_prosa(self):
        crudo = parse_auditoria(BLOQUE_DESVIADO)
        assert crudo["veredicto"] == "desviado"
        assert crudo["no_pedido"][0]["que"].startswith("tocó un log")

    def test_bloque_ausente_da_none(self):
        assert parse_auditoria("El plan se aplicó, todo bien.") is None

    def test_json_roto_da_none(self):
        # Un CLI de terceros puede cortar el bloque a medias: fallback seguro.
        roto = '{"auditoria":{"veredicto":"desviado","no_pedido":['
        assert parse_auditoria(roto) is None

    def test_texto_vacio_da_none(self):
        assert parse_auditoria("") is None

    def test_auditoria_no_dict_da_none(self):
        assert parse_auditoria('{"auditoria":"sí"}') is None

    def test_el_caso_real_del_log(self):
        # El caso que motivó el auditor: plan de doc, ejecución que tocó un .log.
        crudo = parse_auditoria(BLOQUE_DESVIADO)
        assert crudo is not None and crudo["veredicto"] == "desviado"


class TestVeredictoNormalizado:
    def _auditar_con(self, salida: str, diff: str = ""):
        class Stub:
            name = "stub-aud"

            def run(self, prompt, cwd):
                return 0, salida

        return auditar(Stub(), "plan", diff, cwd=".")

    def test_conforme_no_bloquea(self):
        a = self._auditar_con(BLOQUE_CONFORME)
        assert a["veredicto"] == "conforme" and a["bloquea"] is False
        assert bloquea(a) is False

    def test_desviado_bloquea(self):
        a = self._auditar_con(BLOQUE_DESVIADO)
        assert a["veredicto"] == "desviado" and a["bloquea"] is True
        assert bloquea(a) is True

    def test_json_roto_queda_desconocido_y_no_bloquea(self):
        # El fallback ACORDADO: un auditor ilegible no es evidencia de desvío.
        a = self._auditar_con("no emití ningún bloque JSON")
        assert a["veredicto"] == "desconocido" and a["corrio"] is True
        assert bloquea(a) is False

    def test_veredicto_desconocido_del_modelo_no_bloquea(self):
        # Si el modelo inventa un veredicto fuera del vocabulario, no bloquea.
        raro = '{"auditoria":{"veredicto":"quizás","no_pedido":[],"omitido":[]}}'
        a = self._auditar_con(raro)
        assert a["veredicto"] == "desconocido" and bloquea(a) is False

    def test_localiza_la_cita_en_el_diff(self):
        diff = "+++ b/x\n+mock_logicapp_datadog.log añadido\n"
        a = self._auditar_con(BLOQUE_DESVIADO, diff=diff)
        assert a["no_pedido"][0]["cita_localizada"] is True

    def test_cita_ausente_del_diff_se_marca_no_localizada(self):
        a = self._auditar_con(BLOQUE_DESVIADO, diff="+++ b/otra\n+cosa distinta\n")
        # No invalida el hallazgo: solo avisa al vocero que no la pudo confirmar.
        assert a["no_pedido"][0]["cita_localizada"] is False
        assert a["veredicto"] == "desviado"  # el veredicto no cambia


class TestBloqueaPredicado:
    def test_none_no_bloquea(self):
        assert bloquea(None) is False
        assert bloquea({}) is False

    def test_solo_desviado_bloquea(self):
        assert bloquea({"veredicto": "desviado"}) is True
        assert bloquea({"veredicto": "conforme"}) is False
        assert bloquea({"veredicto": "desconocido"}) is False


class TestAuditoriaDataclass:
    def test_bloquea_solo_ante_desviado(self):
        assert Auditoria(veredicto="desviado").bloquea is True
        assert Auditoria(veredicto="conforme").bloquea is False
        assert Auditoria().bloquea is False  # default: desconocido


class TestCrearAuditor:
    def test_familia_claude_resuelve_a_backend_readonly(self):
        for nombre in ("", "claude", "claude-cli", "claude-code"):
            aud = crear_auditor(nombre)
            assert isinstance(aud, ClaudeAuditBackend)

    def test_nombre_no_soportado_es_error_de_config(self):
        # Protocolo 3: un auditor mal configurado se levanta, no se traga.
        with pytest.raises(ValueError, match="no soportado"):
            crear_auditor("antigravity")

    def test_backend_readonly_no_lleva_herramientas_de_escritura(self):
        argv = ClaudeAuditBackend().build_argv("plan")
        assert "Read,Glob,Grep" in argv
        assert "Write" not in " ".join(argv) and "Edit" not in " ".join(argv)
        assert "--dangerously-skip-permissions" not in argv


class StubAuditor:
    """Auditor stub: emite un bloque fijo (desviado por defecto)."""

    name = "stub-auditor"

    def __init__(self, salida: str = BLOQUE_DESVIADO) -> None:
        self.salida = salida
        self.visto_cwd = None

    def run(self, prompt, cwd):
        self.visto_cwd = cwd
        return 0, self.salida


class WriterBackend:
    name = "stub"

    def run(self, prompt, cwd, allow_commands):
        (Path(cwd) / "hola.txt").write_text("hola\nmundo\n", encoding="utf-8")
        return 0, "ok"


class TestIntegracionExecutor:
    def test_sin_auditor_no_audita(self, git_repo):
        out = Executor(str(git_repo), WriterBackend()).execute(PLAN)
        assert out.auditoria == {}

    def test_el_auditor_corre_sobre_el_worktree(self, git_repo):
        aud = StubAuditor()
        out = Executor(str(git_repo), WriterBackend()).execute(
            PLAN, auditor_backend=aud
        )
        # Auditó DENTRO del worktree aislado, no en el árbol del vocero.
        assert aud.visto_cwd == out.worktree
        assert out.auditoria["veredicto"] == "desviado"

    def test_veredicto_viaja_en_outcome_y_sidecar(self, git_repo):
        out = Executor(str(git_repo), WriterBackend()).execute(
            PLAN, auditor_backend=StubAuditor(BLOQUE_CONFORME)
        )
        assert out.auditoria["veredicto"] == "conforme"
        side = gitutil.leer_sidecar(out.worktree)
        assert side["auditoria"]["veredicto"] == "conforme"

    def test_emite_eventos_de_auditoria(self, git_repo):
        eventos = []
        Executor(str(git_repo), WriterBackend(),
                 on_event=lambda ev, val: eventos.append((ev, val))).execute(
            PLAN, auditor_backend=StubAuditor()
        )
        assert ("auditando", "stub-auditor") in eventos
        assert ("auditoria_lista", "desviado") in eventos

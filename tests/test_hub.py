"""Devvating Hub (M7): API, worker de debate y guardas — sin red ni claves."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from devvating.hub import _debate_worker, crear_app
from tests.conftest import StubAdapter


def _fabrica_stub(nombres, cfg, repo):
    a = StubAdapter("claude", ["A0", 'A1 {"convergencia": true}', "síntesis del hub"])
    b = StubAdapter("gemini", ["B0", 'B1 {"convergencia": true}'])
    return a, b


class _StreamStub(StubAdapter):
    """StubAdapter que declara streaming y emite su respuesta como un delta."""

    soporta_streaming = True

    def __init__(self, name, respuestas):
        super().__init__(name, respuestas)
        self.on_delta = None

    def converse(self, system, prompt, registry):
        out = super().converse(system, prompt, registry)
        if self.on_delta is not None:
            self.on_delta(out)
        return out


def _fabrica_streaming(nombres, cfg, repo):
    a = _StreamStub("claude", ["A0", 'A1 {"convergencia": true}', "síntesis del hub"])
    b = StubAdapter("gemini", ["B0", 'B1 {"convergencia": true}'])
    return a, b


_BLOQUE_DECISION = (
    '{"decisiones":[{"id":"d1","pregunta":"¿A o B?","opciones":["A","B"],'
    '"recomendada":"A","crucial":true,"contra":"sin contraargumento en el debate"}]}'
)


def _fabrica_con_decision(nombres, cfg, repo):
    a = StubAdapter("claude", ["A0", 'A1 {"convergencia": false}',
                               "## Plan propuesto\npasos\n\n" + _BLOQUE_DECISION])
    b = StubAdapter("gemini", ["B0", 'B1 {"convergencia": false}'])
    return a, b


CONFIG = {"tema": "¿tema del hub?", "agentes": ["claude-cli", "gemini-api"], "rounds": 1}


class TestWorker:
    def test_emite_eventos_fin_y_guarda_transcript(self, tmp_path):
        eventos: list[dict] = []
        _debate_worker({**CONFIG, "repo": str(tmp_path)}, eventos.append, _fabrica_stub)

        tipos = [e["tipo"] for e in eventos]
        assert tipos[-1] == "cerrado" and "fin" in tipos
        fin = next(e for e in eventos if e["tipo"] == "fin")
        assert fin["sintesis"] == "síntesis del hub" and fin["convergio"]
        # Persistencia idéntica a la CLI: transcript en <repo>/transcripts.
        guardado = tmp_path / "transcripts" / fin["transcript"]
        assert guardado.is_file()
        # Los eventos del orquestador viajan JSON-planos.
        assert any(e["tipo"] == "evento" and e["evento"] == "sintesis_fin" for e in eventos)

    def test_autodebate_aplica_sesgos_y_piso_de_rondas(self, tmp_path):
        # Par desambiguado (claude#1/#2): sin sesgos explícitos, el worker aplica
        # el par por defecto y exige 2 rondas antes de honrar la convergencia.
        def fabrica(nombres, cfg, repo):
            a = StubAdapter("claude#1",
                            ["A0", 'A1 {"convergencia": true}', 'A2 {"convergencia": true}', "s"])
            b = StubAdapter("claude#2", ["B0", 'B1 {"convergencia": true}', 'B2 {"convergencia": true}'])
            fabrica.a = a
            return a, b

        eventos: list[dict] = []
        _debate_worker(
            {"tema": "t", "agentes": ["claude-cli", "claude-cli"], "rounds": 2,
             "repo": str(tmp_path)},
            eventos.append, fabrica,
        )
        from devvating import roles
        # El sesgo audaz por defecto viaja en el system prompt de la propuesta.
        assert roles.SESGOS["audaz"] in fabrica.a.llamadas[0][0]
        # El piso de 2 rondas impidió el corte en la ronda 1 (eco).
        fin = next(e for e in eventos if e["tipo"] == "fin")
        assert fin["convergio"] and fin["ronda_convergencia"] == 2

    def test_emite_capacidades_de_streaming_por_agente(self, tmp_path):
        # El front lo usa para "en vivo" vs "sin vista en vivo". Los stubs no
        # declaran soporta_streaming → ambos False; con _StreamStub, claude True.
        eventos: list[dict] = []
        _debate_worker({**CONFIG, "repo": str(tmp_path)}, eventos.append, _fabrica_stub)
        cap = next(e for e in eventos if e["tipo"] == "capacidades")
        assert cap["streaming"] == {"claude": False, "gemini": False}

        eventos2: list[dict] = []
        _debate_worker({**CONFIG, "repo": str(tmp_path)}, eventos2.append, _fabrica_streaming)
        cap2 = next(e for e in eventos2 if e["tipo"] == "capacidades")
        assert cap2["streaming"] == {"claude": True, "gemini": False}
        # Y los deltas viajan como eventos "delta", solo del agente que emite.
        deltas = [e for e in eventos2 if e["tipo"] == "evento" and e["evento"] == "delta"]
        assert deltas and all(d["agente"] == "claude" for d in deltas)

    def test_fin_lleva_decisiones_y_estado(self, tmp_path):
        eventos: list[dict] = []
        _debate_worker({**CONFIG, "repo": str(tmp_path)}, eventos.append, _fabrica_con_decision)
        fin = next(e for e in eventos if e["tipo"] == "fin")
        assert fin["estado"] == "pendiente_decision"
        assert [d["id"] for d in fin["decisiones"]] == ["d1"]
        assert fin["decisiones"][0]["crucial"] is True
        assert "decisiones" not in fin["sintesis"]  # el bloque se despojó

    def test_par_invalido_emite_error_y_cierra(self, tmp_path):
        eventos: list[dict] = []
        _debate_worker(
            {"tema": "t", "agentes": ["claude-cli"], "repo": str(tmp_path)},
            eventos.append,
            lambda *a: (_ for _ in ()).throw(ValueError("par inválido")),
        )
        assert [e["tipo"] for e in eventos] == ["error", "cerrado"]
        assert "par inválido" in eventos[0]["mensaje"]


def _transcript_completo(carpeta, nombre):
    """Un debate de 1 ronda ya terminado, con una decisión resuelta."""
    carpeta.mkdir(exist_ok=True)
    (carpeta / nombre).write_text(json.dumps({
        "topic": {"prompt": "¿tema original?", "context_hint": ""},
        "turns": [
            {"round": 0, "phase": "propuesta", "agent": "claude", "text": "P claude", "verdict": None},
            {"round": 0, "phase": "propuesta", "agent": "gemini", "text": "P gemini", "verdict": None},
            {"round": 1, "phase": "replica", "agent": "claude", "text": "R claude", "verdict": "no"},
            {"round": 1, "phase": "replica", "agent": "gemini", "text": "R gemini", "verdict": "no"},
            {"round": 1, "phase": "sintesis", "agent": "claude", "text": "síntesis abierta", "verdict": None},
        ],
        "rounds_run": 1, "converged": False, "synthesizer": "claude",
        "decisiones": [{"id": "d1", "pregunta": "¿A o B?", "crucial": True,
                        "resuelta": True, "eleccion": "A (elegida por el vocero)"}],
    }, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def cliente(tmp_path):
    app = crear_app(repo=str(tmp_path), fabrica_par=_fabrica_stub)
    with TestClient(app) as c:
        c.headers["X-Devvating-CSRF"] = app.state.csrf_token
        yield c, tmp_path


class TestApi:
    def test_roster_y_estado(self, cliente):
        c, _ = cliente
        r = c.get("/api/roster").json()
        assert "kimi" in r["agentes"] and r["alias"]["agy"] == "antigravity"
        assert "audaz" in r["sesgos"] and "neutral" in r["sesgos"]
        assert c.get("/api/estado").json()["corriendo"] is False

    def test_lanzar_debate_completo_via_http(self, cliente):
        c, tmp = cliente
        assert c.post("/api/debates", json=CONFIG).status_code == 202
        for _ in range(100):  # el hilo con stubs termina en milisegundos
            if not c.get("/api/estado").json()["corriendo"]:
                break
            time.sleep(0.05)
        else:
            pytest.fail("el debate del hub no terminó")
        lista = c.get("/api/transcripts").json()["transcripts"]
        assert len(lista) == 1
        html = c.get(f"/api/transcripts/{lista[0]}/html")
        assert html.status_code == 200 and "¿tema del hub?" in html.text

    def test_deltas_se_difunden_pero_no_quedan_en_el_historial(self, tmp_path):
        # _difundir excluye los deltas del historial (transitorios): un cliente
        # que reconecta ve capacidades y los turnos finales, nunca los deltas.
        app = crear_app(repo=str(tmp_path), fabrica_par=_fabrica_streaming)
        with TestClient(app) as c:
            c.headers["X-Devvating-CSRF"] = app.state.csrf_token
            assert c.post("/api/debates", json=CONFIG).status_code == 202
            for _ in range(100):
                if not c.get("/api/estado").json()["corriendo"]:
                    break
                time.sleep(0.05)
            else:
                pytest.fail("el debate del hub no terminó")
            with c.websocket_connect("/ws") as ws:
                historial = ws.receive_json()
            assert historial["tipo"] == "historial"
            tipos_evento = [
                e.get("evento") for e in historial["eventos"] if e["tipo"] == "evento"
            ]
            assert "delta" not in tipos_evento          # transitorios, fuera
            assert "sintesis_fin" in tipos_evento        # el turno final, dentro
            assert any(e["tipo"] == "capacidades" for e in historial["eventos"])

    def test_resolver_decisiones_persiste_y_devuelve_pendientes(self, cliente):
        c, tmp = cliente
        nombre = "20260720-000000-x.json"
        carpeta = tmp / "transcripts"
        carpeta.mkdir(exist_ok=True)
        (carpeta / nombre).write_text(json.dumps({
            "topic": {"prompt": "t"}, "synthesis": "p",
            "decisiones": [
                {"id": "d1", "pregunta": "¿A o B?", "crucial": True, "resuelta": False, "eleccion": ""},
                {"id": "d2", "pregunta": "otra", "crucial": True, "resuelta": False, "eleccion": ""},
            ],
        }), encoding="utf-8")
        # Resuelvo solo d1 (elijo A); d2 sigue pendiente.
        r = c.post("/api/decisiones", json={
            "transcript": nombre,
            "decisiones": [{"id": "d1", "eleccion": "A", "resuelta": True}],
        })
        assert r.status_code == 200 and r.json()["pendientes"] == ["otra"]
        data = json.loads((carpeta / nombre).read_text(encoding="utf-8"))
        d1 = next(d for d in data["decisiones"] if d["id"] == "d1")
        assert d1["resuelta"] is True and d1["eleccion"] == "A"

    def test_desmarcar_crucial_desbloquea(self, cliente):
        c, tmp = cliente
        nombre = "20260720-000001-y.json"
        carpeta = tmp / "transcripts"
        carpeta.mkdir(exist_ok=True)
        (carpeta / nombre).write_text(json.dumps({
            "topic": {"prompt": "t"}, "synthesis": "p",
            "decisiones": [{"id": "d1", "pregunta": "¿A o B?", "crucial": True, "resuelta": False}],
        }), encoding="utf-8")
        # El vocero desmarca 'crucial': deja de bloquear aunque no la resuelva.
        r = c.post("/api/decisiones", json={
            "transcript": nombre, "decisiones": [{"id": "d1", "crucial": False}],
        })
        assert r.status_code == 200 and r.json()["pendientes"] == []

    def test_valida_tema_y_par(self, cliente):
        c, _ = cliente
        assert c.post("/api/debates", json={"tema": "", "agentes": ["a", "b"]}).status_code == 422
        assert c.post("/api/debates", json={"tema": "t", "agentes": ["a"]}).status_code == 422

    def test_solo_un_debate_a_la_vez(self, tmp_path):
        import threading

        arranca = threading.Event()

        def fabrica_lenta(nombres, cfg, repo):
            a = StubAdapter("claude", ["A0", 'A1 {"convergencia": true}', "s"])
            lenta = StubAdapter("gemini", ["B0", 'B1 {"convergencia": true}'])
            original = lenta.converse

            def frenada(*args, **kw):
                arranca.wait(timeout=5)
                return original(*args, **kw)

            lenta.converse = frenada
            return a, lenta

        app = crear_app(repo=str(tmp_path), fabrica_par=fabrica_lenta)
        with TestClient(app) as c:
            c.headers["X-Devvating-CSRF"] = app.state.csrf_token
            assert c.post("/api/debates", json=CONFIG).status_code == 202
            assert c.post("/api/debates", json=CONFIG).status_code == 409
            arranca.set()

    def test_transcripts_sin_traversal(self, cliente):
        c, _ = cliente
        assert c.get("/api/transcripts/..%2F..%2Fetc%2Fpasswd.json").status_code == 404
        assert c.get("/api/transcripts/no-existe.json").status_code == 404

    def test_websocket_recibe_historial_y_eventos(self, cliente):
        c, _ = cliente
        with c.websocket_connect("/ws") as ws:
            primero = ws.receive_json()
            assert primero["tipo"] == "historial"
            c.post("/api/debates", json=CONFIG)
            visto_fin = False
            for _ in range(60):
                msg = ws.receive_json()
                if msg["tipo"] == "fin":
                    visto_fin = True
                    assert msg["sintesis"] == "síntesis del hub"
                    break
            assert visto_fin

    def test_raiz_sin_dist_da_instrucciones(self, cliente):
        c, _ = cliente
        r = c.get("/")
        assert r.status_code == 200
        assert "npm run build" in r.text or "<div id=\"root\">" in r.text


class TestCsrf:
    """Paso 0 (auto-auditoría): POST mutante sin token válido se rechaza."""

    def test_roster_entrega_el_token(self, cliente):
        c, _ = cliente
        r = c.get("/api/roster").json()
        assert r["csrf_token"]

    def test_post_sin_token_es_403(self, tmp_path):
        app = crear_app(repo=str(tmp_path), fabrica_par=_fabrica_stub)
        with TestClient(app) as c:
            assert c.post("/api/debates", json=CONFIG).status_code == 403
            assert c.post("/api/debates/cancelar").status_code == 403
            assert c.post("/api/descartar").status_code == 403

    def test_post_con_token_equivocado_es_403(self, tmp_path):
        app = crear_app(repo=str(tmp_path), fabrica_par=_fabrica_stub)
        with TestClient(app) as c:
            c.headers["X-Devvating-CSRF"] = "no-soy-el-token"
            assert c.post("/api/debates", json=CONFIG).status_code == 403

    def test_post_con_token_correcto_pasa(self, tmp_path):
        app = crear_app(repo=str(tmp_path), fabrica_par=_fabrica_stub)
        with TestClient(app) as c:
            c.headers["X-Devvating-CSRF"] = app.state.csrf_token
            assert c.post("/api/debates", json=CONFIG).status_code == 202


class TestIntervencion:
    def test_flujo_completo_de_intervencion(self, tmp_path):
        """El debate espera la nota del vocero y la inyecta en la ronda."""
        def fabrica(nombres, cfg, repo):
            a = StubAdapter("claude", ["A0", 'A1 {"convergencia": true}', "s"])
            b = StubAdapter("gemini", ["B0", 'B1 {"convergencia": true}'])
            fabrica.a = a
            return a, b

        app = crear_app(repo=str(tmp_path), fabrica_par=fabrica)
        with TestClient(app) as c:
            c.headers["X-Devvating-CSRF"] = app.state.csrf_token
            # Sin intervención pendiente, el endpoint rechaza.
            assert c.post("/api/intervencion", json={"nota": "x"}).status_code == 409
            c.post("/api/debates", json={**CONFIG, "interactivo": True})
            for _ in range(100):
                if c.get("/api/estado").json()["intervencion_abierta"]:
                    break
                time.sleep(0.05)
            else:
                pytest.fail("nunca se abrió la intervención")
            assert c.post("/api/intervencion",
                          json={"nota": "ojo con el rendimiento"}).status_code == 200
            for _ in range(100):
                if not c.get("/api/estado").json()["corriendo"]:
                    break
                time.sleep(0.05)
        # La nota llegó al prompt de la réplica (mismo contrato que la CLI).
        assert any("ojo con el rendimiento" in p for _, p in fabrica.a.llamadas)

    def test_nota_vacia_continua_sin_nota(self, tmp_path):
        def fabrica(nombres, cfg, repo):
            a = StubAdapter("claude", ["A0", 'A1 {"convergencia": true}', "s"])
            b = StubAdapter("gemini", ["B0", 'B1 {"convergencia": true}'])
            fabrica.a = a
            return a, b

        app = crear_app(repo=str(tmp_path), fabrica_par=fabrica)
        with TestClient(app) as c:
            c.headers["X-Devvating-CSRF"] = app.state.csrf_token
            c.post("/api/debates", json={**CONFIG, "interactivo": True})
            for _ in range(100):
                if c.get("/api/estado").json()["intervencion_abierta"]:
                    break
                time.sleep(0.05)
            c.post("/api/intervencion", json={"nota": ""})
            for _ in range(100):
                if not c.get("/api/estado").json()["corriendo"]:
                    break
                time.sleep(0.05)
        assert not any("NOTA DEL VOCERO" in p for _, p in fabrica.a.llamadas)


class TestCancelar:
    def test_cancelar_sin_debate_es_409(self, tmp_path):
        app = crear_app(repo=str(tmp_path), fabrica_par=_fabrica_stub)
        with TestClient(app) as c:
            c.headers["X-Devvating-CSRF"] = app.state.csrf_token
            assert c.post("/api/debates/cancelar").status_code == 409

    def test_cancelar_debate_en_curso_emite_cancelado(self, tmp_path):
        import threading

        en_turno = threading.Event()

        class BloqueaEnPrimera(StubAdapter):
            def converse(self, system, prompt, registry):
                en_turno.set()          # avisa que estamos en el primer turno
                time.sleep(0.3)         # da tiempo a que llegue la cancelación
                return super().converse(system, prompt, registry)

        def fabrica(nombres, cfg, repo):
            a = BloqueaEnPrimera("claude", ["A0", 'A1 {"convergencia": true}', "s"])
            b = StubAdapter("gemini", ["B0", 'B1 {"convergencia": true}'])
            return a, b

        app = crear_app(repo=str(tmp_path), fabrica_par=fabrica)
        with TestClient(app) as c:
            c.headers["X-Devvating-CSRF"] = app.state.csrf_token
            with c.websocket_connect("/ws") as ws:
                ws.receive_json()  # historial
                c.post("/api/debates", json={
                    "tema": "t", "agentes": ["claude-cli", "gemini-api"], "rounds": 1,
                })
                assert en_turno.wait(timeout=5)  # el 1er agente ya está en turno
                assert c.post("/api/debates/cancelar").status_code == 200
                visto = False
                for _ in range(80):
                    msg = ws.receive_json()
                    if msg["tipo"] == "cancelado":
                        visto = True
                        break
                    if msg["tipo"] in ("fin", "error"):
                        pytest.fail(f"esperaba cancelado, llegó {msg['tipo']}")
                assert visto
        # El transcript parcial quedó guardado (reanudable).
        parciales = list((tmp_path / "transcripts").glob("*.partial.json"))
        assert len(parciales) == 1


class TestReanudar:
    def _guardar_parcial(self, repo):
        """Escribe un .partial.json con la apertura (round 0) ya pagada."""
        from dataclasses import asdict
        from devvating.orchestrator import DebateSession, DebateTopic, Turn

        sesion = DebateSession(
            topic=DebateTopic(prompt="¿tema reanudado?", context_hint=""),
            turns=[
                Turn(0, "propuesta", "claude", "A0 previo"),
                Turn(0, "propuesta", "gemini", "B0 previo"),
            ],
        )
        carpeta = repo / "transcripts"
        carpeta.mkdir(exist_ok=True)
        nombre = "20260101-000000-reanudar.partial.json"
        (carpeta / nombre).write_text(
            json.dumps(asdict(sesion), ensure_ascii=False), encoding="utf-8"
        )
        return nombre

    def test_reanuda_sin_repetir_turnos_pagados(self, tmp_path):
        def fabrica(nombres, cfg, repo):
            # Solo lo que FALTA: la apertura viene del parcial, no se re-corre.
            a = StubAdapter("claude", ['A1 {"convergencia": true}', "síntesis reanudada"])
            b = StubAdapter("gemini", ['B1 {"convergencia": true}'])
            fabrica.a = a
            return a, b

        nombre = self._guardar_parcial(tmp_path)
        app = crear_app(repo=str(tmp_path), fabrica_par=fabrica)
        with TestClient(app) as c:
            c.headers["X-Devvating-CSRF"] = app.state.csrf_token
            r = c.post("/api/debates", json={
                "agentes": ["claude-cli", "gemini-api"], "resume": nombre, "rounds": 1,
            })
            assert r.status_code == 202
            for _ in range(100):
                if not c.get("/api/estado").json()["corriendo"]:
                    break
                time.sleep(0.05)
            else:
                pytest.fail("el debate reanudado no terminó")

        # La apertura NO se re-pagó: claude solo hizo 2 turnos (réplica +
        # síntesis), no 3. Su primera llamada ya es la réplica, que usa "A0
        # previo" (la postura reusada del parcial) como postura actual.
        assert len(fabrica.a.llamadas) == 2
        assert "Da tu propuesta inicial" not in fabrica.a.llamadas[0][1]
        assert "A0 previo" in fabrica.a.llamadas[0][1]
        # El transcript final reúne la apertura reusada y la síntesis nueva.
        finales = [t for t in c.get("/api/transcripts").json()["transcripts"]
                   if not t.endswith(".partial.json")]
        data = c.get(f"/api/transcripts/{finales[0]}").json()
        textos = [t["text"] for t in data["turns"]]
        assert "A0 previo" in textos and data["synthesis"] == "síntesis reanudada"


class TestRamas:
    @staticmethod
    def _rama_con_commit(repo, nombre, archivo):
        from devvating import gitutil
        gitutil.create_branch(str(repo), nombre)
        (repo / archivo).write_text("x\n", encoding="utf-8")
        gitutil.stage_all(str(repo))
        gitutil.commit(str(repo), f"feat: {nombre}")
        gitutil.checkout(str(repo), "main")

    def test_lista_ramas_de_ejecucion_y_marca_la_actual(self, git_repo):
        self._rama_con_commit(git_repo, "devvating/uno", "a.py")
        app = crear_app(repo=str(git_repo))
        with TestClient(app) as c:
            c.headers["X-Devvating-CSRF"] = app.state.csrf_token
            data = c.get("/api/ramas").json()
        assert data["actual"] == "main"
        nombres = [r["nombre"] for r in data["ramas"]]
        assert "devvating/uno" in nombres
        assert all(r["actual"] is False for r in data["ramas"])  # ninguna es main

    def test_borrar_rama_de_ejecucion(self, git_repo):
        self._rama_con_commit(git_repo, "devvating/uno", "a.py")
        app = crear_app(repo=str(git_repo))
        with TestClient(app) as c:
            c.headers["X-Devvating-CSRF"] = app.state.csrf_token
            assert c.post("/api/ramas/borrar", json={"rama": "devvating/uno"}).status_code == 200
            nombres = [r["nombre"] for r in c.get("/api/ramas").json()["ramas"]]
            assert "devvating/uno" not in nombres

    def test_no_borra_ramas_ajenas(self, git_repo):
        app = crear_app(repo=str(git_repo))
        with TestClient(app) as c:
            c.headers["X-Devvating-CSRF"] = app.state.csrf_token
            assert c.post("/api/ramas/borrar", json={"rama": "main"}).status_code == 422
            assert c.post("/api/ramas/borrar",
                          json={"rama": "feature/x"}).status_code == 422

    def test_no_borra_la_rama_actual(self, git_repo):
        from devvating import gitutil
        gitutil.create_branch(str(git_repo), "devvating/actual")  # quedamos en ella
        app = crear_app(repo=str(git_repo))
        with TestClient(app) as c:
            c.headers["X-Devvating-CSRF"] = app.state.csrf_token
            assert c.post("/api/ramas/borrar",
                          json={"rama": "devvating/actual"}).status_code == 409


class _BackendEscritor:
    """Simula el agente headless de la fase 4 escribiendo en el repo."""

    name = "stub"

    def run(self, prompt, cwd, allow_commands):
        assert allow_commands is False  # salvaguarda del Hub: jamás comandos
        from pathlib import Path

        Path(cwd, "hola.txt").write_text("hola\nmundo-hub\n", encoding="utf-8")
        return 0, "ok"


class _BackendFallido:
    """Backend que escribe algo a medias y sale con código != 0."""

    name = "stub-fallido"

    def run(self, prompt, cwd, allow_commands):
        from pathlib import Path

        Path(cwd, "roto.txt").write_text("a medias\n", encoding="utf-8")
        return 1, "boom: el plan reventó"


class _BackendContador:
    """Como _BackendEscritor, pero cuenta llamadas (detecta la corrección de fase 5)."""

    name = "stub-contador"

    def __init__(self) -> None:
        self.llamadas = 0

    def run(self, prompt, cwd, allow_commands):
        from pathlib import Path

        self.llamadas += 1
        Path(cwd, "hola.txt").write_text("hola\nmundo-hub\n", encoding="utf-8")
        return 0, "ok"


class TestEjecucion:
    @staticmethod
    def _ignorar_transcripts(repo):
        """Como en un repo real: transcripts/ va al .gitignore (árbol limpio)."""
        import subprocess

        (repo / ".gitignore").write_text("transcripts/\n.devvating/\n", encoding="utf-8")
        subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-m", "ignorar transcripts"], cwd=repo,
                       check=True, capture_output=True)

    def _debatir(self, c):
        c.post("/api/debates", json=CONFIG)
        for _ in range(100):
            if not c.get("/api/estado").json()["corriendo"]:
                return
            time.sleep(0.05)
        pytest.fail("el debate no terminó")

    def test_ejecuta_la_sintesis_y_emite_el_diff(self, git_repo):
        self._ignorar_transcripts(git_repo)
        app = crear_app(repo=str(git_repo), fabrica_par=_fabrica_stub,
                        backend_ejecucion=_BackendEscritor())
        with TestClient(app) as c:
            c.headers["X-Devvating-CSRF"] = app.state.csrf_token
            self._debatir(c)
            nombre = c.get("/api/transcripts").json()["transcripts"][0]
            with c.websocket_connect("/ws") as ws:
                ws.receive_json()  # historial
                assert c.post("/api/ejecutar", json={"transcript": nombre}).status_code == 202
                fin = None
                for _ in range(60):
                    msg = ws.receive_json()
                    if msg["tipo"] == "ejecucion_fin":
                        fin = msg
                        break
                    if msg["tipo"] == "ejecucion_error":
                        pytest.fail(msg["mensaje"])
            assert fin and fin["rama"].startswith("devvating/")
            assert fin["archivos"] == ["hola.txt"] and "mundo-hub" in fin["diff"]

    def test_gate_bloquea_ejecutar_con_decision_crucial_y_permite_forzar(self, git_repo):
        self._ignorar_transcripts(git_repo)
        app = crear_app(repo=str(git_repo), fabrica_par=_fabrica_stub,
                        backend_ejecucion=_BackendEscritor())
        with TestClient(app) as c:
            c.headers["X-Devvating-CSRF"] = app.state.csrf_token
            nombre = "20260720-000000-con-decision.json"
            carpeta = git_repo / "transcripts"
            carpeta.mkdir(exist_ok=True)
            (carpeta / nombre).write_text(json.dumps({
                "topic": {"prompt": "tema"}, "synthesis": "un plan",
                "decisiones": [{"pregunta": "¿A o B?", "crucial": True, "resuelta": False}],
            }), encoding="utf-8")
            # Sin forzar: el gate devuelve 422 con la pregunta pendiente.
            r = c.post("/api/ejecutar", json={"transcript": nombre})
            assert r.status_code == 422
            assert "sin resolver" in r.json()["detail"] and "¿A o B?" in r.json()["detail"]
            # Con override explícito: pasa y ejecuta.
            with c.websocket_connect("/ws") as ws:
                ws.receive_json()  # historial
                r2 = c.post("/api/ejecutar",
                            json={"transcript": nombre, "forzar_decisiones": True})
                assert r2.status_code == 202
                for _ in range(60):
                    msg = ws.receive_json()
                    if msg["tipo"] == "ejecucion_fin":
                        break
                    if msg["tipo"] == "ejecucion_error":
                        pytest.fail(msg["mensaje"])

    def test_verificacion_de_devvating_json_no_corre_desde_el_hub(self, git_repo):
        # Alcance diferido (M9): el Hub no expone verify_command, a diferencia
        # de la CLI (ejecutar.py --verificar). Ancla el comportamiento actual
        # como deliberado, no como hueco de seguridad: un .devvating.json con
        # 'verificacion' configurada en el repo objetivo NUNCA dispara esa
        # fase desde el Hub, ni siquiera si el comando fallaría.
        (git_repo / ".devvating.json").write_text(
            json.dumps({"verificacion": "false"}), encoding="utf-8"
        )
        import subprocess
        subprocess.run(["git", "add", ".devvating.json"], cwd=git_repo, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-m", "config"], cwd=git_repo, check=True,
                       capture_output=True)
        self._ignorar_transcripts(git_repo)
        backend = _BackendContador()
        app = crear_app(repo=str(git_repo), fabrica_par=_fabrica_stub,
                        backend_ejecucion=backend)
        with TestClient(app) as c:
            c.headers["X-Devvating-CSRF"] = app.state.csrf_token
            fin = self._ejecutar_hasta_fin(c)
        # Si el Hub leyera y corriera 'verificacion', al fallar dispararía la
        # corrección acotada (fase 5) y el backend se invocaría una 2ª vez.
        assert backend.llamadas == 1
        assert fin["returncode"] == 0

    def _ejecutar_hasta_fin(self, c):
        """Debate + ejecuta con el backend escritor; devuelve el evento fin."""
        self._debatir(c)
        nombre = c.get("/api/transcripts").json()["transcripts"][0]
        with c.websocket_connect("/ws") as ws:
            ws.receive_json()
            c.post("/api/ejecutar", json={"transcript": nombre})
            for _ in range(60):
                msg = ws.receive_json()
                if msg["tipo"] == "ejecucion_fin":
                    return msg
                if msg["tipo"] == "ejecucion_error":
                    pytest.fail(msg["mensaje"])
        pytest.fail("la ejecución no terminó")

    def test_commit_confirma_en_la_rama(self, git_repo):
        import subprocess
        self._ignorar_transcripts(git_repo)
        app = crear_app(repo=str(git_repo), fabrica_par=_fabrica_stub,
                        backend_ejecucion=_BackendEscritor())
        with TestClient(app) as c:
            c.headers["X-Devvating-CSRF"] = app.state.csrf_token
            fin = self._ejecutar_hasta_fin(c)
            r = c.post("/api/commit", json={"mensaje": "feat: cambios del debate"})
            assert r.status_code == 200 and r.json()["sha"]
            # Segundo commit sin nueva ejecución: ya no hay nada pendiente.
            assert c.post("/api/commit", json={"mensaje": "otro"}).status_code == 409
        log = subprocess.run(["git", "log", "--oneline", fin["rama"]],
                             cwd=git_repo, capture_output=True, text=True).stdout
        assert "feat: cambios del debate" in log

    def test_commit_sin_mensaje_es_422(self, git_repo):
        self._ignorar_transcripts(git_repo)
        app = crear_app(repo=str(git_repo), fabrica_par=_fabrica_stub,
                        backend_ejecucion=_BackendEscritor())
        with TestClient(app) as c:
            c.headers["X-Devvating-CSRF"] = app.state.csrf_token
            self._ejecutar_hasta_fin(c)
            assert c.post("/api/commit", json={"mensaje": "  "}).status_code == 422

    def test_descartar_vuelve_a_la_base(self, git_repo):
        from devvating import gitutil
        self._ignorar_transcripts(git_repo)
        app = crear_app(repo=str(git_repo), fabrica_par=_fabrica_stub,
                        backend_ejecucion=_BackendEscritor())
        with TestClient(app) as c:
            c.headers["X-Devvating-CSRF"] = app.state.csrf_token
            self._ejecutar_hasta_fin(c)
            assert c.post("/api/descartar").status_code == 200
            assert c.post("/api/descartar").status_code == 409  # ya no hay nada
        assert gitutil.current_branch(str(git_repo)) == "main"
        assert gitutil.is_clean(str(git_repo))

    def test_commit_sin_ejecucion_es_409(self, git_repo):
        app = crear_app(repo=str(git_repo), fabrica_par=_fabrica_stub)
        with TestClient(app) as c:
            c.headers["X-Devvating-CSRF"] = app.state.csrf_token
            assert c.post("/api/commit", json={"mensaje": "x"}).status_code == 409

    def test_ejecutar_ignora_el_repo_del_cuerpo(self, git_repo, tmp_path):
        # Auto-auditoría (paso 3): el Hub sirve un solo repo; un `repo` arbitrario
        # en el cuerpo se ignora (no se aplica el plan en cualquier ruta del disco).
        self._ignorar_transcripts(git_repo)
        ajeno = tmp_path / "ajeno"
        ajeno.mkdir()  # ni siquiera es repo git
        app = crear_app(repo=str(git_repo), fabrica_par=_fabrica_stub,
                        backend_ejecucion=_BackendEscritor())
        with TestClient(app) as c:
            c.headers["X-Devvating-CSRF"] = app.state.csrf_token
            self._debatir(c)
            nombre = c.get("/api/transcripts").json()["transcripts"][0]
            with c.websocket_connect("/ws") as ws:
                ws.receive_json()
                c.post("/api/ejecutar", json={"transcript": nombre, "repo": str(ajeno)})
                for _ in range(60):
                    msg = ws.receive_json()
                    if msg["tipo"] == "ejecucion_fin":
                        assert msg["repo"] == str(git_repo)  # usó el servido
                        break
                    if msg["tipo"] == "ejecucion_error":
                        pytest.fail(msg["mensaje"])
        assert not (ajeno / "hola.txt").exists()  # no se tocó la ruta ajena

    def test_ejecucion_fallida_bloquea_commit_pero_permite_descartar(self, git_repo):
        # Hallazgo de la auto-auditoría: un returncode != 0 no debe presentarse
        # como éxito commiteable. El diff se muestra (para revisar), pero el
        # commit se bloquea; descartar sigue disponible.
        from devvating import gitutil
        self._ignorar_transcripts(git_repo)
        app = crear_app(repo=str(git_repo), fabrica_par=_fabrica_stub,
                        backend_ejecucion=_BackendFallido())
        with TestClient(app) as c:
            c.headers["X-Devvating-CSRF"] = app.state.csrf_token
            fin = self._ejecutar_hasta_fin(c)
            assert fin["returncode"] == 1  # el fallo viaja en el evento
            r = c.post("/api/commit", json={"mensaje": "no debería"})
            assert r.status_code == 409 and "falló" in r.json()["detail"]
            assert c.post("/api/descartar").status_code == 200  # descartar sí
        assert gitutil.current_branch(str(git_repo)) == "main"

    def test_repo_sucio_ya_no_bloquea_gracias_al_worktree(self, git_repo):
        # Antes, un árbol sucio bloqueaba la ejecución. Con el aislamiento por
        # worktree (D9 paso 2) ejecuta igual, y el trabajo sin confirmar del
        # vocero queda intacto (se aplicó en un worktree aparte, no en su árbol).
        self._ignorar_transcripts(git_repo)
        (git_repo / "hola.txt").write_text("trabajo del vocero\n", encoding="utf-8")
        app = crear_app(repo=str(git_repo), fabrica_par=_fabrica_stub,
                        backend_ejecucion=_BackendEscritor())
        with TestClient(app) as c:
            c.headers["X-Devvating-CSRF"] = app.state.csrf_token
            fin = self._ejecutar_hasta_fin(c)
            assert fin["returncode"] == 0 and fin["archivos"]  # ejecutó
        assert (git_repo / "hola.txt").read_text(encoding="utf-8") == "trabajo del vocero\n"

    def test_transcript_sin_sintesis_es_422(self, git_repo):
        import json as _json

        carpeta = git_repo / "transcripts"
        carpeta.mkdir()
        (carpeta / "vacio.json").write_text(_json.dumps({"synthesis": ""}), encoding="utf-8")
        app = crear_app(repo=str(git_repo), fabrica_par=_fabrica_stub)
        with TestClient(app) as c:
            c.headers["X-Devvating-CSRF"] = app.state.csrf_token
            r = c.post("/api/ejecutar", json={"transcript": "vacio.json"})
            assert r.status_code == 422


class TestCierrePlan:
    def test_reusa_turnos_y_corre_una_ronda_con_las_decisiones(self, tmp_path):
        # Fabrica del cierre: los turnos 0 y 1 se reusan del transcript; los
        # stubs solo responden la ronda NUEVA (réplica 2 + síntesis cerrada).
        cerrada = "## Plan propuesto\nplan cerrado\n\n" + '{"decisiones":[]}'

        def fabrica(nombres, cfg, repo):
            a = StubAdapter("claude", ['R2 A {"convergencia": true}', cerrada])
            b = StubAdapter("gemini", ['R2 B {"convergencia": true}', cerrada])
            fabrica.a, fabrica.b = a, b
            return a, b

        app = crear_app(repo=str(tmp_path), fabrica_par=fabrica)
        with TestClient(app) as c:
            c.headers["X-Devvating-CSRF"] = app.state.csrf_token
            nombre = "20260720-100000-original.json"
            _transcript_completo(tmp_path / "transcripts", nombre)
            antes = set(c.get("/api/transcripts").json()["transcripts"])
            r = c.post("/api/cerrar-plan",
                       json={"transcript": nombre, "agentes": ["claude-cli", "gemini-cli"]})
            assert r.status_code == 202
            for _ in range(100):
                if not c.get("/api/estado").json()["corriendo"]:
                    break
                time.sleep(0.05)
            else:
                pytest.fail("la ronda de cierre no terminó")

        # La decisión resuelta llegó como restricción a la ronda nueva.
        prompt_replica2 = fabrica.a.llamadas[0][1]
        assert "DECISIONES YA TOMADAS" in prompt_replica2
        assert "A (elegida por el vocero)" in prompt_replica2

        # Salió un transcript nuevo: 2 rondas, plan cerrado (sin decisiones).
        nuevos = set(c.get("/api/transcripts").json()["transcripts"]) - antes
        assert len(nuevos) == 1
        data = c.get(f"/api/transcripts/{nuevos.pop()}").json()
        assert data["rounds_run"] == 2
        assert data["synthesis"].startswith("## Plan propuesto")
        assert data["decisiones"] == [] and data["estado"] == "convergido"
        # Reusó los turnos viejos: la propuesta original sigue ahí.
        assert any(t["text"] == "P claude" for t in data["turns"])

    def test_transcript_sin_turnos_da_422(self, tmp_path):
        app = crear_app(repo=str(tmp_path), fabrica_par=_fabrica_stub)
        with TestClient(app) as c:
            c.headers["X-Devvating-CSRF"] = app.state.csrf_token
            nombre = "20260720-110000-vacio.json"
            carpeta = tmp_path / "transcripts"
            carpeta.mkdir(exist_ok=True)
            (carpeta / nombre).write_text(json.dumps(
                {"topic": {"prompt": "t"}, "turns": [], "rounds_run": 0}), encoding="utf-8")
            r = c.post("/api/cerrar-plan",
                       json={"transcript": nombre, "agentes": ["claude-cli", "gemini-cli"]})
            assert r.status_code == 422

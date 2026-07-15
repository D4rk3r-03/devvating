"""Devvating Hub (M7): API, worker de debate y guardas — sin red ni claves."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from devvating.hub import _debate_worker, crear_app
from tests.conftest import StubAdapter


def _fabrica_stub(nombres, cfg, repo):
    a = StubAdapter("claude", ["A0", "A1 [CONVERGENCIA: SÍ]", "síntesis del hub"])
    b = StubAdapter("gemini", ["B0", "B1 [CONVERGENCIA: SÍ]"])
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
                            ["A0", "A1 [CONVERGENCIA: SÍ]", "A2 [CONVERGENCIA: SÍ]", "s"])
            b = StubAdapter("claude#2", ["B0", "B1 [CONVERGENCIA: SÍ]", "B2 [CONVERGENCIA: SÍ]"])
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

    def test_par_invalido_emite_error_y_cierra(self, tmp_path):
        eventos: list[dict] = []
        _debate_worker(
            {"tema": "t", "agentes": ["claude-cli"], "repo": str(tmp_path)},
            eventos.append,
            lambda *a: (_ for _ in ()).throw(ValueError("par inválido")),
        )
        assert [e["tipo"] for e in eventos] == ["error", "cerrado"]
        assert "par inválido" in eventos[0]["mensaje"]


@pytest.fixture
def cliente(tmp_path):
    app = crear_app(repo=str(tmp_path), fabrica_par=_fabrica_stub)
    with TestClient(app) as c:
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

    def test_valida_tema_y_par(self, cliente):
        c, _ = cliente
        assert c.post("/api/debates", json={"tema": "", "agentes": ["a", "b"]}).status_code == 422
        assert c.post("/api/debates", json={"tema": "t", "agentes": ["a"]}).status_code == 422

    def test_solo_un_debate_a_la_vez(self, tmp_path):
        import threading

        arranca = threading.Event()

        def fabrica_lenta(nombres, cfg, repo):
            a = StubAdapter("claude", ["A0", "A1 [CONVERGENCIA: SÍ]", "s"])
            lenta = StubAdapter("gemini", ["B0", "B1 [CONVERGENCIA: SÍ]"])
            original = lenta.converse

            def frenada(*args, **kw):
                arranca.wait(timeout=5)
                return original(*args, **kw)

            lenta.converse = frenada
            return a, lenta

        app = crear_app(repo=str(tmp_path), fabrica_par=fabrica_lenta)
        with TestClient(app) as c:
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

class TestIntervencion:
    def test_flujo_completo_de_intervencion(self, tmp_path):
        """El debate espera la nota del vocero y la inyecta en la ronda."""
        def fabrica(nombres, cfg, repo):
            a = StubAdapter("claude", ["A0", "A1 [CONVERGENCIA: SÍ]", "s"])
            b = StubAdapter("gemini", ["B0", "B1 [CONVERGENCIA: SÍ]"])
            fabrica.a = a
            return a, b

        app = crear_app(repo=str(tmp_path), fabrica_par=fabrica)
        with TestClient(app) as c:
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
            a = StubAdapter("claude", ["A0", "A1 [CONVERGENCIA: SÍ]", "s"])
            b = StubAdapter("gemini", ["B0", "B1 [CONVERGENCIA: SÍ]"])
            fabrica.a = a
            return a, b

        app = crear_app(repo=str(tmp_path), fabrica_par=fabrica)
        with TestClient(app) as c:
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
            a = StubAdapter("claude", ["A1 [CONVERGENCIA: SÍ]", "síntesis reanudada"])
            b = StubAdapter("gemini", ["B1 [CONVERGENCIA: SÍ]"])
            fabrica.a = a
            return a, b

        nombre = self._guardar_parcial(tmp_path)
        app = crear_app(repo=str(tmp_path), fabrica_par=fabrica)
        with TestClient(app) as c:
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


class _BackendEscritor:
    """Simula el agente headless de la fase 4 escribiendo en el repo."""

    name = "stub"

    def run(self, prompt, cwd, allow_commands):
        assert allow_commands is False  # salvaguarda del Hub: jamás comandos
        from pathlib import Path

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
            self._ejecutar_hasta_fin(c)
            assert c.post("/api/commit", json={"mensaje": "  "}).status_code == 422

    def test_descartar_vuelve_a_la_base(self, git_repo):
        from devvating import gitutil
        self._ignorar_transcripts(git_repo)
        app = crear_app(repo=str(git_repo), fabrica_par=_fabrica_stub,
                        backend_ejecucion=_BackendEscritor())
        with TestClient(app) as c:
            self._ejecutar_hasta_fin(c)
            assert c.post("/api/descartar").status_code == 200
            assert c.post("/api/descartar").status_code == 409  # ya no hay nada
        assert gitutil.current_branch(str(git_repo)) == "main"
        assert gitutil.is_clean(str(git_repo))

    def test_commit_sin_ejecucion_es_409(self, git_repo):
        app = crear_app(repo=str(git_repo), fabrica_par=_fabrica_stub)
        with TestClient(app) as c:
            assert c.post("/api/commit", json={"mensaje": "x"}).status_code == 409

    def test_repo_sucio_reporta_error_amable(self, git_repo):
        self._ignorar_transcripts(git_repo)
        (git_repo / "hola.txt").write_text("sucio\n", encoding="utf-8")
        app = crear_app(repo=str(git_repo), fabrica_par=_fabrica_stub,
                        backend_ejecucion=_BackendEscritor())
        with TestClient(app) as c:
            self._debatir(c)
            nombre = c.get("/api/transcripts").json()["transcripts"][0]
            with c.websocket_connect("/ws") as ws:
                ws.receive_json()
                c.post("/api/ejecutar", json={"transcript": nombre})
                for _ in range(60):
                    msg = ws.receive_json()
                    if msg["tipo"] == "ejecucion_error":
                        assert "sin confirmar" in msg["mensaje"]
                        return
            pytest.fail("no llegó el error de árbol sucio")

    def test_transcript_sin_sintesis_es_422(self, git_repo):
        import json as _json

        carpeta = git_repo / "transcripts"
        carpeta.mkdir()
        (carpeta / "vacio.json").write_text(_json.dumps({"synthesis": ""}), encoding="utf-8")
        app = crear_app(repo=str(git_repo), fabrica_par=_fabrica_stub)
        with TestClient(app) as c:
            r = c.post("/api/ejecutar", json={"transcript": "vacio.json"})
            assert r.status_code == 422

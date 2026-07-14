"""Devvating Hub (M7, D6): la sala de debate en el navegador.

Servidor FastAPI local que expone el debate en vivo por websocket — otro
consumidor más de `on_event`, como manda el diseño: el motor no sabe que el
Hub existe. Reutiliza `reporte.render_html` para ver debates pasados y
`_save_transcript` para persistir igual que la CLI.

Uso:
    devvating hub [--port 8777] [--repo .]

Requiere el extra web:  pip install -e ".[hub]"
El front vive en devvating-ui/ (Vite + React); `npm run build` genera el
dist/ que este servidor sirve. Sin dist, sirve una página de instrucciones.
V1 sin intervención del vocero entre rondas (usa la CLI --interactivo para eso).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import queue
import re
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Callable

try:
    from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
    from fastapi.responses import FileResponse, HTMLResponse
    from fastapi.staticfiles import StaticFiles
except ImportError as exc:  # pragma: no cover - guard de instalación
    raise ImportError(
        "El Hub necesita el extra web. Instálalo con: pip install -e '.[hub]'"
    ) from exc

from . import agentes as banco
from . import reporte, rotation
from .config import Config
from .debate import _save_transcript
from .executor import ClaudeCodeBackend, ExecutionPlan, Executor, ExecutorError
from .orchestrator import DebateAbortedError, DebateTopic, Orchestrator

# La intervención del vocero espera hasta este tope y luego continúa sin nota
# (el debate no debe quedar rehén de una pestaña cerrada).
TIMEOUT_INTERVENCION = 300

_DIST = Path(__file__).resolve().parent.parent / "devvating-ui" / "dist"
_NOMBRE_TRANSCRIPT_RE = re.compile(r"^[\w\-. áéíóúñÁÉÍÓÚÑ¿?]+\.json$")

_SIN_DIST = """<!doctype html><html lang="es"><meta charset="utf-8">
<title>Devvating Hub</title>
<body style="font-family:system-ui;background:#0b0c10;color:#f3f4f6;
display:grid;place-items:center;height:100vh;margin:0">
<div style="max-width:34rem;line-height:1.7">
<h1>Devvating Hub</h1>
<p>El servidor está vivo, pero el front no está construido todavía:</p>
<pre style="background:#15161b;padding:1rem;border-radius:8px">cd devvating-ui
npm install
npm run build</pre>
<p>Luego recarga esta página. La API ya responde en <code>/api/roster</code>.</p>
</div></body></html>"""


def _debate_worker(
    config: dict,
    emitir: Callable[[dict], None],
    fabrica_par: Callable,
    esperar_nota: Callable[[int], str | None] | None = None,
) -> None:
    """Corre un debate completo en un hilo, emitiendo eventos JSON-planos.

    `esperar_nota(ronda)` es el puente de la intervención del vocero (D4,
    Fase 2 del plan del Hub): bloquea el hilo del debate hasta que el
    navegador responda o venza el timeout.
    """
    repo = config.get("repo", ".")
    cfg = Config.from_env()
    try:
        agente_a, agente_b = fabrica_par(config["agentes"], cfg, repo)
    except (ValueError, RuntimeError) as exc:
        emitir({"tipo": "error", "mensaje": str(exc)})
        emitir({"tipo": "cerrado"})
        return

    def on_event(evento: str, agente: str, texto: str | None) -> None:
        emitir({"tipo": "evento", "evento": evento, "agente": agente, "texto": texto})

    on_intervention = None
    if config.get("interactivo") and esperar_nota is not None:
        def on_intervention(ronda: int) -> str | None:
            emitir({"tipo": "intervencion_pendiente", "ronda": ronda,
                    "timeout": TIMEOUT_INTERVENCION})
            nota = esperar_nota(ronda)
            emitir({"tipo": "intervencion_resuelta", "ronda": ronda, "texto": nota})
            return nota

    orch = Orchestrator(agente_a, agente_b, repo_root=repo, on_event=on_event)
    topic = DebateTopic(prompt=config["tema"], context_hint=config.get("files", ""))
    estado_rotacion = rotation.load(repo)

    try:
        session = orch.run(
            topic,
            max_rounds=int(config.get("rounds", 2)),
            synthesizer_index=estado_rotacion.synthesizer_index(),
            deep_mode=bool(config.get("profundo", False)),
            on_intervention=on_intervention,
        )
    except DebateAbortedError as exc:
        parcial = None
        if exc.session.turns:
            parcial = _save_transcript(exc.session, repo, parcial=True).name
        emitir({
            "tipo": "error",
            "mensaje": str(exc.causa),
            "resets_at": getattr(exc.causa, "resets_at", None),
            "parcial": parcial,
        })
        emitir({"tipo": "cerrado"})
        return
    except Exception as exc:  # noqa: BLE001 — hilo: reportar, no morir mudo.
        emitir({"tipo": "error", "mensaje": f"{type(exc).__name__}: {exc}"})
        emitir({"tipo": "cerrado"})
        return

    rotation.save(repo, estado_rotacion.advanced())
    path = _save_transcript(session, repo)
    emitir({
        "tipo": "fin",
        "sintesis": session.synthesis,
        "sintetizador": session.synthesizer,
        "convergio": session.converged,
        "ronda_convergencia": session.converged_round,
        "usage": {k: asdict(v) for k, v in session.usage_totals.items()},
        "transcript": path.name,
    })
    emitir({"tipo": "cerrado"})


def _ejecucion_worker(
    config: dict, emitir: Callable[[dict], None], backend=None
) -> None:
    """Aplica un plan en una rama (fase 4) y emite el diff resultante.

    Salvaguardas del plan del Hub: SIEMPRE sin comandos (allow_commands es
    opt-in exclusivo de la CLI), el Executor exige árbol limpio y deja los
    cambios en staging — el Hub solo muestra; el commit es del vocero.
    """
    try:
        plan = ExecutionPlan(text=config["plan"], title=config.get("titulo", "plan"))
        ejecutor = Executor(
            config["repo"],
            backend or ClaudeCodeBackend(),
            on_event=lambda ev, val: emitir(
                {"tipo": "ejecucion_evento", "evento": ev, "valor": val}
            ),
        )
        resultado = ejecutor.execute(plan, allow_commands=False)
    except (ExecutorError, KeyError) as exc:
        mensaje = str(exc)
        if "sin confirmar" in mensaje:
            # Tropiezo común al debatir y ejecutar en el mismo repo: el
            # transcript recién guardado ensucia el árbol.
            mensaje += (
                " Pista: si lo único nuevo son artefactos del propio debate, "
                "añade 'transcripts/' y '.devvating/' al .gitignore del repo."
            )
        emitir({"tipo": "ejecucion_error", "mensaje": mensaje})
        emitir({"tipo": "ejecucion_cerrada"})
        return
    emitir({
        "tipo": "ejecucion_fin",
        "rama": resultado.branch,
        "returncode": resultado.returncode,
        "archivos": resultado.changed_files,
        "diff": resultado.diff,
    })
    emitir({"tipo": "ejecucion_cerrada"})


def crear_app(
    repo: str = ".",
    fabrica_par: Callable = banco.par,
    backend_ejecucion=None,
) -> FastAPI:
    app = FastAPI(title="Devvating Hub")
    app.state.historial = []          # eventos del debate en curso/último
    app.state.clientes = set()        # websockets conectados
    app.state.corriendo = False
    app.state.ejecutando = False
    app.state.cola = None
    # Puente de intervención (Fase 2): el hilo del debate espera aquí la
    # nota del vocero que llega por POST /api/intervencion.
    app.state.notas = queue.Queue()
    app.state.intervencion_abierta = False

    @app.on_event("startup")
    async def _arrancar() -> None:
        app.state.loop = asyncio.get_running_loop()
        app.state.cola = asyncio.Queue()
        asyncio.create_task(_difundir())

    async def _difundir() -> None:
        while True:
            msg = await app.state.cola.get()
            if msg.get("tipo") == "cerrado":
                app.state.corriendo = False
                continue
            if msg.get("tipo") == "ejecucion_cerrada":
                app.state.ejecutando = False
                continue
            app.state.historial.append(msg)
            for ws in set(app.state.clientes):
                try:
                    await ws.send_json(msg)
                except Exception:  # noqa: BLE001 — cliente ido: se limpia solo.
                    app.state.clientes.discard(ws)

    def _emitir(msg: dict) -> None:
        app.state.loop.call_soon_threadsafe(app.state.cola.put_nowait, msg)

    def _esperar_nota(ronda: int) -> str | None:
        """Bloquea el hilo del debate hasta la nota del vocero (o timeout)."""
        # Vaciar notas viejas de una intervención anterior abandonada.
        while not app.state.notas.empty():
            try:
                app.state.notas.get_nowait()
            except queue.Empty:
                break
        app.state.intervencion_abierta = True
        try:
            nota = app.state.notas.get(timeout=TIMEOUT_INTERVENCION)
        except queue.Empty:
            nota = None  # el debate no queda rehén de una pestaña cerrada
        finally:
            app.state.intervencion_abierta = False
        return nota or None

    # ------------------------------------------------------------------ API
    @app.get("/api/roster")
    def roster() -> dict:
        return {"agentes": banco.nombres(), "alias": dict(banco.ALIAS)}

    @app.get("/api/estado")
    def estado() -> dict:
        return {
            "corriendo": app.state.corriendo,
            "ejecutando": app.state.ejecutando,
            "intervencion_abierta": app.state.intervencion_abierta,
            "eventos": len(app.state.historial),
        }

    @app.post("/api/debates", status_code=202)
    def lanzar(config: dict) -> dict:
        if app.state.corriendo:
            raise HTTPException(409, "Ya hay un debate en curso (v1: uno a la vez).")
        tema = str(config.get("tema", "")).strip()
        agentes = config.get("agentes") or []
        if not tema:
            raise HTTPException(422, "Falta el tema del debate.")
        if len(agentes) != 2:
            raise HTTPException(422, "Elige exactamente 2 agentes del roster.")
        config = {**config, "tema": tema, "repo": config.get("repo") or repo}
        app.state.corriendo = True
        app.state.historial = []
        _emitir({"tipo": "inicio", "config": {
            "tema": tema, "agentes": agentes,
            "rounds": config.get("rounds", 2),
            "profundo": bool(config.get("profundo", False)),
            "interactivo": bool(config.get("interactivo", False)),
        }})
        threading.Thread(
            target=_debate_worker,
            args=(config, _emitir, fabrica_par, _esperar_nota),
            daemon=True,
        ).start()
        return {"ok": True}

    @app.post("/api/intervencion")
    def intervenir(cuerpo: dict) -> dict:
        """Recibe la nota del vocero (Fase 2). Nota vacía/null = continuar."""
        if not app.state.intervencion_abierta:
            raise HTTPException(409, "No hay ninguna intervención pendiente.")
        app.state.notas.put(str(cuerpo.get("nota") or "").strip())
        return {"ok": True}

    @app.post("/api/ejecutar", status_code=202)
    def ejecutar(cuerpo: dict) -> dict:
        """Fase 3: aplica la síntesis de un transcript en una rama del repo.

        Decisión del vocero pendiente en el plan → default conservador:
        el Hub se detiene en staging + diff; commit/descartar es manual.
        """
        if app.state.ejecutando:
            raise HTTPException(409, "Ya hay una ejecución en curso.")
        nombre = str(cuerpo.get("transcript", ""))
        data = json.loads(_ruta_transcript(nombre).read_text(encoding="utf-8"))
        plan = str(data.get("synthesis", "")).strip()
        if not plan:
            raise HTTPException(422, "El transcript no contiene una síntesis.")
        repo_objetivo = str(cuerpo.get("repo") or repo)
        app.state.ejecutando = True
        _emitir({"tipo": "ejecucion_inicio", "transcript": nombre, "repo": repo_objetivo})
        threading.Thread(
            target=_ejecucion_worker,
            args=(
                {"plan": plan, "repo": repo_objetivo,
                 "titulo": data.get("topic", {}).get("prompt", "plan")},
                _emitir,
                backend_ejecucion,
            ),
            daemon=True,
        ).start()
        return {"ok": True}

    def _dir_transcripts() -> Path:
        return Path(repo) / "transcripts"

    @app.get("/api/transcripts")
    def transcripts() -> dict:
        carpeta = _dir_transcripts()
        if not carpeta.is_dir():
            return {"transcripts": []}
        archivos = sorted(
            (p.name for p in carpeta.glob("*.json")), reverse=True
        )
        return {"transcripts": archivos}

    def _ruta_transcript(nombre: str) -> Path:
        if not _NOMBRE_TRANSCRIPT_RE.match(nombre):
            raise HTTPException(404, "Transcript no encontrado.")
        ruta = (_dir_transcripts() / nombre).resolve()
        if ruta.parent != _dir_transcripts().resolve() or not ruta.is_file():
            raise HTTPException(404, "Transcript no encontrado.")
        return ruta

    @app.get("/api/transcripts/{nombre}/html")
    def transcript_html(nombre: str) -> HTMLResponse:
        data = json.loads(_ruta_transcript(nombre).read_text(encoding="utf-8"))
        return HTMLResponse(reporte.render_html(data))

    @app.get("/api/transcripts/{nombre}")
    def transcript_json(nombre: str) -> dict:
        return json.loads(_ruta_transcript(nombre).read_text(encoding="utf-8"))

    # ------------------------------------------------------------ Websocket
    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        app.state.clientes.add(websocket)
        await websocket.send_json({
            "tipo": "historial",
            "eventos": app.state.historial,
            "corriendo": app.state.corriendo,
        })
        try:
            while True:
                await websocket.receive_text()  # keepalive; el cliente no manda nada
        except WebSocketDisconnect:
            app.state.clientes.discard(websocket)

    # ------------------------------------------------------------------ UI
    if _DIST.is_dir():
        app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/")
    def raiz():
        indice = _DIST / "index.html"
        if indice.is_file():
            return FileResponse(indice)
        return HTMLResponse(_SIN_DIST)

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="devvating hub", description="Sala de debate web local."
    )
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--repo", default=".", help="Raíz del repo a debatir.")
    args = parser.parse_args(argv)

    import uvicorn

    print(f"Devvating Hub → http://127.0.0.1:{args.port}  (repo: {args.repo})")
    uvicorn.run(crear_app(repo=args.repo), host="127.0.0.1", port=args.port,
                log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

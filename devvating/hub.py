"""Devvating Hub (M7, D6): la sala de debate en el navegador.

Servidor FastAPI local que expone el debate en vivo por websocket — otro
consumidor más de `on_event`, como manda el diseño: el motor no sabe que el
Hub existe. Reutiliza `reporte.render_html` para ver debates pasados y
`_save_transcript` para persistir igual que la CLI.

Uso:
    devvating hub [--port 8777] [--repo .] [--repo otro/proyecto ...]

Sirve uno o varios repositorios (fase B). El cliente los elige por un
`repo_id` opaco de la lista blanca que se da de alta AQUÍ, al arrancar: el
cuerpo de una petición nunca lleva rutas del disco (salvaguarda D9).

Requiere el extra web:  pip install -e ".[hub]"
El front vive en devvating-ui/ (Vite + React); `npm run build` genera el
dist/ que este servidor sirve. Sin dist, sirve una página de instrucciones.
V1 sin intervención del vocero entre rondas (usa la CLI --interactivo para eso).
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import hashlib
import json
import os
import queue
import re
import secrets
import shutil
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Callable

try:
    from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import FileResponse, HTMLResponse
    from fastapi.staticfiles import StaticFiles
except ImportError as exc:  # pragma: no cover - guard de instalación
    raise ImportError(
        "El Hub necesita el extra web. Instálalo con: pip install -e '.[hub]'"
    ) from exc

from . import agentes as banco
from . import gitutil, registro, reporte, roles, rotation
from .appconfig import ProjectConfig
from .auditor import bloquea as auditoria_bloquea
from .auditor import crear_auditor
from .config import Config
from .debate import _load_partial_session, _save_transcript
from .executor import (
    ClaudeCodeBackend,
    ExecutionPlan,
    Executor,
    ExecutorError,
    base_worktrees,
    decisiones_crucial_sin_resolver,
)
from .orchestrator import (
    DebateAbortedError,
    DebateCancelledError,
    DebateTopic,
    Orchestrator,
)

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


def _prompt_cierre(prompt_original: str, decisiones: list[dict]) -> str:
    """Tema de la ronda de cierre (F3): el original + las decisiones ya tomadas
    como restricciones fijas, para que el debate produzca un plan CERRADO."""
    tomadas = [
        f"- {d.get('pregunta', '')} → {d.get('eleccion', '')}"
        for d in decisiones
        if isinstance(d, dict) and str(d.get("eleccion") or "").strip()
    ]
    if not tomadas:
        return prompt_original
    return (
        f"{prompt_original}\n\n"
        "DECISIONES YA TOMADAS POR EL VOCERO (son restricciones FIJAS; no las "
        "rediscutas, intégralas al plan):\n" + "\n".join(tomadas) + "\n\n"
        "Produce un PLAN CERRADO que incorpore estas decisiones. No dejes "
        "decisiones abiertas salvo que surja una genuinamente nueva."
    )


def _debate_worker(
    config: dict,
    emitir: Callable[[dict], None],
    fabrica_par: Callable,
    esperar_nota: Callable[[int], str | None] | None = None,
    old_session=None,
    cancel_event=None,
) -> None:
    """Corre un debate completo en un hilo, emitiendo eventos JSON-planos.

    `esperar_nota(ronda)` es el puente de la intervención del vocero (D4,
    Fase 2 del plan del Hub): bloquea el hilo del debate hasta que el
    navegador responda o venza el timeout.

    `old_session` (reanudar): sesión parcial de un debate cortado; el
    orquestador reusa los turnos ya pagados y solo corre lo que falta.
    """
    repo = config.get("repo", ".")
    cfg = Config.from_env()
    try:
        agente_a, agente_b = fabrica_par(config["agentes"], cfg, repo)
    except (ValueError, RuntimeError) as exc:
        emitir({"tipo": "error", "mensaje": str(exc)})
        emitir({"tipo": "cerrado"})
        return

    # Capacidad de streaming por agente: el front la usa para decidir entre
    # mostrar los tokens en vivo o el estado explícito "sin vista en vivo"
    # (degradación por capacidad, no por configuración — plan del streaming).
    emitir({"tipo": "capacidades", "streaming": {
        agente_a.name: getattr(agente_a, "soporta_streaming", False),
        agente_b.name: getattr(agente_b, "soporta_streaming", False),
    }})

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

    autodebate = banco.es_autodebate(agente_a, agente_b)
    sesgos = [s for s in (config.get("sesgos") or []) if isinstance(s, str)]
    try:
        biases, _ = roles.resolver_biases(sesgos, autodebate)
    except ValueError as exc:
        emitir({"tipo": "error", "mensaje": str(exc)})
        emitir({"tipo": "cerrado"})
        return

    orch = Orchestrator(
        agente_a, agente_b, repo_root=repo, on_event=on_event, biases=biases
    )
    topic = DebateTopic(prompt=config["tema"], context_hint=config.get("files", ""))
    estado_rotacion = rotation.load(repo)
    rounds = int(config.get("rounds", 2))
    # La ronda de cierre (F3) fija min_rounds=rounds para forzar la ronda nueva
    # sin que una convergencia previa reusada corte antes de llegar a ella.
    min_rounds = config.get("min_rounds")
    min_rounds = int(min_rounds) if min_rounds is not None else (
        min(2, rounds) if autodebate else 1
    )

    try:
        session = orch.run(
            topic,
            max_rounds=rounds,
            min_rounds=min_rounds,
            synthesizer_index=estado_rotacion.synthesizer_index(),
            deep_mode=bool(config.get("profundo", False)),
            on_intervention=on_intervention,
            old_session=old_session,
            cancel_event=cancel_event,
        )
    except DebateCancelledError as exc:
        # Cancelación del vocero: corte limpio con transcript parcial reanudable.
        parcial = (
            _save_transcript(exc.session, repo, parcial=True).name
            if exc.session.turns else None
        )
        emitir({"tipo": "cancelado", "parcial": parcial})
        emitir({"tipo": "cerrado"})
        return
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
        # Decisiones que el vocero debe resolver para cerrar el plan (F2), y el
        # estado nominal (convergido / abierto / pendiente_decision).
        "decisiones": [asdict(d) for d in session.decisiones],
        "estado": session.estado,
    })
    emitir({"tipo": "cerrado"})


def _ejecucion_worker(
    config: dict, emitir: Callable[[dict], None], backend=None, auditor=None
) -> None:
    """Aplica un plan en un worktree aislado (fase 4) y emite el diff.

    Salvaguardas del plan del Hub: SIEMPRE sin comandos (allow_commands es
    opt-in exclusivo de la CLI); el Executor aísla la ejecución en un worktree
    desechable (D9 paso 2) y deja los cambios en staging ahí — el Hub solo
    muestra; el commit es del vocero.

    Alcance diferido (M9): no pasa `verify_command` a `Executor.execute` — la
    fase 5 (verificación) solo existe desde la CLI (`ejecutar.py --verificar`,
    con su confirmación aparte). Omisión deliberada, no hueco de seguridad:
    ver tests/test_hub.py::test_verificacion_de_devvating_json_no_corre_desde_el_hub.
    """
    try:
        plan = ExecutionPlan(
            text=config["plan"], title=config.get("titulo", "plan"),
            decisiones_pendientes=config.get("decisiones_pendientes", []),
        )
        ejecutor = Executor(
            config["repo"],
            backend or ClaudeCodeBackend(),
            on_event=lambda ev, val: emitir(
                {"tipo": "ejecucion_evento", "evento": ev, "valor": val}
            ),
        )
        resultado = ejecutor.execute(
            plan, allow_commands=False,
            auditor_backend=auditor,
            allow_open_decisions=config.get("forzar_decisiones", False),
        )
    except (ExecutorError, KeyError) as exc:
        emitir({"tipo": "ejecucion_error", "mensaje": str(exc)})
        emitir({"tipo": "ejecucion_cerrada"})
        return
    emitir({
        "tipo": "ejecucion_fin",
        "rama": resultado.branch,
        "rama_base": resultado.base_branch,
        "repo": config["repo"],
        "repo_id": config.get("repo_id", ""),
        "worktree": resultado.worktree,
        "returncode": resultado.returncode,
        "archivos": resultado.changed_files,
        "diff": resultado.diff,
        # Señal determinista plan↔diff: llega junto al diff, que es donde el
        # vocero decide. No bloquea (a diferencia del auditor).
        "correspondencia": resultado.correspondencia,
        # Veredicto del auditor por modelo (vacío si no se pidió). "desviado"
        # bloqueará el commit; el vocero puede forzar.
        "auditoria": resultado.auditoria,
    })
    emitir({"tipo": "ejecucion_cerrada"})


def id_de_repo(ruta: str) -> str:
    """Identificador opaco y estable de un repo, derivado de su nombre.

    Es lo ÚNICO que viaja por HTTP para referirse a un repositorio: el cuerpo
    de una petición nunca lleva rutas (D9), así que un id desconocido se
    rechaza en vez de resolverse contra el disco.
    """
    base = os.path.basename(os.path.abspath(ruta).rstrip(os.sep)) or "repo"
    limpio = re.sub(r"[^\w.-]+", "-", base).strip("-").lower()
    return limpio or "repo"


def _roster_de_repos(repo: str, repos: list[str] | dict[str, str] | None) -> dict[str, str]:
    """Lista blanca id → ruta absoluta. Se define FUERA del navegador.

    Decisión D3 del vocero (2026-07-22): el roster es autónomo y se da de alta
    solo por CLI, sin depender del índice global (que llega en la fase C). Ids
    repetidos se desambiguan con sufijo para no ocultar un repo con otro.
    """
    if isinstance(repos, dict):
        return {str(k): os.path.abspath(v) for k, v in repos.items()}
    rutas = list(repos) if repos else [repo]
    roster: dict[str, str] = {}
    for ruta in rutas:
        rid = id_de_repo(ruta)
        if rid in roster:
            n = 2
            while f"{rid}-{n}" in roster:
                n += 1
            rid = f"{rid}-{n}"
        roster[rid] = os.path.abspath(ruta)
    return roster


def crear_app(
    repo: str = ".",
    fabrica_par: Callable = banco.par,
    backend_ejecucion=None,
    auditor_ejecucion=None,
    repos: list[str] | dict[str, str] | None = None,
    raices: list[str] | None = None,
) -> FastAPI:
    app = FastAPI(title="Devvating Hub")
    # Repos servibles (fase B). Con uno solo el comportamiento es idéntico al
    # de antes: quien no mande `repo_id` opera sobre el primero.
    app.state.repos = _roster_de_repos(repo, repos)
    repo_default = next(iter(app.state.repos))
    # Raíces donde el Hub puede DESCUBRIR proyectos (D15). Se declaran al
    # arrancar: es lo único que sigue exigiendo consola, y una sola vez.
    # Vacío = sin descubrimiento, exactamente el comportamiento anterior.
    app.state.raices = [os.path.abspath(r) for r in (raices or [])]
    # cand_id → ruta, poblada por el escaneo. Único puente entre lo que el
    # navegador puede nombrar y el disco.
    app.state.candidatos = {}
    app.state.historial = []          # eventos del debate en curso/último
    app.state.clientes = set()        # websockets conectados
    app.state.corriendo = False
    app.state.ejecutando = False
    app.state.cola = None
    # Puente de intervención (Fase 2): el hilo del debate espera aquí la
    # nota del vocero que llega por POST /api/intervencion.
    app.state.notas = queue.Queue()
    app.state.intervencion_abierta = False
    # Señal de cancelación del debate en curso: la fija POST /api/debates/cancelar
    # y la leen el orquestador (entre turnos) y los adaptadores CLI (matan su
    # subprocess en vuelo). Se limpia al lanzar cada debate.
    app.state.cancelar_event = threading.Event()
    # Ejecuciones con cambios en staging a la espera de que el vocero decida
    # (commit o descartar), UNA POR REPO: con varios repos servidos, la
    # pendiente de uno no puede pisar la de otro. {} = nada pendiente.
    app.state.ejecuciones = {}
    # Token anti-CSRF (paso 0, auto-auditoría): sin él, cualquier página abierta
    # en el mismo navegador podría disparar POST /api/ejecutar (y el resto de
    # endpoints mutantes) contra localhost vía fetch. Se genera por proceso, se
    # entrega en /api/roster (que el front carga al montar) y un atacante
    # cross-origin no puede leerlo por la política de mismo origen — solo
    # puede disparar la petición, no leer la respuesta que lo contiene.
    app.state.csrf_token = secrets.token_urlsafe(32)

    def _requiere_csrf(request: Request) -> None:
        recibido = request.headers.get("X-Devvating-CSRF", "")
        if not secrets.compare_digest(recibido, app.state.csrf_token):
            raise HTTPException(403, "Falta o es inválido el token CSRF (X-Devvating-CSRF).")

    _csrf = Depends(_requiere_csrf)

    def _ruta_repo(repo_id: str | None = None) -> str:
        """Resuelve un `repo_id` contra la lista blanca. 404 si no está.

        Aquí vive la salvaguarda D9 ampliada a multi-repo: el cliente elige
        ENTRE los repos que el vocero registró al arrancar, nunca una ruta del
        sistema de archivos. Un id ausente cae al primero (comportamiento
        idéntico al del Hub de un solo repo).
        """
        rid = str(repo_id or repo_default)
        ruta = app.state.repos.get(rid)
        if ruta is None:
            raise HTTPException(
                404,
                f"Repositorio '{rid}' no está registrado en este Hub. "
                f"Disponibles: {', '.join(app.state.repos)}. "
                "Se dan de alta al arrancar (devvating hub --repo …), nunca por HTTP.",
            )
        return ruta

    def _rehidratar_ejecucion() -> None:
        """Recupera la ejecución pendiente tras un reinicio (Fase A del plan).

        No hay estado persistido en paralelo a git: un worktree bajo
        `devvating/` con cambios sin commitear ES la ejecución pendiente. Del
        sidecar solo sale lo que git no puede saber (`returncode`, rama base).

        Sin sidecar o sin `returncode` (el proceso murió a mitad), queda en
        None: `commit_cambios` compara `!= 0` y lo bloquea, así que el
        degradado conservador —solo descartar— sale gratis. Con varias
        pendientes se toma la más reciente; el resto siguen visibles en
        `/api/worktrees`.
        """
        for rid, ruta in app.state.repos.items():
            if not gitutil.is_git_repo(ruta):
                continue
            candidatas = []
            for w in gitutil.list_worktrees(ruta):
                if not w["existe"] or not w["tiene_cambios"]:
                    continue
                side = gitutil.leer_sidecar(w["path"]) or {}
                marca = side.get("terminado") or side.get("iniciado") or ""
                candidatas.append((marca, w, side))
            if not candidatas:
                continue
            candidatas.sort(key=lambda c: c[0], reverse=True)
            _, w, side = candidatas[0]
            app.state.ejecuciones[rid] = {
                "rama": w["rama"],
                "base": side.get("rama_base", ""),
                "repo": ruta,
                "worktree": w["path"],
                "returncode": side.get("returncode"),  # None => no se ofrece commit
                # El veredicto del auditor también sobrevive al reinicio: vive en
                # el sidecar, así que un "desviado" sigue bloqueando el commit.
                "auditoria": side.get("auditoria", {}),
            }

    @app.on_event("startup")
    async def _arrancar() -> None:
        app.state.loop = asyncio.get_running_loop()
        app.state.cola = asyncio.Queue()
        asyncio.create_task(_difundir())
        _rehidratar_ejecucion()

    async def _difundir() -> None:
        while True:
            msg = await app.state.cola.get()
            if msg.get("tipo") == "cerrado":
                app.state.corriendo = False
                continue
            if msg.get("tipo") == "ejecucion_cerrada":
                app.state.ejecutando = False
                continue
            if msg.get("tipo") == "ejecucion_fin":
                # Queda pendiente la decisión del vocero (commit/descartar).
                # Guardamos el returncode: si el backend falló, el commit se
                # bloquea (no presentar éxito sobre un plan roto — hallazgo de
                # la auto-auditoría; el descarte sigue disponible).
                app.state.ejecuciones[msg.get("repo_id", repo_default)] = {
                    "rama": msg["rama"], "base": msg.get("rama_base", ""),
                    "repo": msg["repo"], "worktree": msg.get("worktree", ""),
                    "returncode": msg.get("returncode", 0),
                    # Veredicto del auditor: "desviado" bloquea el commit salvo
                    # forzar. Vacío/ausente = no se auditó, no bloquea.
                    "auditoria": msg.get("auditoria", {}),
                }
            # Los deltas de streaming son transitorios (la vista final llega en
            # el evento *_fin con el texto ya despojado): se difunden a los
            # clientes conectados pero NO se guardan en el historial, o un
            # debate largo lo inflaría con miles de fragmentos y un cliente que
            # reconecta reproduciría turnos a medio escribir en vez del final.
            es_delta = msg.get("tipo") == "evento" and msg.get("evento") == "delta"
            if not es_delta:
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
        return {
            "agentes": banco.nombres(),
            "alias": dict(banco.ALIAS),
            "sesgos": list(roles.SESGOS),
            # El front lo guarda al montar y lo reenvía en cada POST mutante
            # (X-Devvating-CSRF). Ver _requiere_csrf.
            "csrf_token": app.state.csrf_token,
            # Repos servibles: el front solo puede elegir entre estos ids, y
            # se muestra la ruta para que el vocero sepa sobre qué opera.
            "repos": [
                {"id": rid, "ruta": ruta} for rid, ruta in app.state.repos.items()
            ],
            "repo_default": repo_default,
            # Con raíces declaradas, el front ofrece explorar y registrar
            # proyectos sin volver a la consola.
            "raices": list(app.state.raices),
        }

    @app.get("/api/estado")
    def estado() -> dict:
        return {
            "corriendo": app.state.corriendo,
            "ejecutando": app.state.ejecutando,
            "intervencion_abierta": app.state.intervencion_abierta,
            "eventos": len(app.state.historial),
        }

    @app.post("/api/debates", status_code=202, dependencies=[_csrf])
    def lanzar(config: dict) -> dict:
        if app.state.corriendo:
            raise HTTPException(409, "Ya hay un debate en curso (v1: uno a la vez).")
        agentes = config.get("agentes") or []
        if len(agentes) != 2:
            raise HTTPException(422, "Elige exactamente 2 agentes del roster.")

        # Reanudar (resume): el tema, las pistas y el modo profundo vienen del
        # transcript parcial; los agentes se re-eligen (los adaptadores no se
        # serializan). El orquestador reusa los turnos ya pagados.
        # El cuerpo elige un repo_id de la lista blanca, NUNCA una ruta: es la
        # salvaguarda D9 con varios repos servidos (ver _ruta_repo).
        repo_id = str(config.get("repo_id") or repo_default)
        repo_objetivo = _ruta_repo(repo_id)

        old_session = None
        resume = str(config.get("resume") or "").strip()
        if resume:
            old_session = _load_partial_session(str(_ruta_transcript(resume, repo_id)))
            config = {
                **config,
                "tema": old_session.topic.prompt,
                "files": old_session.topic.context_hint or config.get("files", ""),
                "profundo": old_session.deep_mode,
            }

        tema = str(config.get("tema", "")).strip()
        if not tema:
            raise HTTPException(422, "Falta el tema del debate.")
        config = {**config, "tema": tema, "repo": repo_objetivo}
        app.state.corriendo = True
        app.state.historial = []
        app.state.cancelar_event.clear()  # empezamos sin cancelación pendiente
        _emitir({"tipo": "inicio", "config": {
            "tema": tema, "agentes": agentes,
            "rounds": config.get("rounds", 2),
            "profundo": bool(config.get("profundo", False)),
            "interactivo": bool(config.get("interactivo", False)),
            "sesgos": [s for s in (config.get("sesgos") or []) if isinstance(s, str)],
            "reanudado": bool(resume),
            "repo_id": repo_id,
        }})
        threading.Thread(
            target=_debate_worker,
            args=(config, _emitir, fabrica_par, _esperar_nota, old_session,
                  app.state.cancelar_event),
            daemon=True,
        ).start()
        return {"ok": True}

    @app.post("/api/debates/cancelar", dependencies=[_csrf])
    def cancelar_debate() -> dict:
        """Cancela el debate en curso: corte limpio con transcript parcial.

        Fija la señal; el orquestador la ve entre turnos y los adaptadores CLI
        matan su subprocess en vuelo, así que la cancelación es inmediata sin
        esperar a que termine el turno actual.
        """
        if not app.state.corriendo:
            raise HTTPException(409, "No hay ningún debate en curso.")
        app.state.cancelar_event.set()
        return {"ok": True}

    @app.post("/api/intervencion", dependencies=[_csrf])
    def intervenir(cuerpo: dict) -> dict:
        """Recibe la nota del vocero (Fase 2). Nota vacía/null = continuar."""
        if not app.state.intervencion_abierta:
            raise HTTPException(409, "No hay ninguna intervención pendiente.")
        app.state.notas.put(str(cuerpo.get("nota") or "").strip())
        return {"ok": True}

    @app.post("/api/decisiones", dependencies=[_csrf])
    def resolver_decisiones(cuerpo: dict) -> dict:
        """Persiste en el transcript la resolución del vocero (F2).

        Por decisión: `eleccion` (opción elegida o texto propio), `resuelta`, y
        opcionalmente `crucial` (el vocero puede confirmar o desmarcar lo que el
        agente propuso). Devuelve las preguntas crucial que aún faltan — la misma
        verdad que usa el gate del executor.
        """
        nombre = str(cuerpo.get("transcript", ""))
        ruta = _ruta_transcript(nombre, cuerpo.get("repo_id"))
        data = json.loads(ruta.read_text(encoding="utf-8"))
        resoluciones = {
            str(r.get("id")): r
            for r in cuerpo.get("decisiones", [])
            if isinstance(r, dict) and r.get("id")
        }
        for d in data.get("decisiones", []):
            r = resoluciones.get(str(d.get("id")))
            if r is None:
                continue
            if "eleccion" in r:
                d["eleccion"] = str(r.get("eleccion") or "")
            d["resuelta"] = bool(r.get("resuelta", d.get("resuelta", False)))
            if "crucial" in r:  # el vocero confirma o desmarca lo crucial
                d["crucial"] = bool(r["crucial"])
        ruta.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "pendientes": decisiones_crucial_sin_resolver(data.get("decisiones"))}

    @app.post("/api/cerrar-plan", status_code=202, dependencies=[_csrf])
    def cerrar_plan(cuerpo: dict) -> dict:
        """Ronda de cierre (F3): re-sintetiza un debate con las decisiones ya
        resueltas inyectadas como restricciones fijas, para producir un plan
        cerrado. Es una reanudación (reusa los turnos pagados) con exactamente
        UNA ronda nueva; los agentes se re-eligen (no se serializan)."""
        if app.state.corriendo:
            raise HTTPException(409, "Ya hay un debate en curso (v1: uno a la vez).")
        agentes = cuerpo.get("agentes") or []
        if len(agentes) != 2:
            raise HTTPException(422, "Elige exactamente 2 agentes del roster.")
        nombre = str(cuerpo.get("transcript", ""))
        repo_id = str(cuerpo.get("repo_id") or repo_default)
        ruta = _ruta_transcript(nombre, repo_id)
        data = json.loads(ruta.read_text(encoding="utf-8"))
        old_session = _load_partial_session(str(ruta))
        if not old_session.turns:
            raise HTTPException(422, "El transcript no tiene turnos que reanudar.")
        ronda_cierre = old_session.rounds_run + 1
        tema = _prompt_cierre(old_session.topic.prompt, data.get("decisiones") or [])
        config = {
            "tema": tema, "agentes": agentes, "repo": _ruta_repo(repo_id),
            "rounds": ronda_cierre, "min_rounds": ronda_cierre, "profundo": False,
            "sesgos": [s for s in (cuerpo.get("sesgos") or []) if isinstance(s, str)],
            "files": old_session.topic.context_hint or "",
        }
        app.state.corriendo = True
        app.state.historial = []
        app.state.cancelar_event.clear()
        _emitir({"tipo": "inicio", "config": {
            "tema": tema, "agentes": agentes, "rounds": ronda_cierre,
            "profundo": False, "interactivo": False, "sesgos": config["sesgos"],
            "reanudado": True, "cierre": True, "repo_id": repo_id,
        }})
        threading.Thread(
            target=_debate_worker,
            args=(config, _emitir, fabrica_par, _esperar_nota, old_session,
                  app.state.cancelar_event),
            daemon=True,
        ).start()
        return {"ok": True}

    @app.post("/api/ejecutar", status_code=202, dependencies=[_csrf])
    def ejecutar(cuerpo: dict) -> dict:
        """Fase 3: aplica la síntesis de un transcript en una rama del repo.

        Decisión del vocero pendiente en el plan → default conservador:
        el Hub se detiene en staging + diff; commit/descartar es manual.
        """
        if app.state.ejecutando:
            raise HTTPException(409, "Ya hay una ejecución en curso.")
        nombre = str(cuerpo.get("transcript", ""))
        repo_id = str(cuerpo.get("repo_id") or repo_default)
        data = json.loads(_ruta_transcript(nombre, repo_id).read_text(encoding="utf-8"))
        plan = str(data.get("synthesis", "")).strip()
        if not plan:
            raise HTTPException(422, "El transcript no contiene una síntesis.")
        # Gate de decisiones (misma verdad que el Executor, traducida a 422): no
        # ejecutar en seco un plan con una decisión crucial abierta. El vocero
        # puede forzar (opt-in), análogo a --allow-open-decisions del CLI.
        pendientes = decisiones_crucial_sin_resolver(data.get("decisiones"))
        forzar = bool(cuerpo.get("forzar_decisiones"))
        if pendientes and not forzar:
            raise HTTPException(
                422,
                "El plan tiene decisiones cruciales sin resolver; ciérralas antes "
                "de ejecutar (o fuerza bajo tu riesgo). Pendientes: "
                + "; ".join(p for p in pendientes if p),
            )
        # Confinado a los repos REGISTRADOS: el cuerpo elige un id de la lista
        # blanca, nunca una ruta del disco (agujero de escritura remota vía
        # navegador — auto-auditoría D9, ampliada a multi-repo en la fase B).
        repo_objetivo = _ruta_repo(repo_id)
        # Auditoría de fase 5 (D16): opt-in por ejecución (`auditar: true`), como
        # `--verificar` en la CLI. El agente auditor sale de `.devvating.json` del
        # repo OBJETIVO (no del cuerpo: el navegador no nombra al auditor, solo
        # pide que se audite). Es read-only, así que —a diferencia de la
        # verificación, que corre un comando del repo— no exige un ritual de
        # confirmación aparte. Un `auditoria` mal configurado es error de config
        # (422), no un fallback silencioso.
        auditor = None
        if bool(cuerpo.get("auditar")):
            nombre_auditor = ProjectConfig.load(repo_objetivo).auditoria
            if not nombre_auditor:
                raise HTTPException(
                    422,
                    "Pediste auditar, pero '.devvating.json' del repo no trae "
                    "'auditoria' (nombre del agente auditor).",
                )
            try:
                # `auditor_ejecucion` inyecta un backend en tests; en real se
                # resuelve el nombre del roster a un auditor read-only.
                auditor = auditor_ejecucion or crear_auditor(nombre_auditor)
            except ValueError as exc:
                raise HTTPException(422, str(exc))
        app.state.ejecutando = True
        _emitir({"tipo": "ejecucion_inicio", "transcript": nombre,
                 "repo": repo_objetivo, "repo_id": repo_id})
        threading.Thread(
            target=_ejecucion_worker,
            args=(
                {"plan": plan, "repo": repo_objetivo, "repo_id": repo_id,
                 "titulo": data.get("topic", {}).get("prompt", "plan"),
                 "decisiones_pendientes": pendientes, "forzar_decisiones": forzar},
                _emitir,
                backend_ejecucion,
                auditor,
            ),
            daemon=True,
        ).start()
        return {"ok": True}

    def _repo_id_de_ruta(ruta: str) -> str | None:
        """`repo_id` del roster para una ruta absoluta, o None si no se sirve.

        El índice global abarca TODA la máquina, incluidos repos que este Hub
        no tiene registrados. Sus debates se muestran igual —es la vista
        global que se pidió— pero sin acciones: no se opera sobre lo que no
        está en la lista blanca (D9/D12).
        """
        objetivo = os.path.abspath(ruta)
        for rid, r in app.state.repos.items():
            if os.path.abspath(r) == objetivo:
                return rid
        return None

    def _cand_id(ruta: str) -> str:
        """Identificador opaco de un directorio candidato.

        Determinista (hash de la ruta) para que sobreviva a reescaneos, pero
        eso NO es lo que lo hace seguro: la seguridad está en que la tabla
        `app.state.candidatos` solo contiene rutas que el servidor descubrió y
        validó bajo una raíz declarada. Adivinar el hash de `/etc` no sirve de
        nada porque `/etc` nunca entró en la tabla.
        """
        return hashlib.sha256(os.path.abspath(ruta).encode()).hexdigest()[:16]

    def _bajo_alguna_raiz(ruta: str) -> bool:
        """True si `ruta` está realmente dentro de una raíz declarada.

        Con `realpath` en ambos lados y `commonpath`: así un symlink dentro del
        workspace que apunte fuera no pasa el filtro (D9 sigue en pie aunque
        ahora el Hub descubra directorios).
        """
        real = os.path.realpath(ruta)
        for raiz in app.state.raices:
            r = os.path.realpath(raiz)
            try:
                if os.path.commonpath([real, r]) == r:
                    return True
            except ValueError:      # unidades distintas: no comparables
                continue
        return False

    def _escanear_candidatos() -> list[dict]:
        """Directorios bajo las raíces declaradas, hasta 2 niveles.

        Dos niveles porque los proyectos cuelgan de la raíz o de una carpeta
        temática (`TRABAJO/CLIENTE/proyecto`). Rellena la tabla que traduce
        `cand_id` → ruta, único puente entre el navegador y el disco.
        """
        vistos: dict[str, dict] = {}
        for raiz in app.state.raices:
            base = os.path.realpath(raiz)
            if not os.path.isdir(base):
                continue
            for nivel in (1, 2):
                patron = os.path.join(base, *(["*"] * nivel))
                for ruta in sorted(glob.glob(patron)):
                    if not os.path.isdir(ruta) or os.path.basename(ruta).startswith("."):
                        continue
                    real = os.path.realpath(ruta)
                    if not _bajo_alguna_raiz(real) or real in vistos:
                        continue
                    # No descender dentro de un REPOSITORIO ya visto: sus
                    # carpetas internas no son proyectos (verificado en real:
                    # aparecían docs/, tests/, src/ de cada repositorio). Sí se
                    # desciende bajo una carpeta contenedora sin git, que es
                    # como cuelgan muchos proyectos (TRABAJO/CLIENTE/proyecto).
                    if any(real.startswith(v + os.sep) and datos["es_repo"]
                           for v, datos in vistos.items()):
                        continue
                    es_repo = gitutil.es_raiz_de_repo(real)
                    vistos[real] = {
                        "cand_id": _cand_id(real),
                        "nombre": os.path.basename(real),
                        "ruta": real,
                        "es_repo": es_repo,
                        "tiene_commits": gitutil.tiene_commits(real) if es_repo else False,
                        "registrado": any(
                            os.path.abspath(r) == real for r in app.state.repos.values()
                        ),
                    }
        app.state.candidatos = {c["cand_id"]: c["ruta"] for c in vistos.values()}
        # Lo listo para usar primero: repos con commits sin registrar, luego lo
        # que necesita `git init`, y al final lo que ya se sirve.
        def orden(c: dict) -> tuple:
            if c["registrado"]:
                return (3, c["nombre"].lower())
            if c["es_repo"] and c["tiene_commits"]:
                return (0, c["nombre"].lower())
            return (1 if c["es_repo"] else 2, c["nombre"].lower())

        return sorted(vistos.values(), key=orden)

    def _ruta_candidata(cand_id: str) -> str:
        """Traduce un `cand_id` a su ruta. 404 si no está en la tabla.

        Misma forma que `_ruta_repo`: el cliente no describe una ubicación,
        elige una que el servidor ya validó.
        """
        ruta = app.state.candidatos.get(str(cand_id))
        if ruta is None:
            raise HTTPException(
                404, "Candidato desconocido. Vuelve a explorar: los ids salen "
                     "del escaneo del servidor, no se construyen a mano.")
        if not _bajo_alguna_raiz(ruta):   # cinturón y tirantes ante un symlink nuevo
            raise HTTPException(403, "Esa ruta ya no está bajo ninguna raíz declarada.")
        return ruta

    @app.get("/api/candidatos")
    def candidatos() -> dict:
        """Proyectos que se pueden registrar, descubiertos bajo `--raiz`."""
        if not app.state.raices:
            return {"candidatos": [], "raices": [], "aviso":
                    "Arranca el Hub con --raiz <directorio> para poder explorar "
                    "y registrar proyectos desde aquí."}
        return {"candidatos": _escanear_candidatos(),
                "raices": list(app.state.raices)}

    @app.post("/api/repos", dependencies=[_csrf])
    def registrar_repo(cuerpo: dict) -> dict:
        """Da de alta un repositorio en caliente, por `cand_id`.

        El cuerpo nunca trae una ruta: trae un id de la tabla que el servidor
        construyó al escanear bajo las raíces declaradas (D9 intacta).
        """
        ruta = _ruta_candidata(str(cuerpo.get("cand_id", "")))
        if not gitutil.is_git_repo(ruta):
            raise HTTPException(
                422, f"'{os.path.basename(ruta)}' aún no es un repositorio git. "
                     "Inicialízalo primero (POST /api/repos/init).")
        for rid, r in app.state.repos.items():
            if os.path.abspath(r) == os.path.abspath(ruta):
                return {"ok": True, "repo_id": rid, "ya_estaba": True}
        rid = id_de_repo(ruta)
        n = 2
        while rid in app.state.repos:
            rid, n = f"{id_de_repo(ruta)}-{n}", n + 1
        app.state.repos[rid] = os.path.abspath(ruta)
        registro.reindexar([ruta])   # sus debates entran al índice global
        return {"ok": True, "repo_id": rid, "ruta": ruta, "ya_estaba": False}

    @app.post("/api/repos/init", dependencies=[_csrf])
    def init_repo(cuerpo: dict) -> dict:
        """`git init` + commit inicial en un candidato, y lo registra.

        Las guardas de contenido viven en `gitutil.init_inicial` (no vacío, no
        anidado, y rechazo si hay secretos sin `.gitignore`); aquí solo se
        añade el confinamiento a las raíces.
        """
        ruta = _ruta_candidata(str(cuerpo.get("cand_id", "")))
        try:
            sha = gitutil.init_inicial(ruta)
        except RuntimeError as exc:
            raise HTTPException(422, str(exc))
        rid = id_de_repo(ruta)
        n = 2
        while rid in app.state.repos:
            rid, n = f"{id_de_repo(ruta)}-{n}", n + 1
        app.state.repos[rid] = os.path.abspath(ruta)
        return {"ok": True, "repo_id": rid, "ruta": ruta, "sha": sha}

    @app.get("/api/historial")
    def historial_global(limite: int = 40) -> dict:
        """Todo lo debatido en esta máquina (D13), no solo en los repos servidos."""
        filas = registro.listar(limite=limite)
        for f in filas:
            f["repo_id"] = _repo_id_de_ruta(f["repo"])
            f["proyecto"] = os.path.basename(f["repo"])
            # Accionable = está en el roster Y su transcript sigue en disco.
            f["accionable"] = bool(f["repo_id"]) and f["existe"]
        return {
            "debates": filas,
            "coste_total": round(sum(f["coste"] or 0 for f in filas), 4),
            "db": registro.ruta_db(),
        }

    @app.post("/api/historial/reindexar", dependencies=[_csrf])
    def reindexar_historial() -> dict:
        """Reconstruye el índice desde los transcripts de los repos servidos.

        Solo recorre el roster: reindexar una ruta cualquiera del disco sería
        justo lo que D9 impide. Para incluir otro proyecto, se registra al
        arrancar el Hub.
        """
        indexados, saltados = registro.reindexar(list(app.state.repos.values()))
        return {"ok": True, "indexados": indexados, "saltados": saltados}

    @app.get("/api/pendientes")
    def pendientes() -> dict:
        """Todo lo que espera una decisión tuya, junto y accionable.

        Reúne en una sola lista lo que hasta ahora había que ir a buscar a
        sitios distintos (o a la consola): debates cortados a medias,
        decisiones cruciales sin resolver, ejecuciones esperando commit y
        ramas con trabajo que nadie fusionó.
        """
        items: list[dict] = []

        for f in registro.listar(limite=200):
            rid = _repo_id_de_ruta(f["repo"])
            if not rid or not f["existe"]:
                continue  # no servido o borrado: no hay acción que ofrecer
            base = {
                "repo_id": rid, "proyecto": os.path.basename(f["repo"]),
                "transcript": os.path.basename(f["transcript"]),
                "tema": f["tema"], "fecha": f["fecha"],
            }
            if f["parcial"]:
                items.append({**base, "tipo": "debate_a_medias",
                              "detalle": "quedó cortado; se puede reanudar"})
            elif f["decisiones_abiertas"]:
                items.append({**base, "tipo": "decisiones",
                              "cuantas": f["decisiones_abiertas"],
                              "detalle": f"{f['decisiones_abiertas']} decisión(es) "
                                         "crucial(es) sin resolver"})

        for rid, ruta in app.state.repos.items():
            proyecto = os.path.basename(ruta)
            ue = app.state.ejecuciones.get(rid)
            if ue:
                items.append({
                    "tipo": "ejecucion", "repo_id": rid, "proyecto": proyecto,
                    "rama": ue["rama"],
                    "detalle": "cambios en staging esperando commit o descarte",
                })
            if not gitutil.is_git_repo(ruta):
                continue
            for rama in gitutil.ramas_sin_fusionar(ruta):
                items.append({
                    "tipo": "rama_sin_fusionar", "repo_id": rid,
                    "proyecto": proyecto, "rama": rama,
                    "detalle": "tiene trabajo que no está en tu rama actual",
                })

        return {"pendientes": items, "total": len(items)}

    @app.get("/api/ejecucion-pendiente")
    def ejecucion_pendiente(repo_id: str | None = None) -> dict:
        """La ejecución que espera decisión del vocero, con su diff.

        Existe para que la rehidratación del arranque sea VISIBLE: sin esto el
        Hub aceptaría commitear tras un reinicio, pero el front no mostraría
        nada que commitear. El diff se lee del worktree en el momento, no de
        una copia guardada — git es la fuente.
        """
        rid = str(repo_id or repo_default)
        _ruta_repo(rid)  # valida el id contra la lista blanca
        ue = app.state.ejecuciones.get(rid)
        if not ue:
            return {"pendiente": None}
        wt = ue.get("worktree") or ue["repo"]
        return {"pendiente": {
            "repo_id": rid,
            "rama": ue["rama"],
            "rama_base": ue.get("base", ""),
            "worktree": ue.get("worktree", ""),
            "returncode": ue.get("returncode"),
            "archivos": gitutil.staged_changed_files(wt),
            "diff": gitutil.staged_diff(wt),
            # Del sidecar: se calculó al ejecutar y sobrevive al reinicio.
            "correspondencia": (gitutil.leer_sidecar(wt) or {}).get("correspondencia", {}),
            # Veredicto del auditor: el front lo muestra y avisa si "desviado"
            # (el commit está bloqueado salvo forzar).
            "auditoria": ue.get("auditoria") or (gitutil.leer_sidecar(wt) or {}).get("auditoria", {}),
        }}

    @app.post("/api/commit", dependencies=[_csrf])
    def commit_cambios(cuerpo: dict) -> dict:
        """Confirma en la rama devvating/ los cambios en staging (gatillo del vocero).

        Mantiene la invariante: el commit NUNCA es automático — llega solo por
        esta acción explícita. Commitea en la propia rama de ejecución; el merge
        a la rama de trabajo lo hace el vocero cuando revisó.
        """
        rid = str(cuerpo.get("repo_id") or repo_default)
        _ruta_repo(rid)
        ue = app.state.ejecuciones.get(rid)
        if not ue:
            raise HTTPException(409, "No hay una ejecución lista para commitear.")
        if ue.get("returncode", 0) != 0:
            raise HTTPException(
                409,
                f"La ejecución falló (código {ue['returncode']}): el plan no se "
                "aplicó limpio. Revisa el diff y descarta; no se commitea un fallo.",
            )
        # Gate del auditor (D16): un veredicto "desviado" bloquea el commit por
        # defecto. Escape explícito del vocero (`forzar: true`), no un silencio:
        # el vocero revisó el diff y decide asumir el desvío. El fallback
        # ("desconocido"/ausente) NO bloquea — es la política acordada.
        auditoria = ue.get("auditoria") or {}
        if auditoria_bloquea(auditoria) and not bool(cuerpo.get("forzar")):
            raise HTTPException(
                409,
                "El auditor marcó la ejecución como DESVIADA del plan: "
                + (auditoria.get("resumen") or "revisa el diff.")
                + " Revisa y, si asumes el desvío, reenvía con forzar:true.",
            )
        mensaje = str(cuerpo.get("mensaje") or "").strip()
        if not mensaje:
            raise HTTPException(422, "El mensaje de commit no puede estar vacío.")
        try:
            # El commit va en el worktree aislado (sobre la rama devvating/); el
            # merge a la rama de trabajo lo hace el vocero cuando revisó.
            sha = gitutil.commit(ue.get("worktree") or ue["repo"], mensaje)
        except RuntimeError as exc:
            raise HTTPException(422, str(exc))
        # Commiteado: el worktree ya cumplió su función; se quita (la rama queda).
        if ue.get("worktree"):
            gitutil.remove_worktree(ue["repo"], ue["worktree"])
        app.state.ejecuciones.pop(rid, None)
        _emitir({"tipo": "commit_fin", "sha": sha, "rama": ue["rama"], "repo_id": rid})
        return {"ok": True, "sha": sha}

    @app.post("/api/descartar", dependencies=[_csrf])
    def descartar_cambios(cuerpo: dict | None = None) -> dict:
        """Deshace la ejecución: quita el worktree aislado y borra su rama.

        No toca el árbol de trabajo del vocero (D9 paso 2): a diferencia del
        viejo `reset --hard`, aquí no hay nada que resetear en el árbol vivo.
        """
        rid = str((cuerpo or {}).get("repo_id") or repo_default)
        _ruta_repo(rid)
        ue = app.state.ejecuciones.get(rid)
        if not ue:
            raise HTTPException(409, "No hay una ejecución que descartar.")
        try:
            gitutil.discard_worktree(ue["repo"], ue.get("worktree", ""), ue["rama"])
        except RuntimeError as exc:
            raise HTTPException(422, str(exc))
        app.state.ejecuciones.pop(rid, None)
        _emitir({"tipo": "descartar_fin", "base": ue["base"], "rama": ue["rama"],
                 "repo_id": rid})
        return {"ok": True}

    @app.get("/api/ramas")
    def ramas(repo_id: str | None = None) -> dict:
        """Historial de ramas de ejecución (devvating/) del repo elegido."""
        ruta = _ruta_repo(repo_id)
        if not gitutil.is_git_repo(ruta):
            return {"ramas": [], "actual": None}
        actual = gitutil.current_branch(ruta)
        lista = gitutil.list_branches(ruta)
        for r in lista:
            r["actual"] = r["nombre"] == actual
        return {"ramas": lista, "actual": actual}

    @app.get("/api/worktrees")
    def worktrees(repo_id: str | None = None) -> dict:
        """Worktrees de ejecución que quedaron colgando, con lo que decide.

        `tiene_cambios` marca los que perderían trabajo si se retiran (la rama
        y sus commits sobreviven siempre; lo sin commitear, no). Los huérfanos
        van aparte: su repo padre ya no existe, así que ningún repo los ve.
        """
        ruta = _ruta_repo(repo_id)
        if not gitutil.is_git_repo(ruta):
            return {"worktrees": [], "huerfanos": []}
        gitutil.prune_worktrees(ruta)
        return {
            "worktrees": gitutil.list_worktrees(ruta),
            "huerfanos": [
                Path(h).name for h in gitutil.worktrees_huerfanos(base_worktrees())
            ],
        }

    @app.post("/api/worktrees/limpiar", dependencies=[_csrf])
    def limpiar_worktrees(cuerpo: dict) -> dict:
        """Retira los worktrees colgados. Mismo criterio que `devvating limpiar`.

        Sin `forzar`, los que tienen cambios sin commitear se conservan: es
        trabajo que el vocero aún no revisó y quitarlos lo perdería.
        """
        rid = str(cuerpo.get("repo_id") or repo_default)
        ruta = _ruta_repo(rid)
        if not gitutil.is_git_repo(ruta):
            raise HTTPException(422, f"'{ruta}' no es un repositorio git.")
        forzar = bool(cuerpo.get("forzar"))
        gitutil.prune_worktrees(ruta)
        candidatos = [
            w for w in gitutil.list_worktrees(ruta)
            if not w["tiene_cambios"] or forzar
        ]
        # La ejecución pendiente de decisión NO se toca ni con forzar: sus
        # cambios son justo los que el vocero está mirando, y retirarla dejaría
        # los botones de commit/descartar apuntando a un directorio borrado.
        pendiente = (app.state.ejecuciones.get(rid) or {}).get("worktree", "")
        retirados = [w for w in candidatos if w["path"] != pendiente]
        for w in retirados:
            gitutil.remove_worktree(ruta, w["path"])
        # Los huérfanos son globales (su repo padre ya no existe), así que se
        # recogen una vez y no por repo.
        huerfanos = gitutil.worktrees_huerfanos(base_worktrees())
        for h in huerfanos:
            shutil.rmtree(h, ignore_errors=True)
        return {
            "ok": True,
            "retirados": len(retirados),
            "huerfanos": len(huerfanos),
            "conservados": [
                w["rama"] for w in gitutil.list_worktrees(ruta)
            ],
        }

    @app.post("/api/ramas/fusionar", dependencies=[_csrf])
    def fusionar_rama(cuerpo: dict) -> dict:
        """Lleva el trabajo de una rama de ejecución a tu rama actual.

        Decisión del vocero (2026-07-22): el merge SÍ se hace desde la web; el
        push NO — publicar es lo único de la cadena que sale de la máquina y no
        se puede deshacer, así que sigue siendo un acto deliberado en consola.

        Tres guardas antes de tocar nada, porque esta es la única escritura del
        Hub sobre la rama de trabajo:
          - solo ramas `devvating/` (mismo criterio que borrarlas);
          - árbol limpio, o el merge arrastraría cambios sin confirmar del
            vocero a un conflicto que desde el navegador no puede resolver;
          - la rama no puede tener una ejecución sin cerrar: eso es trabajo que
            aún no revisó, y fusionarlo sería decidir por él.
        """
        nombre = str(cuerpo.get("rama") or "")
        rid = str(cuerpo.get("repo_id") or repo_default)
        ruta = _ruta_repo(rid)
        if not nombre.startswith("devvating/"):
            raise HTTPException(422, "Solo se fusionan ramas de ejecución (devvating/).")
        if not gitutil.is_git_repo(ruta):
            raise HTTPException(422, f"'{ruta}' no es un repositorio git.")
        actual = gitutil.current_branch(ruta)
        if nombre == actual:
            raise HTTPException(409, f"Ya estás en '{nombre}': no hay nada que fusionar.")
        if not gitutil.is_clean(ruta):
            raise HTTPException(
                409,
                "Tu árbol de trabajo tiene cambios sin confirmar. Commitéalos o "
                "guárdalos antes de fusionar: si no, un conflicto te dejaría el "
                "merge a medias y desde aquí no se puede resolver.",
            )
        pendiente = app.state.ejecuciones.get(rid)
        if pendiente and pendiente.get("rama") == nombre:
            raise HTTPException(
                409,
                f"'{nombre}' tiene una ejecución sin cerrar. Revisa el diff y "
                "commitea o descarta antes de fusionarla.",
            )
        try:
            resumen = gitutil.merge(ruta, nombre)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        _emitir({"tipo": "merge_fin", "rama": nombre, "destino": actual,
                 "repo_id": rid, "resumen": resumen})
        return {"ok": True, "rama": nombre, "destino": actual, "resumen": resumen}

    @app.post("/api/ramas/borrar", dependencies=[_csrf])
    def borrar_rama(cuerpo: dict) -> dict:
        """Borra una rama de ejecución. Solo devvating/, nunca la rama actual."""
        nombre = str(cuerpo.get("rama") or "")
        ruta = _ruta_repo(cuerpo.get("repo_id"))
        if not nombre.startswith("devvating/"):
            raise HTTPException(422, "Solo se pueden borrar ramas de ejecución (devvating/).")
        if not gitutil.is_git_repo(ruta):
            raise HTTPException(422, f"'{ruta}' no es un repositorio git.")
        if nombre == gitutil.current_branch(ruta):
            raise HTTPException(
                409, "No puedes borrar la rama en la que estás. Cambia de rama primero."
            )
        try:
            gitutil.delete_branch(ruta, nombre)
        except RuntimeError as exc:
            raise HTTPException(422, str(exc))
        return {"ok": True}

    def _dir_transcripts(repo_id: str | None = None) -> Path:
        return Path(_ruta_repo(repo_id)) / "transcripts"

    @app.get("/api/transcripts")
    def transcripts(repo_id: str | None = None) -> dict:
        carpeta = _dir_transcripts(repo_id)
        if not carpeta.is_dir():
            return {"transcripts": []}
        archivos = sorted(
            (p.name for p in carpeta.glob("*.json")), reverse=True
        )
        return {"transcripts": archivos}

    def _ruta_transcript(nombre: str, repo_id: str | None = None) -> Path:
        """Transcript de un repo REGISTRADO, confinado a su carpeta.

        Doble cierre: el nombre pasa por una regex estricta y la ruta resuelta
        debe caer exactamente en `<repo>/transcripts` — así ni un `..` ni un
        symlink sacan la lectura de ahí. El repo se elige por id, nunca por
        ruta (D9).
        """
        if not _NOMBRE_TRANSCRIPT_RE.match(nombre):
            raise HTTPException(404, "Transcript no encontrado.")
        carpeta = _dir_transcripts(repo_id)
        ruta = (carpeta / nombre).resolve()
        if ruta.parent != carpeta.resolve() or not ruta.is_file():
            raise HTTPException(404, "Transcript no encontrado.")
        return ruta

    @app.get("/api/transcripts/{nombre}/html")
    def transcript_html(nombre: str, repo_id: str | None = None) -> HTMLResponse:
        data = json.loads(_ruta_transcript(nombre, repo_id).read_text(encoding="utf-8"))
        return HTMLResponse(reporte.render_html(data))

    @app.get("/api/transcripts/{nombre}")
    def transcript_json(nombre: str, repo_id: str | None = None) -> dict:
        return json.loads(_ruta_transcript(nombre, repo_id).read_text(encoding="utf-8"))

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
    parser.add_argument(
        "--repo", action="append", default=None,
        help="Raíz de un repo a servir. Repetible: --repo a --repo b. Los repos "
             "se dan de alta AQUÍ y nunca por HTTP (decisión D3 del vocero).",
    )
    parser.add_argument(
        "--raiz", action="append", default=None,
        help="Directorio donde el Hub puede DESCUBRIR proyectos para "
             "registrarlos desde la web (repetible). Escanea 2 niveles. Sin "
             "esto no hay descubrimiento: solo se sirve lo dado con --repo.",
    )
    args = parser.parse_args(argv)

    import uvicorn

    rutas = args.repo or ["."]
    app = crear_app(repos=rutas, raices=args.raiz)
    print(f"Devvating Hub → http://127.0.0.1:{args.port}")
    for rid, ruta in app.state.repos.items():
        print(f"  · {rid}: {ruta}")
    for raiz in app.state.raices:
        print(f"  ⌕ explora: {raiz}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

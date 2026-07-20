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
import secrets
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
from . import gitutil, reporte, roles, rotation
from .config import Config
from .debate import _load_partial_session, _save_transcript
from .executor import (
    ClaudeCodeBackend,
    ExecutionPlan,
    Executor,
    ExecutorError,
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
    config: dict, emitir: Callable[[dict], None], backend=None
) -> None:
    """Aplica un plan en una rama (fase 4) y emite el diff resultante.

    Salvaguardas del plan del Hub: SIEMPRE sin comandos (allow_commands es
    opt-in exclusivo de la CLI), el Executor exige árbol limpio y deja los
    cambios en staging — el Hub solo muestra; el commit es del vocero.

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
            allow_open_decisions=config.get("forzar_decisiones", False),
        )
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
        "rama_base": resultado.base_branch,
        "repo": config["repo"],
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
    # Señal de cancelación del debate en curso: la fija POST /api/debates/cancelar
    # y la leen el orquestador (entre turnos) y los adaptadores CLI (matan su
    # subprocess en vuelo). Se limpia al lanzar cada debate.
    app.state.cancelar_event = threading.Event()
    # Última ejecución con cambios en staging, a la espera de que el vocero
    # decida: commit en la rama o descartar. None = nada pendiente.
    app.state.ultima_ejecucion = None
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
            if msg.get("tipo") == "ejecucion_fin":
                # Queda pendiente la decisión del vocero (commit/descartar).
                # Guardamos el returncode: si el backend falló, el commit se
                # bloquea (no presentar éxito sobre un plan roto — hallazgo de
                # la auto-auditoría; el descarte sigue disponible).
                app.state.ultima_ejecucion = {
                    "rama": msg["rama"], "base": msg.get("rama_base", ""),
                    "repo": msg["repo"], "returncode": msg.get("returncode", 0),
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
        old_session = None
        resume = str(config.get("resume") or "").strip()
        if resume:
            old_session = _load_partial_session(str(_ruta_transcript(resume)))
            config = {
                **config,
                "tema": old_session.topic.prompt,
                "files": old_session.topic.context_hint or config.get("files", ""),
                "profundo": old_session.deep_mode,
            }

        tema = str(config.get("tema", "")).strip()
        if not tema:
            raise HTTPException(422, "Falta el tema del debate.")
        # El Hub sirve UN solo repo (el del arranque); no se acepta override
        # del cuerpo — sería aplicar/leer en rutas arbitrarias del disco.
        config = {**config, "tema": tema, "repo": repo}
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
        ruta = _ruta_transcript(nombre)
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
        ruta = _ruta_transcript(nombre)
        data = json.loads(ruta.read_text(encoding="utf-8"))
        old_session = _load_partial_session(str(ruta))
        if not old_session.turns:
            raise HTTPException(422, "El transcript no tiene turnos que reanudar.")
        ronda_cierre = old_session.rounds_run + 1
        tema = _prompt_cierre(old_session.topic.prompt, data.get("decisiones") or [])
        config = {
            "tema": tema, "agentes": agentes, "repo": repo,
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
            "reanudado": True, "cierre": True,
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
        data = json.loads(_ruta_transcript(nombre).read_text(encoding="utf-8"))
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
        # Confinado al repo servido: NO se aplica un plan en una ruta arbitraria
        # del cuerpo (agujero de escritura remota vía navegador — auto-auditoría).
        repo_objetivo = repo
        app.state.ejecutando = True
        _emitir({"tipo": "ejecucion_inicio", "transcript": nombre, "repo": repo_objetivo})
        threading.Thread(
            target=_ejecucion_worker,
            args=(
                {"plan": plan, "repo": repo_objetivo,
                 "titulo": data.get("topic", {}).get("prompt", "plan"),
                 "decisiones_pendientes": pendientes, "forzar_decisiones": forzar},
                _emitir,
                backend_ejecucion,
            ),
            daemon=True,
        ).start()
        return {"ok": True}

    @app.post("/api/commit", dependencies=[_csrf])
    def commit_cambios(cuerpo: dict) -> dict:
        """Confirma en la rama devvating/ los cambios en staging (gatillo del vocero).

        Mantiene la invariante: el commit NUNCA es automático — llega solo por
        esta acción explícita. Commitea en la propia rama de ejecución; el merge
        a la rama de trabajo lo hace el vocero cuando revisó.
        """
        ue = app.state.ultima_ejecucion
        if not ue:
            raise HTTPException(409, "No hay una ejecución lista para commitear.")
        if ue.get("returncode", 0) != 0:
            raise HTTPException(
                409,
                f"La ejecución falló (código {ue['returncode']}): el plan no se "
                "aplicó limpio. Revisa el diff y descarta; no se commitea un fallo.",
            )
        mensaje = str(cuerpo.get("mensaje") or "").strip()
        if not mensaje:
            raise HTTPException(422, "El mensaje de commit no puede estar vacío.")
        try:
            sha = gitutil.commit(ue["repo"], mensaje)
        except RuntimeError as exc:
            raise HTTPException(422, str(exc))
        app.state.ultima_ejecucion = None
        _emitir({"tipo": "commit_fin", "sha": sha, "rama": ue["rama"]})
        return {"ok": True, "sha": sha}

    @app.post("/api/descartar", dependencies=[_csrf])
    def descartar_cambios() -> dict:
        """Deshace la ejecución: vuelve a la rama base y borra la devvating/."""
        ue = app.state.ultima_ejecucion
        if not ue:
            raise HTTPException(409, "No hay una ejecución que descartar.")
        try:
            gitutil.discard_branch(ue["repo"], ue["base"], ue["rama"])
        except RuntimeError as exc:
            raise HTTPException(422, str(exc))
        app.state.ultima_ejecucion = None
        _emitir({"tipo": "descartar_fin", "base": ue["base"], "rama": ue["rama"]})
        return {"ok": True}

    @app.get("/api/ramas")
    def ramas() -> dict:
        """Historial de ramas de ejecución (devvating/) del repo del Hub."""
        if not gitutil.is_git_repo(repo):
            return {"ramas": [], "actual": None}
        actual = gitutil.current_branch(repo)
        lista = gitutil.list_branches(repo)
        for r in lista:
            r["actual"] = r["nombre"] == actual
        return {"ramas": lista, "actual": actual}

    @app.post("/api/ramas/borrar", dependencies=[_csrf])
    def borrar_rama(cuerpo: dict) -> dict:
        """Borra una rama de ejecución. Solo devvating/, nunca la rama actual."""
        nombre = str(cuerpo.get("rama") or "")
        if not nombre.startswith("devvating/"):
            raise HTTPException(422, "Solo se pueden borrar ramas de ejecución (devvating/).")
        if not gitutil.is_git_repo(repo):
            raise HTTPException(422, f"'{repo}' no es un repositorio git.")
        if nombre == gitutil.current_branch(repo):
            raise HTTPException(
                409, "No puedes borrar la rama en la que estás. Cambia de rama primero."
            )
        try:
            gitutil.delete_branch(repo, nombre)
        except RuntimeError as exc:
            raise HTTPException(422, str(exc))
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

"""Motor de debate (M2: N rondas con reglas de convergencia).

Flujo (DISENO.md sección 3, decisiones D3/D4):
  Apertura a ciegas: ambos agentes proponen en paralelo, sin verse.
  Rondas 1..N (tope configurable, por defecto 2): cada agente responde a la
    postura del otro (refina o mantiene) y emite un veredicto de convergencia.
    - Corte temprano: si ambos declaran convergencia en la misma ronda, se para.
    - Entre rondas el vocero puede intervenir (D4) vía on_intervention.
  Ronda de inversión (modo profundo, opt-in, D3): cada uno defiende la postura
    contraria como stress-test.
  Síntesis: un agente (rotativo) reporta acuerdos, desacuerdos y plan.

Todo en SOLO LECTURA: los agentes solo disponen de read_file.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from . import roles
from .adapters.base import (
    AgentAdapter,
    AgentCancelledError,
    AgentError,
    SessionLimitError,
    TransientProviderError,
    TurnUsage,
)
from .tools.readonly import make_read_file
from .tools.registry import ToolRegistry

# Esperas del backoff por turno (plan de resiliencia; constantes fijas por
# decisión del vocero — configurables el día que duela, no antes).
ESPERAS_REINTENTO: tuple[int, ...] = (5, 15, 45)


class DebateAbortedError(RuntimeError):
    """El debate no pudo continuar; lleva la sesión parcial para no perderla."""

    def __init__(self, session: "DebateSession", causa: AgentError) -> None:
        super().__init__(str(causa))
        self.session = session
        self.causa = causa


class DebateCancelledError(RuntimeError):
    """El vocero canceló el debate; lleva la sesión parcial (reanudable)."""

    def __init__(self, session: "DebateSession") -> None:
        super().__init__("Debate cancelado por el vocero.")
        self.session = session

# Bloque JSON final en vez de una marca de texto libre (`[CONVERGENCIA: SÍ/NO]`):
# con 6 CLIs de terceros en el roster no se controla su formato de salida, y un
# booleano JSON es más robusto de extraer que una marca ad-hoc. Si no aparece o
# no se puede leer, el fallback es el mismo de siempre: sin veredicto (None), lo
# que el llamador trata como "no convergió" — nunca rompe el debate.
_VERDICT_JSON_RE = re.compile(
    r'\{\s*"convergencia"\s*:\s*(true|false)\s*\}', re.IGNORECASE
)


def _parse_verdict(text: str) -> tuple[str, str | None]:
    """Extrae el veredicto de convergencia y lo quita del texto mostrado."""
    match = _VERDICT_JSON_RE.search(text)
    if not match:
        return text.strip(), None
    verdict = "si" if match.group(1).lower() == "true" else "no"
    clean = _VERDICT_JSON_RE.sub("", text).strip()
    return clean, verdict


# Bloque de DECISIONES del vocero (plan del debate "que el vocero pueda resolver
# las decisiones abiertas"). Va SOLO en la síntesis (no en la réplica: con 6 CLIs
# de terceros duplicar la superficie de parseo degradaría el veredicto de una
# línea). A diferencia del veredicto —un booleano que un regex basta—, este
# bloque es JSON anidado; se localiza por marcador y se corta con raw_decode, que
# respeta strings y anidamiento. Fallback seguro: cualquier fallo → lista vacía
# (mismo espíritu que None en _parse_verdict; nunca rompe el debate).
_DECISIONES_MARK_RE = re.compile(r'\{\s*"decisiones"\s*:', re.IGNORECASE)
# Fragmento textual citado en 'contra', entre comillas angulares/curvas/rectas.
_CITA_RE = re.compile(r'[«“"]([^»”"]{3,})[»”"]')
_SIN_CONTRA_RE = re.compile(r"(?i)sin contraargumento")


@dataclass
class Decision:
    """Una decisión que el plan deja al vocero, con opciones y una recomendada.

    `contra`: mejor argumento contra la recomendada, con un fragmento textual
    citado. `contra_en_debate` es una señal BLANDA (no bloquea ni borra): False
    si ese fragmento no se localiza en la transcripción — la UI lo marca ⚠ para
    avisar que la cita no se pudo verificar, sin degradar el texto.
    `resuelta`/`eleccion`: los llena la resolución del vocero (fase F2), no el
    parser; aquí nacen sin resolver.
    """

    id: str
    pregunta: str
    opciones: list[str] = field(default_factory=list)
    recomendada: str = ""
    crucial: bool = False
    contra: str = ""
    contra_en_debate: bool = True
    resuelta: bool = False
    eleccion: str = ""


def _normalizar(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _cita_localizada(contra: str, transcript_norm: str) -> bool:
    """True si el 'contra' es verificable: o declara honestamente que no hubo
    contraargumento, o cita un fragmento textual que aparece en la transcripción."""
    if not contra.strip() or _SIN_CONTRA_RE.search(contra):
        return True  # ausencia declarada: honesta, no sospechosa
    fragmentos = [f for f in _CITA_RE.findall(contra) if len(f.strip()) >= 8]
    if not fragmentos:
        return False  # afirma un contra pero sin fragmento verificable
    return any(_normalizar(f) in transcript_norm for f in fragmentos)


def _parse_decisiones(text: str) -> tuple[str, list[Decision]]:
    """Extrae el bloque de decisiones y lo quita del texto visible.

    Cualquier problema (sin bloque, JSON roto, forma inesperada) → ([], texto
    intacto): el fallback seguro del contrato.
    """
    m = _DECISIONES_MARK_RE.search(text)
    if not m:
        return text.strip(), []
    inicio = m.start()
    try:
        obj, fin = json.JSONDecoder().raw_decode(text, inicio)
    except ValueError:
        return text.strip(), []
    crudas = obj.get("decisiones") if isinstance(obj, dict) else None
    if not isinstance(crudas, list):
        return text.strip(), []
    decisiones: list[Decision] = []
    for d in crudas:
        if not isinstance(d, dict):
            continue
        opciones = [str(o) for o in d.get("opciones", []) if isinstance(o, (str, int, float))]
        decisiones.append(Decision(
            id=str(d.get("id", "")),
            pregunta=str(d.get("pregunta", "")),
            opciones=opciones,
            recomendada=str(d.get("recomendada", "")),
            crucial=bool(d.get("crucial", False)),
            contra=str(d.get("contra", "")),
        ))
    clean = (text[:inicio] + text[fin:]).strip()
    return clean, decisiones


def _verificar_contra(decisiones: list[Decision], transcripcion: str) -> None:
    """Marca blanda por decisión: fija `contra_en_debate` sin bloquear ni borrar."""
    norm = _normalizar(transcripcion)
    for d in decisiones:
        d.contra_en_debate = _cita_localizada(d.contra, norm)


def _estado_de(session: "DebateSession") -> str:
    """Estado nominal: pendiente_decision manda sobre la convergencia — un plan
    con una decisión crucial abierta no está cerrado aunque los agentes convergieran."""
    if any(d.crucial and not d.resuelta for d in session.decisiones):
        return "pendiente_decision"
    return "convergido" if session.converged else "abierto"


@dataclass
class DebateTopic:
    prompt: str
    context_hint: str = ""


@dataclass
class Turn:
    round: int  # 0 = apertura a ciegas
    phase: str  # "propuesta" | "replica" | "inversion" | "sintesis"
    agent: str
    text: str
    verdict: str | None = None  # "si" | "no" | None
    usage: TurnUsage | None = None  # métricas del turno (§13); None si no hay


@dataclass
class DebateSession:
    topic: DebateTopic
    turns: list[Turn] = field(default_factory=list)
    rounds_run: int = 0
    converged: bool = False
    converged_round: int | None = None
    deep_mode: bool = False
    synthesis: str = ""
    synthesizer: str = ""
    # Decisiones que la síntesis deja al vocero (bloque JSON despojado del texto)
    # y el estado nominal resultante: "convergido" / "abierto" / "pendiente_decision"
    # (este último si hay alguna decisión crucial sin resolver).
    decisiones: list[Decision] = field(default_factory=list)
    estado: str = "abierto"
    # Totales por agente + "total" global, derivados de turns al final (§13).
    usage_totals: dict[str, TurnUsage] = field(default_factory=dict)


# Callbacks opcionales:
#   on_event(evento, agente, texto|None)  -> reportar progreso a la UI
#   on_intervention(ronda) -> nota del vocero para esa ronda, o None
EventCb = Callable[[str, str, str | None], None]
InterventionCb = Callable[[int], str | None]


class Orchestrator:
    def __init__(
        self,
        agent_a: AgentAdapter,
        agent_b: AgentAdapter,
        repo_root: str = ".",
        on_event: EventCb | None = None,
        retry_waits: tuple[int, ...] = ESPERAS_REINTENTO,
        sleep: Callable[[float], None] = time.sleep,
        biases: list[str] | None = None,
    ) -> None:
        self.agents = [agent_a, agent_b]
        self.repo_root = repo_root
        self._on_event = on_event or (lambda *_: None)
        self._retry_waits = retry_waits
        self._sleep = sleep
        # Sesgo por agente (texto ya resuelto), paralelo a self.agents. Vacío =
        # sin sesgo (comportamiento clásico). Solo aplica en propuesta y réplica.
        if biases is not None and len(biases) != len(self.agents):
            raise ValueError(
                f"biases debe tener {len(self.agents)} entradas (una por agente); "
                f"recibí {len(biases)}."
            )
        self._biases = list(biases) if biases else ["" for _ in self.agents]

    def _bias_de(self, agent: AgentAdapter) -> str:
        return self._biases[self.agents.index(agent)]

    def _converse_con_reintento(
        self, agent: AgentAdapter, system: str, prompt: str, registry: ToolRegistry
    ) -> str:
        """Envuelve converse con la política por clase de fallo (plan resiliencia).

        Transitorios (503/429 momentáneo): backoff según `retry_waits`.
        Límite de sesión y fallos sin clasificar: no se reintentan — el
        llamador (run) convierte en DebateAbortedError con la sesión parcial.
        """
        ultimo: TransientProviderError | None = None
        for espera in (*self._retry_waits, None):
            try:
                return agent.converse(system, prompt, registry)
            except TransientProviderError as exc:
                ultimo = exc
                if espera is None:
                    break
                self._on_event(
                    "reintento", agent.name,
                    f"fallo transitorio del proveedor; reintentando en {espera}s",
                )
                self._sleep(espera)
        raise ultimo  # agotados los reintentos

    def _registry(self) -> ToolRegistry:
        # Solo lectura durante el debate (nadie puede escribir ni ejecutar).
        reg = ToolRegistry()
        reg.register(make_read_file(self.repo_root))
        return reg

    def _other(self, agent: AgentAdapter) -> AgentAdapter:
        return self.agents[1] if agent is self.agents[0] else self.agents[0]

    def run(
        self,
        topic: DebateTopic,
        *,
        max_rounds: int = 2,
        min_rounds: int = 1,
        synthesizer_index: int = 0,
        deep_mode: bool = False,
        on_intervention: InterventionCb | None = None,
        old_session: DebateSession | None = None,
        cancel_event=None,
    ) -> DebateSession:
        session = DebateSession(topic=topic, deep_mode=deep_mode)
        registry = self._registry()
        # La señal de cancelación llega a los adaptadores CLI para que maten su
        # subprocess en vuelo; el orquestador también la chequea entre turnos.
        self._cancel_event = cancel_event
        for agent in self.agents:
            if hasattr(agent, "cancel_event"):
                agent.cancel_event = cancel_event
            # Streaming (opcional): el adaptador que lo soporte recibe un
            # callback que reenvía cada delta como evento "delta" a la UI. El
            # orquestador sigue ciego al streaming: usa solo el retorno completo.
            if getattr(agent, "soporta_streaming", False):
                agent.on_delta = self._delta_cb(agent.name)
        try:
            return self._correr(
                session, topic, registry, max_rounds, min_rounds, synthesizer_index,
                deep_mode, on_intervention, old_session
            )
        except (AgentCancelledError, DebateCancelledError):
            # Cancelación limpia (no fallo): sesión parcial reanudable.
            session.usage_totals = self._totalizar(session)
            raise DebateCancelledError(session)
        except AgentError as exc:
            # Fallo irrecuperable: totalizar lo pagado y entregar la sesión
            # parcial — los turnos completados nunca se pierden (plan §13).
            session.usage_totals = self._totalizar(session)
            raise DebateAbortedError(session, exc) from exc
        finally:
            # No dejar el callback de streaming colgando: el adaptador puede
            # sobrevivir a esta sesión (en el Hub se reusa el objeto), y un
            # on_delta apuntando a un on_event ya cerrado emitiría al vacío.
            for agent in self.agents:
                if getattr(agent, "soporta_streaming", False):
                    agent.on_delta = None

    def _delta_cb(self, nombre: str) -> Callable[[str], None]:
        """Callback de streaming para un agente: cada fragmento va a la UI como
        evento "delta". Cerrado sobre el nombre porque el adaptador solo pasa el
        texto; el orquestador añade de quién es."""
        return lambda fragmento: self._on_event("delta", nombre, fragmento)

    def _cancelado(self) -> bool:
        ev = getattr(self, "_cancel_event", None)
        return ev is not None and ev.is_set()

    def _correr(
        self,
        session: DebateSession,
        topic: DebateTopic,
        registry: ToolRegistry,
        max_rounds: int,
        min_rounds: int,
        synthesizer_index: int,
        deep_mode: bool,
        on_intervention: InterventionCb | None,
        old_session: DebateSession | None = None,
    ) -> DebateSession:
        
        def _get_turn(round_idx: int, phase: str, agent_name: str) -> Turn | None:
            if not old_session:
                return None
            for t in old_session.turns:
                if t.round == round_idx and t.phase == phase and t.agent == agent_name:
                    return t
            return None

        # --- Apertura a ciegas (ronda 0) ------------------------------------
        self._on_event("ronda", "apertura a ciegas", None)
        positions: dict[str, str] = {}
        for agent in self.agents:
            if self._cancelado():
                raise DebateCancelledError(session)
            self._on_event("propuesta_inicio", agent.name, None)
            existente = _get_turn(0, "propuesta", agent.name)
            if existente:
                text = existente.text
                session.turns.append(existente)
            else:
                text = self._converse_con_reintento(
                    agent,
                    roles.con_sesgo(roles.PROPONENTE, self._bias_de(agent)),
                    roles.prompt_propuesta(topic),
                    registry,
                )
                session.turns.append(
                    Turn(0, "propuesta", agent.name, text, usage=self._usage_de(agent))
                )
            positions[agent.name] = text
            self._on_event("propuesta_fin", agent.name, text)

        # --- Rondas de réplica con reglas de convergencia -------------------
        for r in range(1, max_rounds + 1):
            session.rounds_run = r
            self._on_event("ronda", f"ronda {r}", None)
            nota = on_intervention(r) if on_intervention else None

            # Ambos replican a la postura del otro de la ronda anterior
            # (se actualiza después del bucle -> réplicas simultáneas, sin sesgo).
            new_positions: dict[str, str] = {}
            verdicts: dict[str, str | None] = {}
            for agent in self.agents:
                if self._cancelado():
                    raise DebateCancelledError(session)
                other = self._other(agent)
                self._on_event("replica_inicio", agent.name, None)
                existente = _get_turn(r, "replica", agent.name)
                if existente:
                    clean, verdict = existente.text, existente.verdict
                    session.turns.append(existente)
                else:
                    raw = self._converse_con_reintento(
                        agent,
                        roles.con_sesgo(roles.REPLICA, self._bias_de(agent)),
                        roles.prompt_replica(
                            topic, positions[agent.name], positions[other.name], other.name, nota
                        ),
                        registry,
                    )
                    clean, verdict = _parse_verdict(raw)
                    session.turns.append(
                        Turn(r, "replica", agent.name, clean, verdict, usage=self._usage_de(agent))
                    )
                new_positions[agent.name] = clean
                verdicts[agent.name] = verdict
                self._on_event("replica_fin", agent.name, clean)

            positions = new_positions
            # El corte por convergencia se ignora hasta cumplir min_rounds: en un
            # auto-debate (min_rounds=2) evita que un eco declare acuerdo en la
            # primera réplica, sin haber surgido desacuerdo real que escrutar.
            if r >= min_rounds and all(v == "si" for v in verdicts.values()):
                session.converged = True
                session.converged_round = r
                self._on_event("convergencia", f"ronda {r}", None)
                break

        # --- Ronda de inversión (modo profundo, opt-in) ---------------------
        if deep_mode:
            self._on_event("ronda", "inversión (modo profundo)", None)
            for agent in self.agents:
                if self._cancelado():
                    raise DebateCancelledError(session)
                other = self._other(agent)
                self._on_event("inversion_inicio", agent.name, None)
                existente = _get_turn(session.rounds_run, "inversion", agent.name)
                if existente:
                    text = existente.text
                    session.turns.append(existente)
                else:
                    text = self._converse_con_reintento(
                        agent,
                        roles.INVERSION,
                        roles.prompt_inversion(
                            topic, positions[agent.name], positions[other.name], other.name
                        ),
                        registry,
                    )
                    session.turns.append(
                        Turn(session.rounds_run, "inversion", agent.name, text,
                             usage=self._usage_de(agent))
                    )
                self._on_event("inversion_fin", agent.name, text)

        # --- Síntesis (agente rotativo) -------------------------------------
        if self._cancelado():
            raise DebateCancelledError(session)
        synth = self.agents[synthesizer_index % len(self.agents)]
        self._on_event("ronda", "síntesis", None)
        self._on_event("sintesis_inicio", synth.name, None)
        existente = _get_turn(session.rounds_run, "sintesis", synth.name)
        if existente:
            text = existente.text
            session.turns.append(existente)
        else:
            nota_conv = (
                f"Los agentes CONVERGIERON en la ronda {session.converged_round}."
                if session.converged
                else f"NO hubo convergencia tras {session.rounds_run} ronda(s); "
                "reporta explícitamente los desacuerdos abiertos."
            )
            text = self._converse_con_reintento(
                synth,
                roles.SINTETIZADOR,
                roles.prompt_sintesis(topic, self._transcript_text(session), nota_conv),
                registry,
            )
            session.turns.append(
                Turn(session.rounds_run, "sintesis", synth.name, text, usage=self._usage_de(synth))
            )
        # Despojar el bloque de decisiones del texto visible (como el veredicto
        # en la réplica). El mismo turno guarda ya el texto limpio, para que el
        # reporte y una reanudación no arrastren el JSON crudo.
        clean, decisiones = _parse_decisiones(text)
        session.turns[-1].text = clean
        session.synthesis = clean
        session.synthesizer = synth.name
        session.decisiones = decisiones
        _verificar_contra(decisiones, self._transcript_text(session))
        session.estado = _estado_de(session)
        self._on_event("sintesis_fin", synth.name, clean)

        session.usage_totals = self._totalizar(session)
        return session

    @staticmethod
    def _usage_de(agent: AgentAdapter) -> TurnUsage | None:
        """Copia el uso del último turno del adaptador (accessor del plan §13)."""
        return getattr(agent, "last_usage", None)

    @staticmethod
    def _totalizar(session: DebateSession) -> dict[str, TurnUsage]:
        """Totales por agente + global. Turnos sin métricas simplemente no suman."""
        por_agente: dict[str, TurnUsage] = {}
        for t in session.turns:
            if t.usage is None:
                continue
            por_agente[t.agent] = por_agente.get(t.agent, TurnUsage()) + t.usage
        if por_agente:
            total = TurnUsage()
            for u in por_agente.values():
                total = total + u
            por_agente["total"] = total
        return por_agente

    @staticmethod
    def _transcript_text(session: DebateSession) -> str:
        """Arma una transcripción legible del debate para la síntesis."""
        lines: list[str] = []
        for t in session.turns:
            if t.phase == "sintesis":
                continue
            etiqueta = {
                "propuesta": "Propuesta inicial",
                "replica": f"Réplica (ronda {t.round})",
                "inversion": "Inversión (steelman)",
            }.get(t.phase, t.phase)
            marca = f" [veredicto: {t.verdict}]" if t.verdict else ""
            lines.append(f"— {etiqueta} · {t.agent}{marca}:\n{t.text}\n")
        return "\n".join(lines)

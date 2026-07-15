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

import re
import time
from dataclasses import dataclass, field
from typing import Callable

from . import roles
from .adapters.base import (
    AgentAdapter,
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

_VERDICT_RE = re.compile(r"\[\s*CONVERGENCIA\s*:\s*(S[IÍ]|NO)\s*\]", re.IGNORECASE)


def _parse_verdict(text: str) -> tuple[str, str | None]:
    """Extrae el veredicto de convergencia y lo quita del texto mostrado."""
    match = _VERDICT_RE.search(text)
    if not match:
        return text.strip(), None
    verdict = "si" if match.group(1).upper() in ("SI", "SÍ") else "no"
    clean = _VERDICT_RE.sub("", text).strip()
    return clean, verdict


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
    ) -> DebateSession:
        session = DebateSession(topic=topic, deep_mode=deep_mode)
        registry = self._registry()
        try:
            return self._correr(
                session, topic, registry, max_rounds, min_rounds, synthesizer_index,
                deep_mode, on_intervention, old_session
            )
        except AgentError as exc:
            # Fallo irrecuperable: totalizar lo pagado y entregar la sesión
            # parcial — los turnos completados nunca se pierden (plan §13).
            session.usage_totals = self._totalizar(session)
            raise DebateAbortedError(session, exc) from exc

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
        session.synthesis = text
        session.synthesizer = synth.name
        self._on_event("sintesis_fin", synth.name, text)

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

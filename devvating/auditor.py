"""Auditor de correspondencia plan↔ejecución (fase 5, D16).

Segunda línea de defensa tras `executor.correspondencia` (determinista): un
agente de roster, en modo SOLO LECTURA sobre el worktree ya ejecutado, contesta
una pregunta con la carga de la prueba INVERTIDA — no "¿el plan salió bien?"
(que invita a un sí complaciente), sino "¿qué se hizo que el plan NO pidió y qué
pidió el plan que NO se hizo?", cada hallazgo con una CITA textual del diff.

Diseño (debate del 2026-07-22, sintetizado por Claude, arbitrado por el vocero):
  - Es un agente de roster, opt-in por ejecución (campo `auditoria` de
    `.devvating.json`, mismo régimen que `verificacion`). No un modelo de alto
    razonamiento cableado.
  - Efecto: BLOQUEA el commit por defecto ante veredicto "desviado", con escape
    explícito (`forzar`) para el vocero.
  - Fallback ACORDADO = NO bloquear: si el auditor no corre, no emite un bloque
    legible, o su JSON viene roto, es culpa del auditor, no evidencia de
    desvío. `parse_auditoria` cae a None y el veredicto queda "desconocido";
    solo "desviado" bloquea.

Camino headless read-only (NO el bucle de debate de `adapters/`): el auditor
corre `claude -p` con `--allowedTools Read,Glob,Grep` en el worktree — no toca
el disco. No unificar con el camino API (CLAUDE.md).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .adapters.cli import env_suscripcion
from .roles import AUDITOR, prompt_auditoria

# Bloque JSON del veredicto, análogo al de decisiones: se localiza por marcador
# y se corta con raw_decode (respeta anidamiento y strings). Cualquier fallo →
# None (fallback seguro; nunca bloquea por su cuenta).
_AUDITORIA_MARK_RE = re.compile(r'\{\s*"auditoria"\s*:', re.IGNORECASE)
# Fragmento citado entre comillas angulares/curvas/rectas (mismo criterio que
# `orchestrator._cita_localizada`, la verificación blanda de la síntesis).
_CITA_RE = re.compile(r'[«“"]([^»”"]{3,})[»”"]')

VEREDICTOS = ("conforme", "desviado")


@dataclass
class Auditoria:
    """Veredicto normalizado del auditor.

    `veredicto`: "conforme" | "desviado" | "desconocido". Solo "desviado"
    bloquea; "desconocido" es el fallback seguro (no corrió, JSON roto o forma
    inesperada). `no_pedido`/`omitido`: hallazgos con su cita textual;
    `cita_localizada` es una SEÑAL blanda (no descarta el hallazgo) — False si
    la cita no se encuentra en el diff, para que el vocero sepa qué tan fiable
    es antes de usar el escape.
    """

    veredicto: str = "desconocido"
    no_pedido: list[dict] = field(default_factory=list)
    omitido: list[dict] = field(default_factory=list)
    resumen: str = ""
    agente: str = ""
    corrio: bool = False

    @property
    def bloquea(self) -> bool:
        return self.veredicto == "desviado"

    def as_dict(self) -> dict:
        return {
            "veredicto": self.veredicto,
            "no_pedido": self.no_pedido,
            "omitido": self.omitido,
            "resumen": self.resumen,
            "agente": self.agente,
            "corrio": self.corrio,
            "bloquea": self.bloquea,
        }


def bloquea(auditoria: dict | None) -> bool:
    """Predicado único de bloqueo, compartido por Executor y Hub.

    None (no se pidió auditoría) o cualquier veredicto que no sea "desviado" →
    no bloquea. Un solo lugar decide, para que la CLI y el Hub no diverjan.
    """
    return bool(auditoria) and auditoria.get("veredicto") == "desviado"


def _normalizar(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _cita_en_diff(cita: str, diff_norm: str) -> bool:
    """True si algún fragmento «entre comillas» de la cita aparece en el diff."""
    fragmentos = [f for f in _CITA_RE.findall(cita or "") if len(f.strip()) >= 8]
    if not fragmentos:
        # Sin fragmento verificable: no se puede confirmar, se marca no
        # localizada (señal para el vocero), pero no invalida el hallazgo.
        return False
    return any(_normalizar(f) in diff_norm for f in fragmentos)


def _lista_de_hallazgos(crudo: object) -> list[dict]:
    """Normaliza una lista de hallazgos {que, cita} tolerando basura."""
    salida: list[dict] = []
    if not isinstance(crudo, list):
        return salida
    for h in crudo:
        if not isinstance(h, dict):
            continue
        salida.append({"que": str(h.get("que", "")), "cita": str(h.get("cita", ""))})
    return salida


def parse_auditoria(text: str) -> dict | None:
    """Extrae el bloque `{"auditoria":{...}}`. Cualquier fallo → None.

    Devuelve el dict crudo del bloque (sin normalizar veredicto ni citas): eso
    lo hace `_a_auditoria`, que además ancla las citas contra el diff. Aquí solo
    se aísla el JSON del texto libre del CLI.
    """
    if not text:
        return None
    m = _AUDITORIA_MARK_RE.search(text)
    if not m:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text, m.start())
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    interior = obj.get("auditoria")
    return interior if isinstance(interior, dict) else None


def _a_auditoria(crudo: dict | None, diff: str, agente: str) -> Auditoria:
    """Normaliza el bloque crudo a `Auditoria`, anclando citas contra el diff.

    Sin bloque legible (crudo None) → veredicto "desconocido": corrió pero no se
    entendió, no bloquea (fallback acordado). El veredicto lo declara el modelo;
    la localización de citas es una señal aparte, no cambia el veredicto (no
    reemplazamos el juicio del auditor por una heurística — el vocero tiene el
    escape).
    """
    if crudo is None:
        return Auditoria(veredicto="desconocido", agente=agente, corrio=True)
    v = str(crudo.get("veredicto", "")).strip().lower()
    veredicto = v if v in VEREDICTOS else "desconocido"
    diff_norm = _normalizar(diff)
    no_pedido = _lista_de_hallazgos(crudo.get("no_pedido"))
    for h in no_pedido:
        h["cita_localizada"] = _cita_en_diff(h["cita"], diff_norm)
    return Auditoria(
        veredicto=veredicto,
        no_pedido=no_pedido,
        # 'omitido' cita el PLAN (lo que faltó), no el diff: no se ancla contra
        # el diff porque por definición no está ahí.
        omitido=_lista_de_hallazgos(crudo.get("omitido")),
        resumen=str(crudo.get("resumen", "")),
        agente=agente,
        corrio=True,
    )


@runtime_checkable
class AuditorBackend(Protocol):
    name: str

    def run(self, prompt: str, cwd: str) -> tuple[int, str]:
        """Corre el auditor headless SOLO LECTURA en cwd; (returncode, salida)."""
        ...


class ClaudeAuditBackend:
    """Auditor por Claude Code headless, restringido a lectura.

    `--allowedTools Read,Glob,Grep` y NADA de escritura: el auditor no puede
    tocar el worktree que audita. Corre con `env_suscripcion()` como el resto
    del camino CLI (sin heredar ANTHROPIC_API_KEY — trampa de facturación, D5).
    """

    name = "claude-code"

    def __init__(self, binary: str = "claude", model: str | None = None) -> None:
        self.binary = binary
        # Un modelo de más razonamiento sí cabe aquí (auditar es barato y raro,
        # a diferencia de ejecutar): default al alias que resuelva el CLI.
        self.model = model or os.environ.get("DEVVATING_AUDIT_MODEL", "sonnet")

    def build_argv(self, prompt: str) -> list[str]:
        return [
            self.binary, "-p", prompt, "--model", self.model,
            "--allowedTools", "Read,Glob,Grep",
        ]

    def run(self, prompt: str, cwd: str) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                self.build_argv(prompt),
                cwd=cwd,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                env=env_suscripcion(),
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"No se encontró el binario '{self.binary}' para auditar."
            ) from exc
        return proc.returncode, (proc.stdout + proc.stderr)


# Auditores soportados hoy. El campo `auditoria` NOMBRA un agente de roster; por
# ahora la familia claude (el mismo binario probado del ejecutor) en read-only.
# Otros CLIs como auditor están diseñados pero pendientes: un nombre no
# soportado es un ERROR de config (protocolo 3), no un fallback silencioso.
_AUDITORES_CLAUDE = {"", "claude", "claude-cli", "claude-code", "claude-api"}


def crear_auditor(nombre: str, model: str | None = None) -> AuditorBackend:
    """Resuelve el nombre del campo `auditoria` a un backend read-only."""
    clave = (nombre or "").strip().lower()
    if clave in _AUDITORES_CLAUDE:
        return ClaudeAuditBackend(model=model)
    raise ValueError(
        f"Auditor '{nombre}' no soportado todavía; usa claude "
        "(otros agentes de roster como auditor están diseñados y pendientes)."
    )


def auditar(
    backend: AuditorBackend, plan_text: str, diff: str, cwd: str
) -> dict:
    """Corre el auditor sobre el worktree y devuelve el veredicto normalizado.

    Nunca levanta por un mal veredicto: si el CLI falla o no emite un bloque
    legible, el resultado es "desconocido" (no bloquea). Solo un error de
    entorno del propio backend (binario ausente) se propaga.
    """
    _, salida = backend.run(prompt_auditoria(plan_text, diff), cwd)
    crudo = parse_auditoria(salida)
    return _a_auditoria(crudo, diff, backend.name).as_dict()


# Reexport para que quien importe el auditor tenga el prompt/rol a mano.
__all__ = [
    "Auditoria",
    "AuditorBackend",
    "ClaudeAuditBackend",
    "auditar",
    "bloquea",
    "crear_auditor",
    "parse_auditoria",
    "AUDITOR",
    "prompt_auditoria",
]

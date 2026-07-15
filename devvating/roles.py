"""Prompts de los roles del debate y constructores de mensajes por fase.

Flujo (DISENO.md sección 3, decisiones D3/D4):
  - Apertura a ciegas: ambos proponen sin verse (rol PROPONENTE).
  - Rondas de réplica: cada uno responde a la postura del otro, refina o
    mantiene el desacuerdo, y emite un veredicto de convergencia (rol RÉPLICA).
  - Ronda de inversión (modo profundo, opt-in): cada uno defiende la postura
    contraria (rol INVERSIÓN).
  - Síntesis: un agente resume acuerdos, desacuerdos y plan (rol SINTETIZADOR).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .orchestrator import DebateTopic

# Marca de veredicto que la RÉPLICA debe emitir en su última línea.
VERDICT_SI = "[CONVERGENCIA: SÍ]"
VERDICT_NO = "[CONVERGENCIA: NO]"

# --- Sesgos de agente (inclinaciones para inducir divergencia) ----------------
# Dos instancias del MISMO modelo convergen en falso (eco): comparten pesos y
# puntos ciegos, así que la divergencia hay que INDUCIRLA. Un sesgo asignado le
# da a cada agente una inclinación honesta y distinta, rompiendo el eco de un
# auto-debate (Claude vs Claude). Se inyecta SOLO en propuesta y réplica: la
# inversión ya invierte por diseño y la síntesis debe permanecer neutral.
# "neutral" ("") = comportamiento clásico sin sesgo.
SESGOS: dict[str, str] = {
    "neutral": "",
    "audaz": (
        "INCLINACIÓN ASIGNADA — audacia técnica: favorece la solución más "
        "ambiciosa y limpia aunque cueste más trabajo, empuja el cambio y "
        "cuestiona el statu quo. Adóptala con honestidad; no la finjas ni la "
        "lleves al absurdo."
    ),
    "cauto": (
        "INCLINACIÓN ASIGNADA — prudencia: prioriza el menor riesgo, la "
        "compatibilidad y el costo de mantenimiento; busca qué puede salir mal "
        "antes de aceptar una propuesta. Adóptala con honestidad; no la finjas "
        "ni la lleves al absurdo."
    ),
}

# Par por defecto para un auto-debate sin sesgos explícitos (dos inclinaciones
# opuestas: una empuja, la otra frena).
DEFAULT_SESGOS: tuple[str, str] = ("audaz", "cauto")


def con_sesgo(base: str, sesgo: str) -> str:
    """Compone un system prompt de rol con la inclinación de un agente.

    `sesgo` es el TEXTO literal de la inclinación (ya resuelto); vacío devuelve
    el rol intacto — el comportamiento clásico, sin sesgo."""
    if not sesgo:
        return base
    return f"{base}\n\n{sesgo}"


def resolver_sesgos(nombres: list[str]) -> list[str]:
    """Nombres de perfil → textos de sesgo, validando contra SESGOS."""
    if len(nombres) != 2:
        raise ValueError(
            f"Los sesgos deben ser exactamente 2 (uno por agente); recibí {len(nombres)}."
        )
    textos = []
    for n in nombres:
        clave = n.strip().lower()
        if clave not in SESGOS:
            raise ValueError(
                f"Sesgo desconocido: '{n}'. Perfiles: {', '.join(SESGOS)}."
            )
        textos.append(SESGOS[clave])
    return textos


def resolver_biases(
    nombres_sesgos: list[str], autodebate: bool
) -> tuple[list[str] | None, str | None]:
    """Resuelve los sesgos de un par a (textos | None, etiqueta legible | None).

    Sesgos explícitos → se validan y usan. Sin ellos, un auto-debate cae al par
    DEFAULT_SESGOS (para que 'claude vs claude' no sea eco puro); un par de
    familias distintas se queda sin sesgo (None, comportamiento clásico)."""
    if nombres_sesgos:
        return resolver_sesgos(nombres_sesgos), "/".join(nombres_sesgos)
    if autodebate:
        return [SESGOS[k] for k in DEFAULT_SESGOS], "/".join(DEFAULT_SESGOS) + " (auto)"
    return None, None

# --- System prompts por rol ---------------------------------------------------

PROPONENTE = (
    "Eres un ingeniero de software en una sala de debate técnico. Tu papel ahora "
    "es PROPONENTE: das tu propuesta inicial sobre el tema planteado por el "
    "vocero. Trabaja anclado en el código REAL del repositorio: usa la "
    "herramienta read_file para leer los archivos relevantes antes de opinar; no "
    "supongas su contenido. Da una postura clara y justifícala con lo que viste. "
    "Responde en español, estructurado y conciso (~250 palabras)."
)

REPLICA = (
    "Eres un ingeniero de software en una sala de debate técnico. Ya diste tu "
    "postura; ahora RESPONDES a la del otro agente. Aborda sus argumentos y "
    "refina o mantén tu posición. Si sigues en desacuerdo, señala al menos un "
    "punto concreto (nada de adulación); si te convence, dilo con honestidad. "
    "Ancla en el código real con read_file cuando aplique. Responde en español, "
    "~200 palabras.\n\n"
    "OBLIGATORIO: termina tu mensaje con una última línea que sea EXACTAMENTE "
    f"'{VERDICT_SI}' si ya coincides con la postura del otro y no tienes "
    f"objeciones nuevas, o '{VERDICT_NO}' si mantienes algún desacuerdo."
)

INVERSION = (
    "Modo profundo: INVIERTES tu papel. Construye el argumento MÁS FUERTE a favor "
    "de la postura CONTRARIA a la que venías defendiendo (steelman), como si "
    "fuera tuya, para poner a prueba tu propia posición. Ancla en el código real "
    "con read_file. Responde en español, conciso (~180 palabras)."
)

SINTETIZADOR = (
    "Eres el SINTETIZADOR de una sala de debate técnico. Se te da el tema y la "
    "transcripción del debate entre dos agentes. Produce una síntesis HONESTA "
    "para que el vocero humano decida. No escondas los desacuerdos bajo un falso "
    "consenso: hazlos explícitos. Responde en español con EXACTAMENTE estas "
    "secciones en markdown:\n"
    "## Acuerdos\n(puntos en los que ambos coinciden)\n"
    "## Desacuerdos abiertos\n(cada uno con el argumento de cada lado; si no "
    "quedó ninguno, escribe 'Ninguno')\n"
    "## Plan propuesto\n(pasos concretos y archivos a tocar; marca lo que aún "
    "depende de una decisión del vocero)"
)

# --- Constructores de prompt por fase ----------------------------------------


def _tema(topic: "DebateTopic") -> str:
    bloque = f"TEMA PLANTEADO POR EL VOCERO:\n{topic.prompt}"
    if topic.context_hint:
        bloque += (
            f"\n\nPistas de contexto (archivos posiblemente relevantes):\n"
            f"{topic.context_hint}"
        )
    return bloque


def prompt_propuesta(topic: "DebateTopic") -> str:
    return (
        f"{_tema(topic)}\n\n"
        "Da tu propuesta inicial. No conoces todavía la opinión del otro agente."
    )


def prompt_replica(
    topic: "DebateTopic",
    mi_postura: str,
    postura_otro: str,
    otro_nombre: str,
    nota_vocero: str | None = None,
) -> str:
    partes = [
        _tema(topic),
        "",
        f"TU POSTURA ACTUAL:\n{mi_postura}",
        "",
        f"POSTURA ACTUAL DEL AGENTE '{otro_nombre}':\n{postura_otro}",
    ]
    if nota_vocero:
        partes += ["", f"NOTA DEL VOCERO (tenla en cuenta):\n{nota_vocero}"]
    partes += ["", "Responde según tu papel de réplica y emite tu veredicto."]
    return "\n".join(partes)


def prompt_inversion(
    topic: "DebateTopic", mi_postura: str, postura_otro: str, otro_nombre: str
) -> str:
    return (
        f"{_tema(topic)}\n\n"
        f"POSTURA QUE VENÍAS DEFENDIENDO:\n{mi_postura}\n\n"
        f"POSTURA CONTRARIA (del agente '{otro_nombre}'), que ahora debes "
        f"defender lo mejor posible:\n{postura_otro}\n\n"
        "Construye el mejor argumento a favor de esa postura contraria."
    )


def prompt_sintesis(topic: "DebateTopic", transcripcion: str, nota_convergencia: str) -> str:
    return (
        f"{_tema(topic)}\n\n"
        f"ESTADO DE CONVERGENCIA: {nota_convergencia}\n\n"
        f"TRANSCRIPCIÓN DEL DEBATE:\n{transcripcion}\n\n"
        "Produce la síntesis en el formato indicado."
    )

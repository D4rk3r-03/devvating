# DEVVATING — Documento de Diseño

> Sistema de deliberación multi-agente para desarrollo de software.
> Dos IAs de consola (Claude y Gemini) debaten un tema propuesto por el
> **vocero** (el humano), anclados en el código real del proyecto, y
> producen un plan que el vocero aprueba antes de ejecutarse.

Estado: **borrador de diseño** · Autor: Luis · Fecha: 2026-07-01

---

## 1. Visión

DEVVATING convierte una decisión de desarrollo en un **debate estructurado**
entre dos modelos con sesgos y fortalezas distintas. El humano no es un
espectador: es el **vocero y árbitro** que plantea el tema y toma la decisión
final. El valor no está en que las IAs "se pongan de acuerdo", sino en que
**hagan explícitos sus desacuerdos** para que el humano decida con más
información.

### Principios de diseño

1. **El humano decide.** Ninguna acción irreversible ocurre sin aprobación
   del vocero. Las IAs proponen; el vocero dispone.
2. **Debate anclado en la realidad.** Durante el debate los agentes leen el
   código real del proyecto (solo lectura). No debaten sobre suposiciones.
3. **Separar deliberar de ejecutar.** El razonamiento ocurre por API
   (barato, controlado). La ejecución ocurre en una fase distinta, con
   herramientas distintas y solo tras aprobación.
4. **Desacuerdo > falso consenso.** El sistema penaliza el "sí, tienes
   razón" mutuo. La síntesis debe reportar qué quedó sin resolver.
5. **Rondas acotadas.** El debate tiene un número máximo de rondas para
   controlar costo, latencia y divagación.

---

## 2. Roles

| Rol | Quién | Responsabilidad |
|-----|-------|-----------------|
| **Vocero** | Humano (Luis) | Plantea el tema, arbitra, aprueba el plan y la ejecución. |
| **Proponente** | Agente A (p. ej. Claude) | Propuesta inicial y refinamiento. |
| **Crítico** | Agente B (p. ej. Gemini) | Critica, detecta puntos ciegos, contrapropone. |
| **Sintetizador** | Un agente (rotativo) | Resume acuerdos, desacuerdos y produce el plan. |
| **Ejecutor** | Claude Code / Gemini CLI (headless) | Aplica el plan aprobado sobre el entorno. |

> Los roles Proponente/Crítico **rotan** entre temas para no sesgar siempre
> al mismo modelo hacia el mismo papel.

---

## 3. Flujo del debate (fases)

```
┌──────────────────────────────────────────────────────────────┐
│ FASE 0 — PLANTEAMIENTO                                        │
│   El vocero define el tema: bug, mejora, feature, decisión.   │
│   El orquestador reúne contexto del repo (árbol, archivos     │
│   relevantes, git diff si aplica).                            │
└──────────────────────────────────────────────────────────────┘
                          │
┌──────────────────────────────────────────────────────────────┐
│ FASE 1 — DEBATE (herramientas de SOLO LECTURA)               │
│   Herramientas: read_file, list_dir, grep, git_diff          │
│   Apertura A CIEGAS: A y B proponen en PARALELO sin ver la    │
│     propuesta del otro (elimina anchoring de primer movimiento)│
│   Ronda 1:  A critica la propuesta de B / B critica la de A   │
│   Ronda 2:  cada uno responde, refina o mantiene desacuerdo   │
│   (máx N rondas configurable, por defecto 2)                 │
│   [opt-in "modo profundo"] Ronda de INVERSIÓN: se intercambian│
│     los roles y se stress-testea la postura contraria.        │
│   El vocero PUEDE intervenir entre rondas para reencauzar.    │
└──────────────────────────────────────────────────────────────┘
                          │
┌──────────────────────────────────────────────────────────────┐
│ FASE 2 — SÍNTESIS                                            │
│   El sintetizador produce un documento estructurado:         │
│     · Acuerdos                                               │
│     · Desacuerdos abiertos (con el argumento de cada lado)   │
│     · Plan propuesto (archivos a tocar, pasos, riesgos)      │
└──────────────────────────────────────────────────────────────┘
                          │
┌──────────────────────────────────────────────────────────────┐
│ FASE 3 — ARBITRAJE (el vocero)                               │
│   Aprueba / ajusta / rechaza el plan. Puede pedir otra ronda.│
└──────────────────────────────────────────────────────────────┘
                          │
┌──────────────────────────────────────────────────────────────┐
│ FASE 4 — EJECUCIÓN (herramientas de ESCRITURA, tras OK)      │
│   El ejecutor aplica el plan: write_file, run_command, tests.│
│   Se ejecuta vía Claude Code / Gemini CLI headless.          │
│   Resultado y diff se muestran al vocero.                    │
└──────────────────────────────────────────────────────────────┘
```

### Reglas de convergencia

- **Máximo de rondas**: por defecto 2 (configurable). Al alcanzarlo se pasa
  a síntesis aunque no haya consenso — el desacuerdo se reporta.
- **Corte temprano por consenso**: si ambos agentes coinciden explícitamente
  y no aportan argumentos nuevos, se corta y se sintetiza.
- **Anti-adulación**: el prompt del Crítico le exige señalar al menos un
  riesgo o alternativa en cada ronda; no puede limitarse a aprobar.

---

## 4. Arquitectura técnica

```
                        ┌───────────────────┐
                        │   Vocero (CLI/TUI) │
                        └─────────┬─────────┘
                                  │
                        ┌─────────▼─────────┐
                        │    Orquestador     │  ← núcleo en Python
                        │  (motor de debate) │
                        └───┬───────────┬────┘
             ┌──────────────┘           └──────────────┐
     ┌───────▼────────┐                        ┌────────▼───────┐
     │  Adapter Claude │                        │ Adapter Gemini │
     │   (API + tools) │                        │  (API + tools) │
     └───────┬────────┘                        └────────┬───────┘
             │        capa común de herramientas          │
             └──────────────────┬───────────────────────┘
                        ┌────────▼─────────┐
                        │   Tool Runtime    │  ← ejecuta en la laptop
                        │ read/list/grep/…  │
                        │ (write/run gated) │
                        └────────┬─────────┘
                                 │
                     ┌───────────▼────────────┐
                     │  Ejecutor (fase 4)      │
                     │  claude -p / gemini …   │  ← CLIs headless
                     └────────────────────────┘
```

### 4.1 Concepto clave: tool use / function calling

El modelo **no toca la máquina**. En cada turno, el orquestador le pasa una
lista de herramientas disponibles. El modelo **responde pidiendo** una
llamada (`read_file(path=...)`); el **Tool Runtime local la ejecuta** y
devuelve el resultado al modelo. Así se obtiene control total *y* acceso al
entorno, sin que el modelo tenga acceso directo al disco.

### 4.2 Adaptadores (abstracción de proveedor)

Interfaz común para no acoplar el motor a un proveedor:

```python
class AgentAdapter(Protocol):
    name: str
    def respond(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> AgentTurn: ...
    # AgentTurn = texto + (opcional) lista de tool_calls
```

Implementaciones: `ClaudeAdapter` (SDK Anthropic), `GeminiAdapter`
(SDK google-genai). Añadir un tercer modelo = un adaptador nuevo.

### 4.3 Sistema de herramientas (dos niveles de permiso)

| Herramienta | Fase debate | Fase ejecución |
|-------------|:-----------:|:--------------:|
| `read_file` | ✅ | ✅ |
| `list_dir` | ✅ | ✅ |
| `grep` | ✅ | ✅ |
| `git_diff` | ✅ | ✅ |
| `write_file` | ❌ | ✅ (tras OK) |
| `run_command` | ❌ | ✅ (tras OK) |

- Durante el debate **nadie puede escribir ni ejecutar**: seguridad por diseño.
- En ejecución, cada `write_file`/`run_command` puede requerir confirmación
  (modo interactivo) o ir por lote según lo aprobado en el plan.

### 4.4 Estrategia de ejecución (Fase 4) — decisión

**Recomendado: híbrido.** El debate se hace por API; la ejecución se delega a
**Claude Code o Gemini CLI en modo headless** (`claude -p "<plan>"`), que
*ya traen* herramientas de entorno probadas y con sus propios controles de
seguridad. Evita reimplementar `write_file`/`run_command` con todos sus
riesgos.

> Alternativa futura: ejecutor propio con el Agent SDK, si se quiere control
> más fino sobre el bucle de ejecución.

---

## 5. Stack técnico

- **Lenguaje**: Python 3.11+
- **SDKs**: `anthropic`, `google-genai`
- **Config**: `pydantic` + `.env` (API keys, límites, modelos)
- **CLI/TUI**: empezar con CLI simple (`rich` para formato); TUI (`textual`)
  como mejora posterior.
- **Persistencia**: cada debate se guarda como transcript en
  `transcripts/<fecha>-<slug>.json` (auditable y reproducible).
- **Ejecución fase 4**: subprocess a `claude -p` / `gemini`.

---

## 6. Estructura de directorios (propuesta)

```
DEVVATING/
├── DISENO.md                  # este documento
├── README.md
├── pyproject.toml
├── .env.example
├── devvating/
│   ├── __init__.py
│   ├── config.py              # modelos, límites, keys
│   ├── orchestrator.py        # motor de debate (fases, rondas)
│   ├── roles.py               # prompts de Proponente/Crítico/Sintetizador
│   ├── adapters/
│   │   ├── base.py            # AgentAdapter (Protocol)
│   │   ├── claude.py
│   │   └── gemini.py
│   ├── tools/
│   │   ├── registry.py        # ToolSpec + niveles de permiso
│   │   ├── readonly.py        # read_file, list_dir, grep, git_diff
│   │   └── execution.py       # write_file, run_command (gated)
│   ├── executor.py            # fase 4: delega a claude -p / gemini
│   └── transcript.py          # guardar/cargar debates
├── transcripts/
└── tests/
```

---

## 7. Modelo de datos (esbozo)

```python
@dataclass
class DebateTopic:
    prompt: str                 # tema del vocero
    context_files: list[str]    # archivos relevantes
    max_rounds: int = 2

@dataclass
class Turn:
    role: str                   # "proponente" | "critico" | "sintetizador"
    agent: str                  # "claude" | "gemini"
    text: str
    tool_calls: list[ToolCall]

@dataclass
class Synthesis:
    agreements: list[str]
    open_disagreements: list[Disagreement]   # argumento de cada lado
    plan: Plan                  # archivos, pasos, riesgos

@dataclass
class DebateSession:
    topic: DebateTopic
    turns: list[Turn]
    synthesis: Synthesis | None
    approved: bool
```

---

## 8. Seguridad y control de costo

- **Solo lectura en el debate**: imposible dañar el repo mientras se delibera.
- **Aprobación explícita** antes de la fase 4. Nada se escribe sin OK.
- **Sandbox de comandos**: `run_command` con lista blanca / confirmación.
- **Límites duros**: máx rondas, máx tokens por turno, máx tool-calls por
  turno → cortan divagación y gasto.
- **Transcripts**: todo debate queda auditado en disco.
- **Git como red de seguridad**: sugerir trabajar en rama y revisar el diff
  tras la fase 4.

---

## 9. Riesgos conocidos y mitigaciones

| Riesgo | Mitigación |
|--------|-----------|
| Falso consenso / adulación | Prompt del Crítico exige riesgo/alternativa por ronda. |
| Debate que no converge | Máx de rondas → forzar síntesis con desacuerdos. |
| Costo/latencia altos | Rondas y tokens acotados; solo lectura barata en debate. |
| Fragilidad al orquestar CLIs | Debate por API; CLIs solo en ejecución headless. |
| Contexto de repo demasiado grande | Selección de archivos relevantes + grep, no volcar todo. |

---

## 10. Roadmap por hitos

- **M0 — Andamiaje** ✅ (en curso): config, adaptadores Claude/Gemini y un
  `read_file` sobre un bucle de tool use. Prueba de vida en
  `devvating/pruebavida.py`. Núcleo (registro + sandbox de `read_file`)
  verificado; falta ejecutar la prueba con SDKs instaladas y claves de API.
- **M1 — Debate mínimo** ✅ (en curso): una ronda con apertura a ciegas →
  crítica cruzada → síntesis, en solo lectura. `roles.py`, `orchestrator.py`
  y CLI `debate.py` (guarda transcript). Flujo verificado end-to-end con
  stubs; falta correr con claves de API.
- **M2 — Debate completo** ✅ (en curso): N rondas de réplica con veredicto de
  convergencia, corte temprano por consenso, tope de rondas, intervención del
  vocero entre rondas (D4), ronda de inversión opt-in (D3 modo profundo) y
  reporte estructurado de desacuerdos. CLI: `--rounds`, `--profundo`,
  `--interactivo`. Verificado end-to-end con stubs; falta correr con claves.
- **M3 — Ejecución** ✅ (en curso): fase 4 vía backend headless (`claude -p`)
  tras aprobación del vocero, en una rama y mostrando diff. Freno destructivo
  (por defecto solo edición; comandos con `--allow-commands`). Salvaguardas:
  rechaza no-repo y árbol sucio. `gitutil.py`, `executor.py`, CLI `ejecutar.py`
  (`--from-transcript` enlaza con la síntesis del debate). Verificado con git
  real + backend stub; falta correr con Claude Code instalado.
- **M4 — Ergonomía** ✅ (parcial): rotación automática del sintetizador entre
  temas con estado persistente (`rotation.py`, `.devvating/state.json`); config
  de proyecto (`appconfig.py`, `.devvating.json`) con precedencia flags > config
  > default; comando unificado `devvating <subcomando>` (`__main__.py` + entry
  point). Verificado end-to-end. **Pendiente**: TUI gráfica (Textual) —
  diferida por no poder conducir/verificar una UI interactiva aquí.

---

## 11. Decisiones tomadas

### D1 — Selección de contexto: el orquestador propone, el vocero confirma

El orquestador propone los archivos relevantes (grep/heurística) **y el
vocero los confirma antes de arrancar el debate**. El checkpoint es barato y
evita que un mal contexto contamine el debate completo sin que se note hasta
el final.

### D2 — Ejecución por lote, con freno para lo destructivo

La fase 4 aplica el plan aprobado **por lote** (coherente con que el objetivo
del debate es producir un plan). Salvedades de seguridad:

- `write_file` va por lote (git cubre; se muestra el diff al terminar).
- `run_command` **pide confirmación** para acciones destructivas o que salen
  del repo (borrar, push, red, etc.), aunque el resto vaya por lote.
- La ejecución corre **en una rama** y se muestra el **diff final** al vocero.

### D3 — Rotación de roles: a ciegas por defecto, inversión opt-in

Se ataca el sesgo en tres capas:

- **Rotación entre temas** (siempre): alterna quién abre en cada debate nuevo
  → elimina el sesgo de largo plazo (que un modelo sea siempre el Crítico).
- **Apertura a ciegas** (default): en cada debate, ambos agentes producen su
  propuesta inicial **en paralelo, sin ver la del otro** → elimina el
  *anchoring* de primer movimiento con una sola tanda extra, y aporta dos
  ángulos genuinamente independientes.
- **Ronda de inversión completa** (opt-in, "modo profundo"): tras las rondas
  normales, los roles se intercambian y cada uno defiende/critica la postura
  contraria. Más exhaustivo pero ~2x costo → reservado a decisiones de alto
  impacto, no al default.

### D4 — El vocero puede intervenir a mitad del debate

El debate no es 100% autónomo: entre rondas, el vocero puede inyectar una
observación, acotar el alcance o reencauzar antes de la siguiente ronda.

### D5 — Backends mixtos por agente: API o CLI headless (2026-07-12)

Cada agente del debate puede correr por **API** (SDK + Tool Runtime propio;
requiere créditos API) o por **CLI headless** (`claude -p` / `gemini -p`;
cubierto por las suscripciones de consumidor). Se elige por agente en
`.devvating.json` (`"backends": {"claude": "api|cli", "gemini": "api|cli"}`)
o con los flags `--claude-backend` / `--gemini-backend`; default `api`.

Motivo: las suscripciones (Claude Pro/Max, Google AI Pro) no cubren las
claves API — son sistemas de facturación separados — pero sí cubren los CLI.
El diseño mixto convierte esa restricción en una opción de configuración.

Invariantes del camino CLI:
- Solo lectura vía flags del CLI (`--allowedTools Read,Glob,Grep` en Claude;
  herramientas de lectura auto-permitidas en Gemini). Garantía más débil que
  el sandbox propio de `read_file`; aceptada a sabiendas.
- El ToolRegistry local no aplica: las herramientas son las del CLI.
- El subprocess corre **sin** `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`
  heredadas — si el CLI las ve, les da precedencia sobre el login de
  suscripción y factura contra la clave (trampa verificada en real).

## 12. Preguntas abiertas (para decidir antes de M0)

Ninguna bloqueante. Las decisiones D1–D4 dejan M0 listo para empezar.
Detalles menores a afinar durante la implementación:

- Heurística concreta de selección de contexto (grep + tipos de archivo).
- Formato exacto del transcript y del reporte de síntesis.

## 13. Mejoras futuras (backlog, post-M4)

- ~~Contador de tokens y costos~~ — **IMPLEMENTADO (2026-07-12)** según el
  plan del primer debate: `TurnUsage` en `adapters/base.py` con accessor
  `last_usage` por turno en los cuatro adaptadores (API suma las iteraciones
  del bucle de tool use; CLI mapea el JSON de `claude -p`); el orquestador
  copia a `Turn.usage` y totaliza por agente + global en
  `DebateSession.usage_totals` (persistido en el transcript); tabla de
  precios en `pricing.py` (decisión del vocero delegada: módulo propio,
  reporte en resumen final + transcript, tarifa desconocida → costo None).
  Nota: Anthropic no expone endpoint público de "saldo restante" para claves
  normales; el conteo local por sesión es el camino. Para estimar antes de
  correr: `client.messages.count_tokens`.
- ~~Restricción de facturación: suscripción no cubre claves API~~ —
  **resuelto con D5** (backends mixtos, implementado en M5 el 2026-07-12).
  El plan de implementación del contador de tokens quedó afinado por el
  primer debate real de DEVVATING (transcript
  `20260712-202258-el-contador-de-tokens-y-costos.json`): dataclass
  `TurnUsage` en `adapters/base.py` + accessor `last_usage` por adaptador
  (sin cambiar la firma de `converse`), el orquestador copia a `Turn.usage`
  y totaliza en `DebateSession`, tabla de precios fuera de los adaptadores.
- TUI gráfica (Textual) — diferida de M4.

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
  point). Verificado end-to-end. La TUI gráfica (Textual) que quedaba
  pendiente se **cerró formalmente** a favor del Devvating Hub (M7): la sala
  web cumple la misma necesidad (ver el debate en vivo e intervenir) sin la
  reescritura async que exigía Textual. Ver §13.

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

### D6 — Rumbo de la interfaz: incremental, streaming diferido (2026-07-13)

Decidido tras un debate profundo de la propia herramienta sobre su interfaz
(transcript `20260713-135839-*`) y el arbitraje del vocero:

- **M6a (implementado)**: heartbeat en la CLI (spinner con agente, etapa y
  cronómetro sobre los eventos `*_inicio/*_fin` existentes; solo el callback
  de `debate.py`) + comando `devvating reporte <transcript.json>` → HTML
  estático autocontenido (`reporte.py`; puro renderizado, no toca el motor).
- **TUI (Textual): descartada** — exigiría reescritura async
  (`on_intervention` es input bloqueante) y el motivo del aplazamiento de M4
  sigue vigente. **Cierre formal (2026-07-20)**: el Hub (M7) la dejó
  redundante; se retira del backlog (§13), no se retoma.
- **Streaming: DIFERIDO hasta la sala web (M7)** — decisión del vocero sobre
  las 3 opciones que dejó la ronda de inversión. Motivos: el orquestador
  necesita réplicas completas (el streaming es cosmético aquí), la marca
  `[CONVERGENCia]` se vería cruda en un flujo parcial, y el salto a
  `Popen + stream-json` se amortiza mejor cuando exista la web.
- **M7 — Devvating Hub (v1 implementado 2026-07-14)**: el vocero activó el
  gate de demanda y la sala web existe — `devvating hub` (FastAPI +
  websocket en `hub.py`; extra `[hub]`) + front React/Vite en
  `devvating-ui/` (npm run build → dist servido por el propio hub). Cumple
  D6: el motor sigue ciego a la UI (`_debate_worker` es otro consumidor de
  `on_event`), los debates pasados se ven con `reporte.render_html`
  reutilizado tal cual, y la persistencia es la misma `_save_transcript` de
  la CLI. V1: un debate a la vez; el front es autocontenido (sin fuentes ni
  CDNs externos).
- **M7 v1.1 (2026-07-14, plan del primer debate corrido EN el propio Hub —
  transcript `20260714-034445-*`)**: ciclo de vida completo en el navegador.
  Fase 2: intervención del vocero — checkbox "interactivo", el hilo del
  debate espera en `_esperar_nota` (cola + timeout de 5 min para no quedar
  rehén de una pestaña cerrada) la nota que llega por `POST
  /api/intervencion`; mismo contrato `on_intervention` de la CLI. Fase 3:
  botón "Ejecutar plan" en la síntesis → `POST /api/ejecutar` corre el
  `Executor` (SIEMPRE `allow_commands=False`; ese opt-in es exclusivo de la
  CLI) y el visor de diff coloreado muestra el resultado. **Decisión del
  vocero que el debate dejó abierta, resuelta con el default conservador**:
  el Hub se detiene en staging + diff; commit/descartar sigue siendo manual
  (git/CLI). Amabilidad: si el árbol está sucio solo por artefactos del
  debate, el error sugiere gitignorear `transcripts/` y `.devvating/`.

### D7 — Banco de agentes plugable (2026-07-13)

Motivo (diagnóstico del vocero, con evidencia en transcripts): un modelo
pequeño debatiendo contra uno grande no debate — defiere. La asimetría
flash-lite vs Opus producía adulación y socavaba el valor adversarial. La
solución estructural no es coronar a un lado sino **nivelar el banco**:

- `agentes.py`: roster nombre→fábrica (`claude-api`, `claude-cli`,
  `gemini-api`, `gemini-cli`, `antigravity`, `kimi`; alias `agy`). El par se
  elige con `--agentes a,b`, con `"agentes": [...]` en `.devvating.json`, o
  cae al par clásico de D5. Validación: exactamente 2, identidades distintas.
- Nuevos adaptadores sobre `PlainCliAdapter`: **Antigravity** (`agy -p`, usa
  el modelo default del CLI — p. ej. Gemini 3.1 Pro — o `--model`) y **Kimi**
  (`kimi -p --output-format text`, diversidad de familia de modelos).
- `--synthesizer` acepta ahora el nombre de cualquier agente del par.
- `env_suscripcion()` ampliada: también quita `GEMINI_API_KEY`,
  `GOOGLE_API_KEY` y `GOOGLE_CLOUD_PROJECT` — trampa gemela a la de
  Anthropic: el CLI de Google desvía la facturación al proyecto/clave
  heredados (verificado en real: agy atado a un proyecto GCP sin billing).

### D8 — Separación de modelos por fase: razonar ≠ ejecutar (2026-07-14)

Decisión del vocero: los modelos de más razonamiento (Opus/Fable, Gemini
Pro) se reservan para el DEBATE — donde el valor está en pensar — y la
EJECUCIÓN usa un modelo ejecutor disciplinado: **Sonnet** (default
`sonnet`, que el CLI resuelve al Sonnet vigente — 5 hoy, 4.x como
respaldo). Razones: el plan ya llega cerrado y el prompt del ejecutor exige
interpretación conservadora sin mejoras extra (el trabajo es de obediencia,
no de creatividad); Sonnet es más rápido y consume mucha menos cuota de
suscripción por ejecución. Configurable con `DEVVATING_EXEC_MODEL` o
`devvating ejecutar --model`; el Hub hereda el mismo default.

### D9 — Auto-auditoría de seguridad del Hub (2026-07-15)

DEVVATING se auditó a sí mismo como herramienta de desarrollo (transcript
`20260715-200644-audita-devvating-como-herramienta-de-desarrollo.json`). El
plan salió en cuatro pasos; se documenta aquí porque hasta ahora solo vivía en
mensajes de commit:

- **Paso 1 — Hotfix del `returncode` (consenso, hecho).** `claude -p` podía
  salir con `code != 0` y el Hub igual presentaba éxito y ofrecía commit —
  señal de éxito falsa sobre un plan roto. `_difundir` lee ahora el
  `returncode` (que ya viajaba en `ExecutionOutcome`) y, si es distinto de 0,
  guarda `ultima_ejecucion` marcada: el diff se muestra pero el botón de
  commit queda bloqueado (`/api/commit` responde 409); el descarte sigue
  disponible. Con regresión en `tests/`. Commit `899a2ac`.
- **Paso 2 — Aislamiento por `git worktree` (raíz arquitectónica). PENDIENTE
  de decisión del vocero.** La raíz de los problemas es el árbol de trabajo
  *compartido*: entre `create_branch` y el commit/descarte, `claude -p` muta
  el árbol vivo del vocero; un agente que aborta a medias lo deja tocado, y
  el `reset --hard` de `discard_branch` es incondicional. La solución
  acordada es `git worktree add` a un directorio desechable (el árbol del
  vocero no se toca; neutraliza además el TOCTOU de estado mono-usuario). El
  desacuerdo abierto **no es la solución sino el calendario**: trabajo
  comprometido inmediato (por el `reset --hard` activo) vs. hoja de ruta con
  su mantenimiento presupuestado aparte (worktrees colgados, choques con
  submódulos/LFS/hooks, concurrencia que hoy no existe en un Hub mono-usuario
  en `127.0.0.1`). **No implementado**: espera arbitraje del vocero.
- **Paso 3 — Confinar el `repo` objetivo (seguridad, hecho).** `/api/ejecutar`
  aceptaba una ruta arbitraria en el cuerpo → agujero de escritura vía
  navegador. Ahora la ejecución se confina al repo servido al arrancar el Hub
  (se ignora cualquier override del cuerpo). Commit `ef62268`.
- **Paso 0 — Token anti-CSRF (parte del "evaluar CSRF/CORS" del paso 3,
  hecho).** Aun con el bind a `127.0.0.1`, cualquier página abierta en el
  mismo navegador podía disparar los POST mutantes vía `fetch`. Se genera un
  token por proceso, se entrega en `/api/roster` y se exige en cada POST
  mutante (`X-Devvating-CSRF`); un atacante cross-origin puede disparar la
  petición pero no leer el token por la política de mismo origen. Introducido
  junto al commit `5eb63a7`.

### D10 — Decisiones del vocero: de síntesis abierta a plan cerrado (2026-07-20)

Diseñado debatiéndolo EN DEVVATING (transcript
`20260720-152141-*`, no convergió — prueba viva de que hacía falta) y
arbitrado por el vocero. Motivo: la síntesis dejaba decisiones en prosa
("depende de tu decisión") que rompían el ejecutor — al aplicar un plan con
"Decides: X o Y", `claude -p` resolvía la ambigüedad en silencio y entraba en
bucle. Cuatro fases, todas implementadas:

- **F1 — Contrato/parser**: `roles.SINTETIZADOR` emite al final un bloque JSON
  `{"decisiones":[{id,pregunta,opciones,recomendada,crucial,contra}]}` (cada
  opción cita agente+ronda). `orchestrator._parse_decisiones` lo localiza por
  marcador y lo corta con `json.raw_decode` (respeta anidamiento, a diferencia
  del regex del veredicto); fallback a `[]`. El bloque se despoja del texto
  visible. `Decision` + tercer estado nominal `convergido`/`abierto`/
  `pendiente_decision` (este manda si hay una crucial sin resolver).
- **Verificación de citas BLANDA** (arbitraje del vocero): `_verificar_contra`
  marca `contra_en_debate=False` si el fragmento citado no aparece en la
  transcripción; la UI muestra ⚠ pero NO bloquea ni degrada el texto (respeta
  la doctrina "el parser mira forma, nunca contenido").
- **F2 — UI de resolución**: el evento `fin` lleva las decisiones y el estado;
  `POST /api/decisiones` persiste la resolución (elección propia incluida, y
  confirmar/desmarcar `crucial`) en el transcript. Panel en el Hub con
  opciones + recomendada + escribir la tuya.
- **F3 — Ronda de cierre**: `POST /api/cerrar-plan` reusa los turnos
  (`old_session`, como el resume) e inyecta las decisiones resueltas como
  restricciones fijas; fija `min_rounds = max_rounds = rondas+1` para forzar
  UNA ronda nueva sin que una convergencia previa corte antes. Produce un plan
  cerrado.
- **F4 — Gate del executor** (arbitraje: override opt-in): `Executor.execute`
  levanta `ExecutorError` antes de crear rama si el plan trae decisiones
  crucial sin resolver, salvo `allow_open_decisions`; una sola verdad
  (`decisiones_crucial_sin_resolver`) que el Hub traduce a 422 y el CLI a
  `--allow-open-decisions`. Mata el bucle de raíz.

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
- ~~Resiliencia ante fallos del proveedor~~ — **IMPLEMENTADA (2026-07-13)**
  según el plan del primer debate del banco nivelado (antigravity vs claude,
  transcript `20260713-145857-*`): taxonomía en `adapters/base.py`
  (`AgentError` / `TransientProviderError` / `SessionLimitError(resets_at)`),
  clasificación en los adaptadores (`clasificar_fallo` textual en CLI,
  traducción de excepciones SDK en API), reintento con backoff en el
  orquestador (`_converse_con_reintento`, esperas fijas 5/15/45s, evento
  "reintento" al vocero), límite de sesión y fallos desconocidos abortan de
  inmediato, y `DebateAbortedError` entrega la sesión parcial que `debate.py`
  vuelca a `transcripts/*.partial.json` con mensaje humano (hora de reset
  incluida). El comando de reanudación llegó al día siguiente:
  **`--resume <x>.partial.json` (implementado 2026-07-14, dirigido por el
  vocero)** — carga la sesión parcial y hace fast-forward de los turnos ya
  pagados (por ronda/fase/agente), retomando exactamente donde se cortó.
  Estrenado en real reanudando la auditoría de FIEL-FILE tras un corte de
  cuota, sin repetir ni un turno.
- ~~TUI gráfica (Textual)~~ — **CERRADA (2026-07-20)** a favor del Devvating
  Hub (M7): la sala web cubre la misma necesidad sin la reescritura async que
  Textual exigía. Retirada del backlog; no se retoma (ver D6, M4).
- **Aislamiento por `git worktree` en la ejecución** — pendiente de decisión
  del vocero (paso 2 de la auto-auditoría, D9): reemplazar el `checkout -b`
  sobre el árbol vivo compartido por `git worktree add` desechable. Diferido
  hasta que el vocero arbitre calendario (trabajo comprometido vs. hoja de
  ruta con mantenimiento presupuestado).
- **Streaming de tokens en el Hub** — siguiente bloque de trabajo (deuda de D6
  cuya condición "cuando exista la web" ya se cumplió con M7). Camino CLI
  primero (`ClaudeCliAdapter` → `Popen` + `--output-format stream-json` con
  lector incremental), evento opcional `on_delta` en el contrato sin tocar
  `orchestrator._parse_verdict`, y degradación explícita "sin soporte de
  streaming" para los adaptadores que no emiten deltas.

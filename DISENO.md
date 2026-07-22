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
- **Paso 2 — Aislamiento por `git worktree` (raíz arquitectónica).
  IMPLEMENTADO (2026-07-20).** La raíz de los problemas era el árbol de trabajo
  *compartido*: entre `create_branch` y el commit/descarte, `claude -p` mutaba
  el árbol vivo del vocero, y el `reset --hard` de `discard_branch` era
  incondicional. Ahora `Executor.execute` crea un `git worktree add` a un dir
  desechable (`gitutil.add_worktree`) y corre el agente con `cwd` ahí; el árbol
  del vocero no se toca. El commit ocurre en el worktree (sobre la rama
  `devvating/`) y luego se quita; descartar es `discard_worktree` (quitar
  worktree + borrar rama, sin `reset --hard` sobre el árbol vivo). Como el
  worktree se ramifica de HEAD y aísla, se **relajó la exigencia de árbol
  limpio** (decisión del vocero): se ejecuta aunque el árbol tenga trabajo sin
  confirmar, y muere el tropiezo del "transcript recién escrito ensucia el
  árbol". **Trampa verificada**: el worktree va en el **temp del sistema**, NO
  bajo `.git/` — un worktree dentro de `.git` confunde a `claude -p` (lo trata
  como interno y no escribe ahí). Path con sufijo `uuid` (no timestamp) para no
  colisionar entre ejecuciones del mismo segundo.
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

### D11 — Estado de la ejecución que sobrevive al reinicio (2026-07-22)

Debatido EN DEVVATING (transcript `20260722-093004-*`, convergieron en la
ronda 2 — primer debate con Antigravity leyendo de verdad el repositorio) y
arbitrado por el vocero. Motivo: reiniciar el Hub costaba perder la ejecución
que esperaba decisión. El worktree y la rama seguían en disco, pero
`app.state.ultima_ejecucion` vivía solo en memoria, así que ya no se ofrecía
commitear ni descartar el trabajo que estaba a la espera.

Principio que ordena la solución: **no se persiste estado en paralelo a git**.
Un worktree bajo `devvating/` con cambios sin commitear ES la ejecución
pendiente, y `gitutil.list_worktrees` ya lo reporta. Solo se guarda aparte lo
que git no puede saber — sobre todo el `returncode` del backend, que es quien
decide si el Hub deja commitear (no se presenta como bueno un plan roto).

- **Dónde vive (decisión D1 del vocero)**: en el directorio ADMINISTRATIVO del
  worktree (`<repo>/.git/worktrees/<n>/devvating-ejecucion.json`), no dentro de
  su árbol. Dentro, `executor.stage_all` (`add -A`) lo metería en el staging,
  en el diff que revisa el vocero y en el commit. Verificado en real: en el dir
  administrativo `git add -A` no lo ve **y** `git worktree remove` se lo lleva
  igual, así que conserva la virtud que defendía la opción in-tree.
- **Quién lo escribe (decisión D2)**: el `Executor`, no el consumidor de la
  cola del Hub. Escribe un marcador `en_curso` antes de lanzar el backend y el
  sidecar completo al terminar; si el proceso muere a mitad, la ausencia de
  `returncode` significa inequívocamente "no terminó".
- **Degradado conservador**: sin sidecar o sin `returncode`, la ejecución se
  recupera igual (el diff se lee de git) pero NO se ofrece commitear — solo
  descartar. `commit_cambios` ya comparaba `!= 0`, así que `None` bloquea sin
  código extra.
- **Varias pendientes**: se rehidrata la más reciente por marca de tiempo del
  sidecar; el resto siguen visibles en `/api/worktrees`. Hueco que ninguno de
  los dos agentes trató y que detectó el sintetizador.
- `GET /api/ejecucion-pendiente` la expone al front con su diff leído al vuelo,
  para que la recuperación sea visible y no solo commiteable.

Pendientes de las fases B y C del mismo plan: multi-repo por `repo_id` contra
lista blanca (roster autónomo por CLI, decisión D3 del vocero) e índice global
de ejecuciones en `~/.devvating/` como punteros reconstruibles, nunca como
reemplazo de los transcripts junto a su repo.

### D12 — Multi-repo en el Hub por `repo_id` (fase B de D11, 2026-07-22)

El Hub servía UN repositorio, el de su arranque, así que trabajar en otro
proyecto obligaba a reiniciarlo. La fase B lo abre sin tocar la salvaguarda de
D9, que prohíbe aceptar rutas del sistema en el cuerpo de una petición.

- **Lista blanca dada de alta por CLI** (decisión D3 del vocero): `devvating
  hub --repo a --repo b`. El roster es autónomo y no depende del índice global
  (fase C), que llegaría después y habría invertido el orden acordado.
- **El cliente elige por `repo_id` opaco**, derivado del nombre del directorio
  y desambiguado con sufijo si dos repos comparten basename. Una ruta como id
  no resuelve: se responde 404. Verificado en real con `/tmp/...`, `/etc`,
  `../beta` y rutas escapadas.
- **Estado por repo**: `app.state.ejecuciones` es un dict `repo_id → ejecución
  pendiente`, no un escalar. La ejecución de un repo no puede pisar la de otro,
  y la rehidratación del arranque (D11) recorre todos los registrados.
- **Compatibilidad**: `crear_app(repo=...)` con uno solo se comporta igual que
  antes y el selector ni aparece en la UI. Quien no manda `repo_id` opera sobre
  el primero.

Queda pendiente la fase C: índice global en `~/.devvating/` como punteros
reconstruibles a los transcripts, que siguen viviendo junto a su repo.

### D13 — Índice global de debates (fase C de D11, 2026-07-22)

Última pieza del plan debatido: una vista de todo lo que devvating ha hecho en
la máquina, sin recorrer proyecto por proyecto.

- **Índice, no reemplazo** (lo que el debate zanjó): los transcripts siguen
  junto a su repo, porque un debate es SOBRE un repositorio y así su historia
  viaja con el clon. `~/.devvating/registro.db` (SQLite, por atomicidad entre
  CLI y Hub) guarda **solo punteros y metadatos**: repo, ruta, tema, fecha,
  estado, decisiones abiertas y coste. Nunca la síntesis ni los turnos.
- **Prescindible por diseño**: al no duplicar contenido no puede contradecir a
  la fuente, y `devvating historial --reindexar` lo reconstruye entero desde
  los transcripts. Hay un test que borra el `.db` y lo recupera sin pérdida.
- **Alta automática en `debate._save_transcript`**, el punto único por el que
  pasan la CLI y el Hub. Nunca levanta: el índice es una comodidad y un fallo
  suyo no puede costar un debate ya pagado.
- **`devvating historial`** lista lo indexado (`--pendientes` filtra lo que
  espera algo del vocero: decisiones abiertas o debates a medias). Las entradas
  cuyo transcript ya no existe se marcan `✗` en vez de fingirse vivas, y
  `--limpiar` las olvida.
- **Trampa de la suite**: varios tests del Hub corren el debate en un hilo
  daemon que puede terminar tras el teardown, cuando monkeypatch ya restauró el
  entorno — y daban de alta en el `~/.devvating` real (verificado: aparecieron
  debates `test_*` en el historial del vocero). Por eso el aislamiento del
  registro es un fixture de SESIÓN, no por test.

Con esto quedan cerradas las tres fases de D11.

### D14 — El merge entra en la web; el push no (2026-07-22)

Arbitrado por el vocero al fijar la hoja de ruta hacia "toda la gestión desde
el navegador, la consola solo para lanzar el Hub". Cerrar el ciclo exigía
llevar el trabajo de una rama `devvating/` a la rama de trabajo, y ahí hay un
salto: hasta ahora el Hub solo escribía en ramas de ejecución y worktrees
desechables, cosas que si salen mal se borran y no pasó nada.

- **`merge` sí** (`POST /api/ramas/fusionar`). Es recuperable con git y es el
  paso que faltaba para no volver a la terminal tras aprobar un plan.
- **`push` no**, y no por falta de tiempo: publicar es lo único de la cadena
  que sale de la máquina y que no se deshace. Sigue siendo un acto deliberado
  en consola. Hay un test que verifica que el endpoint NO existe, para que no
  reaparezca por descuido.

Tres guardas antes de tocar la rama de trabajo, todas con su porqué:

1. **Solo ramas `devvating/`** — mismo criterio que ya regía para borrarlas.
2. **Árbol limpio** — con cambios sin confirmar, un conflicto dejaría el merge
   a medias, y desde el navegador no hay herramientas para resolverlo.
3. **Sin ejecución abierta en esa rama** — es trabajo que el vocero todavía no
   revisó; fusionarlo sería decidir por él.

Ante conflicto, `gitutil.merge` dispara `merge --abort`: el árbol queda
exactamente como estaba y el error se reporta entero. Verificado en real — sin
marcas de conflicto, sin `MERGE_HEAD`, repositorio limpio.

Con esto la consola queda para: lanzar el Hub, publicar, y registrar repos o
inicializarlos con git (bloque 3, pendiente de debate porque choca con D3/D9).

### D15 — Descubrir y registrar proyectos desde la web (bloque 3, 2026-07-22)

Último paso hacia "toda la gestión en el navegador". Debatido EN DEVVATING
(transcript `20260722-155107-*`) y arbitrado por el vocero. **No hizo falta
relajar D9**: el cuerpo de una petición sigue sin poder nombrar una ruta.

- **Raíces declaradas al arrancar** (`devvating hub --raiz <dir>`, repetible).
  Sin ellas no hay descubrimiento y el Hub se comporta como antes. Es lo único
  que sigue exigiendo consola, y una sola vez — no por proyecto.
- **`cand_id` opaco**: `GET /api/candidatos` escanea 2 niveles bajo las raíces
  y devuelve ids que el servidor asocia a rutas en su memoria. El cliente elige
  de esa tabla; un id ausente es 404, igual que `repo_id`. Confinamiento con
  `realpath` + `commonpath`, así que un symlink dentro del workspace que
  apunte fuera tampoco pasa.
- **`POST /api/repos`** registra en caliente e indexa sus debates;
  **`POST /api/repos/init`** hace `git init` + primer commit y registra.

Guardas de `gitutil.init_inicial`, todas nacidas de objeciones del debate:

1. **No sobre directorio vacío** — no hay nada que debatir, y commitear
   exigiría inventar contenido del proyecto.
2. **Ni sobre un repo, ni anidado en otro** — historias solapadas y worktrees
   saliendo del árbol equivocado.
3. **Rechaza si hay `.env`/`.venv`/`node_modules` sin `.gitignore`**, con un
   error accionable. Verificado antes de implementarlo: un `add -A` ciego mete
   el `.env` en el primer commit y `git show` recupera la clave, aunque después
   se borre el archivo. NO se genera el `.gitignore`: decidir qué se versiona
   en un proyecto ajeno es del vocero.

Nota del debate: no convergió formalmente (claude votó "no" las dos rondas),
pero sus tres objeciones están en el plan y su última réplica dice «con esa
guarda dentro, compro el diseño completo». Otro caso del artefacto de las
réplicas simultáneas — y esta vez el sintetizador (antigravity) declaró
"ningún desacuerdo" sin señalar la contradicción, cosa que claude sí hizo
cuando le tocó sintetizar en D11.

Trampa hallada al probar con directorios reales: `is_git_repo` mira hacia
arriba, así que cada carpeta interna de un repo (`docs/`, `src/`, `tests/`)
aparecía como proyecto registrable. De ahí `gitutil.es_raiz_de_repo` y que el
escaneo no descienda dentro de un repositorio ya visto — pero sí bajo una
carpeta contenedora sin git, que es como cuelgan muchos proyectos.

### D16 — Auditoría de la ejecución: guarda determinista primero (2026-07-22)

El vocero preguntó si se comprueba que lo ejecutado corresponde al plan. No se
comprobaba: la fase 5 (`--verificar`) corre los tests del proyecto, que miden
su SALUD, no la correspondencia. Un agente puede aplicar algo distinto al plan
y los tests pasar igual — ocurrió: un plan de cuatro ediciones sobre
documentación terminó tocando un único `.log` sin relación, y se descubrió
revisando el diff a mano.

Debatido EN DEVVATING (transcript `20260722-165849-*`, cortado por cuota
agotada pero **convergido de hecho**: antigravity votó "sí" y aceptó las tres
correcciones de claude diciendo "no tengo más objeciones"). Diseño acordado:

1. **Guarda determinista ANTES del modelo** — `executor.correspondencia()`
   cruza las rutas que el plan nombra con `changed_files`, que ya se calculaba.
   Coste cero, sin alucinación posible. **Implementada.** Verificada contra los
   dos casos reales: marca la ejecución mala y NO marca la buena.
2. **Auditor: tercero limpio** — ni el ejecutor (se autoevaluaría) ni un
   debatiente (llega con postura). Mismo `HeadlessBackend`, `--allowedTools
   Read,Glob,Grep`. *Pendiente.*
3. **Anti-complacencia** — invertir la carga de la prueba (pedirle listar solo
   lo no solicitado y lo omitido, no "validar") **más** exigirle cita textual
   verificable contra el diff, al estilo de `_cita_localizada`: invertir la
   carga sin verificar solo cambia la dirección del sesgo. *Pendiente.*
4. **Efecto: bloqueo blando con escape explícito**, como el gate de decisiones
   (409 + `forzar`). *Pendiente.*
5. **Fallback = NO bloquear.** Un JSON roto del auditor es un fallo SUYO, no
   evidencia contra el diff; bloquear ahí castiga trabajo bueno por un error de
   formato de un tercero. Coherente con `_parse_verdict` → `None` y
   `_parse_decisiones` → `[]`.

La guarda determinista es una SEÑAL, no un veredicto: un plan puede nombrar
archivos que solo cita, y una ejecución legítima puede tocar algo que el plan
no nombró. Se muestra junto al diff, que es donde el vocero decide, y viaja en
el sidecar para sobrevivir a un reinicio.

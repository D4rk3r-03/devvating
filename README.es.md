# DEVVATING

[![tests](https://github.com/D4rk3r-03/devvating/actions/workflows/tests.yml/badge.svg)](https://github.com/D4rk3r-03/devvating/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)

**Sala de debate multi-agente para desarrollo de software.** Dos agentes de IA
(Claude y Gemini) debaten un tema anclado en tu código real y producen un
plan; tú actúas como *vocero y árbitro* — planteas el tema, conduces el
debate, y nada se ejecuta sin tu aprobación explícita.

📖 [English documentation](./README.md) · Diseño completo: [`DISENO.md`](./DISENO.md) · Protocolo operativo: [`docs/PROTOCOLO.md`](./docs/PROTOCOLO.md)

## Cómo funciona

```
1. PLANTEAMIENTO  formulas una pregunta debatible sobre tu código
2. DEBATE         apertura a ciegas (ambos proponen sin verse)
                  → rondas de réplica con reglas de convergencia → síntesis
3. ARBITRAJE      la síntesis reporta acuerdos, desacuerdos ABIERTOS y un plan
4. EJECUCIÓN      solo tras tu aprobación; en una rama git nueva; diff al final
5. CIERRE         revisas el diff y commiteas — o descartas la rama
```

La fase de debate es estrictamente de **solo lectura**: los agentes pueden
leer tu repositorio pero nunca escribir. La ejecución se delega a un agente
de código headless en una rama dedicada, y nada se commitea automáticamente.

## Requisitos

- Python 3.11+
- Por agente, **uno** de los dos backends:
  - `api` — una clave de API ([Anthropic](https://console.anthropic.com) y/o [Google AI Studio](https://aistudio.google.com))
  - `cli` — el CLI del agente instalado y con sesión iniciada ([Claude Code](https://code.claude.com) y/o [Gemini CLI](https://github.com/google-gemini/gemini-cli)), cubierto por una suscripción de consumidor

## Instalación

```bash
git clone https://github.com/D4rk3r-03/devvating.git
cd devvating
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Si usas backends API, añade tus claves:
cp .env.example .env    # y rellena ANTHROPIC_API_KEY / GEMINI_API_KEY
```

## Inicio rápido

```bash
# 1) Prueba de vida — cada agente debe leer un archivo del repo y resumirlo:
devvating pruebavida README.md

# 2) Primer debate (por defecto ambos agentes vía API):
devvating debate "¿Conviene extraer el bucle de tool use de los adaptadores?" \
    --files "devvating/adapters/claude.py, devvating/adapters/gemini.py"

# 3) Convertir cualquier debate en un reporte HTML autocontenido:
devvating reporte transcripts/<fecha>-<tema>.json

# 4) Ejecutar la síntesis aprobada sobre un repositorio git objetivo:
devvating ejecutar --repo /ruta/al/proyecto \
    --from-transcript transcripts/<fecha>-<tema>.json

# 5) Recuperar los worktrees aislados que dejaron ejecuciones pasadas:
devvating limpiar --repo /ruta/al/proyecto

# 6) O todo desde el navegador — la sala de debate, en vivo:
pip install -e ".[hub]"
cd devvating-ui && npm install && npm run build && cd ..
devvating hub          # → http://127.0.0.1:8777
```

Cada debate imprime la síntesis (acuerdos / desacuerdos abiertos / plan), un
resumen de tokens y costo por agente, y guarda el transcript JSON completo en
`transcripts/`.

### Opciones del debate

| Flag | Efecto |
|------|--------|
| `--files "a.py, b.py"` | Pista de contexto — archivos que los agentes deben mirar primero |
| `--rounds N` | Tope de rondas de réplica (default 2; corta antes si convergen) |
| `--interactivo` | Te deja inyectar una nota entre rondas |
| `--profundo` | Añade una ronda de inversión (cada agente defiende la postura contraria) |
| `--synthesizer claude\|gemini\|auto` | Quién escribe la síntesis (`auto` rota) |
| `--claude-backend api\|cli` | Claude vía SDK (créditos API) o `claude -p` (suscripción) |
| `--gemini-backend api\|cli` | Gemini vía SDK o `gemini -p` (suscripción) |
| `--agentes a,b` | Cualquier par del roster: `claude-api`, `claude-cli`, `gemini-api`, `gemini-cli`, `antigravity` (el `agy` de Google), `kimi` |

> 💡 **Combinación de costo cero**: `--claude-backend cli` (suscripción
> Claude Pro/Max) + Gemini en el tier gratuito de la API corre el pipeline
> completo sin créditos API.

### Guardas de la ejecución

`devvating ejecutar` rechaza árboles git sucios, trabaja siempre en una rama
nueva `devvating/<slug>-<fecha>`, por defecto solo permite editar archivos
(`--allow-commands` es un opt-in explícito y con advertencia), y deja los
cambios **en staging, nunca commiteados** — el juicio final es tuyo.

## Configuración

Los defaults del proyecto viven en `.devvating.json` (ver
`.devvating.example.json`); los flags CLI siempre mandan:

```json
{
  "rounds": 2,
  "deep_mode": false,
  "auto_rotate": true,
  "backends": { "claude": "cli", "gemini": "api" }
}
```

Variables de entorno (o `.env`):

| Variable | Default | Propósito |
|----------|---------|-----------|
| `ANTHROPIC_API_KEY` | — | Backend API de Claude |
| `GEMINI_API_KEY` | — | Backend API de Gemini |
| `DEVVATING_CLAUDE_MODEL` | `claude-opus-4-8` | Modelo de Claude (backend api) |
| `DEVVATING_GEMINI_MODEL` | `gemini-3.5-flash` | Modelo de Gemini (backend api) |
| `DEVVATING_MAX_TOOL_ITERATIONS` | `8` | Tope del bucle de herramientas por turno |
| `DEVVATING_EXEC_MODEL` | `sonnet` | Modelo del agente ejecutor (fase 4) — los modelos de razonamiento quedan para el debate |

## Estructura

```
devvating/
├── __main__.py          # CLI unificada: devvating debate|ejecutar|pruebavida
├── orchestrator.py      # motor del debate: rondas, convergencia, totales de uso
├── roles.py             # prompts por rol: proponente, réplica, inversión, sintetizador
├── executor.py          # fase de ejecución: backend headless + rama + diff
├── pricing.py           # tabla de precios (fuera de los adaptadores)
├── adapters/            # una interfaz, cuatro implementaciones
│   ├── base.py          #   protocolo AgentAdapter + TurnUsage
│   ├── claude.py        #   SDK anthropic + bucle de tool use manual
│   ├── gemini.py        #   SDK google-genai + function calling
│   └── cli.py           #   CLIs headless (cubiertos por suscripción)
└── tools/               # tool runtime local de solo lectura, confinado al repo
tests/                   # suite pytest — sin claves API
```

## Desarrollo

```bash
pytest              # 61 tests, <3s, completamente offline
```

## Créditos

Idea y dirección: [D4rk3r-03](https://github.com/D4rk3r-03) · Ingeniería: Claudio

```
   /\_/\
  ( o.o )  ✳
   > ^ <
```

## Licencia

[MIT](./LICENSE)

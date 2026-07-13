# DEVVATING

[![tests](https://github.com/D4rk3r-03/devvating/actions/workflows/tests.yml/badge.svg)](https://github.com/D4rk3r-03/devvating/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)

Sala de debate multi-agente para desarrollo de software. Dos IAs de consola
(Claude y Gemini) debaten un tema anclado en el código real y producen un plan;
tú (el **vocero**) planteas el tema, arbitras y apruebas antes de ejecutar.

- Diseño completo: [`DISENO.md`](./DISENO.md)
- Protocolo operativo (flujo vocero + checklist de seguridad): [`docs/PROTOCOLO.md`](./docs/PROTOCOLO.md)

## Estado

**M0–M3 completos, M4 parcial** (roadmap en `DISENO.md` §10):

| Hito | Qué cubre | Estado |
|------|-----------|--------|
| M0 | Andamiaje: adaptadores Claude/Gemini + bucle de tool use + `read_file` sandboxeado | ✅ |
| M1 | Debate básico: apertura a ciegas → crítica → síntesis | ✅ |
| M2 | Debate multi-ronda con convergencia, intervención del vocero, modo profundo | ✅ |
| M3 | Ejecución híbrida: plan aprobado → rama git → `claude -p` headless → diff | ✅ |
| M4 | Rotación persistente + `.devvating.json` + CLI unificada (`devvating`) | ✅ · TUI diferida |
| M5 | Backends mixtos por agente: API (SDK) o CLI headless (suscripción) — D5 | ✅ |

**Ciclo completo verificado en real el 2026-07-12**: primer debate (Claude por
CLI de suscripción + Gemini por API, con convergencia y síntesis) y primera
ejecución fase 4 (plan de un debate aplicado por `claude -p` en una rama,
diff revisado y fusionado por el vocero). Todo el flujo está además cubierto
por la suite de tests (`pytest`, stubs + git real, sin claves).

## Uso

```bash
# Debate: apertura a ciegas → N rondas de réplica → síntesis.
devvating debate "¿Conviene separar el bucle de tool use del adaptador?" \
    --files "devvating/adapters/claude.py, devvating/adapters/gemini.py"

# Opciones: --rounds N   --profundo (ronda de inversión, ~2x coste)
#           --interactivo (notas del vocero entre rondas)
#           --synthesizer claude|gemini|auto  (auto = rota entre debates)
#           --claude-backend api|cli   --gemini-backend api|cli
#             (cli = agente headless cubierto por tu suscripción; api = SDK
#              con Tool Runtime propio, requiere créditos API)

# Ejecución: aplica la síntesis aprobada en una rama del repo objetivo.
devvating ejecutar --repo /ruta/proyecto \
    --from-transcript transcripts/20260702-...-tema.json
```

Los defaults (rondas, modo profundo, repo, rotación) se pueden fijar en un
`.devvating.json` (ver `.devvating.example.json`); los flags CLI mandan.

La síntesis reporta acuerdos y **desacuerdos abiertos** para que el vocero
decida; el transcript se guarda en `transcripts/`. En la ejecución nada se
confirma (commit) automáticamente: revisas el diff en la rama y decides. Por
defecto el ejecutor solo edita archivos; correr comandos requiere
`--allow-commands` (peligroso, opt-in).

## Puesta en marcha

```bash
# 1) Entorno virtual e instalación (con dependencias de desarrollo)
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 2) Claves de API
cp .env.example .env      # y rellena ANTHROPIC_API_KEY y GEMINI_API_KEY

# 3) Tests (no requieren claves)
pytest

# 4) Prueba de vida: Claude y Gemini leen un archivo del repo vía read_file
devvating pruebavida DISENO.md
```

Si la prueba de vida va bien, cada agente responde con un resumen del archivo
que solo pudo obtener llamando a la herramienta `read_file` — confirmando que
el bucle de *tool use* con ejecución local funciona en ambos proveedores.

## Estructura

```
devvating/
├── __main__.py          # CLI unificada: devvating debate|ejecutar|pruebavida
├── config.py            # claves, modelos, límites (.env)
├── appconfig.py         # defaults del proyecto (.devvating.json)
├── roles.py             # prompts por rol: proponente, réplica, inversión, sintetizador
├── orchestrator.py      # motor del debate (rondas, convergencia, intervención)
├── debate.py            # CLI de debate (rich, transcripts, rotación)
├── executor.py          # fase 4: backend headless + rama + diff
├── ejecutar.py          # CLI de ejecución (aprobación del vocero)
├── gitutil.py           # envoltura fina de git para la fase 4
├── rotation.py          # rotación del sintetizador entre debates (D3)
├── adapters/            # abstracción por proveedor
│   ├── base.py          # interfaz AgentAdapter (Protocol)
│   ├── claude.py        # SDK anthropic + bucle de tool use
│   ├── gemini.py        # SDK google-genai + function calling
│   └── cli.py           # D5: claude -p / gemini -p headless (suscripción)
├── tools/
│   ├── registry.py      # ToolSpec + niveles de permiso (READONLY/WRITE)
│   └── readonly.py      # read_file confinado a repo_root
└── pruebavida.py        # M0: prueba de vida
tests/                   # suite pytest (stubs de agente + repos git reales)
```

## Licencia

[MIT](./LICENSE)

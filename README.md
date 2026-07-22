# DEVVATING

[![tests](https://github.com/D4rk3r-03/devvating/actions/workflows/tests.yml/badge.svg)](https://github.com/D4rk3r-03/devvating/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)

**A multi-agent debate room for software development.** Two AI agents (Claude
and Gemini) debate a topic grounded in your real codebase and produce a plan;
you act as the *arbiter* — you frame the topic, steer the debate, and nothing
gets executed without your explicit approval.

📖 [Documentación en español](./README.es.md) · Design document (Spanish): [`DISENO.md`](./DISENO.md) · Operator guide (Spanish): [`docs/PROTOCOLO.md`](./docs/PROTOCOLO.md)

## How it works

```
1. TOPIC       you pose a debatable question about your code
2. DEBATE      blind opening (both agents propose without seeing each other)
               → rebuttal rounds with convergence rules → synthesis
3. ARBITRATION the synthesis reports agreements, OPEN disagreements and a plan
4. EXECUTION   only after your approval; on a fresh git branch; diff at the end
5. CLOSE       you review the diff and commit — or discard the branch
```

The debate phase is strictly **read-only**: agents can read your repository
but never write. Execution is delegated to a headless coding agent on a
dedicated branch, and nothing is ever committed automatically.

## Requirements

- Python 3.11+
- Per agent, **one** of the two backends:
  - `api` — an API key ([Anthropic](https://console.anthropic.com) and/or [Google AI Studio](https://aistudio.google.com))
  - `cli` — the agent's CLI installed and logged in ([Claude Code](https://code.claude.com) and/or [Gemini CLI](https://github.com/google-gemini/gemini-cli)), covered by a consumer subscription

## Installation

```bash
git clone https://github.com/D4rk3r-03/devvating.git
cd devvating
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# If using API backends, add your keys:
cp .env.example .env    # then fill ANTHROPIC_API_KEY / GEMINI_API_KEY
```

## Quick start

```bash
# 1) Smoke test — each agent must read a repo file and summarize it:
devvating pruebavida README.md

# 2) First debate (defaults: both agents via API):
devvating debate "Should the tool-use loop be extracted from the adapters?" \
    --files "devvating/adapters/claude.py, devvating/adapters/gemini.py"

# 3) Turn any debate into a shareable, self-contained HTML report:
devvating reporte transcripts/<timestamp>-<topic>.json

# 4) Execute the approved synthesis on a target git repository:
devvating ejecutar --repo /path/to/project \
    --from-transcript transcripts/<timestamp>-<topic>.json

# 5) See every debate run on this machine, across all your projects:
devvating historial            # add --pendientes for the ones awaiting you

# 6) Reclaim the isolated worktrees left behind by past executions:
devvating limpiar --repo /path/to/project

# 7) Or run it all from the browser — the debate room, live:
pip install -e ".[hub]"
cd devvating-ui && npm install && npm run build && cd ..
devvating hub          # → http://127.0.0.1:8777
# Serve several projects at once (pick one from the UI):
devvating hub --repo ~/work/api --repo ~/work/web
```

Every debate prints the synthesis (agreements / open disagreements / plan),
a per-agent token & cost summary, and saves a full JSON transcript under
`transcripts/`.

### Debate options

| Flag | Effect |
|------|--------|
| `--files "a.py, b.py"` | Context hint — files the agents should look at first |
| `--rounds N` | Max rebuttal rounds (default 2; stops early on convergence) |
| `--interactivo` | Lets you inject a note between rounds |
| `--profundo` | Adds a role-inversion round (each agent steelmans the other) |
| `--synthesizer claude\|gemini\|auto` | Who writes the synthesis (`auto` rotates) |
| `--claude-backend api\|cli` | Claude via SDK (API credits) or `claude -p` (subscription) |
| `--gemini-backend api\|cli` | Gemini via SDK or `gemini -p` (subscription) |
| `--agentes a,b` | Pick any two debaters from the roster: `claude-api`, `claude-cli`, `gemini-api`, `gemini-cli`, `antigravity` (Google's `agy`), `kimi` |

> 💡 **Zero-marginal-cost combo**: `--claude-backend cli` (Claude Pro/Max
> subscription) + Gemini on the API free tier runs the whole pipeline without
> API credits.

### Execution guardrails

`devvating ejecutar` refuses dirty git trees, always works on a fresh
`devvating/<slug>-<date>` branch, only allows file edits by default
(`--allow-commands` is an explicit, loudly-warned opt-in), and leaves changes
**staged, never committed** — the final judgment is yours.

## Configuration

Project defaults live in `.devvating.json` (see `.devvating.example.json`);
CLI flags always win:

```json
{
  "rounds": 2,
  "deep_mode": false,
  "auto_rotate": true,
  "backends": { "claude": "cli", "gemini": "api" }
}
```

Environment variables (or `.env`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `ANTHROPIC_API_KEY` | — | Claude API backend |
| `GEMINI_API_KEY` | — | Gemini API backend |
| `DEVVATING_CLAUDE_MODEL` | `claude-opus-4-8` | Claude model (API backend) |
| `DEVVATING_GEMINI_MODEL` | `gemini-3.5-flash` | Gemini model (API backend) |
| `DEVVATING_MAX_TOOL_ITERATIONS` | `8` | Tool-use loop cap per turn |
| `DEVVATING_EXEC_MODEL` | `sonnet` | Model for the execution agent (phase 4) — reasoning-heavy models stay on debate duty |

## Project layout

```
devvating/
├── __main__.py          # unified CLI: devvating debate|ejecutar|pruebavida
├── orchestrator.py      # debate engine: rounds, convergence, usage totals
├── roles.py             # role prompts: proponent, rebuttal, inversion, synthesizer
├── executor.py          # execution phase: headless backend + branch + diff
├── pricing.py           # cost table (kept out of the adapters)
├── adapters/            # one interface, four implementations
│   ├── base.py          #   AgentAdapter protocol + TurnUsage
│   ├── claude.py        #   Anthropic SDK + manual tool-use loop
│   ├── gemini.py        #   google-genai SDK + function calling
│   └── cli.py           #   headless CLIs (subscription-covered)
└── tools/               # local read-only tool runtime, sandboxed to the repo
tests/                   # pytest suite — no API keys required
```

## Development

```bash
pytest              # 61 tests, <3s, fully offline
```

## Credits

Idea & direction: [D4rk3r-03](https://github.com/D4rk3r-03) · Engineering: Claudio

```
   /\_/\
  ( o.o )  ✳
   > ^ <
```

## License

[MIT](./LICENSE)

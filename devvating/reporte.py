"""Generador de reportes HTML desde un transcript de debate (M6a, D6).

`devvating reporte <transcript.json>` produce un HTML estático y
AUTOCONTENIDO (CSS inline, sin dependencias de red) con el debate navegable
por rondas, los veredictos de convergencia, la síntesis destacada y el
desglose de uso y costos. Es puro renderizado sobre el `asdict(DebateSession)`
del transcript: no toca orquestador ni adaptadores (plan del debate M6).
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

import markdown as _markdown

_COLORES = {
    "claude": "#0e7490",
    "gemini": "#a21caf",
    "antigravity": "#1d4ed8",
    "kimi": "#15803d",
}
_COLOR_DEFECTO = "#52525b"

_FASES = {
    "propuesta": "Propuesta inicial",
    "replica": "Réplica",
    "inversion": "Inversión (steelman)",
    "sintesis": "Síntesis",
}

_CSS = """
:root { --fondo:#fafaf9; --tinta:#1c1917; --carta:#ffffff; --borde:#e7e5e4;
        --tenue:#78716c; --si:#15803d; --no:#c2410c; }
@media (prefers-color-scheme: dark) {
  :root { --fondo:#1c1917; --tinta:#e7e5e4; --carta:#292524; --borde:#44403c;
          --tenue:#a8a29e; --si:#4ade80; --no:#fb923c; }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--fondo); color:var(--tinta);
       font:16px/1.6 system-ui, sans-serif; }
main { max-width:56rem; margin:0 auto; padding:2rem 1.25rem 4rem; }
h1 { font-size:1.5rem; margin:.2rem 0; }
.meta { color:var(--tenue); font-size:.875rem; }
.meta b { color:var(--tinta); }
nav { margin:1rem 0 2rem; font-size:.875rem; }
nav a { color:var(--tenue); margin-right:1rem; text-decoration:none; }
nav a:hover { color:var(--tinta); }
section h2 { font-size:1.05rem; border-bottom:1px solid var(--borde);
             padding-bottom:.4rem; margin:2.2rem 0 1rem; }
.turno { background:var(--carta); border:1px solid var(--borde);
         border-left:4px solid var(--agente); border-radius:8px;
         padding:1rem 1.25rem; margin:1rem 0; overflow-x:auto; }
.turno header { display:flex; gap:.6rem; align-items:baseline;
                margin-bottom:.4rem; }
.turno header .agente { color:var(--agente); font-weight:700; }
.turno header .fase { color:var(--tenue); font-size:.8rem; }
.chip { font-size:.72rem; font-weight:700; padding:.1rem .55rem;
        border-radius:99px; border:1px solid currentColor; margin-left:auto; }
.chip.si { color:var(--si); } .chip.no { color:var(--no); }
.sintesis { border-left-color:#b45309; }
table { border-collapse:collapse; width:100%; font-size:.875rem; }
th, td { text-align:left; padding:.45rem .7rem; border-bottom:1px solid var(--borde); }
td.n { text-align:right; font-variant-numeric:tabular-nums; }
.turno :is(h1,h2,h3) { font-size:1rem; margin:.9rem 0 .35rem; }
.turno p { margin:.5rem 0; }
code { background:var(--fondo); padding:.1rem .3rem; border-radius:4px;
       font-size:.85em; }
footer { color:var(--tenue); font-size:.8rem; margin-top:3rem; }
"""


def _md(texto: str) -> str:
    """Markdown → HTML con el texto del modelo previamente escapado."""
    return _markdown.markdown(html.escape(texto), extensions=["tables", "fenced_code"])


def _chip(verdict: str | None) -> str:
    if verdict == "si":
        return '<span class="chip si">CONVERGE</span>'
    if verdict == "no":
        return '<span class="chip no">DISIENTE</span>'
    return ""


def _turno_html(t: dict, extra_clase: str = "") -> str:
    agente = t.get("agent", "?")
    color = _COLORES.get(agente, _COLOR_DEFECTO)
    fase = _FASES.get(t.get("phase", ""), t.get("phase", ""))
    return (
        f'<article class="turno {extra_clase}" style="--agente:{color}">'
        f"<header><span class='agente'>{html.escape(agente)}</span>"
        f"<span class='fase'>{html.escape(fase)}</span>{_chip(t.get('verdict'))}</header>"
        f"{_md(t.get('text', ''))}</article>"
    )


def _seccion_usage(totales: dict) -> str:
    if not totales:
        return ""
    filas = []
    orden = [k for k in totales if k != "total"] + (["total"] if "total" in totales else [])
    for nombre in orden:
        u = totales[nombre]
        costo = f"${u['cost_usd']:.4f}" if u.get("cost_usd") is not None else "—"
        negrita = "font-weight:700" if nombre == "total" else ""
        filas.append(
            f"<tr style='{negrita}'><td>{html.escape(nombre)}</td>"
            f"<td class='n'>{u.get('input_tokens', 0):,}</td>"
            f"<td class='n'>{u.get('output_tokens', 0):,}</td>"
            f"<td class='n'>{u.get('cache_read_tokens', 0):,}</td>"
            f"<td class='n'>{costo}</td></tr>"
        )
    return (
        '<section id="uso"><h2>Uso y costos</h2><div class="turno" style="--agente:var(--borde)">'
        "<table><thead><tr><th>Agente</th><th>Entrada</th><th>Salida</th>"
        "<th>Caché</th><th>Costo</th></tr></thead>"
        f"<tbody>{''.join(filas)}</tbody></table></div></section>"
    )


def render_html(data: dict) -> str:
    tema = data.get("topic", {}).get("prompt", "(sin tema)")
    turns = data.get("turns", [])
    convergio = data.get("converged", False)
    estado = (
        f"convergieron en la ronda {data.get('converged_round')}"
        if convergio
        else f"sin convergencia tras {data.get('rounds_run', '?')} ronda(s)"
    )

    # Agrupar turnos por sección visible.
    secciones: list[tuple[str, str, list[dict]]] = []  # (id, título, turnos)

    def _agregar(clave: str, titulo: str, t: dict) -> None:
        if not secciones or secciones[-1][0] != clave:
            secciones.append((clave, titulo, []))
        secciones[-1][2].append(t)

    for t in turns:
        fase = t.get("phase")
        if fase == "propuesta":
            _agregar("apertura", "Apertura a ciegas", t)
        elif fase == "replica":
            r = t.get("round", "?")
            _agregar(f"ronda-{r}", f"Ronda {r}", t)
        elif fase == "inversion":
            _agregar("inversion", "Inversión (modo profundo)", t)
        # la síntesis se renderiza aparte, destacada

    cuerpo = []
    enlaces = []
    for clave, titulo, ts in secciones:
        enlaces.append(f'<a href="#{clave}">{html.escape(titulo)}</a>')
        cuerpo.append(f'<section id="{clave}"><h2>{html.escape(titulo)}</h2>')
        cuerpo.extend(_turno_html(t) for t in ts)
        cuerpo.append("</section>")

    sintesis = data.get("synthesis", "")
    if sintesis:
        enlaces.append('<a href="#sintesis">Síntesis</a>')
        cuerpo.append(
            '<section id="sintesis"><h2>Síntesis</h2>'
            + _turno_html(
                {"agent": data.get("synthesizer", "?"), "phase": "sintesis", "text": sintesis},
                extra_clase="sintesis",
            )
            + "</section>"
        )

    usage = _seccion_usage(data.get("usage_totals", {}))
    if usage:
        enlaces.append('<a href="#uso">Uso y costos</a>')

    profundo = " · modo profundo" if data.get("deep_mode") else ""
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Debate — {html.escape(tema[:80])}</title>
<style>{_CSS}</style></head><body><main>
<p class="meta">DEVVATING · reporte de debate</p>
<h1>{html.escape(tema)}</h1>
<p class="meta">Sintetizador: <b>{html.escape(data.get('synthesizer', '?'))}</b> ·
<b>{html.escape(estado)}</b>{profundo}</p>
<nav>{''.join(enlaces)}</nav>
{''.join(cuerpo)}
{usage}
<footer>Generado con <code>devvating reporte</code>.</footer>
</main></body></html>"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="devvating reporte",
        description="Genera un reporte HTML estático desde un transcript de debate.",
    )
    parser.add_argument("transcript", help="Ruta del transcript JSON del debate.")
    parser.add_argument("-o", "--salida", help="Ruta del HTML (default: junto al transcript).")
    args = parser.parse_args(argv)

    origen = Path(args.transcript)
    try:
        data = json.loads(origen.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"No se pudo leer el transcript: {exc}")
        return 1

    destino = Path(args.salida) if args.salida else origen.with_suffix(".html")
    destino.write_text(render_html(data), encoding="utf-8")
    print(f"Reporte generado: {destino}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

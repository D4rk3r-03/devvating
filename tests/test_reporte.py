"""Reporte HTML (M6a): renderizado, escape, secciones y CLI."""

from __future__ import annotations

import json

from devvating.reporte import main, render_html


def _transcript() -> dict:
    return {
        "topic": {"prompt": "¿Refactor X o Y?", "context_hint": "a.py"},
        "turns": [
            {"round": 0, "phase": "propuesta", "agent": "claude",
             "text": "## Postura\nPrefiero **X**.", "verdict": None,
             "usage": {"input_tokens": 10, "output_tokens": 5,
                       "cache_read_tokens": 0, "cache_creation_tokens": 0,
                       "cost_usd": 0.01}},
            {"round": 0, "phase": "propuesta", "agent": "gemini",
             "text": "Prefiero Y <script>alert(1)</script>", "verdict": None,
             "usage": None},
            {"round": 1, "phase": "replica", "agent": "claude",
             "text": "Mantengo X.", "verdict": "no", "usage": None},
            {"round": 1, "phase": "replica", "agent": "gemini",
             "text": "Acepto X.", "verdict": "si", "usage": None},
            {"round": 1, "phase": "inversion", "agent": "claude",
             "text": "Steelman de Y.", "verdict": None, "usage": None},
            {"round": 1, "phase": "sintesis", "agent": "gemini",
             "text": "## Acuerdos\nGana X.", "verdict": None, "usage": None},
        ],
        "rounds_run": 1,
        "converged": True,
        "converged_round": 1,
        "deep_mode": True,
        "synthesis": "## Acuerdos\nGana X.\n## Plan propuesto\n1. Hacer X.",
        "synthesizer": "gemini",
        "usage_totals": {
            "claude": {"input_tokens": 10, "output_tokens": 5,
                       "cache_read_tokens": 0, "cache_creation_tokens": 0,
                       "cost_usd": 0.01},
            "gemini": {"input_tokens": 7, "output_tokens": 3,
                       "cache_read_tokens": 0, "cache_creation_tokens": 0,
                       "cost_usd": None},
            "total": {"input_tokens": 17, "output_tokens": 8,
                      "cache_read_tokens": 0, "cache_creation_tokens": 0,
                      "cost_usd": 0.01},
        },
    }


class TestRenderHtml:
    def test_contiene_tema_estado_y_secciones(self):
        out = render_html(_transcript())
        assert "¿Refactor X o Y?" in out
        assert "convergieron en la ronda 1" in out
        for seccion in ("Apertura a ciegas", "Ronda 1", "Inversión", "Síntesis",
                        "Uso y costos", "modo profundo"):
            assert seccion in out

    def test_markdown_renderizado_y_html_del_modelo_escapado(self):
        out = render_html(_transcript())
        assert "<h2>Postura</h2>" in out          # markdown del turno
        assert "<strong>X</strong>" in out
        assert "<script>alert(1)</script>" not in out  # inyección escapada
        assert "&lt;script&gt;" in out

    def test_veredictos_como_chips(self):
        out = render_html(_transcript())
        assert "CONVERGE" in out and "DISIENTE" in out

    def test_costos_y_desconocidos(self):
        out = render_html(_transcript())
        assert "$0.0100" in out
        assert "—" in out  # costo desconocido de gemini

    def test_transcript_minimo_sin_usage_no_revienta(self):
        out = render_html({"topic": {"prompt": "t"}, "turns": [],
                           "rounds_run": 0, "synthesis": "", "synthesizer": ""})
        assert "<h1>t</h1>" in out and "Uso y costos" not in out


class TestCli:
    def test_genera_html_junto_al_transcript(self, tmp_path, capsys):
        origen = tmp_path / "debate.json"
        origen.write_text(json.dumps(_transcript(), ensure_ascii=False), encoding="utf-8")
        assert main([str(origen)]) == 0
        destino = tmp_path / "debate.html"
        assert destino.exists() and "¿Refactor X o Y?" in destino.read_text(encoding="utf-8")
        assert str(destino) in capsys.readouterr().out

    def test_salida_personalizada(self, tmp_path):
        origen = tmp_path / "d.json"
        origen.write_text(json.dumps(_transcript(), ensure_ascii=False), encoding="utf-8")
        destino = tmp_path / "sub" / "reporte.html"
        destino.parent.mkdir()
        assert main([str(origen), "-o", str(destino)]) == 0
        assert destino.exists()

    def test_transcript_invalido_falla_claro(self, tmp_path, capsys):
        malo = tmp_path / "roto.json"
        malo.write_text("{no es json", encoding="utf-8")
        assert main([str(malo)]) == 1
        assert "No se pudo leer" in capsys.readouterr().out

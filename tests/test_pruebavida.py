"""Prueba de vida (M0): selección de agentes a probar, sin red ni claves."""

from __future__ import annotations

import argparse

from devvating.pruebavida import _elegir_objetivos

ROSTER = ["antigravity", "claude-api", "claude-cli", "gemini-api", "gemini-cli", "kimi"]


def _args(agentes=None, roster_cli=False) -> argparse.Namespace:
    return argparse.Namespace(agentes=agentes, roster_cli=roster_cli)


class TestElegirObjetivos:
    def test_por_defecto_usa_el_par_claude_gemini(self):
        objetivos = _elegir_objetivos(_args(), ROSTER, "api", "cli")
        assert objetivos == ["claude-api", "gemini-cli"]

    def test_agentes_explicitos_ganan(self):
        objetivos = _elegir_objetivos(_args(agentes="antigravity, kimi"), ROSTER, "api", "api")
        assert objetivos == ["antigravity", "kimi"]

    def test_roster_cli_prueba_todos_los_adaptadores_cli(self):
        objetivos = _elegir_objetivos(_args(roster_cli=True), ROSTER, "api", "api")
        assert objetivos == ["antigravity", "claude-cli", "gemini-cli", "kimi"]
        assert "claude-api" not in objetivos and "gemini-api" not in objetivos

    def test_agentes_explicitos_ganan_sobre_roster_cli(self):
        objetivos = _elegir_objetivos(
            _args(agentes="claude-api", roster_cli=True), ROSTER, "api", "api"
        )
        assert objetivos == ["claude-api"]

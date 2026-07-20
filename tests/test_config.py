"""Config de proyecto (.devvating.json) y rotación persistente."""

from __future__ import annotations

import json

from devvating import rotation
from devvating.appconfig import ProjectConfig


class TestProjectConfig:
    def test_sin_archivo_usa_defaults(self, tmp_path):
        pc = ProjectConfig.load(str(tmp_path))
        assert pc.rounds == 2 and pc.auto_rotate and not pc.deep_mode
        assert pc.verificacion == ""

    def test_lee_el_comando_de_verificacion(self, tmp_path):
        (tmp_path / ".devvating.json").write_text(
            json.dumps({"verificacion": "pytest -q"}), encoding="utf-8"
        )
        assert ProjectConfig.load(str(tmp_path)).verificacion == "pytest -q"

    def test_lee_valores_del_archivo(self, tmp_path):
        (tmp_path / ".devvating.json").write_text(
            json.dumps({"rounds": 4, "deep_mode": True, "files": "a.py"}),
            encoding="utf-8",
        )
        pc = ProjectConfig.load(str(tmp_path))
        assert pc.rounds == 4 and pc.deep_mode and pc.files == "a.py"

    def test_json_corrupto_cae_a_defaults(self, tmp_path):
        (tmp_path / ".devvating.json").write_text("{rota", encoding="utf-8")
        assert ProjectConfig.load(str(tmp_path)) == ProjectConfig()

    def test_valores_null_o_de_tipo_erroneo_caen_a_defaults(self, tmp_path):
        (tmp_path / ".devvating.json").write_text(
            json.dumps({"rounds": None}), encoding="utf-8"
        )
        assert ProjectConfig.load(str(tmp_path)) == ProjectConfig()
        (tmp_path / ".devvating.json").write_text("[1, 2]", encoding="utf-8")
        assert ProjectConfig.load(str(tmp_path)) == ProjectConfig()


class TestRotation:
    def test_estado_inicial_sintetiza_el_indice_cero(self, tmp_path):
        state = rotation.load(str(tmp_path))
        assert state.debates == 0 and state.synthesizer_index() == 0

    def test_avanza_y_persiste_alternando(self, tmp_path):
        repo = str(tmp_path)
        state = rotation.load(repo)
        rotation.save(repo, state.advanced())
        state = rotation.load(repo)
        assert state.debates == 1 and state.synthesizer_index() == 1
        rotation.save(repo, state.advanced())
        assert rotation.load(repo).synthesizer_index() == 0

    def test_estado_corrupto_cae_a_cero(self, tmp_path):
        d = tmp_path / ".devvating"
        d.mkdir()
        (d / "state.json").write_text("???", encoding="utf-8")
        assert rotation.load(str(tmp_path)).debates == 0

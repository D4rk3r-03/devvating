"""Herramientas: registro con permisos y read_file confinado al repo."""

from __future__ import annotations

import os

import pytest

from devvating.tools.readonly import make_read_file
from devvating.tools.registry import Permission, ToolRegistry, ToolSpec


def _spec(name: str, handler) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="x",
        input_schema={"type": "object", "properties": {}},
        permission=Permission.READONLY,
        handler=handler,
    )


class TestRegistry:
    def test_ejecuta_herramienta_registrada(self):
        reg = ToolRegistry()
        reg.register(_spec("eco", lambda inp: f"eco:{inp.get('v', '')}"))
        assert reg.execute("eco", {"v": "hola"}) == "eco:hola"

    def test_rechaza_nombre_duplicado(self):
        reg = ToolRegistry()
        reg.register(_spec("a", lambda inp: ""))
        with pytest.raises(ValueError):
            reg.register(_spec("a", lambda inp: ""))

    def test_herramienta_desconocida_devuelve_error_como_texto(self):
        reg = ToolRegistry()
        assert "desconocida" in reg.execute("nada", {})

    def test_excepcion_del_handler_se_reporta_no_se_propaga(self):
        reg = ToolRegistry()

        def explota(inp):
            raise RuntimeError("bum")

        reg.register(_spec("mala", explota))
        out = reg.execute("mala", {})
        assert "bum" in out and out.startswith("Error")


class TestReadFile:
    def test_lee_archivo_dentro_del_repo(self, tmp_path):
        (tmp_path / "a.txt").write_text("contenido", encoding="utf-8")
        tool = make_read_file(str(tmp_path))
        assert tool.handler({"path": "a.txt"}) == "contenido"

    def test_rechaza_escape_con_punto_punto(self, tmp_path):
        (tmp_path / "repo").mkdir()
        (tmp_path / "secreto.txt").write_text("clave", encoding="utf-8")
        tool = make_read_file(str(tmp_path / "repo"))
        with pytest.raises(ValueError):
            tool.handler({"path": "../secreto.txt"})

    def test_rechaza_ruta_absoluta_fuera_del_repo(self, tmp_path):
        tool = make_read_file(str(tmp_path))
        with pytest.raises(ValueError):
            tool.handler({"path": "/etc/hostname"})

    def test_rechaza_symlink_que_escapa(self, tmp_path):
        (tmp_path / "repo").mkdir()
        (tmp_path / "secreto.txt").write_text("clave", encoding="utf-8")
        os.symlink(tmp_path / "secreto.txt", tmp_path / "repo" / "enlace.txt")
        tool = make_read_file(str(tmp_path / "repo"))
        with pytest.raises(ValueError):
            tool.handler({"path": "enlace.txt"})

    def test_archivo_inexistente_y_path_vacio_devuelven_error_texto(self, tmp_path):
        tool = make_read_file(str(tmp_path))
        assert tool.handler({"path": "no-existe.txt"}).startswith("Error")
        assert tool.handler({}).startswith("Error")

    def test_trunca_archivos_grandes(self, tmp_path):
        (tmp_path / "grande.txt").write_text("x" * 500, encoding="utf-8")
        tool = make_read_file(str(tmp_path), max_bytes=100)
        out = tool.handler({"path": "grande.txt"})
        assert out.endswith("[truncado]") and len(out) < 500

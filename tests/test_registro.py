"""Índice global (D13, fase C): punteros a los transcripts, nunca su contenido.

La invariante que protegen estos tests: el índice es PRESCINDIBLE. Se puede
borrar entero y reconstruirlo desde los transcripts, que siguen siendo la
única fuente y siguen viviendo junto a su repo.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from devvating import historial, registro


def _transcript(repo: Path, nombre: str, **campos) -> Path:
    carpeta = repo / "transcripts"
    carpeta.mkdir(parents=True, exist_ok=True)
    base = {
        "topic": {"prompt": "¿tema debatido?", "context_hint": ""},
        "turns": [], "rounds_run": 2, "converged": True,
        "synthesis": "el plan", "synthesizer": "claude",
        "decisiones": [], "estado": "convergido",
        "usage_totals": {"total": {"input_tokens": 10, "output_tokens": 5,
                                   "cache_read_tokens": 0,
                                   "cache_creation_tokens": 0, "cost_usd": 1.25}},
    }
    base.update(campos)
    ruta = carpeta / nombre
    ruta.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    return ruta


class TestRegistrar:
    def test_indexa_metadatos_y_no_contenido(self, tmp_path):
        # El índice no puede contradecir a la fuente porque no la duplica.
        t = _transcript(tmp_path, "20260722-120000-tema.json")
        assert registro.registrar(t, str(tmp_path)) is True
        fila = registro.listar()[0]
        assert fila["tema"] == "¿tema debatido?"
        assert fila["fecha"] == "20260722-120000"
        assert fila["convergio"] == 1 and fila["coste"] == 1.25
        assert fila["transcript"] == str(t.resolve())
        # Nada de contenido: ni síntesis ni turnos viajan al índice.
        assert "synthesis" not in fila and "turns" not in fila
        assert "el plan" not in json.dumps(fila)

    def test_cuenta_las_decisiones_cruciales_abiertas(self, tmp_path):
        t = _transcript(tmp_path, "20260722-130000-con-decisiones.json",
                        estado="pendiente_decision", decisiones=[
                            {"id": "d1", "crucial": True, "resuelta": False},
                            {"id": "d2", "crucial": True, "resuelta": True},
                            {"id": "d3", "crucial": False, "resuelta": False},
                        ])
        registro.registrar(t, str(tmp_path))
        assert registro.listar()[0]["decisiones_abiertas"] == 1

    def test_marca_los_parciales(self, tmp_path):
        t = _transcript(tmp_path, "20260722-140000-corte.partial.json")
        registro.registrar(t, str(tmp_path))
        assert registro.listar()[0]["parcial"] == 1

    def test_reindexar_es_idempotente(self, tmp_path):
        t = _transcript(tmp_path, "20260722-150000-tema.json")
        registro.registrar(t, str(tmp_path))
        registro.registrar(t, str(tmp_path))
        assert len(registro.listar()) == 1  # PRIMARY KEY sobre la ruta

    def test_transcript_ilegible_se_salta_sin_romper(self, tmp_path):
        malo = tmp_path / "transcripts" / "20260722-160000-roto.json"
        malo.parent.mkdir(parents=True, exist_ok=True)
        malo.write_text("{ esto no es json", encoding="utf-8")
        assert registro.registrar(malo, str(tmp_path)) is False
        assert registro.listar() == []

    def test_inexistente_no_levanta(self, tmp_path):
        assert registro.registrar(tmp_path / "no-existe.json", str(tmp_path)) is False


class TestReindexar:
    def test_reconstruye_el_indice_entero_tras_borrarlo(self, tmp_path):
        # La prueba de que el índice es prescindible.
        repo = tmp_path / "proyecto"
        for i in range(3):
            _transcript(repo, f"2026072{i}-120000-tema-{i}.json")
        indexados, saltados = registro.reindexar([str(repo)])
        assert (indexados, saltados) == (3, 0)

        os.remove(registro.ruta_db())
        assert registro.listar() == []

        indexados, _ = registro.reindexar([str(repo)])
        assert indexados == 3 and len(registro.listar()) == 3

    def test_varios_repos_en_un_solo_indice(self, tmp_path):
        a, b = tmp_path / "alfa", tmp_path / "beta"
        _transcript(a, "20260722-100000-de-alfa.json")
        _transcript(b, "20260722-110000-de-beta.json")
        registro.reindexar([str(a), str(b)])
        assert len(registro.listar()) == 2
        # Y se puede filtrar por proyecto.
        solo_a = registro.listar(repo=str(a))
        assert len(solo_a) == 1 and solo_a[0]["tema"] == "¿tema debatido?"
        assert solo_a[0]["repo"] == str(a.resolve())

    def test_repo_sin_transcripts_no_falla(self, tmp_path):
        assert registro.reindexar([str(tmp_path / "vacio")]) == (0, 0)

    def test_ordena_por_fecha_descendente(self, tmp_path):
        _transcript(tmp_path, "20260101-120000-viejo.json")
        _transcript(tmp_path, "20260722-120000-nuevo.json")
        registro.reindexar([str(tmp_path)])
        assert [f["fecha"] for f in registro.listar()] == \
            ["20260722-120000", "20260101-120000"]


class TestEntradasMuertas:
    def test_marca_las_que_ya_no_estan_en_disco(self, tmp_path):
        t = _transcript(tmp_path, "20260722-120000-borrado.json")
        registro.registrar(t, str(tmp_path))
        assert registro.listar()[0]["existe"] is True
        t.unlink()
        assert registro.listar()[0]["existe"] is False  # se muestra, no se finge

    def test_olvidar_inexistentes_las_quita(self, tmp_path):
        t = _transcript(tmp_path, "20260722-120000-borrado.json")
        vivo = _transcript(tmp_path, "20260722-130000-vivo.json")
        registro.reindexar([str(tmp_path)])
        t.unlink()
        assert registro.olvidar_inexistentes() == 1
        filas = registro.listar()
        assert len(filas) == 1 and filas[0]["transcript"] == str(vivo.resolve())


class TestAltaAutomatica:
    def test_guardar_un_debate_lo_indexa(self, tmp_path):
        # El enganche está en _save_transcript, punto único de la CLI y el Hub.
        from devvating.debate import _save_transcript
        from devvating.orchestrator import DebateSession, DebateTopic, Turn

        s = DebateSession(topic=DebateTopic(prompt="¿indexado al guardar?"),
                          turns=[Turn(0, "propuesta", "claude", "A0")])
        path = _save_transcript(s, str(tmp_path))
        filas = registro.listar()
        assert len(filas) == 1
        assert filas[0]["tema"] == "¿indexado al guardar?"
        assert filas[0]["transcript"] == str(path.resolve())


class TestComandoHistorial:
    def test_lista_sin_fallar(self, tmp_path, capsys):
        _transcript(tmp_path, "20260722-120000-tema.json")
        registro.reindexar([str(tmp_path)])
        assert historial.main(["--limite", "10"]) == 0
        assert "historial de debates" in capsys.readouterr().out

    def test_indice_vacio_explica_como_poblarlo(self, capsys):
        assert historial.main([]) == 0
        assert "--reindexar" in capsys.readouterr().out

    def test_reindexar_desde_la_cli(self, tmp_path, capsys):
        _transcript(tmp_path, "20260722-120000-tema.json")
        assert historial.main(["--reindexar", "--repo", str(tmp_path)]) == 0
        assert "1 debate(s) indexados" in capsys.readouterr().out
        assert len(registro.listar()) == 1

    def test_filtro_pendientes(self, tmp_path, capsys):
        # Se asienta sobre el resumen, no sobre las celdas: el ancho con que
        # rich renderiza la tabla depende del entorno y recortaría el tema.
        _transcript(tmp_path, "20260722-120000-cerrado.json")
        _transcript(tmp_path, "20260722-130000-abierto.json",
                    estado="pendiente_decision",
                    decisiones=[{"id": "d1", "crucial": True, "resuelta": False}])
        registro.reindexar([str(tmp_path)])

        historial.main([])
        assert "2 debate(s)" in capsys.readouterr().out

        historial.main(["--pendientes"])
        salida = capsys.readouterr().out
        assert "1 debate(s)" in salida and "1 esperando algo de ti" in salida

    def test_la_suite_no_toca_el_registro_del_usuario(self, tmp_path):
        # El fixture autouse redirige DEVVATING_REGISTRO_DIR al tmp_path.
        assert str(tmp_path) in registro.ruta_db()
        assert not registro.ruta_db().startswith(str(Path.home() / ".devvating"))

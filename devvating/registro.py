"""Índice global de lo que devvating ha hecho en esta máquina (fase C, D13).

Los transcripts siguen viviendo junto a su repo — un debate es SOBRE un
repositorio, y así su historia viaja con el clon. Lo que falta es una vista
global: qué se ha debatido, dónde y cómo quedó, sin recorrer proyecto por
proyecto.

Este índice guarda **solo punteros y metadatos** (repo, ruta del transcript,
tema, fecha, estado). Nunca el contenido: al no duplicar la fuente no puede
contradecirla, y si se pierde se reconstruye entero con `devvating reindexar`.
Es caché consultable, no una segunda verdad.

SQLite por atomicidad: dos debates que terminan a la vez (CLI y Hub, o dos
repos) escriben sin pisarse.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS debates (
    transcript  TEXT PRIMARY KEY,   -- ruta absoluta: el puntero a la fuente
    repo        TEXT NOT NULL,
    tema        TEXT,
    fecha       TEXT,               -- del nombre del archivo (ordenable)
    estado      TEXT,               -- convergido | abierto | pendiente_decision
    sintetizador TEXT,
    convergio   INTEGER,
    parcial     INTEGER,            -- 1 si es .partial.json (reanudable)
    decisiones_abiertas INTEGER,
    coste       REAL
);
CREATE INDEX IF NOT EXISTS idx_debates_fecha ON debates(fecha DESC);
"""


def directorio() -> str:
    """Base del registro. `DEVVATING_REGISTRO_DIR` la redirige (la usa la suite)."""
    return os.environ.get("DEVVATING_REGISTRO_DIR") or os.path.join(
        os.path.expanduser("~"), ".devvating"
    )


def ruta_db() -> str:
    return os.path.join(directorio(), "registro.db")


def _conectar() -> sqlite3.Connection:
    os.makedirs(directorio(), exist_ok=True)
    con = sqlite3.connect(ruta_db())
    con.row_factory = sqlite3.Row
    con.executescript(_ESQUEMA)
    return con


def _metadatos(ruta: Path, repo: str) -> dict | None:
    """Extrae del transcript lo justo para indexarlo. None si no es legible.

    Un transcript ilegible NO es un error del registro: se salta. El índice es
    una comodidad; que un archivo roto rompa `reindexar` sería peor.
    """
    try:
        data = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    decisiones = data.get("decisiones") or []
    abiertas = sum(
        1 for d in decisiones
        if isinstance(d, dict) and d.get("crucial") and not d.get("resuelta")
    )
    total = (data.get("usage_totals") or {}).get("total") or {}
    return {
        "transcript": str(ruta.resolve()),
        "repo": str(Path(repo).resolve()),
        "tema": (data.get("topic") or {}).get("prompt", ""),
        # El nombre lleva AAAAMMDD-HHMMSS al principio: ordena bien como texto.
        "fecha": ruta.name[:15],
        "estado": data.get("estado") or "",
        "sintetizador": data.get("synthesizer") or "",
        "convergio": 1 if data.get("converged") else 0,
        "parcial": 1 if ruta.name.endswith(".partial.json") else 0,
        "decisiones_abiertas": abiertas,
        "coste": total.get("cost_usd"),
    }


def registrar(transcript: str | Path, repo: str) -> bool:
    """Indexa (o actualiza) un transcript. False si no se pudo leer.

    Nunca levanta: guardar un debate no puede fallar porque el índice esté
    ocupado o el disco lleno — el transcript ya está a salvo en su repo.
    """
    meta = _metadatos(Path(transcript), repo)
    if meta is None:
        return False
    try:
        with _conectar() as con:
            con.execute(
                "INSERT OR REPLACE INTO debates "
                "(transcript, repo, tema, fecha, estado, sintetizador, convergio,"
                " parcial, decisiones_abiertas, coste) "
                "VALUES (:transcript, :repo, :tema, :fecha, :estado, :sintetizador,"
                " :convergio, :parcial, :decisiones_abiertas, :coste)",
                meta,
            )
    except sqlite3.Error:
        return False
    return True


def listar(limite: int = 50, repo: str | None = None) -> list[dict]:
    """Debates indexados, más recientes primero.

    Marca `existe` por fila: el índice puede apuntar a un transcript borrado o
    a un repo que se movió, y eso se muestra en vez de fingir que sigue ahí.
    """
    consulta = "SELECT * FROM debates"
    params: list = []
    if repo:
        consulta += " WHERE repo = ?"
        params.append(str(Path(repo).resolve()))
    consulta += " ORDER BY fecha DESC LIMIT ?"
    params.append(int(limite))
    try:
        with _conectar() as con:
            filas = [dict(f) for f in con.execute(consulta, params)]
    except sqlite3.Error:
        return []
    for f in filas:
        f["existe"] = os.path.isfile(f["transcript"])
    return filas


def olvidar_inexistentes() -> int:
    """Quita del índice las entradas cuyo transcript ya no está en disco."""
    borradas = 0
    try:
        with _conectar() as con:
            for fila in con.execute("SELECT transcript FROM debates").fetchall():
                if not os.path.isfile(fila["transcript"]):
                    con.execute("DELETE FROM debates WHERE transcript = ?",
                                (fila["transcript"],))
                    borradas += 1
    except sqlite3.Error:
        return 0
    return borradas


def reindexar(repos: list[str]) -> tuple[int, int]:
    """Reconstruye el índice desde los transcripts de `repos`.

    Devuelve (indexados, saltados). Es la prueba de que el índice es
    prescindible: se puede borrar el .db y recuperarlo entero desde la fuente.
    """
    indexados = saltados = 0
    for repo in repos:
        carpeta = Path(repo) / "transcripts"
        if not carpeta.is_dir():
            continue
        for ruta in sorted(carpeta.glob("*.json")):
            if registrar(ruta, repo):
                indexados += 1
            else:
                saltados += 1
    return indexados, saltados

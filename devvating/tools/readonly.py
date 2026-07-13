"""Herramientas de SOLO LECTURA para la fase de debate.

De momento (M0): read_file. En M1/M2 se añadirán list_dir, grep y git_diff.
Todas quedan confinadas a repo_root — el modelo no puede leer fuera del repo.
"""

from __future__ import annotations

import os

from .registry import Permission, ToolSpec


def _safe_resolve(repo_root: str, rel_path: str) -> str:
    """Resuelve rel_path dentro de repo_root y rechaza escapes (../, symlinks)."""
    root = os.path.realpath(repo_root)
    target = os.path.realpath(os.path.join(root, rel_path))
    if target != root and not target.startswith(root + os.sep):
        raise ValueError(f"Ruta fuera del repositorio: {rel_path}")
    return target


def make_read_file(repo_root: str, max_bytes: int = 100_000) -> ToolSpec:
    """Crea la herramienta read_file confinada a repo_root."""

    def handler(tool_input: dict) -> str:
        rel_path = tool_input.get("path", "")
        if not rel_path:
            return "Error: falta el parámetro 'path'."
        target = _safe_resolve(repo_root, rel_path)
        if not os.path.isfile(target):
            return f"Error: no existe el archivo '{rel_path}'."
        with open(target, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read(max_bytes + 1)
        if len(data) > max_bytes:
            data = data[:max_bytes] + "\n... [truncado]"
        return data

    return ToolSpec(
        name="read_file",
        description=(
            "Lee un archivo de texto del repositorio y devuelve su contenido. "
            "La ruta es relativa a la raíz del repositorio."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Ruta del archivo relativa a la raíz del repo.",
                }
            },
            "required": ["path"],
        },
        permission=Permission.READONLY,
        handler=handler,
    )

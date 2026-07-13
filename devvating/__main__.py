"""Punto de entrada unificado: `devvating <subcomando> ...`.

Subcomandos:
    debate      Corre un debate multi-agente sobre un tema.
    ejecutar    Aplica un plan aprobado en una rama (fase de ejecución).
    pruebavida  Prueba de vida de los adaptadores (M0).
"""

from __future__ import annotations

import sys

_USAGE = (
    "DEVVATING — Sala de debate multi-agente para desarrollo.\n\n"
    "Uso: devvating <subcomando> [opciones]\n\n"
    "Subcomandos:\n"
    "  debate      Corre un debate multi-agente sobre un tema.\n"
    "  ejecutar    Aplica un plan aprobado en una rama.\n"
    "  reporte     Genera un reporte HTML desde un transcript.\n"
    "  pruebavida  Prueba de vida de los adaptadores.\n\n"
    "Ejemplo: devvating debate \"¿Refactor X o Y?\" --files a.py,b.py\n"
)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_USAGE)
        return 0 if argv else 1

    cmd, rest = argv[0], argv[1:]

    if cmd == "debate":
        from . import debate

        return debate.main(rest)
    if cmd == "ejecutar":
        from . import ejecutar

        return ejecutar.main(rest)
    if cmd == "reporte":
        from . import reporte

        return reporte.main(rest)
    if cmd == "pruebavida":
        from . import pruebavida

        return pruebavida.main(rest)

    print(f"Subcomando desconocido: {cmd}\n\n{_USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

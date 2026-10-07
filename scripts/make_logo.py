#!/usr/bin/env python3
"""Regenera fpga_cli/logo.py a partir de una imagen del logo (negro sobre fondo claro).

    python3 scripts/make_logo.py assets/pocket-labs.jpeg

Necesita Pillow y numpy (solo para regenerar; la herramienta no los usa). Recorta el gnomo (todo lo que está por encima de
`--hasta`, para dejar fuera el texto), lo reduce a varios tamaños y lo convierte en bloques de media altura (▀ ▄ █), que dan
el doble de resolución vertical que una letra por celda. Para terminales sin UTF-8 genera también una versión solo ASCII.
"""
import argparse
from pathlib import Path

import numpy as np
from PIL import Image

SIZES = (12, 18, 22)          # filas de texto de cada versión


def mask(path: Path, hasta: float) -> np.ndarray:
    g = np.array(Image.open(path).convert("L"))
    m = g < 128
    m[int(len(m) * hasta):] = False                     # fuera el texto de abajo
    ys, xs = np.where(m)
    return m[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


def grid(m: np.ndarray, rows: int) -> np.ndarray:
    px = rows * 2
    w = round(m.shape[1] * px / m.shape[0])
    im = Image.fromarray((~m * 255).astype("uint8")).resize((w, px), Image.LANCZOS)
    return np.array(im) < 140


def render(g: np.ndarray, utf: bool) -> list[str]:
    both, top, bot = ("█", "▀", "▄") if utf else ("#", '"', ",")
    out = []
    for r in range(0, g.shape[0], 2):
        out.append("".join(both if g[r, c] and g[r + 1, c] else top if g[r, c] else bot if g[r + 1, c] else " " for c in range(g.shape[1])).rstrip())
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("imagen", type=Path)
    ap.add_argument("--hasta", type=float, default=0.67, help="fracción de la altura donde termina el gnomo (por defecto 0.67)")
    ap.add_argument("--salida", type=Path, default=Path(__file__).resolve().parent.parent / "fpga_cli" / "logo.py")
    a = ap.parse_args()
    m = mask(a.imagen, a.hasta)
    lines = ['"""Logo de Pocket Labs para la pantalla de inicio. Generado por scripts/make_logo.py: no se edita a mano."""', "",
             "# filas de texto -> líneas del dibujo", "ART_UTF = {"]
    for n in SIZES:
        lines.append(f"    {n}: {render(grid(m, n), True)!r},")
    lines += ["}", "ART_ASCII = {"]
    for n in SIZES:
        lines.append(f"    {n}: {render(grid(m, n), False)!r},")
    lines += ["}", ""]
    a.salida.write_text("\n".join(lines), encoding="utf-8")
    print(f"Escrito {a.salida}")


if __name__ == "__main__":
    main()

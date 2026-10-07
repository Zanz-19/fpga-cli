"""fpga install-tools: iverilog, vvp, gtkwave y yosys dentro de fpga-cli/tools (sin sudo ni apt).

Usa OSS CAD Suite (YosysHQ), un paquete autocontenido y reubicable. Se descarga por HTTPS del
repositorio oficial de lanzamientos y se extrae en caliente, sin guardar el .tgz."""
from __future__ import annotations

import os
import platform
import shutil
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

from . import core as C

REPO = "YosysHQ/oss-cad-suite-build"
SUITE = "oss-cad-suite"
NEEDED_GB = 3.5            # ~750 MB de descarga + ~2.5 GB extraído (se extrae en caliente, no se guarda el .tgz)


def asset_url(tag: str) -> str:
    arch = {"x86_64": "linux-x64", "aarch64": "linux-arm64", "arm64": "linux-arm64"}.get(platform.machine())
    if not arch:
        C.die(f"Arquitectura no soportada por OSS CAD Suite: {platform.machine()}. Usa: sudo apt install iverilog gtkwave yosys")
    return f"https://github.com/{REPO}/releases/download/{tag}/{SUITE}-{arch}-{tag.replace('-', '')}.tgz"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def latest_tag() -> str:
    """Lee la etiqueta de la última versión desde la redirección de /releases/latest (sin usar la API)."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        opener.open(f"https://github.com/{REPO}/releases/latest", timeout=30)
    except urllib.error.HTTPError as exc:
        loc = exc.headers.get("Location", "")
        if exc.code in (301, 302, 303, 307, 308) and "/tag/" in loc:
            return loc.rsplit("/", 1)[1]
    except urllib.error.URLError as exc:
        C.die(f"Sin conexión con GitHub: {exc.reason}")
    C.die("No pude averiguar la última versión. Indica una con --tag AAAA-MM-DD (p. ej. 2026-10-05).")


class _Progress:
    """Envuelve la respuesta HTTP y muestra el avance mientras tarfile la lee."""

    def __init__(self, resp, total: int):
        self.resp, self.total, self.done, self.last = resp, total, 0, -1
        self.tty = sys.stdout.isatty()

    def read(self, n=-1):
        chunk = self.resp.read(n)
        self.done += len(chunk)
        if self.total:
            pct = self.done * 100 // self.total
            step = pct if self.tty else pct // 10 * 10
            if step != self.last:
                self.last = step
                msg = f"  descargando y extrayendo: {self.done // 1_000_000} de {self.total // 1_000_000} MB ({pct}%)"
                print("\r" + msg if self.tty else msg, end="" if self.tty else "\n", flush=True)
        return chunk


def cmd_install_tools(a) -> int:
    base = C.tools_dir()
    dest = base / SUITE
    if dest.exists() and not a.force:
        ver = (dest / "VERSION").read_text().strip().splitlines()[0] if (dest / "VERSION").exists() else ""
        C.ok(f"Ya están instaladas en {dest} {ver}")
        C.info("Para reinstalar: fpga install-tools --force")
        return 0

    url = a.url or asset_url(a.tag or latest_tag())
    free = C._free_gb(base)
    print(f"Se instalará en : {dest}")
    print(f"Origen          : {url}")
    print(f"Descarga ~750 MB, ocupa ~2.5 GB; espacio libre: {free:.0f} GB")
    print("Incluye iverilog, vvp, gtkwave y yosys (y más herramientas de OSS CAD Suite que no usamos).")
    if free < NEEDED_GB:
        C.die(f"Espacio libre insuficiente: hacen falta unos {NEEDED_GB} GB.")
    if a.dry_run:
        C.info("dry-run: no se descarga nada.")
        return 0
    if not a.yes:
        try:
            if input("¿Descargar e instalar? [s/N]: ").strip().lower() not in ("s", "si", "sí", "y"):
                C.die("Cancelado.")
        except EOFError:
            C.die("Cancelado (sin entrada interactiva; usa --yes).")

    partial = base / f".{SUITE}.partial"
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            with tarfile.open(fileobj=_Progress(resp, total), mode="r|gz") as tar:
                tar.extractall(partial, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
        print()
        src = partial / SUITE
        if not (src / "bin" / "iverilog").exists():
            C.die("El paquete descargado no tiene la estructura esperada (falta bin/iverilog).")
        shutil.rmtree(dest, ignore_errors=True)
        src.rename(dest)
    except (urllib.error.URLError, tarfile.TarError, OSError) as exc:
        print()
        C.die(f"Falló la descarga o extracción: {exc}")
    finally:
        shutil.rmtree(partial, ignore_errors=True)
    C.ok(f"Instaladas en {dest}")
    C.info("fpga las usa automáticamente (tienen prioridad sobre las del sistema). Comprueba con: fpga doctor")
    C.info("La carpeta tools/ está en .gitignore: no se sube a GitHub.")
    return 0

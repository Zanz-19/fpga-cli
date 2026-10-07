"""fpga install-quartus: guía la instalación de Quartus Prime Lite 25.1 con solo MAX V y Cyclone IV.

Altera exige iniciar sesión para descargar y aceptar su licencia, así que la descarga la haces tú.
Dos caminos: el instalador oficial pequeño (qinst) o los 3 archivos individuales (modo desatendido)."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import core as C

MANIFEST = Path(os.environ.get("FPGA_CLI_QUARTUS_MANIFEST") or C.ROOT / "quartus.toml")


def sha1_of(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_sha1(path: Path, expected: str) -> None:
    got = sha1_of(path)
    if got != expected:
        C.die(f"SHA1 distinto en {path.name}\n  esperado {expected}\n  obtenido {got}\n"
              "Descarga incompleta o archivo distinto: bórralo y vuélvelo a bajar.")
    C.ok(path.name)


def _download_dir(arg: str | None, names: list[str]) -> Path:
    if arg:
        return Path(arg).expanduser().resolve()
    candidates = [Path.home() / "Descargas", Path.home() / "Downloads"]
    for d in candidates:
        if d.is_dir() and any((d / n).exists() for n in names):
            return d
    for d in candidates:
        if d.is_dir():
            return d
    return Path.cwd()


_free_gb = C._free_gb


def _bin_from(path: Path) -> Path | None:
    """Acepta la carpeta de instalación, quartus/ o quartus/bin y devuelve la que tiene quartus_sh."""
    for cand in (path, path / "bin", path / "quartus" / "bin"):
        if (cand / "quartus_sh").exists():
            return cand
    return None


def _register(binp: Path) -> int:
    C.config_set("quartus_bin", str(binp))
    C.ok(f"Quartus registrado: fpga-cli lo usará desde {binp}")
    print(f'\nPara usarlo también fuera de fpga-cli, agrega a ~/.bashrc:\n  export PATH="$PATH:{binp}"')
    print("Después: fpga doctor")
    return 0


def _locate_after_install(prefix: Path | None) -> Path | None:
    if prefix and _bin_from(prefix):
        return _bin_from(prefix)
    tool = C.find_tool("quartus_sh")
    if tool:
        return Path(tool).parent
    if sys.stdin.isatty():
        answer = C.ask("No lo encuentro solo. ¿En qué carpeta instalaste Quartus? (Enter para omitir)", "")
        if answer:
            return _bin_from(Path(answer).expanduser())
    return None


def _missing_message(m: dict, dl: Path, files: list[dict], a) -> int:
    C.warn(f"No encuentro lo necesario en {dl}. Descarga de la página oficial de Quartus Prime Lite {m['version']} para Linux")
    print("(la descarga pide iniciar sesión con tu cuenta de Altera):\n")
    q = m.get("qinst")
    if q:
        print("  Opción 1, la más simple (pestaña «Installer (Recommended)»):")
        print(f"      {q['name']}  ({q['size']})\n")
    print("  Opción 2, tres archivos (pestaña «Individual Files»):")
    for f in files:
        mark = "FALTA" if not (dl / f["name"]).exists() else "ok   "
        print(f"      [{mark}] {f['name']}  ({f['size']})  {f['what']}")
    print(f"\n  {m['page']}\n")
    if not a.no_open and shutil.which("xdg-open") and sys.stdin.isatty():
        subprocess.Popen(["xdg-open", m["page"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        C.info("Abrí la página en tu navegador.")
    print(f"Cuando termine la descarga, vuelve a correr:  fpga install-quartus --dir {dl}")
    return 2


def _qinst_flow(m: dict, dl: Path, a) -> int:
    q = m["qinst"]
    path = dl / q["name"]
    C.info("Verificando SHA1 del instalador…")
    _check_sha1(path, q["sha1"])
    free = _free_gb(Path.home())
    if free < float(m["min_disk_gb"]):
        C.die(f"Espacio libre insuficiente en tu carpeta personal: {free:.0f} GB, la página pide al menos {m['min_disk_gb']} GB.")
    C.ok(f"Espacio libre: {free:.0f} GB")
    prefix = Path(a.prefix or m["prefix"]).expanduser()
    print("\nEl instalador abre su propia ventana (si no hay pantalla, sigue en modo texto). Ahí:")
    print("  · Acepta la licencia.")
    print(f"  · Carpeta de instalación sugerida: {prefix}")
    print("  · En la lista de componentes déjalo así (el instalador trae marcados otros por defecto):")
    print("      [x] Quartus Prime Lite Edition > Quartus Prime")
    print("      [x] Devices > Cyclone IV device support        (viene DESMARCADO: márcalo)")
    print("      [x] Devices > MAX II, MAX V device support     (viene DESMARCADO: márcalo)")
    print("      [ ] Questa-Altera FPGA and Starter Editions    (desmárcalo: simulamos con iverilog)")
    print("      [ ] Cyclone V device support y MAX 10 FPGA device support (desmárcalos)")
    print("    Así bajas unos 2.3 GB e instala unos 8.8 GB. Los nombres exactos pueden variar un poco según la versión.")
    print(C._c("2", f"\n$ {path}"))
    if a.dry_run:
        print(C._c("2", "  (dry-run: no se ejecuta)"))
        return 0
    path.chmod(0o755)
    rc = subprocess.call([str(path)], cwd=dl)
    if rc != 0:
        C.die(f"El instalador terminó con código {rc}.")
    binp = _locate_after_install(prefix)
    if not binp:
        C.warn("Terminó el instalador pero no encuentro quartus_sh.")
        C.die("Dime dónde quedó con:  fpga install-quartus --register RUTA   (la carpeta de instalación)")
    return _register(binp)


def _manual_flow(m: dict, dl: Path, files: list[dict], a) -> int:
    C.info("Verificando SHA1 (el archivo grande tarda unos segundos)…")
    for f in files:
        _check_sha1(dl / f["name"], f["sha1"])
    prefix = Path(a.prefix or m["prefix"]).expanduser()
    free = _free_gb(prefix)
    if free < float(m["min_disk_gb"]):
        C.die(f"Espacio libre insuficiente en {prefix.parent}: {free:.0f} GB, la página pide al menos {m['min_disk_gb']} GB.")
    C.ok(f"Espacio libre: {free:.0f} GB")

    installer = next(dl / f["name"] for f in files if f["role"] == "installer")
    cmd = [str(installer)]
    if not a.interactive:
        cmd += ["--mode", "unattended", "--unattendedmodeui", "minimal", "--installdir", str(prefix), "--accept_eula", "1"]
    print(f"\nInstalador : {installer.name}")
    print(f"Destino    : {prefix}" + ("  (el instalador te preguntará)" if a.interactive else ""))
    print(f"Licencia   : {m['license']}")
    C.warn("Las banderas del modo desatendido no están verificadas con tu máquina; si fallan, usa --interactive.")
    print(C._c("2", "$ " + " ".join(cmd)))
    if a.dry_run:
        print(C._c("2", "  (dry-run: no se ejecuta)"))
        return 0
    if not a.interactive:
        print("\nAl continuar aceptas el acuerdo de licencia de Quartus Prime (enlace arriba).")
        try:
            answer = input("Escribe ACEPTO para instalar (cualquier otra cosa cancela): ").strip()
        except EOFError:
            answer = ""
        if answer != "ACEPTO":
            C.die("Cancelado: no se instaló nada.")
    for f in files:
        if f["name"].endswith(".run"):
            (dl / f["name"]).chmod(0o755)
    rc = subprocess.call(cmd, cwd=dl)
    if rc != 0:
        C.die(f"El instalador terminó con código {rc}. Prueba con: fpga install-quartus --interactive")
    binp = _locate_after_install(prefix)
    if not binp:
        C.die("Terminó la instalación pero no encuentro quartus_sh. Usa: fpga install-quartus --register RUTA")
    return _register(binp)


def cmd_install_quartus(a) -> int:
    if a.register:
        binp = _bin_from(Path(a.register).expanduser())
        if not binp:
            C.die(f"No encuentro quartus_sh en {a.register} (busqué en ., bin/ y quartus/bin/).")
        return _register(binp)

    m = C.load_toml(MANIFEST)
    files = m["files"]
    q = m.get("qinst")
    existing = C.find_tool("quartus_sh")
    if existing and not a.force:
        C.ok(f"Quartus ya está instalado: {existing}")
        C.info("Si quieres reinstalar usa --force.")
        return 0

    dl = _download_dir(a.dir, [f["name"] for f in files] + ([q["name"]] if q else []))
    have_all = all((dl / f["name"]).exists() for f in files)
    have_qinst = bool(q) and (dl / q["name"]).exists()
    if have_qinst and not (a.manual and have_all):
        return _qinst_flow(m, dl, a)
    if have_all:
        return _manual_flow(m, dl, files, a)
    return _missing_message(m, dl, files, a)

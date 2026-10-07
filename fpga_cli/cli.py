"""Interfaz de línea de comandos de fpga-cli."""
from __future__ import annotations

import argparse
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from . import core as C
from . import install as I
from . import tools as T
from . import prog as P
from . import scaffold as S
from . import tui as U

# ---------------------------------------------------------------- boards / pins
def cmd_boards(a) -> int:
    for b in C.list_boards():
        mode = "solo flash (con límite de cargas)" if b["program"]["mode"] == "flash_only" else "RAM o flash"
        print(f"{b['id']:9s} {b['name']:22s} {b['device']:15s} {len(b['pins']):3d} señales · {mode}")
    return 0


def cmd_pins(a) -> int:
    b = S.pick_board(a.board)
    print(f"{b['name']} ({b['device']}): {len(b['pins'])} señales")
    items = [f"{k:<12s} PIN_{v}" for k, v in b["pins"].items()]
    cols = 3
    rows = (len(items) + cols - 1) // cols
    for r in range(rows):
        print("  ".join(f"{items[r + c * rows]:<26s}" for c in range(cols) if r + c * rows < len(items)).rstrip())
    return 0


# ---------------------------------------------------------------- simulación
def _pick_tb(p: C.Project, name: str | None) -> str:
    tbs = p.testbenches
    if name:
        wanted = {name, name + ".v", name + ".sv"}
        for t in tbs:                                   # por ruta, por nombre de archivo o por nombre sin extensión
            if t in wanted or Path(t).name in wanted or Path(t).stem == name:
                return t
        for base in (p.dir, p.tb_dir):                  # un testbench que existe pero no está en la lista
            for cand in wanted:
                if (base / cand).is_file():
                    return (base / cand).relative_to(p.dir).as_posix()
        C.die(f"No encuentro el testbench '{name}'. Disponibles: {', '.join(tbs) or '(ninguno)'}")
    if not tbs:
        C.die("Este proyecto no tiene testbench. Crea uno con: fpga new-tb")
    if len(tbs) == 1:
        return tbs[0]
    if sys.stdin.isatty():
        print("Testbenches:")
        for i, t in enumerate(tbs, 1):
            print(f"  {i}) {t}")
        choice = C.ask("Elige el número", "1")
        try:
            return tbs[int(choice) - 1]
        except (ValueError, IndexError):
            C.die("Opción no válida.")
    C.die(f"Hay varios testbenches ({', '.join(tbs)}). Indica cuál: fpga sim NOMBRE")


def _vcd_for(p: C.Project, tb: str) -> Path | None:
    found = [d / n for d in (p.wave_dir, p.dir) for n in (f"{Path(tb).stem}.vcd", "dump.vcd") if (d / n).exists()]
    return max(found, key=lambda f: f.stat().st_mtime) if found else None


def cmd_sim(a) -> int:
    p = C.load_project(C.resolve_project_dir(a.C))
    if not C.find_tool("iverilog"):
        C.die("Falta iverilog. Instálalo dentro de fpga-cli con: fpga install-tools")
    tb = _pick_tb(p, a.tb)
    stem = Path(tb).stem
    p.sim_dir.mkdir(parents=True, exist_ok=True)
    p.wave_dir.mkdir(parents=True, exist_ok=True)
    vvp = p.sim_dir / f"sim_{stem}.vvp"
    rc = C.run(["iverilog", "-g2012", "-o", os.path.relpath(vvp, p.dir), tb] + p.sim_sources(), p.dir)
    if rc != 0:
        return rc
    # vvp corre dentro de waves/: el $dumpfile("x.vcd") de los testbenches cae ahí sin tocarlos
    rc = C.run(["vvp", os.path.relpath(vvp, p.wave_dir)], p.wave_dir)
    if rc == 0:
        sims = dict(C.state_read(p).get("sims", {}))
        sims[stem] = time.time()
        C.state_update(p, sims=sims)
        C.ok("Simulación terminada." + (f" Mira las ondas con: fpga wave {stem}" if _vcd_for(p, tb) else ""))
    return rc


def cmd_wave(a) -> int:
    p = C.load_project(C.resolve_project_dir(a.C))
    tb = _pick_tb(p, a.tb)
    vcd = _vcd_for(p, tb)
    if vcd is None:
        C.die(f"No hay forma de onda de {tb}. Corre primero: fpga sim {Path(tb).stem} (el testbench debe tener $dumpfile y $dumpvars)")
    if not C.find_tool("gtkwave"):
        C.die("Falta gtkwave. Instálalo dentro de fpga-cli con: fpga install-tools")
    if os.environ.get("FPGA_WAVE_DETACH"):          # lo pone el menú: GTKWave abre su ventana y el menú sigue libre
        exe = C.find_tool("gtkwave")
        print("$ gtkwave " + vcd.name)
        subprocess.Popen([exe, vcd.name], cwd=vcd.parent, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        C.ok(f"GTKWave abierto en su propia ventana con {vcd.name}. Puedes seguir usando el menú.")
        return 0
    return C.run(["gtkwave", vcd.name], vcd.parent)


def cmd_netlist(a) -> int:
    p = C.load_project(C.resolve_project_dir(a.C))
    if not C.find_tool("yosys"):
        C.die("Falta yosys. Instálalo dentro de fpga-cli con: fpga install-tools")
    (p.dir / ".fpga").mkdir(exist_ok=True)         # ahí van las salidas: no se mezclan con tus .v
    script = (f"read_verilog -sv {' '.join(p.sim_sources())}; synth -top {p.top}; "
              "tee -o .fpga/netlist.stats stat; write_verilog -noattr .fpga/netlist.v")
    rc = C.run(["yosys", "-q", "-p", script], p.dir)
    if rc == 0:
        C.ok("Netlist genérica (solo inspección, no es la del fitter de Quartus): .fpga/netlist.v y .fpga/netlist.stats")
        stats = p.dir / ".fpga" / "netlist.stats"
        if stats.exists():
            print(stats.read_text(encoding="utf-8"))
    return rc


# ---------------------------------------------------------------- Quartus
def cmd_project(a) -> int:
    p = C.load_project(C.resolve_project_dir(a.C))
    board = C.load_board(p.board_id)
    C.write_generated(p, board)
    where = p.build_dir.relative_to(p.dir).as_posix()
    C.ok(f"Regenerados {p.name}.tcl y {p.name}.sdc desde fpga.toml" + (f" (en {where}/)" if where != "." else ""))
    return C.run(["quartus_sh", "-t", f"{p.name}.tcl"], p.build_dir, dry=a.dry_run)


def cmd_build(a) -> int:
    rc = cmd_project(a)
    if rc != 0:
        return rc
    p = C.load_project(C.resolve_project_dir(a.C))
    rc = C.run(["quartus_sh", "--flow", "compile", p.name], p.build_dir, dry=a.dry_run)
    if rc == 0 and not a.dry_run:
        outs = sorted(x.name for x in p.out_dir.glob(f"{p.name}.*") if x.suffix in (".pof", ".sof", ".rbf"))
        C.ok("Compilación terminada. Archivos: " + (", ".join(outs) or "(ninguno de programación)"))
    return rc


# ---------------------------------------------------------------- doctor
def _first_line(cmd: list[str]) -> str:
    """Devuelve la línea de versión (la que contiene 'Version'); si no hay, la primera."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        lines = ((out.stdout or "") + (out.stderr or "")).strip().splitlines()
        return next((l.strip() for l in lines if "version" in l.lower()), lines[0].strip() if lines else "")
    except Exception:
        return ""


def cmd_doctor(a) -> int:
    print(f"fpga-cli {__version__} · Python {sys.version.split()[0]}")
    tools = [("iverilog", "corre: fpga install-tools"), ("vvp", ""), ("gtkwave", "corre: fpga install-tools"),
             ("yosys", "corre: fpga install-tools (opcional)"), ("git", ""),
             ("quartus_sh", "corre: fpga install-quartus"), ("quartus_pgm", ""), ("quartus_cpf", ""), ("jtagconfig", "")]
    missing = 0
    for name, hint in tools:
        path = C.find_tool(name)
        if path:
            ver = _first_line([path, "--version"]) if name.startswith("quartus_sh") else ""
            C.ok(f"{name:12s} {path} {ver}")
        else:
            missing += 1
            C.warn(f"{name:12s} no encontrado {('→ ' + hint) if hint else ''}")
    if shutil.which("lsusb"):
        usb = [l for l in subprocess.run(["lsusb"], capture_output=True, text=True).stdout.splitlines() if "09fb" in l]
        (C.ok if usb else C.warn)("USB-Blaster (09fb) en lsusb: " + ("; ".join(usb) if usb else "no aparece (¿cable conectado?)"))
    tool = C.find_tool("jtagconfig")
    if tool:
        print("jtagconfig:")
        subprocess.call([tool])
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="fpga", description="Flujo FPGA/CPLD con Quartus desde la terminal (sin abrir la GUI).")
    ap.add_argument("-C", metavar="RUTA", help="carpeta del trabajo (si no, la actual; si no es un trabajo, la pregunta)")
    ap.add_argument("--version", action="version", version=f"fpga-cli {__version__}")
    sub = ap.add_subparsers(dest="cmd", metavar="comando")

    def add(name, func, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(func=func)
        return sp

    add("tui", cmd_tui, "abre el menú (también: fpga sin argumentos)")
    add("boards", cmd_boards, "lista las tarjetas soportadas")
    sp = add("pins", cmd_pins, "muestra las señales y pines de una tarjeta"); sp.add_argument("--board")
    sp = add("new", S.cmd_new, "crea un trabajo nuevo en la carpeta que indiques")
    sp.add_argument("name"); sp.add_argument("--board"); sp.add_argument("--dir", help="carpeta donde se crea (si no, la pregunta)")
    sp.add_argument("--template", choices=["blink", "empty"], help="blink: ejemplo de parpadeo; empty: solo el top con reloj (si no, pregunta)")
    sp.add_argument("--led", help="señal de LED de la tarjeta para la plantilla blink"); sp.add_argument("--unit", help="etiqueta de la tarjeta física")
    sp.add_argument("--no-git", action="store_true")
    sp = add("init", S.cmd_init, "convierte la carpeta actual (o -C) con archivos .v en un proyecto de fpga-cli")
    sp.add_argument("--board"); sp.add_argument("--top", help="módulo top"); sp.add_argument("--unit")
    sp = add("add", S.cmd_add, "crea un módulo nuevo y lo suma a las fuentes"); sp.add_argument("name")
    sp.add_argument("--port", action="append", metavar="PUERTO", help="un puerto del módulo, p. ej. --port 'input clk' --port 'output reg [7:0] q' (repetible)")
    sp = add("new-tb", S.cmd_new_tb, "crea un testbench para un módulo, con sus puertos ya conectados")
    sp.add_argument("module", nargs="?", help="módulo a probar (por defecto el top)"); sp.add_argument("--name", help="nombre del testbench (por defecto tb_<módulo>)")
    sp.add_argument("--force", action="store_true", help="regenera el testbench aunque ya exista (sobrescribe lo que tenga)")
    sp.add_argument("--stim", action="append", metavar="ESTÍMULO", help="un estímulo, p. ej. --stim '#100 rst_n=1' (repetible; # = ns de espera antes de asignar)")
    sp = add("pin", S.cmd_pin, "asigna puertos del top a señales de la tarjeta (sin argumentos: lista)")
    sp.add_argument("port", nargs="?", help="puerto, o un bus como 'led[3:0]'"); sp.add_argument("signals", nargs="*", help="señal(es) de la tarjeta o PIN_nn")
    sp.add_argument("--remove", metavar="PUERTO", help="quita una asignación")
    sp = add("tidy", S.cmd_tidy, "ordena un proyecto de estructura plana en src/, tb/, sim/, waves/ y build/")
    sp.add_argument("--dry-run", action="store_true", help="muestra qué se movería sin tocar nada"); sp.add_argument("--yes", action="store_true")
    sp = add("sim", cmd_sim, "simula con iverilog + vvp"); sp.add_argument("tb", nargs="?", help="testbench (si hay varios)")
    sp = add("wave", cmd_wave, "abre la forma de onda en gtkwave"); sp.add_argument("tb", nargs="?")
    add("netlist", cmd_netlist, "netlist genérica con yosys (solo inspección)")
    sp = add("project", cmd_project, "regenera .tcl/.sdc y crea el proyecto de Quartus"); sp.add_argument("--dry-run", action="store_true")
    sp = add("build", cmd_build, "crea el proyecto y compila con Quartus"); sp.add_argument("--dry-run", action="store_true")
    sp = add("prog", P.cmd_prog, "programa la tarjeta (con avisos)")
    sp.add_argument("--target", choices=["ram", "flash"], help="solo Cyclone IV"); sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--convert-only", action="store_true", help="con --target flash: solo convierte el .sof al archivo de la flash (.jic), sin tocar la tarjeta")
    sp = add("counter", P.cmd_counter, "cargas registradas de la flash (MAX V)")
    sp.add_argument("--unit"); sp.add_argument("--add", type=int); sp.add_argument("--set", type=int)
    sp = add("install-quartus", I.cmd_install_quartus, "guía la instalación de Quartus Lite 25.1 (descarga, SHA1, instalación)")
    sp.add_argument("--dir", help="carpeta donde descargaste los archivos (por defecto ~/Descargas o ~/Downloads)")
    sp.add_argument("--prefix", help="dónde instalar (por defecto ~/altera_lite/25.1std)")
    sp.add_argument("--interactive", action="store_true", help="usa los menús del propio instalador, sin banderas")
    sp.add_argument("--manual", action="store_true", help="usa los 3 archivos individuales aunque tengas el instalador qinst")
    sp.add_argument("--register", metavar="RUTA", help="no instala: solo registra una instalación existente de Quartus")
    sp.add_argument("--force", action="store_true"); sp.add_argument("--no-open", action="store_true"); sp.add_argument("--dry-run", action="store_true")
    sp = add("install-tools", T.cmd_install_tools, "instala iverilog, vvp, gtkwave y yosys dentro de fpga-cli/tools (sin sudo)")
    sp.add_argument("--tag", help="versión de OSS CAD Suite (AAAA-MM-DD); por defecto la última")
    sp.add_argument("--url", help="URL (o file://) del .tgz, en vez de GitHub")
    sp.add_argument("--yes", action="store_true"); sp.add_argument("--force", action="store_true"); sp.add_argument("--dry-run", action="store_true")
    add("doctor", cmd_doctor, "revisa herramientas y programador")
    return ap


def cmd_tui(a) -> int:
    return U.run(a.C)


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd is None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            return U.run(args.C)
        parser.print_help()
        return 0
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        print()
        return 130
    except BrokenPipeError:
        return 0

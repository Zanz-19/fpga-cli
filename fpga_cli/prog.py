"""Programación de las tarjetas, con las salvaguardas de cada una.

MAX V      -> solo flash interna: advertencia fuerte, confirmación escrita y contador de cargas.
Cyclone IV -> hay que elegir: RAM (.sof, volátil) o flash (.jic por JTAG, arranca sola).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from . import core as C

COUNTERS = C.DATA_DIR / "counters.json"


# ---------------------------------------------------------------- contador de cargas
def counters_load() -> dict:
    return C._json_read(COUNTERS, {})


def counter_get(unit: str) -> dict:
    return counters_load().get(unit, {"count": 0, "history": []})


def counter_save(unit: str, entry: dict) -> None:
    data = counters_load()
    data[unit] = entry
    C._json_write(COUNTERS, data)


def counter_add(unit: str, n: int, project: str, file: str) -> int:
    entry = counter_get(unit)
    entry["count"] = int(entry["count"]) + n
    entry["history"].append({"ts": C.now_iso(), "project": project, "file": file, "n": n})
    counter_save(unit, entry)
    return entry["count"]


def cmd_counter(a) -> int:
    if a.set is not None or a.add is not None:
        if not a.unit:
            C.die("Indica la unidad con --unit (por ejemplo: --unit maxv-1).")
        if a.set is not None:
            if input(f"Fijar el contador de '{a.unit}' en {a.set}. Escribe CONFIRMO: ").strip() != "CONFIRMO":
                C.die("Cancelado.")
            entry = counter_get(a.unit)
            entry["count"] = a.set
            entry["history"].append({"ts": C.now_iso(), "project": "-", "file": "-", "n": f"set {a.set}"})
            counter_save(a.unit, entry)
        else:
            counter_add(a.unit, a.add, "-", "ajuste manual")
    data = counters_load()
    if not data:
        C.info("Aún no hay cargas registradas.")
        return 0
    for unit, entry in sorted(data.items()):
        if a.unit and unit != a.unit:
            continue
        last = entry["history"][-1]["ts"] if entry["history"] else "-"
        print(f"{unit}: {entry['count']} cargas registradas (última: {last})")
    C.info("Solo cuenta lo que graba esta herramienta; es una estimación, no una lectura del chip.")
    return 0


# ---------------------------------------------------------------- utilidades
def _rel(p: Path, base: Path) -> str:
    return os.path.relpath(p, base)


def check_fresh(p: C.Project, path: Path) -> None:
    if not path.exists():
        C.die(f"No existe {path.name}. Compila primero: fpga build")
    if p.newest_source_mtime() > path.stat().st_mtime:
        C.die(f"{path.name} es más viejo que tus fuentes. Compila otra vez (fpga build) antes de programar.")


def _type_to_confirm(word: str) -> bool:
    try:
        return input(f"Escribe {word} para continuar (cualquier otra cosa cancela): ").strip() == word
    except EOFError:
        return False


# ---------------------------------------------------------------- MAX V
def prog_flash_only(p: C.Project, board: dict, a) -> int:
    prog = board["program"]
    limit, warn_at = int(prog.get("limit", 100)), int(prog.get("warn_at", 80))
    pof = p.out_file(prog["file"])
    check_fresh(p, pof)
    count = counter_get(p.unit)["count"]
    simulated = p.is_simulated()

    bar = "=" * 66
    print(C._c("33", bar))
    print(C._c("33", f"  ATENCIÓN: {board['name']}: cada carga GASTA un ciclo de la flash"))
    print(C._c("33", bar))
    print(f"  Dispositivo : {p.device}")
    print(f"  Archivo     : {_rel(pof, p.dir)}")
    print(f"  Unidad      : {p.unit}")
    print(f"  Garantía    : solo {limit} ciclos de borrado/programación de la flash de configuración")
    print(f"  Cargas registradas por esta herramienta: {count} de {limit} (estimación)")
    if not simulated:
        C.warn("No has simulado esta versión del diseño (fpga sim). Simula antes de gastar un ciclo.")
    if count >= warn_at:
        C.warn(f"Ya llevas {count} cargas: estás cerca del límite de {limit}.")
    print(C._c("33", bar))

    cmd = C.program_cmd(p, ["-m", "jtag", "-o", f"p;{_rel(pof, p.dir)}"])
    if a.dry_run:
        C.info("dry-run: no se pide confirmación ni se cuenta la carga.")
        C.run(cmd, p.dir, dry=True)
        return 0
    if count >= limit:
        C.warn(f"Superarías el límite garantizado ({limit}). La flash puede fallar.")
        if not _type_to_confirm("SUPERAR LIMITE"):
            C.die("Cancelado: no se grabó nada.")
    if not _type_to_confirm("GRABAR"):
        C.die("Cancelado: no se grabó nada.")

    rc = C.run(cmd, p.dir)
    if rc == 0:
        new = counter_add(p.unit, 1, str(p.dir), pof.name)
        C.ok(f"Cargado. Contador de '{p.unit}': {new} de {limit}.")
    else:
        C.warn("quartus_pgm falló. NO se contó la carga, pero si falló a mitad la flash pudo borrarse.")
        C.warn(f"Si crees que sí gastó un ciclo: fpga counter --unit {p.unit} --add 1")
    return rc


# ---------------------------------------------------------------- Cyclone IV
def _choose_target(a, prog: dict) -> tuple[str, bool]:
    """Devuelve (destino, solo_convertir)."""
    if a.target:
        return a.target, bool(a.convert_only)
    if not sys.stdin.isatty():
        C.die("Indica el destino con --target ram|flash.")
    print("¿Qué quieres hacer?")
    print(f"  1) Cargar a la RAM   (.{prog['ram_file']} por JTAG; se pierde al apagar; NO toca la flash)")
    print(f"  2) Grabar la flash   (.{prog['flash_file']} por {prog.get('flash_via', 'AS')}; la FPGA arranca sola; SOBRESCRIBE lo que traiga la tarjeta)")
    print(f"  3) Solo convertir el archivo para la flash (.{prog['flash_file']}); NO toca la tarjeta")
    choice = C.ask("Elige 1, 2 o 3", "1")
    if choice not in ("1", "2", "3"):
        C.die("Opción no válida.")
    return {"1": ("ram", False), "2": ("flash", False), "3": ("flash", True)}[choice]


def prog_ram_or_flash(p: C.Project, board: dict, a) -> int:
    prog = board["program"]
    target, convert_only = _choose_target(a, prog)
    sof = p.out_file(prog["ram_file"])
    check_fresh(p, sof)

    if target == "ram":
        C.info("Destino: RAM de la FPGA (volátil). Al apagar, el diseño se pierde.")
        cmd = C.program_cmd(p, ["-m", "jtag", "-o", f"p;{_rel(sof, p.dir)}"])
        return C.run(cmd, p.dir, dry=a.dry_run)

    pof = p.out_file(prog["flash_file"])          # el archivo de la flash (.jic o .pof según la tarjeta)
    out = _rel(pof, p.dir)
    fmt = {"sof": _rel(sof, p.dir), "out": out, "pof": out, "jic": out}
    convert = [s.format(**fmt) for s in prog["flash_convert"]]
    program = [s.format(**fmt) for s in prog["flash_program"]]
    if p.cable and program[0] == "quartus_pgm":
        program += ["-c", p.cable]
    C.info("Destino: FLASH de configuración (la FPGA arrancará sola con este diseño).")
    if prog.get("flash_note"):
        C.warn(prog["flash_note"])
    C.warn("Los comandos de la flash no están verificados en la placa: revisa que Quartus no marque error.")
    if convert_only:
        rc = C.run(convert, p.dir, dry=a.dry_run)
        if rc == 0 and not a.dry_run:
            C.ok(f"Conversión lista: {_rel(pof, p.dir)}. No se tocó la tarjeta; para grabar: fpga prog --target flash")
        return rc
    if not a.dry_run:
        C.warn("Grabar la flash SOBRESCRIBE lo que tenga (por ejemplo, el programa de demostración de fábrica).")
        if input("¿Convertir y grabar la flash ahora? [s/N]: ").strip().lower() not in ("s", "si", "sí", "y"):
            C.die("Cancelado: no se grabó nada.")
    rc = C.run(convert, p.dir, dry=a.dry_run)
    if rc != 0:
        C.die(f"Falló la conversión a .{prog['flash_file']}; no se programó nada.")
    rc = C.run(program, p.dir, dry=a.dry_run)
    if rc == 0 and not a.dry_run:
        C.ok("Flash grabada. Apaga y enciende la tarjeta para comprobar que arranca sola.")
    return rc


def cmd_prog(a) -> int:
    p = C.load_project(C.resolve_project_dir(a.C))
    board = C.load_board(p.board_id)
    mode = board["program"]["mode"]
    if a.convert_only and (mode == "flash_only" or a.target != "flash"):
        C.die("--convert-only solo aplica a la Cyclone IV con --target flash.")
    if mode == "flash_only":
        if a.target:
            C.warn(f"--target no aplica a {board['name']}: solo tiene flash interna.")
        return prog_flash_only(p, board, a)
    return prog_ram_or_flash(p, board, a)

"""Creación y edición de proyectos: new, init, add, new-tb y pin."""
from __future__ import annotations

import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import core as C

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def render(template: str, **kv) -> str:
    text = (C.TEMPLATES_DIR / template).read_text(encoding="utf-8")
    for key, value in kv.items():
        text = text.replace(f"@@{key.upper()}@@", str(value))
    return text


def pick_board(board_id: str | None) -> dict:
    if board_id:
        return C.load_board(board_id)
    boards = C.list_boards()
    if not sys.stdin.isatty():
        C.die("Indica la tarjeta con --board (" + ", ".join(b["id"] for b in boards) + ").")
    print("Tarjetas disponibles:")
    for i, b in enumerate(boards, 1):
        print(f"  {i}) {b['id']:9s} {b['name']}  [{b['device']}]")
    choice = C.ask("Elige el número", "1")
    try:
        return boards[int(choice) - 1]
    except (ValueError, IndexError):
        C.die("Opción no válida.")


# ---------------------------------------------------------------- edición mínima de fpga.toml
def _key_matches(line: str, key: str) -> bool:
    m = re.match(r'^\s*("?)([^"=\s]+)\1\s*=', line)
    return bool(m and m.group(2) == key)


def _quote_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else f'"{key}"'


def _bounds(lines: list[str], section: str):
    start = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == f"[{section}]":
            start = i + 1
        elif start is not None and stripped.startswith("["):
            return start, i
    return (start, len(lines)) if start is not None else None


def toml_set(path: Path, section: str, key: str, literal: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    new_line = f"{_quote_key(key)} = {literal}"
    b = _bounds(lines, section)
    if b is None:
        lines += ["", f"[{section}]", new_line]
    else:
        start, end = b
        for i in range(start, end):
            if _key_matches(lines[i], key):
                lines[i] = new_line
                break
        else:
            # justo después de la última clave de la sección (no después de comentarios finales ni de líneas en blanco)
            last = start
            for i in range(start, end):
                if lines[i].strip() and not lines[i].lstrip().startswith("#"):
                    last = i + 1
            lines.insert(last, new_line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def toml_remove(path: Path, section: str, key: str) -> bool:
    lines = path.read_text(encoding="utf-8").splitlines()
    b = _bounds(lines, section)
    if b is None:
        return False
    for i in range(*b):
        if _key_matches(lines[i], key):
            del lines[i]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return True
    return False


def _list_literal(items: list[str]) -> str:
    return "[" + ", ".join(f'"{x}"' for x in items) + "]"


# ---------------------------------------------------------------- lectura de módulos Verilog
def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def _match_paren(text: str, i: int) -> int:
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    return -1


def _split_top(text: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts


def parse_module(text: str, name: str) -> dict | None:
    """Lee la cabecera ANSI de un módulo: {'params': [(nombre, valor)], 'ports': [(dir, ancho, nombre)], 'ansi': bool}."""
    t = _strip_comments(text)
    m = re.search(r"\bmodule\s+" + re.escape(name) + r"\b", t)
    if not m:
        return None
    j = m.end()
    ws = lambda k: k + len(t[k:]) - len(t[k:].lstrip())
    j = ws(j)
    params = []
    if j < len(t) and t[j] == "#":
        j = ws(j + 1)
        if j >= len(t) or t[j] != "(":
            return None
        end = _match_paren(t, j)
        for item in _split_top(t[j + 1:end]):
            pm = re.match(r"(?:parameter|localparam)?\s*(?:integer|int|signed|unsigned|\[[^\]]*\]|\s)*?(\w+)\s*=\s*(.+)$", item, re.S)
            if pm:
                params.append((pm.group(1), " ".join(pm.group(2).split())))
        j = ws(end + 1)
    if j >= len(t) or t[j] != "(":
        return {"params": params, "ports": [], "ansi": True}
    end = _match_paren(t, j)
    ports, direction, width = [], None, ""
    for item in _split_top(t[j + 1:end]):
        item = re.sub(r"=.*$", "", item, flags=re.S).strip()
        dm = re.match(r"(input|output|inout)\b", item)
        if dm:
            direction = dm.group(1)
            wm = re.search(r"\[[^\]]*\]", item)
            width = wm.group(0) if wm else ""
        elif direction is None:
            return {"params": params, "ports": [], "ansi": False}
        nm = re.findall(r"[A-Za-z_]\w*", re.sub(r"\[[^\]]*\]", "", item))
        if nm:
            ports.append((direction, width, nm[-1]))
    return {"params": params, "ports": ports, "ansi": True}


def make_testbench(module: str, info: dict | None, tb_name: str, clock_mhz: float, dumpfile: str,
                   stim: list[tuple[str, list[tuple[str, str]]]] | None = None) -> str:
    half = 500.0 / clock_mhz
    L = ["`timescale 1ns/1ps", f"module {tb_name};"]
    inst = []
    params = info["params"] if info else []
    for n, v in params:                       # los anchos de los puertos pueden depender de ellos
        L.append(f"    localparam {n} = {v};")
    if params:
        L.append("")
    if info and info["ansi"] and info["ports"]:
        for d, w, n in info["ports"]:
            decl = "reg" if d == "input" else "wire"
            L.append(f"    {decl} {(w + ' ') if w else ''}{n}{' = 0' if d == 'input' else ''};")
            inst.append(f"        .{n}({n})")
        L.append("")
        hdr = f"{module} #(" + ", ".join(f".{n}({n})" for n, _ in params) + ") uut (" if params else f"{module} uut ("
        L.append(f"    {hdr}\n" + ",\n".join(inst) + "\n    );")
        has_clk = any(n == "clk" and d == "input" for d, w, n in info["ports"])
    else:
        L += ["    // TODO: no pude leer los puertos de este módulo (¿cabecera no ANSI?); conéctalo a mano.",
              f"    // {module} uut (...);"]
        has_clk = False
    if has_clk:
        L += ["", f"    always #{half:g} clk = ~clk;      // {clock_mhz:g} MHz"]
    L += ["", "    initial begin", f'        $dumpfile("{dumpfile}");', f"        $dumpvars(0, {tb_name});"]
    if stim:
        total = 0.0
        L.append("        // estímulos: cada # espera esa cantidad de ns antes de asignar")
        for delay, assigns in stim:
            total += float(delay or 0)
            body = " ".join(f"{n} = {v};" for n, v in assigns)
            L.append("        " + " ".join(x for x in ((f"#{delay}" if delay else ""), body) if x) + ("" if assigns else ";"))
        L.append(f"        #{max(2000 - total, 1000):g} $finish;")
    else:
        L += ["        // TODO: estímulos", "        #2000 $finish;"]
    L += ["    end", "endmodule", ""]
    return "\n".join(L)


# ---------------------------------------------------------------- new / init
def _project_toml(name, top, sources, tbs, board, unit, pins, layout="dirs") -> str:
    pins_lines = "\n".join(f'{_quote_key(k)} = "{v}"' for k, v in pins.items())
    return (
        "# Proyecto de fpga-cli. Tras editar, corre `fpga project` para regenerar el .tcl y el .sdc.\n"
        f'[project]\nname = "{name}"\ntop = "{top}"\nlayout = "{layout}"      # dirs: src/ tb/ sim/ waves/ build/ · flat: todo en la raíz\n'
        f"sources = {_list_literal(sources)}      # admite comodines: \"*.v\", \"src/**/*.v\"\n"
        f"testbenches = {_list_literal(tbs)}\n\n"
        f'[board]\nid = "{board["id"]}"\nunit = "{unit}"      # etiqueta de la tarjeta física (el contador de cargas va por unidad)\n'
        f'family = "{board["family"]}"\ndevice = "{board.get("quartus_device", board["device"])}"\n'
        f'clock_mhz = {board["clock"]["mhz"]}\n\n'
        f"[pins]\n{pins_lines}\n\n"
        '# [programmer]\n# cable = "USB-Blaster [1-1]"   # solo si hay más de un programador conectado\n')


def cmd_new(a) -> int:
    if not NAME_RE.match(a.name):
        C.die("El nombre debe empezar con letra y usar solo letras, números, guion y guion bajo.")
    folder = a.name
    name = folder.replace("-", "_")      # módulo de Verilog y proyecto de Quartus no admiten guiones
    board = pick_board(a.board)

    template = a.template
    if not template:
        if sys.stdin.isatty():
            print("Plantilla inicial:\n  1) blink  parpadeo de un LED: un ejemplo que ya funciona\n  2) empty  solo el módulo top con el reloj: para tus propios diseños")
            template = {"1": "blink", "2": "empty"}.get(C.ask("Elige 1 o 2", "1"))
            if not template:
                C.die("Opción no válida.")
        else:
            template = "blink"

    default = C.config_get("last_dir") or str(Path.cwd())
    parent = Path(a.dir or C.ask("Ruta de la carpeta donde se creará el trabajo", default)).expanduser().resolve()
    if not parent.exists():
        if not a.dir and sys.stdin.isatty() and C.ask(f"{parent} no existe. ¿Crearla? [S/n]", "S").lower() not in ("s", "si", "sí", "y"):
            C.die("Cancelado.")
        parent.mkdir(parents=True)
    target = parent / folder
    if target.exists() and any(target.iterdir()):
        C.die(f"{target} ya existe y no está vacía.")
    target.mkdir(exist_ok=True)

    mhz = board["clock"]["mhz"]
    clk_pin = C.pin_name(board["pins"]["clk"])
    pins = {"clk": clk_pin}
    src_file, tb_file = f"src/{name}.v", "tb/tb.v"
    (target / "src").mkdir(); (target / "tb").mkdir()
    if template == "blink":
        led = a.led or board["default_led"]
        if led not in board["pins"]:
            C.die(f"La tarjeta no tiene la señal '{led}'. Mira las disponibles con: fpga pins --board {board['id']}")
        pins["led"] = C.pin_name(board["pins"][led])
        low = board.get("led_active_low", False)
        (target / src_file).write_text(render(
            "blink.v", name=name, board=board["name"], mhz=mhz, n=round(math.log2(mhz * 1e6 / 1.5)),
            lednote="LED activo en bajo en esta tarjeta: enciende con 0" if low else "LED activo en alto"), encoding="utf-8")
        (target / tb_file).write_text(render("tb.v", name=name), encoding="utf-8")
    else:
        (target / src_file).write_text(render("empty.v", name=name, mhz=mhz, clkpin=clk_pin), encoding="utf-8")
        tb_file = f"tb/tb_{name}.v"              # el testbench del top; se regenera con `fpga new-tb --force` si cambian sus puertos
        tb = make_testbench(name, parse_module((target / src_file).read_text(), name), f"tb_{name}", mhz, f"tb_{name}.vcd")
        (target / tb_file).write_text(tb, encoding="utf-8")
    unit = a.unit or f"{board['id']}-1"
    (target / "Makefile").write_text(render("Makefile.dirs", name=name, tb=tb_file), encoding="utf-8")
    (target / ".gitignore").write_text((C.TEMPLATES_DIR / "gitignore.dirs").read_text(encoding="utf-8"), encoding="utf-8")
    (target / "fpga.toml").write_text(_project_toml(name, name, [src_file], [tb_file], board, unit, pins, "dirs"), encoding="utf-8")

    p = C.load_project(target)
    C.write_generated(p, board)
    note = ""
    if board["program"]["mode"] == "flash_only":
        note = (f"> **Aviso:** la flash de la {board['name']} garantiza solo {board['program'].get('limit', 100)} ciclos "
                "de programación. Simula siempre antes de grabar y programa únicamente con `fpga prog`.\n")
    (target / "README.md").write_text(render(
        "README.md", name=name, boardname=board["name"], device=board["device"], note=note,
        pins="\n".join(f"- `{k}` → `{v}`" for k, v in pins.items())), encoding="utf-8")

    if not a.no_git and shutil.which("git"):
        subprocess.run(["git", "init", "-q"], cwd=target, check=False)
    C.config_set("last_dir", str(parent))
    C.remember_project(target)
    C.ok(f"Trabajo creado en {target}  (plantilla: {template})")
    if name != folder:
        C.info(f"Módulo y proyecto de Quartus: {name} (los guiones se cambian por guion bajo)")
    print(f"  Tarjeta: {board['name']} · {board['device']} · reloj {mhz} MHz")
    print("  Pines  : " + ", ".join(f"{k} = {v}" for k, v in pins.items()))
    if template == "empty":
        print(f"\nSiguiente:\n  cd {target}\n  edita src/{name}.v, asigna puertos con `fpga pin`, y luego: fpga sim && fpga build")
    else:
        print(f"\nSiguiente:\n  cd {target}\n  fpga sim && fpga build && fpga prog")
    return 0


def cmd_init(a) -> int:
    """Convierte una carpeta con archivos .v que ya tienes en un proyecto de fpga-cli (no crea archivos de diseño)."""
    d = Path(a.C or ".").expanduser().resolve()
    if (d / "fpga.toml").exists():
        C.die(f"{d} ya es un proyecto (tiene fpga.toml).")
    board = pick_board(a.board)
    vfiles = sorted(p.name for p in d.glob("*.v"))
    if not vfiles:
        C.die(f"No hay archivos .v en {d}. Para empezar de cero usa: fpga new NOMBRE --template empty")
    tbs = [f for f in vfiles if re.match(r"tb", f, re.I)]
    default_top = d.name.replace("-", "_")
    top = a.top or C.ask("Nombre del módulo top", default_top)
    if not IDENT_RE.match(top):
        C.die("El módulo top debe ser un identificador de Verilog (letras, números y guion bajo).")
    text = "\n".join((d / f).read_text(errors="ignore") for f in vfiles if f not in tbs)
    if not re.search(r"\bmodule\s+" + re.escape(top) + r"\b", _strip_comments(text)):
        C.warn(f"No encuentro `module {top}` en tus fuentes: revisa el nombre.")
    mhz = board["clock"]["mhz"]
    pins = {"clk": C.pin_name(board["pins"]["clk"])} if re.search(r"\binput\b[^;,)]*\bclk\b", _strip_comments(text)) else {}
    unit = a.unit or f"{board['id']}-1"
    (d / "fpga.toml").write_text(_project_toml(top, top, ["*.v"], tbs, board, unit, pins, "flat"), encoding="utf-8")
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text((C.TEMPLATES_DIR / "gitignore").read_text(encoding="utf-8"), encoding="utf-8")
    p = C.load_project(d)
    C.write_generated(p, board)
    C.remember_project(d)
    C.ok(f"Proyecto iniciado en {d}")
    print(f"  Top: {top} · fuentes: {', '.join(p.resolved_sources()) or '(ninguna)'} · testbenches: {', '.join(tbs) or '(ninguno)'}")
    print("  Pines: " + (", ".join(f"{k} = {v}" for k, v in pins.items()) or "(ninguno todavía)"))
    print("\nAsigna los puertos del top a la tarjeta con `fpga pin <puerto> <señal>` (señales: `fpga pins`).")
    C.info("Tus archivos se quedaron donde estaban (estructura plana). Para ordenarlos en src/, tb/, sim/, waves/ y build/: fpga tidy")
    return 0


# ---------------------------------------------------------------- puertos de un módulo nuevo y estímulos de un testbench
VERILOG_KEYWORDS = {
    "always", "and", "assign", "begin", "case", "end", "endcase", "endmodule", "for", "function", "if", "else", "initial", "inout", "input",
    "integer", "localparam", "module", "negedge", "or", "output", "parameter", "posedge", "reg", "wire", "while", "not", "xor", "nand", "nor"}
DIRECTIONS = {"input": "input", "in": "input", "entrada": "input", "output": "output", "out": "output", "salida": "output", "inout": "inout"}
WIDTH_RE = re.compile(r"^\[\s*([\w+\-*/() ]+?)\s*:\s*([\w+\-*/() ]+?)\s*\]$")
PORT_HELP = "Ejemplos: input clk · input [3:0] a · output reg [7:0] q · output y"
STIM_HELP = "Formato: #100 rst_n=1 (espera 100 ns y asigna) · varias: #50 a=3 b=4'b1010 · solo esperar: #200"
VALUE_RE = re.compile(r"^(?:\d+'[sS]?[bBoOdDhH][0-9a-fA-F_xXzZ?]+|\d+)$")


def parse_port(text: str) -> tuple[str, str, str, str]:
    """«output reg [7:0] q» → ('output', 'reg', '[7:0]', 'q'). Lanza ValueError con un mensaje claro si algo no cuadra."""
    t = text.strip().rstrip(",;").strip()
    if not t:
        raise ValueError("Escribe el puerto, por ejemplo: input clk")
    head, _, rest = t.partition(" ")
    direction = DIRECTIONS.get(head.lower())
    if not direction:
        raise ValueError(f"«{head}» no es una dirección: empieza con input, output o inout (ej: input clk)")
    found = re.findall(r"\[[^\]]*\]", rest)
    if len(found) > 1:
        raise ValueError("Un puerto lleva un solo ancho, como [7:0]")
    width = ""
    if found:
        m = WIDTH_RE.match(found[0])
        if not m:
            raise ValueError(f"El ancho «{found[0]}» no es válido: se escribe [7:0]")
        width = f"[{m.group(1).replace(' ', '')}:{m.group(2).replace(' ', '')}]"
        rest = rest.replace(found[0], " ")
    words = rest.replace(",", " ").split()
    kind = "wire"
    if "reg" in words:
        if direction != "output":
            raise ValueError("Solo una salida puede ser reg (output reg ...)")
        kind = "reg"
    words = [w for w in words if w not in ("wire", "reg")]
    if not words:
        raise ValueError("Falta el nombre del puerto (ej: input clk)")
    if len(words) > 1:
        raise ValueError(f"Sobra «{' '.join(words[1:])}»: un puerto por línea, con un solo nombre")
    name = words[0]
    if not IDENT_RE.match(name):
        raise ValueError(f"«{name}» no es un nombre válido: letras, números y guion bajo, sin empezar con número")
    if name in VERILOG_KEYWORDS:
        raise ValueError(f"«{name}» es una palabra reservada de Verilog: elige otro nombre")
    return direction, kind, width, name


def format_port(port: tuple[str, str, str, str]) -> str:
    d, k, w, n = port
    return " ".join(x for x in (d, k if k == "reg" else "", w, n) if x)


def make_module(name: str, ports: list[tuple[str, str, str, str]]) -> str:
    """Esqueleto de un módulo con la cabecera ANSI que `fpga new-tb` sabe volver a leer."""
    if not ports:
        return render("module.v", name=name)
    ww = max(len(w) for _, _, w, _ in ports)
    rows = [f"    {d:<6} {k:<4} {w:<{ww}} {n}" for d, k, w, n in ports]
    rows = [r + ("," if i < len(rows) - 1 else "") for i, r in enumerate(rows)]
    return "\n".join([f"// {name}", f"module {name} (", *rows, ");", "", "endmodule", ""])


def parse_stimulus(line: str, inputs: list[str]) -> tuple[str, list[tuple[str, str]]]:
    """«#100 rst_n=1 a=4'b1010» → ('100', [('rst_n', '1'), ('a', "4'b1010")]). Valida contra las entradas del módulo."""
    t = re.sub(r"\s*=\s*", "=", line.strip().rstrip(";"))
    if not t:
        raise ValueError("Escribe un estímulo, por ejemplo: #100 rst_n=1")
    tokens = t.replace(",", " ").split()
    delay = ""
    if tokens[0].startswith("#"):
        delay = tokens[0][1:]
        if not re.match(r"^\d+(\.\d+)?$", delay):
            raise ValueError(f"«{tokens[0]}» no es un tiempo válido: #100 espera 100 ns")
        tokens = tokens[1:]
    assigns = []
    for tok in tokens:
        name, eq, value = tok.partition("=")
        if not eq or not name or not value:
            raise ValueError(f"«{tok}» no es una asignación: se escribe señal=valor (ej: rst_n=1)")
        if name == "clk" and "clk" in inputs:
            raise ValueError("clk lo genera el testbench solo; asigna las otras entradas")
        if name not in inputs:
            raise ValueError(f"«{name}» no es una entrada del módulo (entradas: {', '.join(n for n in inputs if n != 'clk') or 'ninguna'})")
        if not VALUE_RE.match(value):
            raise ValueError(f"El valor «{value}» no es válido: usa 1, 0, 5, 4'b1010 o 8'hFF")
        assigns.append((name, value))
    if not delay and not assigns:
        raise ValueError("Escribe un tiempo (#100) o una asignación (rst_n=1)")
    return delay, assigns


def format_stimulus(stim: tuple[str, list[tuple[str, str]]]) -> str:
    delay, assigns = stim
    return " ".join(x for x in ((f"#{delay}" if delay else ""), *(f"{n}={v}" for n, v in assigns)) if x)


def module_info(p: C.Project, module: str) -> tuple[dict | None, list[tuple[str, int]]]:
    """Busca `module <nombre>` en las fuentes: (cabecera leída, [(archivo, veces definido)])."""
    info, defs = None, []
    for rel in p.sim_sources():
        text = (p.dir / rel).read_text(errors="ignore")
        n = len(re.findall(r"\bmodule\s+" + re.escape(module) + r"\b", _strip_comments(text)))
        if n:
            defs.append((rel, n))
            if info is None:
                info = parse_module(text, module)
    return info, defs


def module_inputs(p: C.Project, module: str) -> list[str]:
    info, _ = module_info(p, module)
    return [n for d, w, n in info["ports"] if d == "input"] if info and info["ansi"] else []


# ---------------------------------------------------------------- add / new-tb
def _explicit_sources(p: C.Project) -> bool:
    return not any(any(ch in s for ch in "*?[") for s in p.sources)


def cmd_add(a) -> int:
    p = C.load_project(C.resolve_project_dir(a.C))
    if not IDENT_RE.match(a.name):
        C.die("El nombre del módulo debe ser un identificador de Verilog (letras, números y guion bajo).")
    path = p.src_dir / f"{a.name}.v"
    rel = path.relative_to(p.dir).as_posix()
    if path.exists():
        C.die(f"{rel} ya existe.")
    ports = []
    for text in (getattr(a, "port", None) or []):
        try:
            ports.append(parse_port(text))
        except ValueError as exc:
            C.die(f"Puerto «{text}»: {exc}")
    dup = sorted({n for _, _, _, n in ports if [x[3] for x in ports].count(n) > 1})
    if dup:
        C.die(f"Puerto repetido: {', '.join(dup)}")
    p.src_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(make_module(a.name, ports), encoding="utf-8")
    if _explicit_sources(p):
        toml_set(p.dir / "fpga.toml", "project", "sources", _list_literal(p.sources + [rel]))
        C.ok(f"Creado {rel} y agregado a `sources`.")
    else:
        C.ok(f"Creado {rel} (ya lo cubren los comodines de `sources`).")
    if ports:
        C.info("Puertos: " + " · ".join(format_port(x) for x in ports))
    C.info(f"Conéctalo desde el top instanciando `{a.name}` y crea su testbench con: fpga new-tb {a.name}")
    return 0


def cmd_new_tb(a) -> int:
    p = C.load_project(C.resolve_project_dir(a.C))
    module = a.module or p.top
    info, defs = module_info(p, module)
    if info is None:
        C.die(f"No encuentro `module {module}` en las fuentes del proyecto. ¿Guardaste el archivo?")
    if sum(n for _, n in defs) > 1:
        C.warn(f"`module {module}` está definido {sum(n for _, n in defs)} veces ({', '.join(f'{r} ×{n}' for r, n in defs)}): Verilog no lo permite. "
               "¿Pegaste el código nuevo sin borrar el esqueleto anterior? Se usó la PRIMERA definición.")
    tb_name = a.name or f"tb_{module}"
    if not IDENT_RE.match(tb_name):
        C.die("El nombre del testbench debe ser un identificador de Verilog.")
    path = p.tb_dir / f"{tb_name}.v"
    rel = path.relative_to(p.dir).as_posix()
    if path.exists() and not a.force:
        C.die(f"{rel} ya existe. Para regenerarlo (y perder lo que le hayas escrito): fpga new-tb {module} --force")
    stim = []
    if getattr(a, "stim", None):
        if not (info["ansi"] and info["ports"]):
            C.die("No pude leer los puertos del módulo, así que no puedo validar los estímulos. Edítalos a mano en el testbench.")
        inputs = [n for d, w, n in info["ports"] if d == "input"]
        for text in a.stim:
            try:
                stim.append(parse_stimulus(text, inputs))
            except ValueError as exc:
                C.die(f"Estímulo «{text}»: {exc}")
    p.tb_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(make_testbench(module, info, tb_name, p.clock_mhz, f"{tb_name}.vcd", stim), encoding="utf-8")
    if rel not in p.testbenches:
        toml_set(p.dir / "fpga.toml", "project", "testbenches", _list_literal(p.testbenches + [rel]))
    C.ok(f"Creado {rel} para `{module}` y agregado a `testbenches`.")
    if info["ansi"] and info["ports"]:
        ins = [n for d, w, n in info["ports"] if d == "input"]
        outs = [n for d, w, n in info["ports"] if d != "input"]
        C.info(f"Puertos conectados · entradas: {', '.join(ins) or '(ninguna)'} · salidas: {', '.join(outs) or '(ninguna)'}")
        if info["params"]:
            C.info("Parámetros declarados arriba del testbench: " + ", ".join(f"{n} = {v}" for n, v in info["params"]) + " (acórtalos si hacen la simulación muy larga)")
        C.info("Si esos puertos no son los de tu módulo, el archivo no estaba guardado o tiene otra definición: corrígelo y usa --force.")
    else:
        C.warn("No pude leer los puertos del módulo; completa la instancia a mano.")
    if stim:
        C.info("Estímulos: " + " · ".join(format_stimulus(x) for x in stim))
        C.info(f"Corre: fpga sim {tb_name}")
    else:
        C.info(f"Edita los estímulos y corre: fpga sim {tb_name}")
    return 0


# ---------------------------------------------------------------- pin
def _resolve_signal(board: dict, sig: str) -> str:
    if sig in board["pins"]:
        return C.pin_name(board["pins"][sig])
    if re.fullmatch(r"(?i)pin_\d+|\d+", sig):
        return C.pin_name(sig)
    C.die(f"'{sig}' no es una señal de {board['name']} ni un pin (PIN_nn). Mira las señales con: fpga pins --board {board['id']}")


def cmd_pin(a) -> int:
    p = C.load_project(C.resolve_project_dir(a.C))
    board = C.load_board(p.board_id)
    toml = p.dir / "fpga.toml"
    if a.remove:
        if toml_remove(toml, "pins", a.remove):
            C.ok(f"Quitado {a.remove}")
            return 0
        C.die(f"'{a.remove}' no está en [pins].")
    if not a.port:
        rev = {C.pin_name(v): k for k, v in board["pins"].items()}
        for port, pin in p.pins.items():
            print(f"{port:14s} {pin:8s} {rev.get(pin, '')}")
        if not p.pins:
            C.info("Aún no hay pines asignados. Uso: fpga pin <puerto> <señal de la tarjeta>")
        return 0
    rng = re.fullmatch(r"(\w+)\[(\d+):(\d+)\]", a.port)
    if rng:
        base, hi, lo = rng.group(1), int(rng.group(2)), int(rng.group(3))
        idx = list(range(hi, lo - 1, -1)) if hi >= lo else list(range(hi, lo + 1))
        if len(a.signals) != len(idx):
            C.die(f"{a.port} necesita {len(idx)} señales (de la más a la menos significativa) y diste {len(a.signals)}.")
        for i, sig in zip(idx, a.signals):
            toml_set(toml, "pins", f"{base}[{i}]", f'"{_resolve_signal(board, sig)}"')
            print(f"  {base}[{i}] → {sig}")
        C.ok("Pines asignados. Aplican en la próxima compilación (fpga build).")
        return 0
    if len(a.signals) != 1:
        C.die("Uso: fpga pin <puerto> <señal>   (para un bus: fpga pin 'led[3:0]' led3 led2 led1 led0)")
    toml_set(toml, "pins", a.port, f'"{_resolve_signal(board, a.signals[0])}"')
    C.ok(f"{a.port} → {a.signals[0]} ({_resolve_signal(board, a.signals[0])}). Aplica en la próxima compilación (fpga build).")
    return 0


# ---------------------------------------------------------------- tidy: de estructura plana a carpetas
TIDY_DIRS = ("db", "incremental_db", "output_files", "qdb", "greybox_tmp")
TIDY_BUILD_EXT = ("qpf", "qsf", "qws", "tcl", "sdc")


def _is_tracked(root: Path, rel: str) -> bool:
    if not (root / ".git").exists() or not shutil.which("git"):
        return False
    return subprocess.run(["git", "ls-files", "--error-unmatch", rel], cwd=root, capture_output=True).returncode == 0


def _move(root: Path, src: str, dst: str) -> None:
    (root / dst).parent.mkdir(parents=True, exist_ok=True)
    if (root / src).is_file() and _is_tracked(root, src):          # git mv conserva el historial del archivo
        if subprocess.run(["git", "mv", src, dst], cwd=root, capture_output=True).returncode == 0:
            return
    shutil.move(str(root / src), str(root / dst))


def plan_tidy(p: C.Project) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """(movimientos, avisos, archivos .v sueltos que no son fuentes ni testbenches)."""
    root, moves, notes = p.dir, [], []

    def want(src: str, dst: str):
        if not (root / src).exists():
            return
        if (root / dst).exists():
            notes.append(f"{dst} ya existe: {src} se queda donde está")
        else:
            moves.append((src, dst))

    srcs, tbs = p.resolved_sources(), list(p.testbenches)
    for s in srcs:
        if "/" not in s:
            want(s, f"src/{s}")
    for t in tbs:
        if "/" not in t:
            want(t, f"tb/{t}")
    for f in sorted(root.glob("*.vvp")):
        want(f.name, f"sim/{f.name}")
    for f in sorted(root.glob("*.vcd")):
        want(f.name, f"waves/{f.name}")
    for ext in TIDY_BUILD_EXT:                                       # solo los archivos de ESTE proyecto
        want(f"{p.name}.{ext}", f"build/{p.name}.{ext}")
    for d in TIDY_DIRS:
        if (root / d).is_dir():
            want(d, f"build/{d}")
    strays = sorted(f.name for f in root.glob("*.v") if f.name not in srcs and f.name not in tbs)
    return moves, notes, strays


def cmd_tidy(a) -> int:
    p = C.load_project(C.resolve_project_dir(a.C))
    board = C.load_board(p.board_id)
    moves, notes, strays = plan_tidy(p)
    if p.layout == "dirs" and not moves:
        C.ok("Este proyecto ya está ordenado.")
        return 0
    print(f"Se ordenará {p.dir}:")
    for src, dst in moves:
        print(f"  {src:<34} →  {dst}")
    for n in notes:
        C.warn(n)
    if strays:
        C.warn("Estos .v no son fuentes ni testbenches del proyecto y no se mueven: " + ", ".join(strays))
    print("  fpga.toml: layout = \"dirs\"; las rutas de `sources` y `testbenches` se actualizan")
    print("  build/: se regeneran el .tcl y el .sdc; Makefile y .gitignore se ajustan (si el Makefile no está en git, el anterior queda como Makefile.bak)")
    if a.dry_run:
        C.info("dry-run: no se movió nada.")
        return 0
    if not a.yes:
        if not sys.stdin.isatty():
            C.die("Sin terminal interactiva: usa --yes para confirmar.")
        if C.ask("¿Ordenar ahora? [s/N]", "N").lower() not in ("s", "si", "sí", "y"):
            C.die("Cancelado: no se movió nada.")

    moved = dict(moves)
    for src, dst in moves:
        _move(p.dir, src, dst)

    def retarget(entries: list[str], folder: str) -> list[str]:
        out = []
        for e in entries:
            if any(ch in e for ch in "*?["):
                out.append(e if "/" in e else f"{folder}/{e}")
            else:
                out.append(moved.get(e, e))
        return out

    toml = p.dir / "fpga.toml"
    toml_set(toml, "project", "layout", '"dirs"')
    toml_set(toml, "project", "sources", _list_literal(retarget(p.sources, "src")))
    toml_set(toml, "project", "testbenches", _list_literal(retarget(p.testbenches, "tb")))

    p2 = C.load_project(p.dir)
    C.write_generated(p2, board)
    mk = p.dir / "Makefile"
    if mk.exists() and not _is_tracked(p.dir, "Makefile"):          # si está en git, su historial ya lo guarda
        shutil.copy2(mk, p.dir / "Makefile.bak")
    tb0 = p2.testbenches[0] if p2.testbenches else "tb/tb.v"
    mk.write_text(render("Makefile.dirs", name=p2.name, tb=tb0), encoding="utf-8")
    gi = p.dir / ".gitignore"
    have = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    wanted = [l for l in (C.TEMPLATES_DIR / "gitignore.dirs").read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    if (p.dir / "Makefile.bak").exists():
        wanted.append("Makefile.bak")
    missing = [l for l in wanted if l not in have]
    if missing:
        gi.write_text("\n".join(have + ["", "# estructura de carpetas de fpga-cli"] + missing) + "\n", encoding="utf-8")
    C.ok(f"Proyecto ordenado: {len(moves)} elementos movidos.")
    if (p.dir / ".git").exists():
        C.info("Los archivos versionados se movieron con `git mv`: revisa con `git status` y confirma el cambio.")
    C.info("Siguiente: fpga build (Quartus regenera su proyecto dentro de build/)")
    return 0

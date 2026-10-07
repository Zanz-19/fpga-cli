"""Menú de lanzamiento en la terminal (curses, solo biblioteca estándar).

Cada opción ejecuta el mismo comando que escribirías (`fpga sim`, `fpga build`…), así que el menú nunca hace nada
que la línea de comandos no pueda hacer. El comando corre en una ventana dentro del propio menú: su salida (la de
Quartus incluida) se desplaza con las flechas y lo que pide (nombres, confirmaciones como GRABAR) se escribe ahí mismo,
sin volver a la terminal. Cada ejecución queda guardada en un registro (`.fpga/logs/` del proyecto).
El editor de pines es una pantalla propia en la que se escribe, no se elige."""
from __future__ import annotations

import contextlib
import curses
import io
import locale
import os
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from argparse import Namespace
from pathlib import Path

from . import __version__
from . import core as C
from . import logo as L
from . import prog as P
from . import runner as R
from . import scaffold as S

MIN_ROWS, MIN_COLS = 24, 80
MINI_UTF = ["  ▲   ", " ▐••▌ ", " ▝██▘ "]          # sombrero, cara y barba
MINI_ASCII = ["  /\\  ", " (oo) ", " '--' "]
ANSI = re.compile(r"\x1b\[[0-9;]*m")

MENU = [  # (columna, título, [(tecla, texto, acción, necesita_proyecto)])
    (0, "PROYECTO", [("n", "Nuevo proyecto", "new", False), ("i", "Iniciar con mis .v", "init", False),
                     ("o", "Abrir otro proyecto", "open", False)]),
    (0, "DISEÑO", [("a", "Agregar módulo", "add", True), ("t", "Nuevo testbench", "newtb", True),
                   ("p", "Asignar pines", "pins", True), ("e", "Editar con nano", "edit", True)]),
    (1, "SIMULACIÓN", [("s", "Simular", "sim", True), ("w", "Ver ondas (GTKWave)", "wave", True)]),
    (1, "QUARTUS", [("b", "Compilar", "build", True), ("g", "Programar la tarjeta", "prog", True),
                    ("f", "Convertir para flash", "convert", True), ("c", "Contador de cargas", "counter", False)]),
    (2, "SISTEMA", [("d", "Diagnóstico", "doctor", False), ("q", "Instalar Quartus", "iq", False),
                    ("h", "Instalar simulación", "it", False), ("r", "Ordenar carpetas", "tidy", True),
                    ("l", "Ver registros", "logs", False), ("x", "Salir", "exit", False)]),
]
COL_X = {0: 2, 1: 28, 2: 54}
MENU_TOP = 10                       # primera fila de acciones; el menú ocupa 9 filas como máximo

HELP = {
    "new": "Crea una carpeta de trabajo nueva: te pregunta tarjeta, plantilla (blink o vacía) y dónde guardarla.",
    "init": "Convierte una carpeta con archivos .v que ya tienes en un proyecto de fpga-cli.",
    "open": "Cambia de proyecto: elige uno reciente o escribe su ruta.",
    "add": "Crea un módulo nuevo (.v) y lo suma a las fuentes del proyecto.",
    "newtb": "Crea un testbench con los puertos del módulo ya conectados y el reloj generado.",
    "pins": "Asigna los puertos de tu top a señales de la tarjeta, escribiendo (con ? ves las señales disponibles).",
    "sim": "Simula con iverilog. Si hay varios testbenches te pregunta cuál.",
    "wave": "Abre la forma de onda del testbench en GTKWave.",
    "build": "Genera el proyecto de Quartus y compila (síntesis, fitter, tiempos y archivo de programación).",
    "prog": "Programa la tarjeta. En la MAX V avisa del límite de la flash y pide escribir GRABAR.",
    "convert": "Cyclone IV: convierte el .sof a .jic (archivo de la flash) SIN tocar la tarjeta; sirve para comprobar el archivo.",
    "counter": "Muestra cuántas cargas de flash ha registrado esta herramienta por tarjeta.",
    "doctor": "Revisa qué herramientas encuentra y si ve el USB-Blaster y la tarjeta.",
    "tidy": "Ordena un proyecto de estructura plana en src/, tb/, sim/, waves/ y build/ (usa git mv si está versionado).",
    "iq": "Guía la instalación de Quartus Prime Lite 25.1 (descarga, verificación y registro de la ruta).",
    "it": "Instala iverilog, vvp, gtkwave y yosys dentro de la carpeta de fpga-cli (sin sudo), para simular y ver ondas.",
    "logs": "Muestra el registro de una ejecución anterior (lo que imprimió el comando, con fecha y código de salida).",
    "exit": "Salir del menú.",
}


def plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def drain_escape(stdscr) -> str:
    """Tras un ESC, descarta lo que llegue pegado (una flecha que la terminal mandó como ESC [ B se leería como la tecla B).
    Devuelve lo descartado; vacío significa que fue un ESC de verdad."""
    junk = ""
    stdscr.timeout(40)
    try:
        while True:
            try:
                nxt = stdscr.get_wch()
            except curses.error:
                break
            junk += nxt if isinstance(nxt, str) else "?"
    finally:
        stdscr.timeout(-1)
    return junk


ACTION_LABEL = {a: text for _, _, items in MENU for _, text, a, _ in items}


def suggest_action(info: dict | None) -> str:
    """El siguiente paso lógico según el estado del proyecto."""
    if not info:
        return "new"
    if not info["simulated"]:
        return "sim"
    if not info["built"]:
        return "build"
    return "prog"


def strip_ansi(text: str) -> str:
    return ANSI.sub("", text)


def utf8_ok() -> bool:
    if os.environ.get("FPGA_CLI_ASCII"):
        return False
    return "utf" in (locale.getpreferredencoding(False) or "").lower()


# ---------------------------------------------------------------- lógica sin pantalla (probable por separado)
def project_info(path: Path | None) -> dict | None:
    """Resumen del proyecto para el panel; None si no hay proyecto o no se puede leer."""
    if not path or not (Path(path) / "fpga.toml").exists():
        return None
    try:
        p = C.load_project(Path(path))
        board = C.load_board(p.board_id)
    except SystemExit:
        return None
    fresh = p.newest_source_mtime()
    outputs = [p.out_file(e) for e in ("pof", "sof") if p.out_file(e).exists()]
    info = {
        "name": p.name, "top": p.top, "dir": str(p.dir), "board": board["name"], "device": board["device"],
        "sources": len(p.resolved_sources()), "testbenches": len(p.testbenches),
        "simulated": p.is_simulated(), "built": bool(outputs) and all(o.stat().st_mtime >= fresh for o in outputs),
        "pins": len(p.pins), "counter": None, "mode": board["program"]["mode"], "layout": p.layout,
    }
    if board["program"]["mode"] == "flash_only":
        info["counter"] = (P.counter_get(p.unit)["count"], int(board["program"].get("limit", 100)), p.unit)
    return info


def env_status() -> list[tuple[str, bool]]:
    usb = False
    if shutil.which("lsusb"):
        usb = "09fb" in subprocess.run(["lsusb"], capture_output=True, text=True).stdout
    return [("Quartus", bool(C.find_tool("quartus_sh"))), ("iverilog", bool(C.find_tool("iverilog"))),
            ("GTKWave", bool(C.find_tool("gtkwave"))), ("yosys", bool(C.find_tool("yosys"))), ("USB-Blaster", usb)]


def parse_pin_line(line: str) -> tuple:
    """Interpreta lo que se escribe en el editor de pines. Devuelve (acción, …) o ('error', mensaje)."""
    parts = line.split()
    if not parts:
        return ("empty",)
    head = parts[0].lower()
    if head in ("salir", "exit", "q", "quit"):
        return ("exit",)
    if head.startswith("?") or head in ("ayuda", "help", "señales", "senales"):
        rest = " ".join(parts[1:]) if head in ("ayuda", "help", "señales", "senales") else (head[1:] + " ".join(parts[1:]))
        return ("list", rest.strip())
    if head in ("quitar", "rm", "borrar"):
        return ("remove", parts[1]) if len(parts) == 2 else ("error", "Uso: quitar <puerto>")
    if len(parts) < 2:
        return ("error", "Uso: <puerto> <señal>   (bus: led[3:0] led3 led2 led1 led0)   ·   ? para ver señales")
    return ("assign", parts[0], parts[1:])


def board_signals(board: dict, needle: str = "") -> list[str]:
    needle = needle.lower()
    return [f"{k} PIN_{v}" for k, v in board["pins"].items() if needle in k.lower()]


def run_pin(project_dir: Path, action: tuple) -> str:
    """Ejecuta una asignación o baja con el mismo código que `fpga pin`, capturando sus mensajes."""
    if action[0] == "assign":
        ns = Namespace(C=str(project_dir), port=action[1], signals=action[2], remove=None)
    else:
        ns = Namespace(C=str(project_dir), port=None, signals=[], remove=action[1])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            S.cmd_pin(ns)
        except SystemExit:
            pass
    return strip_ansi(buf.getvalue()).strip()


def resolve_project_choice(answer: str, recent: list[str]) -> tuple[Path | None, str | None]:
    """Número de la lista, nombre de un reciente, ruta (absoluta, con ~ o relativa) o nombre dentro de ~/Repositorios."""
    answer = answer.strip()
    if answer.isdigit():
        n = int(answer)
        if not 0 < n <= len(recent):
            return None, f"No hay un proyecto número {n} en la lista."
        return Path(recent[n - 1]), None
    candidates = [Path(r) for r in recent if Path(r).name == answer]
    candidates += [Path(answer).expanduser(), Path.home() / "Repositorios" / answer]
    for path in candidates:
        if (path / "fpga.toml").exists():
            return path.resolve(), None
    return None, f"«{answer}» no es un proyecto de fpga-cli (no tiene fpga.toml). Escribe su ruta, por ejemplo ~/Repositorios/{answer}"


def editable_files(p: C.Project) -> list[str]:
    """Archivos que se pueden abrir en nano: fuentes, testbenches y fpga.toml (solo los que existen), sin repetir."""
    out: list[str] = []
    for rel in [*p.resolved_sources(), *p.testbenches, "fpga.toml"]:
        if rel not in out and (p.dir / rel).is_file():
            out.append(rel)
    return out


def resolve_file_choice(answer: str, files: list[str]) -> tuple[str | None, str | None]:
    """Número de la lista o ruta escrita → (archivo, None) o (None, mensaje de error)."""
    answer = answer.strip()
    if answer.isdigit():
        n = int(answer)
        return (files[n - 1], None) if 0 < n <= len(files) else (None, f"No hay un archivo número {n} en la lista.")
    if answer in files:
        return answer, None
    return None, f"«{answer}» no está en la lista: elige un número o escribe la ruta tal cual aparece."


def pretty_log_name(path: Path) -> str:
    """20261006-153012-compilar.log → «2026-10-06 15:30:12 · compilar»."""
    m = re.match(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})-(.*)\.log$", path.name)
    return f"{m[1]}-{m[2]}-{m[3]} {m[4]}:{m[5]}:{m[6]} · {m[7]}" if m else path.name


def log_display(path: Path, project: Path | None) -> str:
    """Ruta corta del registro: relativa al proyecto si está dentro de él; si no, con ~."""
    if project:
        try:
            return Path(path).relative_to(project).as_posix()
        except ValueError:
            pass
    return str(path).replace(str(Path.home()), "~")


def read_log_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [f"✗ No se pudo leer {path}: {exc}"]


# ---------------------------------------------------------------- pantalla
class App:
    def __init__(self, stdscr, project: Path | None):
        self.s = stdscr
        self.project = project
        self.msg, self.msg_bad = "", False
        self.col, self.row = 0, 0
        self.env = env_status()
        self.utf = utf8_ok()
        self.cols = {0: [], 1: [], 2: []}
        for c, title, items in MENU:
            self.cols[c].append(("h", title))
            self.cols[c] += [("i", *it) for it in items]
        self.sel = {c: [i for i, e in enumerate(v) if e[0] == "i"] for c, v in self.cols.items()}
        # una fila en blanco entre grupos solo si la columna cabe en las 9 filas disponibles (80x24)
        self.gap = {c: 1 if len(v) + sum(1 for e in v if e[0] == "h") - 1 <= 9 else 0 for c, v in self.cols.items()}
        self.curs = {0: 0, 1: 0, 2: 0}
        self.focus(suggest_action(project_info(project)))
        curses.curs_set(0)
        stdscr.keypad(True)
        stdscr.idlok(False)                 # sin desplazar líneas por hardware: el repintado es igual en cualquier terminal
        stdscr.scrollok(False)
        curses.use_default_colors()
        for n, fg in ((1, curses.COLOR_CYAN), (2, curses.COLOR_YELLOW), (3, curses.COLOR_GREEN), (4, curses.COLOR_RED), (5, curses.COLOR_WHITE)):
            curses.init_pair(n, fg, -1)

    def focus(self, action: str):
        for c in self.cols:
            for pos, idx in enumerate(self.sel[c]):
                if self.cols[c][idx][3] == action:
                    self.col, self.curs[c] = c, pos

    # -- utilidades de dibujo
    def put(self, y, x, text, attr=0):
        try:
            self.s.addstr(y, x, text[: max(0, self.s.getmaxyx()[1] - x - 1)], attr)
        except curses.error:
            pass

    def rule(self, y, w, label=""):
        ch = "─" if self.utf else "-"
        line = ch * (w - 2)
        if label:
            line = f"{ch} {label} " + ch * max(0, w - 6 - len(label))
        self.put(y, 1, line[: w - 2], curses.color_pair(5) | curses.A_DIM)

    def chip(self, ok):
        return ("✓" if self.utf else "+") if ok else ("✗" if self.utf else "x")

    # -- dibujo principal
    def draw(self):
        s = self.s
        s.erase()
        h, w = s.getmaxyx()
        if h < MIN_ROWS or w < MIN_COLS:
            self.put(1, 2, f"Agranda la terminal (mínimo {MIN_COLS}x{MIN_ROWS}; ahora {w}x{h}).", curses.color_pair(2) | curses.A_BOLD)
            self.put(3, 2, "Con X sales del menú.")
            s.refresh()
            return
        cyan, amber = curses.color_pair(1) | curses.A_BOLD, curses.color_pair(2) | curses.A_BOLD
        dim, ok, bad = curses.color_pair(5) | curses.A_DIM, curses.color_pair(3) | curses.A_BOLD, curses.color_pair(4) | curses.A_BOLD
        for i, line in enumerate(MINI_UTF if self.utf else MINI_ASCII):          # el gnomo de Pocket Labs en pequeño
            self.put(i, 3, line, cyan)
        self.put(0, 11, "fpga-cli", cyan)
        self.put(0, 20, f"v{__version__}", dim)
        self.put(0, 28, "· Pocket Labs", dim)
        self.put(1, 11, "Quartus sin ventanas: simula, compila y programa")
        self.put(2, 11, "MAX V · Cyclone IV", amber)
        self.rule(3, w, "PROYECTO")
        info = project_info(self.project)
        if info:
            self.put(4, 3, "Proyecto", dim); self.put(4, 13, info["name"], curses.A_BOLD)
            self.put(4, 42, f"{self.chip(info['simulated'])} simulado", ok if info["simulated"] else bad)
            self.put(4, 56, f"{self.chip(info['built'])} compilado", ok if info["built"] else bad)
            self.put(5, 3, "Tarjeta", dim); self.put(5, 13, f"{info['board']} · {info['device']}")
            if info["layout"] == "flat":
                self.put(5, 13 + len(f"{info['board']} · {info['device']}") + 3, "▸ carpetas sin ordenar: R" if self.utf else "> carpetas sin ordenar: R", amber)
            path = info["dir"].replace(str(Path.home()), "~")
            self.put(6, 3, "Carpeta", dim); self.put(6, 13, path if len(path) <= w - 16 else "…" + path[-(w - 17):])
            self.put(7, 3, "Contiene", dim)
            line = f"{plural(info['sources'], 'fuente', 'fuentes')} · {plural(info['testbenches'], 'testbench', 'testbenches')} · {plural(info['pins'], 'pin', 'pines')}"
            self.put(7, 13, line)
            if info["counter"]:
                n, lim, unit = info["counter"]
                self.put(7, 13 + len(line) + 3, f"Flash: {n} de {lim} cargas ({unit})", bad if n >= int(lim * 0.8) else amber)
        else:
            self.put(4, 3, "Sin proyecto abierto.", amber)
            self.put(5, 3, "Crea uno con N, adopta tus .v con I, o abre uno existente con O.")
        self.put(8, 1, ("─ " if self.utf else "- ") + "ENTORNO", curses.color_pair(5) | curses.A_DIM)
        x = 14
        for name, ok_ in self.env:
            self.put(8, x, f"{self.chip(ok_)} {name}", curses.color_pair(3 if ok_ else 4) | curses.A_BOLD)
            x += len(name) + 4
        self.rule(9, w, "ACCIONES")
        for c in (0, 1, 2):
            y = MENU_TOP
            for idx, e in enumerate(self.cols[c]):
                if e[0] == "h":
                    y += self.gap[c] if idx else 0
                    self.put(y, COL_X[c], e[1], amber)
                else:
                    _, key, text, action, needs = e
                    selected = (c == self.col and idx == self.sel[c][self.curs[c]])
                    attr = curses.A_DIM if (needs and not info) else 0
                    if selected:
                        attr |= curses.A_REVERSE | curses.A_BOLD
                    self.put(y, COL_X[c], f" {key.upper()} ", attr | (0 if selected else curses.color_pair(2) | curses.A_BOLD))
                    self.put(y, COL_X[c] + 4, f"{text:<20}", attr)
                y += 1
        cur = self.cols[self.col][self.sel[self.col][self.curs[self.col]]]
        self.rule(h - 5, w)
        for i, line in enumerate(textwrap.wrap(HELP.get(cur[3], ""), w - 8)[:2]):
            self.put(h - 4 + i, 3, line, curses.color_pair(5))
        if self.msg:
            self.put(h - 2, 3, self.msg, (curses.color_pair(4) if self.msg_bad else curses.color_pair(3)) | curses.A_BOLD)
        arrows = "↑↓←→" if self.utf else "flechas"
        self.put(h - 1, 3, f"{arrows} mover · Enter ejecutar · letra = acceso directo · X salir", dim)
        s.refresh()

    # -- ventana de salida (comando en marcha o registro guardado)
    def line_attr(self, line: str) -> int:
        if line.startswith("✗") or line.startswith("Error") or "Traceback" in line:
            return curses.color_pair(4)
        if line.startswith("✓"):
            return curses.color_pair(3)
        if line.startswith("!"):
            return curses.color_pair(2)
        if line.startswith("$ ") or line.startswith("#"):
            return curses.color_pair(5) | curses.A_DIM
        return 0

    def output_window(self, title: str, job: "R.Job | None" = None, lines: list[str] | None = None):
        """Muestra la salida de un comando que corre (job) o un texto guardado (lines). Devuelve cuando la persona sale."""
        buf = R.OutputBuffer()
        if lines is not None:
            for ln in lines:
                buf.lines.append(ln)
        out = job.buf if job else buf
        curses.flushinp()                      # nada tecleado antes de abrir la ventana puede llegar a una pregunta del comando
        curses.curs_set(1 if job else 0)
        got_sigint = []
        old_handler = signal.signal(signal.SIGINT, lambda *_: got_sigint.append(1))   # Ctrl-C nunca revienta el dibujo: se atiende en el bucle
        try:
            self._window_loop(title, job, out, got_sigint)
        finally:
            signal.signal(signal.SIGINT, old_handler)
            curses.curs_set(0)

    def _window_loop(self, title, job, out, got_sigint):
        s = self.s
        text, pos, back, notice = "", 0, 0, ""
        while True:
            h, w = s.getmaxyx()
            if job:
                job.poll()
            if True:
                s.erase()
                width = max(10, w - 4)
                oh = max(3, h - 6)
                view = out.view(width)
                back = max(0, min(back, max(0, len(view) - oh)))
                end = len(view) - back
                shown = view[max(0, end - oh):end]
                cyan, amber = curses.color_pair(1) | curses.A_BOLD, curses.color_pair(2) | curses.A_BOLD
                dim = curses.color_pair(5) | curses.A_DIM
                self.put(0, 2, ("▎ " if self.utf else "| ") + title, cyan)
                if job is None:
                    status, sattr = "registro guardado", dim
                elif not job.done:
                    status, sattr = f"{'●' if self.utf else '*'} en curso · {int(job.elapsed())} s", amber
                elif job.interrupted:
                    status, sattr = f"{'✗' if self.utf else 'x'} interrumpido · {job.elapsed():.1f} s", curses.color_pair(4) | curses.A_BOLD
                elif job.rc:
                    status, sattr = f"{'✗' if self.utf else 'x'} error (código {job.rc}) · {job.elapsed():.1f} s", curses.color_pair(4) | curses.A_BOLD
                else:
                    status, sattr = f"{self.chip(True)} listo · {job.elapsed():.1f} s", curses.color_pair(3) | curses.A_BOLD
                self.put(0, max(2, w - len(status) - 3), status, sattr)
                self.rule(1, w)
                for i, line in enumerate(shown):
                    self.put(2 + i, 2, line, self.line_attr(line))
                if not view:
                    self.put(2, 2, "(sin salida todavía)", dim)
                label = f"↑ {back} líneas más arriba" if back and self.utf else (f"^ {back} lineas mas arriba" if back else "")
                self.rule(h - 4, w, label)
                if job and not job.done:
                    prompt = "responder ▸ " if self.utf else "responder > "
                    self.put(h - 3, 2, prompt, amber)
                    self.put(h - 3, 2 + len(prompt), text[-(w - len(prompt) - 4):] if len(text) > w - len(prompt) - 4 else text)
                    hint = f"Enter envía lo escrito · Ctrl-C interrumpe · {'↑↓' if self.utf else 'flechas'} PgUp PgDn desplazan"
                    if "prog" in job.argv and not notice:
                        self.put(h - 1, 2, "Ojo: interrumpir en plena grabación puede dejar la flash a medias.", amber)
                elif job:
                    self.put(h - 3, 2, notice or "Terminó. Enter o Esc vuelven al menú.", curses.color_pair(3) if not job.rc else curses.color_pair(4))
                    hint = f"Registro: {log_display(job.log.path, job.project)}" if job.log else ""
                else:
                    self.put(h - 3, 2, "Esc o Enter vuelven al menú.", dim)
                    hint = f"{'↑↓' if self.utf else 'flechas'} PgUp PgDn Inicio Fin desplazan"
                if notice and job and not job.done:
                    self.put(h - 2, 2, notice, amber)
                else:
                    self.put(h - 2, 2, hint[: w - 4], dim)
                if job and not job.done:
                    s.move(h - 3, min(2 + len("responder ▸ ") + pos, w - 2))
                s.refresh()
            s.timeout(80)
            try:
                ch = s.get_wch()
            except curses.error:
                ch = None
            finally:
                s.timeout(-1)
            if got_sigint:
                got_sigint.clear()
                if job and not job.done:
                    job.interrupt()
                    notice = "Ctrl-C enviado: esperando a que el comando termine…"
                    continue
                break
            if ch is None:
                continue
            notice = ""
            if ch == "\x03":                    # Ctrl-C llegado como tecla (según cómo esté configurada la terminal)
                if job and not job.done:
                    job.interrupt()
                    notice = "Ctrl-C enviado: esperando a que el comando termine…"
                    continue
                break
            if ch == curses.KEY_RESIZE:
                if job:
                    job.resize(h - 6, w - 4)
                continue
            if ch == curses.KEY_PPAGE:
                back += oh - 1
            elif ch == curses.KEY_NPAGE:
                back = max(0, back - (oh - 1))
            elif ch == curses.KEY_UP:
                back += 1
            elif ch == curses.KEY_DOWN:
                back = max(0, back - 1)
            elif ch == curses.KEY_HOME and (job is None or job.done or not text):
                back = 10 ** 9
            elif ch == curses.KEY_END and (job is None or job.done or not text):
                back = 0
            elif job is None or job.done:
                if ch == "\x1b":
                    if drain_escape(s):
                        continue
                    break
                if ch in ("\n", "\r", "q", "x") or ch == curses.KEY_ENTER:
                    break
            else:
                if ch == "\x1b":
                    if not drain_escape(s):
                        notice = "El comando sigue en marcha: espera a que termine o interrúmpelo con Ctrl-C."
                    continue
                text, pos, ev = R.edit_line(text, pos, ch)
                if ev == "enter":
                    job.send_line(text)
                    text, pos, back = "", 0, 0

    def run_job(self, label: str, argv: list[str], scoped: bool = True) -> int:
        """Corre `fpga <argv>` dentro de la ventana del menú y deja el estado listo para el siguiente paso."""
        h, w = self.s.getmaxyx()
        project = self.project if scoped and self.project else None
        job = R.Job(argv, label, project, rows=h - 6, cols=w - 4)
        title = f"{label} · {project.name}" if project else label
        try:
            self.output_window(title, job=job)
        finally:
            if not job.done:                    # salió con el comando vivo (p. ej. se cerró la terminal): no se deja huérfano
                job.interrupt()
                time.sleep(0.2)
                job.poll()
                if not job.done:
                    job.kill()
                    job.poll()
        self.s.clear()
        self.env = env_status()
        rc = job.rc or 0
        self.msg_bad = bool(rc)
        nxt = suggest_action(project_info(self.project))
        self.focus(nxt)
        if job.interrupted:
            self.msg = f"{label}: interrumpido."
        else:
            self.msg = (f"{label}: terminó con error (código {rc})." if rc else f"{label}: listo.") + f"  Siguiente paso sugerido: {ACTION_LABEL[nxt]}."
        return rc

    # -- cuadro de entrada (se escribe; Esc cancela)
    def ask(self, title: str, label: str, default: str = "", lines: list[str] | None = None) -> str | None:
        s = self.s
        text, pos = default, len(default)
        curses.curs_set(1)
        try:
            while True:
                h, w = s.getmaxyx()
                s.erase()
                amber, dim = curses.color_pair(2) | curses.A_BOLD, curses.color_pair(5) | curses.A_DIM
                self.put(1, 2, title, curses.color_pair(1) | curses.A_BOLD)
                self.rule(2, w)
                y = 4
                wrapped = [part for ln in (lines or []) for part in (textwrap.wrap(ln, w - 6) or [""])]
                for part in wrapped[-max(0, h - 11):]:
                    self.put(y, 3, part, self.line_attr(part) or curses.color_pair(5))
                    y += 1
                y = min(y + 1, h - 5)
                self.put(y, 3, label, amber)
                prompt = "▸ " if self.utf else "> "
                self.put(y + 1, 3, prompt, amber)
                self.put(y + 1, 3 + len(prompt), text)
                self.put(h - 2, 3, "Enter acepta · Esc cancela · Ctrl-U borra la línea", dim)
                s.move(y + 1, min(3 + len(prompt) + pos, w - 2))
                s.refresh()
                try:
                    ch = s.get_wch()
                except KeyboardInterrupt:
                    return None
                if ch == curses.KEY_RESIZE:
                    continue
                text, pos, ev = R.edit_line(text, pos, ch)
                if ev == "esc":
                    if drain_escape(s):
                        continue
                    return None
                if ev == "enter":
                    return text.strip()
        finally:
            curses.curs_set(0)

    def cancelled(self, what: str):
        self.msg, self.msg_bad = f"{what}: cancelado, no se tocó nada.", False

    # -- acciones
    def act(self, action: str) -> bool:
        needs = any(a == action and n for _, _, items in MENU for _, _, a, n in items)
        if needs and not project_info(self.project):
            self.msg, self.msg_bad = "Primero abre o crea un proyecto (N, I u O).", True
            return True
        if action == "exit":
            return False
        proj = ["-C", str(self.project)] if self.project else []
        if action == "pins":
            self.pin_editor()
        elif action == "new":
            self._new()
        elif action == "init":
            self._init()
        elif action == "open":
            self._open()
        elif action == "add":
            self._add()
        elif action == "newtb":
            self._newtb()
        elif action == "edit":
            self._edit()
        elif action == "logs":
            self._logs()
        elif action == "tidy":
            self.run_job("Ordenar carpetas", proj + ["tidy"])
        elif action == "convert":
            if project_info(self.project)["mode"] == "flash_only":
                self.msg, self.msg_bad = "Esto aplica solo a la Cyclone IV: la MAX V ya genera su .pof al compilar.", True
            else:
                self.run_job("Convertir para flash", proj + ["prog", "--target", "flash", "--convert-only"])
        elif action in ("sim", "wave", "build", "prog"):
            labels = {"sim": "Simular", "wave": "Ver ondas", "build": "Compilar", "prog": "Programar"}
            self.run_job(labels[action], proj + [action])
        elif action == "counter":
            self.run_job("Contador de cargas", ["counter"], scoped=False)
        elif action == "doctor":
            self.run_job("Diagnóstico", ["doctor"], scoped=False)
        elif action == "iq":
            self.run_job("Instalar Quartus", ["install-quartus"], scoped=False)
        elif action == "it":
            self.run_job("Instalar herramientas", ["install-tools"], scoped=False)
        return True

    def _adopt_last(self):
        last = C.config_get("last_project")
        if last and (Path(last) / "fpga.toml").exists():
            self.project = Path(last)

    def _new(self):
        name = self.ask("Nuevo proyecto", "Nombre del proyecto (será el nombre de la carpeta):")
        if not name:
            return self.cancelled("Nuevo proyecto")
        self.run_job("Nuevo proyecto", ["new", name], scoped=False)
        self._adopt_last()

    def _init(self):
        path = self.ask("Iniciar con mis .v", "Carpeta con tus archivos .v:", default=str(Path.cwd()))
        if not path:
            return self.cancelled("Iniciar con mis .v")
        self.run_job("Iniciar con mis .v", ["-C", str(Path(path).expanduser()), "init"], scoped=False)
        self._adopt_last()

    def _open(self):
        recent = [p for p in C.config_get("recent", []) if (Path(p) / "fpga.toml").exists()]
        lines = [f"{i}) {p.replace(str(Path.home()), '~')}" for i, p in enumerate(recent, 1)] or ["(todavía no hay proyectos recientes)"]
        answer = self.ask("Abrir otro proyecto", "Número de la lista o ruta del proyecto:", lines=lines)
        if not answer:
            return self.cancelled("Abrir proyecto")
        path, err = resolve_project_choice(answer, recent)
        if err:
            self.msg, self.msg_bad = err, True
            return
        self.project = path
        C.remember_project(self.project)
        self.focus(suggest_action(project_info(self.project)))
        self.msg, self.msg_bad = f"Proyecto abierto: {self.project.name}.", False

    def collect_lines(self, title: str, prompt: str, hints: list[str], parse, shown) -> list[str] | None:
        """Pide líneas una por una (escribiendo). Cada línea se valida al momento: si está mal se explica y se vuelve a pedir
        la misma. Enter vacío termina; «borrar» quita la última; Esc cancela todo (devuelve None)."""
        raw, ok, err, default = [], [], "", ""
        while True:
            lines = list(hints)
            if ok:
                lines += ["", "Hasta ahora:"] + [f"  {i}) {t}" for i, t in enumerate(ok, 1)][-5:]
            if err:
                lines += ["", f"✗ {err}"]
            ans = self.ask(title, f"{prompt} {len(ok) + 1} (Enter vacío = terminar, «borrar» = quitar la última):", default=default, lines=lines)
            if ans is None:
                return None
            if not ans:
                return raw
            if ans.lower() in ("borrar", "deshacer"):
                if raw:
                    raw.pop(); ok.pop()
                err, default = "", ""
                continue
            try:
                ok.append(shown(parse(ans)))
            except ValueError as exc:
                err, default = str(exc), ans
                continue
            raw.append(ans); err, default = "", ""

    def _add(self):
        name = self.ask("Agregar módulo", "Nombre del módulo nuevo:")
        if not name:
            return self.cancelled("Agregar módulo")
        if not S.IDENT_RE.match(name):
            self.msg, self.msg_bad = "El nombre del módulo debe ser un identificador de Verilog (letras, números y guion bajo).", True
            return
        ports = self.collect_lines(f"Agregar módulo · {name}", "Puerto",
                                   ["Escribe los puertos de uno en uno, con su dirección.", S.PORT_HELP,
                                    "Sin puertos, el módulo queda con solo clk y un comentario."],
                                   S.parse_port, S.format_port)
        if ports is None:
            return self.cancelled("Agregar módulo")
        argv = ["-C", str(self.project), "add", name]
        for t in ports:
            argv += ["--port", t]
        self.run_job("Agregar módulo", argv)

    def _newtb(self):
        info = project_info(self.project)
        name = self.ask("Nuevo testbench", f"Módulo a probar (vacío = {info['top']}):")
        if name is None:
            return self.cancelled("Nuevo testbench")
        name = name or info["top"]
        proj = C.load_project(self.project)
        extra = []
        if (proj.tb_dir / f"tb_{name}.v").exists():
            warn = [f"tb_{name}.v ya existe.", "Regenerarlo vuelve a conectar los puertos del módulo, pero BORRA lo que le hayas escrito."]
            if (self.ask("Nuevo testbench", "¿Regenerarlo? Escribe s para sí (cualquier otra cosa cancela):", lines=warn) or "").lower() not in ("s", "si", "sí", "y"):
                return self.cancelled("Nuevo testbench")
            extra = ["--force"]
        inputs = S.module_inputs(proj, name)
        stim: list[str] = []
        if inputs:
            hints = [f"Entradas del módulo: {', '.join(n for n in inputs if n != 'clk') or '(solo clk)'}"
                     + (" · clk la genera el testbench" if "clk" in inputs else ""), S.STIM_HELP,
                     "Cada estímulo espera su tiempo (en ns) después del anterior. Enter vacío al terminar."]
            got = self.collect_lines(f"Nuevo testbench · {name}", "Estímulo", hints, lambda t: S.parse_stimulus(t, inputs), S.format_stimulus)
            if got is None:
                return self.cancelled("Nuevo testbench")
            stim = got
        argv = ["-C", str(self.project), "new-tb", name, *extra]
        for t in stim:
            argv += ["--stim", t]
        self.run_job("Nuevo testbench", argv)

    def _edit(self):
        proj = C.load_project(self.project)
        files = editable_files(proj)
        lines = [f"{i}) {f}" for i, f in enumerate(files, 1)]
        answer = self.ask("Editar con nano", "Número del archivo (o su ruta tal como aparece):", default="1", lines=lines)
        if answer is None:
            return self.cancelled("Editar")
        rel, err = resolve_file_choice(answer, files)
        if err:
            self.msg, self.msg_bad = err, True
            return
        self.run_nano(rel)

    def run_nano(self, rel: str):
        """Cierra la pantalla del menú, abre nano sobre el archivo y vuelve a dibujar el menú cuando se cierra."""
        exe = shutil.which("nano")
        if not exe:
            self.msg, self.msg_bad = "No encuentro nano. Instálalo con: sudo apt install nano", True
            return
        path = self.project / rel
        before = path.stat().st_mtime_ns if path.exists() else None
        curses.def_prog_mode()
        curses.endwin()
        try:
            rc = subprocess.call([exe, str(path)], cwd=self.project)
        finally:
            curses.reset_prog_mode()
            self.s.clear()
            self.s.refresh()
        self.env = env_status()
        after = path.stat().st_mtime_ns if path.exists() else None
        info = project_info(self.project)
        if info is None:
            self.msg, self.msg_bad = f"{rel} quedó con un error y el proyecto ya no se puede leer: ábrelo otra vez con E y corrígelo.", True
            return
        nxt = suggest_action(info)
        self.focus(nxt)
        self.msg_bad = bool(rc)
        if rc:
            self.msg = f"nano terminó con código {rc}."
        elif after != before:
            self.msg = f"Guardaste cambios en {rel}.  Siguiente paso sugerido: {ACTION_LABEL[nxt]}."
        else:
            self.msg = f"Sin cambios en {rel}."

    def _logs(self):
        logs = R.list_logs(self.project)
        if not logs:
            self.msg, self.msg_bad = "Todavía no hay registros: se crean al ejecutar una acción.", False
            return
        lines = [f"{i}) {pretty_log_name(p)}" for i, p in enumerate(logs, 1)]
        answer = self.ask("Registros", "Número del registro (Enter = el más reciente):", default="1", lines=lines)
        if answer is None:
            return self.cancelled("Registros")
        if not answer.isdigit() or not 0 < int(answer) <= len(logs):
            self.msg, self.msg_bad = "Elige un número de la lista.", True
            return
        path = logs[int(answer) - 1]
        self.output_window(pretty_log_name(path), lines=read_log_lines(path))
        self.s.clear()

    # -- editor de pines
    def pin_editor(self):
        history, hi, text = [], 0, ""
        out = ["Escribe una asignación y Enter. Para volver al menú: escribe salir (o pulsa Esc)."]
        curses.curs_set(1)
        EJEMPLOS = ["led led0                         un puerto a una señal de la tarjeta",
                    "led[3:0] led3 led2 led1 led0     un bus, del bit más al menos significativo",
                    "ext PIN_100                      un pin suelto",
                    "quitar led                       elimina una asignación",
                    "?led                             lista las señales de la tarjeta que contienen «led»",
                    "salir                            vuelve al menú (también Esc)"]
        while True:
            h, w = self.s.getmaxyx()
            self.s.erase()
            try:
                p = C.load_project(self.project)
                board = C.load_board(p.board_id)
            except SystemExit:
                break
            amber, dim = curses.color_pair(2) | curses.A_BOLD, curses.color_pair(5) | curses.A_DIM
            self.put(1, 2, f"Asignar pines · {p.name} · {board['name']}", curses.color_pair(1) | curses.A_BOLD)
            pins = list(p.pins.items())
            self.rule(2, w, f"ASIGNACIONES ACTUALES ({len(pins)})")
            # tabla en columnas; si hay muchas asignaciones se oculta el panel de ejemplos para que quepan todas
            colw = 32
            max_cols = max(1, (w - 4) // colw)
            with_examples = -(-len(pins) // max(3, h - 16)) <= max_cols and h >= 24
            avail = max(3, h - (16 if with_examples else 9))
            cols = min(max_cols, max(1, -(-len(pins) // avail)))
            rows = max(3, min(avail, -(-len(pins) // cols))) if pins else 3
            rev = {C.pin_name(v): k for k, v in board["pins"].items()}
            for c in range(cols):
                self.put(3, 3 + c * colw, f"{'PUERTO':<12}{'PIN':<9}SEÑAL", dim)
            for i, (port, pin) in enumerate(pins[: rows * cols]):
                c, r = divmod(i, rows)
                self.put(4 + r, 3 + c * colw, f"{port:<12}{pin:<9}{rev.get(pin, '')}")
            if not pins:
                self.put(4, 3, "(ninguna todavía)", curses.A_DIM)
            if len(pins) > rows * cols:
                self.put(3, w - 22, f"… y {len(pins) - rows * cols} más", amber)
            y = 4 + rows
            if with_examples:
                self.rule(y, w, "EJEMPLOS")
                for i, line in enumerate(EJEMPLOS):
                    self.put(y + 1 + i, 3, line, curses.color_pair(5))
                y += 1 + len(EJEMPLOS)
            self.rule(y, w, "RESULTADO")
            room = h - 3 - y
            for i, line in enumerate(out[-room:] if room > 0 else []):
                bad = line.startswith("✗") or line.startswith("Uso")
                self.put(y + 1 + i, 3, line, curses.color_pair(4) if bad else curses.color_pair(3) if line.startswith("✓") else 0)
            self.put(h - 2, 2, "pin ▸ " if self.utf else "pin > ", amber)
            self.put(h - 2, 8, text)
            self.put(h - 1, 3, "Enter ejecuta · salir o Esc: volver al menú · ? lista señales · " + ("↑↓" if self.utf else "flechas") + " historial", dim)
            self.s.move(h - 2, min(8 + len(text), w - 2))
            self.s.refresh()
            try:
                ch = self.s.get_wch()
            except KeyboardInterrupt:
                break
            if ch == "\x1b":
                if drain_escape(self.s):
                    continue                    # era una flecha mal codificada, no un Esc
                break
            if ch in ("\n", "\r") or ch == curses.KEY_ENTER:
                action = parse_pin_line(text)
                if text.strip():
                    history.append(text)
                    hi = len(history)
                text = ""
                if action[0] == "exit":
                    break
                if action[0] == "empty":
                    continue
                if action[0] == "error":
                    out = [action[1]]
                elif action[0] == "list":
                    sigs = board_signals(board, action[1])
                    ncol = max(1, (w - 6) // 24)
                    lines = max(1, room - 1)
                    out = [f"{len(sigs)} señales" + (f" con «{action[1]}»" if action[1] else "") + ":"] + [
                        "".join(f"{x:<24}" for x in sigs[i:i + ncol]).rstrip() for i in range(0, min(len(sigs), ncol * lines), ncol)]
                    if len(sigs) > ncol * lines:
                        out.append(f"… y {len(sigs) - ncol * lines} más: afina el filtro, por ejemplo ?led")
                else:
                    out = run_pin(self.project, action).splitlines() or ["(sin salida)"]
                continue
            if ch in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                text = text[:-1]
            elif ch == curses.KEY_UP and history:
                hi = max(0, hi - 1); text = history[hi]
            elif ch == curses.KEY_DOWN and history:
                hi = min(len(history), hi + 1); text = history[hi] if hi < len(history) else ""
            elif ch == "\x15":
                text = ""
            elif isinstance(ch, str) and ch.isprintable():
                text += ch
        curses.curs_set(0)

    # -- bucle
    def splash(self):
        """Pantalla de inicio con el logo de Pocket Labs: dura un instante y cualquier tecla la salta.
        Se apaga con FPGA_NO_SPLASH=1. El dibujo se elige según el alto de la terminal."""
        if os.environ.get("FPGA_NO_SPLASH"):
            return
        s = self.s
        h, w = s.getmaxyx()
        arts = L.ART_UTF if self.utf else L.ART_ASCII
        sizes = [n for n in arts if n <= h - 6]
        if h < MIN_ROWS or w < MIN_COLS or not sizes:
            return
        art = arts[max(sizes)]
        top = max(0, (h - len(art) - 4) // 2)
        aw = max(len(line) for line in art)
        cyan, dim = curses.color_pair(1) | curses.A_BOLD, curses.color_pair(5) | curses.A_DIM
        s.erase()
        for i, line in enumerate(art):
            self.put(top + i, (w - aw) // 2, line, cyan)
        name = "P O C K E T   L A B S"
        self.put(top + len(art) + 1, (w - len(name)) // 2, name, curses.A_BOLD)
        tag = f"fpga-cli v{__version__}"
        self.put(top + len(art) + 2, (w - len(tag)) // 2, tag, dim)
        s.refresh()
        s.timeout(1400)
        try:
            s.get_wch()
        except (curses.error, KeyboardInterrupt):
            pass
        finally:
            s.timeout(-1)
        curses.flushinp()

    def loop(self):
        self.splash()
        while True:
            self.draw()
            try:
                ch = self.s.get_wch()
            except KeyboardInterrupt:
                return
            h, w = self.s.getmaxyx()
            if h < MIN_ROWS or w < MIN_COLS:
                if isinstance(ch, str) and ch.lower() == "x":
                    return
                continue
            self.msg = ""
            if ch == curses.KEY_RESIZE:
                continue
            if ch == "\x1b":
                drain_escape(self.s)            # ESC y lo que traiga pegado nunca son atajos
                continue
            if ch == curses.KEY_UP:
                self.curs[self.col] = (self.curs[self.col] - 1) % len(self.sel[self.col])
            elif ch == curses.KEY_DOWN:
                self.curs[self.col] = (self.curs[self.col] + 1) % len(self.sel[self.col])
            elif ch in (curses.KEY_LEFT, curses.KEY_RIGHT):
                self.col = max(0, self.col - 1) if ch == curses.KEY_LEFT else min(2, self.col + 1)
                self.curs[self.col] = min(self.curs[self.col], len(self.sel[self.col]) - 1)
            elif ch in ("\n", "\r") or ch == curses.KEY_ENTER:
                entry = self.cols[self.col][self.sel[self.col][self.curs[self.col]]]
                if not self.act(entry[3]):
                    return
            elif isinstance(ch, str):
                for c in (0, 1, 2):
                    for pos, idx in enumerate(self.sel[c]):
                        if self.cols[c][idx][1] == ch.lower():
                            self.col, self.curs[c] = c, pos
                            if not self.act(self.cols[c][idx][3]):
                                return


def run(project: str | None = None) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("El menú necesita una terminal interactiva. Usa los comandos directamente: fpga --help")
        return 1
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass                                    # idioma mal configurado: se sigue con el predeterminado (símbolos en ASCII)
    os.environ.setdefault("ESCDELAY", "25")
    start = Path(project).expanduser().resolve() if project else None
    if not start and (Path.cwd() / "fpga.toml").exists():     # dentro de la carpeta de un proyecto: ese es el que se abre
        start = Path.cwd()
    if not start:
        last = C.config_get("last_project")
        start = Path(last) if last and (Path(last) / "fpga.toml").exists() else None
    if start:                                       # que aparezca en la lista de «Abrir otro proyecto» (sin cambiar cuál es el último usado)
        C.config_set("recent", [str(start)] + [p for p in C.config_get("recent", []) if p != str(start)][:7])
    curses.wrapper(lambda scr: App(scr, start).loop())
    return 0

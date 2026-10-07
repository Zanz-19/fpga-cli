"""Ejecuta un comando de fpga dentro de una terminal virtual (pty) para mostrarlo dentro del menú.

Sin curses: aquí solo hay lógica que se puede probar sin pantalla.
- `OutputBuffer` convierte los bytes de la terminal en líneas (maneja \\r, borrados y secuencias ANSI).
- `Job` lanza el comando, entrega su salida y recibe lo que la persona escribe (así siguen funcionando
  las confirmaciones escritas, como GRABAR en la MAX V: la escribe la persona, nunca la herramienta).
- `LogFile` guarda cada ejecución en un archivo de texto.
- `edit_line` es el editor de una línea que usan los cuadros de entrada del menú.
"""
from __future__ import annotations

import codecs
import fcntl
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from datetime import datetime
from pathlib import Path

from . import core as C

ENTRY = Path(__file__).resolve().parent.parent / "fpga"
MAX_LINES = 20000
KEEP_LOGS = 60
CSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
ESC_OTHER = re.compile(r"\x1b[@-Z\\-_]")


# ---------------------------------------------------------------- salida de la terminal → líneas
class OutputBuffer:
    """Líneas ya terminadas (`lines`) más la que se está escribiendo (`partial`, p. ej. una pregunta sin salto de línea)."""

    def __init__(self, tabsize: int = 8):
        self.lines: list[str] = []
        self.cur: list[str] = []
        self.col = 0
        self.dropped = 0
        self.tabsize = tabsize
        self._dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending = ""            # secuencia de escape cortada entre dos lecturas

    @property
    def partial(self) -> str:
        return "".join(self.cur).rstrip()

    def _newline(self):
        self.lines.append("".join(self.cur).rstrip())
        self.cur, self.col = [], 0
        if len(self.lines) > MAX_LINES:
            cut = len(self.lines) - MAX_LINES
            del self.lines[:cut]
            self.dropped += cut

    def _put(self, ch: str):
        if self.col < len(self.cur):
            self.cur[self.col] = ch
        else:
            self.cur.append(ch)
        self.col += 1

    def feed(self, data: bytes) -> list[str]:
        """Procesa bytes y devuelve las líneas que se terminaron con ellos."""
        before = len(self.lines) + self.dropped
        text = self._pending + self._dec.decode(data)
        self._pending = ""
        # una secuencia de escape incompleta al final se guarda para la próxima lectura
        m = re.search(r"\x1b(?:\[[0-9;?]*[ -/]*|\][^\x07\x1b]*)?$", text)
        if m:
            self._pending, text = text[m.start():], text[:m.start()]
        text = OSC.sub("", text)
        text = CSI.sub("", text)
        text = ESC_OTHER.sub("", text)
        for ch in text:
            if ch == "\n":
                self._newline()
            elif ch == "\r":
                self.col = 0
            elif ch == "\b":
                self.col = max(0, self.col - 1)
            elif ch == "\t":
                for _ in range(self.tabsize - self.col % self.tabsize):
                    self._put(" ")
            elif ch == "\x07" or (ch < " " and ch not in "\n\r\b\t"):
                continue
            else:
                self._put(ch)
        return self.lines[-(len(self.lines) + self.dropped - before):] if len(self.lines) + self.dropped > before else []

    def flush(self):
        """Al terminar el comando: lo que quedó sin salto de línea pasa a ser una línea más."""
        if self.cur and "".join(self.cur).strip():
            self._newline()
        self.cur, self.col = [], 0

    def view(self, width: int) -> list[str]:
        """Todas las líneas (incluida la parcial) cortadas al ancho de la ventana."""
        out: list[str] = []
        for line in self.lines + ([self.partial] if self.partial else []):
            if len(line) <= width:
                out.append(line)
            else:
                out += [line[i:i + width] for i in range(0, len(line), width)]
        return out


# ---------------------------------------------------------------- registro de cada ejecución
def log_dir(project: Path | None) -> Path:
    return (Path(project) / ".fpga" / "logs") if project else (C.CONFIG_DIR / "logs")


def safe_label(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower().replace("ó", "o").replace("é", "e").replace("í", "i")
                  .replace("á", "a").replace("ú", "u").replace("ñ", "n")).strip("-") or "accion"


class LogFile:
    def __init__(self, project: Path | None, label: str, argv: list[str]):
        self.dir = log_dir(project)
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = self.dir / f"{stamp}-{safe_label(label)}.log"
        n = 1
        while self.path.exists():
            n += 1
            self.path = self.dir / f"{stamp}-{safe_label(label)}-{n}.log"
        self.fh = open(self.path, "w", encoding="utf-8")
        self.started = time.time()
        self.fh.write(f"# {label}\n# {datetime.now().astimezone().isoformat(timespec='seconds')}\n# fpga {' '.join(argv)}\n\n")
        self.fh.flush()

    def write_lines(self, lines: list[str]):
        if lines:
            self.fh.write("\n".join(lines) + "\n")
            self.fh.flush()

    def close(self, rc: int | None, tail: str = ""):
        if tail:
            self.fh.write(tail + "\n")
        status = "interrumpido" if rc is None else f"código {rc}"
        self.fh.write(f"\n# terminó: {status} · {time.time() - self.started:.1f} s\n")
        self.fh.close()
        self.prune()

    def prune(self):
        files = sorted(self.dir.glob("*.log"), key=_age_key)
        for old in files[:-KEEP_LOGS]:
            try:
                old.unlink()
            except OSError:
                pass


def _age_key(path: Path):
    """Más viejo primero: por la hora en que se escribió por última vez (con nanosegundos) y, si empatan, por el nombre."""
    return (path.stat().st_mtime_ns, path.name)


def list_logs(project: Path | None, limit: int = 12) -> list[Path]:
    d = log_dir(project)
    return sorted(d.glob("*.log"), key=_age_key, reverse=True)[:limit] if d.is_dir() else []


# ---------------------------------------------------------------- el comando en una terminal virtual
class Job:
    """Un comando de fpga corriendo en un pty. Se usa con `poll()` repetido desde el bucle de la pantalla."""

    def __init__(self, argv: list[str], label: str, project: Path | None, rows: int = 24, cols: int = 80, log: bool = True):
        self.argv, self.label, self.project = list(argv), label, project
        self.buf = OutputBuffer()
        self.rc: int | None = None
        self.done = False
        self.interrupted = False
        self.started = time.time()
        self.ended: float | None = None
        self.log = LogFile(project, label, argv) if log else None
        env = dict(os.environ, NO_COLOR="1", PYTHONUNBUFFERED="1", TERM="dumb", FPGA_WAVE_DETACH="1")
        self.master, slave = pty.openpty()
        self.resize(rows, cols, fd=slave)
        self.proc = subprocess.Popen([sys.executable, str(ENTRY), *argv], stdin=slave, stdout=slave, stderr=slave, env=env,
                                     close_fds=True, start_new_session=True)
        os.close(slave)

    def resize(self, rows: int, cols: int, fd: int | None = None):
        try:
            fcntl.ioctl(self.master if fd is None else fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(rows, 1), max(cols, 1), 0, 0))
        except OSError:
            pass

    def elapsed(self) -> float:
        return (self.ended or time.time()) - self.started

    def poll(self) -> bool:
        """Lee lo disponible. Devuelve True si hubo salida nueva o el comando terminó."""
        changed = False
        while not self.done:
            try:
                ready, _, _ = select.select([self.master], [], [], 0)
            except (OSError, ValueError):
                ready = []
            if not ready:
                break
            try:
                data = os.read(self.master, 65536)
            except OSError:
                data = b""
            if not data:
                self._finish()
                return True
            finished = self.buf.feed(data)
            if self.log:
                self.log.write_lines(finished)
            changed = True
        if not self.done and self.proc.poll() is not None:
            # el proceso terminó: lo último que quede en el pty se lee una vez más y se cierra
            deadline = time.time() + 0.3
            while time.time() < deadline:
                try:
                    ready, _, _ = select.select([self.master], [], [], 0.05)
                    if not ready:
                        break
                    data = os.read(self.master, 65536)
                except OSError:
                    break
                if not data:
                    break
                finished = self.buf.feed(data)
                if self.log:
                    self.log.write_lines(finished)
            self._finish()
            return True
        return changed

    def _finish(self):
        if self.done:
            return
        try:
            self.rc = self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.kill()
            self.rc = self.proc.wait()
        if self.rc is not None and self.rc < 0:
            self.interrupted = True
            self.rc = 128 - self.rc
        tail = self.buf.partial
        self.buf.flush()
        self.ended = time.time()
        self.done = True
        if self.log:
            self.log.write_lines([tail] if tail else [])
            self.log.close(None if self.interrupted else self.rc)
        try:
            os.close(self.master)
        except OSError:
            pass

    def send_line(self, text: str):
        """Lo que la persona escribió; el pty lo muestra (eco) y el comando lo lee como si fuera la terminal."""
        if not self.done:
            try:
                os.write(self.master, (text + "\n").encode())
            except OSError:
                pass

    def interrupt(self):
        if not self.done:
            self.interrupted = True
            try:
                os.killpg(self.proc.pid, signal.SIGINT)
            except (ProcessLookupError, PermissionError):
                pass

    def kill(self):
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


# ---------------------------------------------------------------- editor de una línea (para los cuadros de entrada)
def edit_line(text: str, pos: int, key) -> tuple[str, int, str | None]:
    """Aplica una tecla. Devuelve (texto, posición, evento) con evento 'enter', 'esc' o None."""
    import curses
    if key in ("\n", "\r") or key == curses.KEY_ENTER:
        return text, pos, "enter"
    if key == "\x1b":
        return text, pos, "esc"
    if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
        return (text[:pos - 1] + text[pos:], pos - 1, None) if pos else (text, pos, None)
    if key == curses.KEY_DC:
        return text[:pos] + text[pos + 1:], pos, None
    if key == curses.KEY_LEFT:
        return text, max(0, pos - 1), None
    if key == curses.KEY_RIGHT:
        return text, min(len(text), pos + 1), None
    if key in (curses.KEY_HOME, "\x01"):
        return text, 0, None
    if key in (curses.KEY_END, "\x05"):
        return text, len(text), None
    if key == "\x15":                                   # Ctrl-U: borrar la línea
        return "", 0, None
    if key == "\x0b":                                   # Ctrl-K: borrar hasta el final
        return text[:pos], pos, None
    if isinstance(key, str) and key.isprintable():
        return text[:pos] + key + text[pos:], pos + 1, None
    return text, pos, None

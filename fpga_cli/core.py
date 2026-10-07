"""Utilidades compartidas: configuración, tarjetas, proyectos, ejecución de comandos."""
from __future__ import annotations

import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:          # Python < 3.11 (p. ej. Linux Mint 21)
    try:
        import tomli as tomllib      # pip install tomli
    except ModuleNotFoundError:
        tomllib = None

ROOT = Path(__file__).resolve().parent.parent
BOARDS_DIR = ROOT / "boards"
TEMPLATES_DIR = ROOT / "templates"
CONFIG_DIR = Path(os.environ.get("FPGA_CLI_HOME") or Path.home() / ".config" / "fpga-cli")
DATA_DIR = Path(os.environ.get("FPGA_CLI_DATA") or Path.home() / ".local" / "share" / "fpga-cli")

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def info(msg): print(_c("36", "›"), msg)
def ok(msg): print(_c("32", "✓"), msg)
def warn(msg): print(_c("33", "!"), msg)
def bold(text): return _c("1", text)


def die(msg: str, code: int = 1):
    print(_c("31", "✗"), msg, file=sys.stderr)
    sys.exit(code)


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        die("Se necesitaba una respuesta y no hay entrada interactiva. Pasa el dato como opción.")
    return answer or (default or "")


# ---------------------------------------------------------------- configuración
def _json_read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _json_write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def config_get(key: str, default=None):
    return _json_read(CONFIG_DIR / "config.json", {}).get(key, default)


def config_set(key: str, value) -> None:
    cfg = _json_read(CONFIG_DIR / "config.json", {})
    cfg[key] = value
    _json_write(CONFIG_DIR / "config.json", cfg)


def remember_project(path) -> None:
    """Guarda el proyecto como el último usado y en la lista de recientes (para el menú «Abrir»)."""
    path = str(path)
    recent = [p for p in config_get("recent", []) if p != path and Path(p, "fpga.toml").exists()]
    config_set("recent", [path] + recent[:7])
    config_set("last_project", path)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- tarjetas
def load_toml(path: Path) -> dict:
    if tomllib is None:
        die("Falta un lector de TOML: usa Python 3.11 o superior, o instala 'tomli' (pip install tomli).")
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        die(f"No existe el archivo {path}")
    except tomllib.TOMLDecodeError as exc:
        die(f"TOML inválido en {path}: {exc}")


def list_boards() -> list[dict]:
    return [load_toml(p) for p in sorted(BOARDS_DIR.glob("*.toml"))]


def load_board(board_id: str) -> dict:
    path = BOARDS_DIR / f"{board_id}.toml"
    if not path.exists():
        ids = ", ".join(p.stem for p in sorted(BOARDS_DIR.glob("*.toml")))
        die(f"Tarjeta desconocida: '{board_id}'. Disponibles: {ids}")
    return load_toml(path)


def pin_name(value) -> str:
    """Acepta 20, '20' o 'PIN_20' y devuelve 'PIN_20'."""
    text = str(value).strip().upper()
    return text if text.startswith("PIN_") else f"PIN_{text}"


# ---------------------------------------------------------------- proyecto
SRC_EXT = (".v", ".sv", ".vhd", ".vhdl")
SIM_EXT = (".v", ".sv")
QUARTUS_FILE_TYPE = {".v": "VERILOG_FILE", ".sv": "SYSTEMVERILOG_FILE", ".vhd": "VHDL_FILE", ".vhdl": "VHDL_FILE"}


@dataclass
class Project:
    dir: Path
    name: str
    top: str
    sources: list[str]
    board_id: str
    unit: str
    family: str
    device: str
    clock_mhz: float
    pins: dict[str, str] = field(default_factory=dict)
    cable: str = ""
    testbenches: list[str] = field(default_factory=lambda: ["tb.v"])
    layout: str = "flat"          # "dirs": src/ tb/ sim/ waves/ build/ · "flat": todo en la raíz (proyectos antiguos)

    def _sub(self, name: str) -> Path:
        return self.dir / name if self.layout == "dirs" else self.dir

    @property
    def src_dir(self) -> Path: return self._sub("src")
    @property
    def tb_dir(self) -> Path: return self._sub("tb")
    @property
    def sim_dir(self) -> Path: return self._sub("sim")
    @property
    def wave_dir(self) -> Path: return self._sub("waves")
    @property
    def build_dir(self) -> Path: return self._sub("build")

    @property
    def out_dir(self) -> Path:
        return self.build_dir / "output_files"

    def out_file(self, ext: str) -> Path:
        return self.out_dir / f"{self.name}.{ext}"

    @property
    def state_file(self) -> Path:
        return self.dir / ".fpga" / "state.json"

    def resolved_sources(self) -> list[str]:
        """Expande los comodines de `sources` (p. ej. "*.v", "src/**/*.v"); nunca incluye testbenches ni ocultos."""
        tbs, out = set(self.testbenches), []
        for pattern in self.sources:
            if any(ch in pattern for ch in "*?["):
                found = sorted(p.relative_to(self.dir) for p in self.dir.glob(pattern) if p.is_file())
                found = [str(p) for p in found if p.suffix in SRC_EXT and not any(x.startswith(".") for x in p.parts)]
            else:
                found = [pattern]
            out += [f for f in found if f not in tbs and f not in out]
        return out

    def sim_sources(self) -> list[str]:
        return [s for s in self.resolved_sources() if s.endswith(SIM_EXT)]

    def source_paths(self) -> list[Path]:
        return [self.dir / s for s in self.resolved_sources()] + [self.dir / "fpga.toml"]

    def newest_source_mtime(self) -> float:
        return max((p.stat().st_mtime for p in self.source_paths() if p.exists()), default=0.0)

    def is_simulated(self) -> bool:
        """¿Algún testbench pasó después del último cambio de las fuentes y de ese testbench? (no cuenta fpga.toml)"""
        srcs = [self.dir / s for s in self.resolved_sources()]
        src_m = max((p.stat().st_mtime for p in srcs if p.exists()), default=0.0)
        sims = state_read(self).get("sims", {})
        for tb in self.testbenches:
            done = sims.get(Path(tb).stem)
            tb_path = self.dir / tb
            if done and tb_path.exists() and done >= max(src_m, tb_path.stat().st_mtime):
                return True
        return False


def load_project(directory: Path) -> Project:
    data = load_toml(directory / "fpga.toml")
    try:
        proj, brd = data["project"], data["board"]
        return Project(
            dir=directory, name=proj["name"], top=proj.get("top", proj["name"]),
            sources=list(proj["sources"]), board_id=brd["id"],
            unit=brd.get("unit", brd["id"] + "-1"), family=brd["family"], device=brd["device"],
            clock_mhz=float(brd.get("clock_mhz", 50)),
            pins={k: pin_name(v) for k, v in data.get("pins", {}).items()},
            cable=data.get("programmer", {}).get("cable", ""),
            testbenches=list(proj.get("testbenches", ["tb.v"])),
            layout=proj.get("layout", "flat") if proj.get("layout", "flat") in ("flat", "dirs") else "flat",
        )
    except KeyError as exc:
        die(f"Falta la clave {exc} en {directory / 'fpga.toml'}")


def resolve_project_dir(explicit: str | None) -> Path:
    """-C RUTA, o la carpeta actual si tiene fpga.toml, o la pregunta (si hay terminal)."""
    if explicit:
        path = Path(explicit).expanduser().resolve()
    elif (Path.cwd() / "fpga.toml").exists():
        path = Path.cwd()
    elif sys.stdin.isatty():
        warn("Esta carpeta no es un trabajo de fpga-cli (no tiene fpga.toml).")
        path = Path(ask("Ruta de la carpeta del trabajo", config_get("last_project"))).expanduser().resolve()
    else:
        die("No hay fpga.toml aquí. Entra a la carpeta del trabajo o usa: fpga -C RUTA <comando>")
    if not (path / "fpga.toml").exists():
        die(f"{path} no contiene fpga.toml.")
    remember_project(path)
    return path


def quartus_device(p: Project, board: dict) -> str:
    """Quartus no acepta el código de pedido completo (p. ej. la N final de 5M240ZT144C5N).
    Si el proyecto trae el código de pedido de la tarjeta, se traduce al nombre de Quartus."""
    if board.get("quartus_device") and p.device == board.get("device"):
        return board["quartus_device"]
    return p.device


def make_tcl(p: Project, board: dict) -> str:
    lines = [
        "# Generado por fpga-cli a partir de fpga.toml. Para cambiarlo, edita fpga.toml y corre `fpga project`.",
        f"project_new {p.name} -overwrite",
        f'set_global_assignment -name FAMILY "{p.family}"',
        f"set_global_assignment -name DEVICE {quartus_device(p, board)}",
        f"set_global_assignment -name TOP_LEVEL_ENTITY {p.top}",
    ]
    srcs = p.resolved_sources()
    if not srcs:
        die("El proyecto no tiene archivos fuente (revisa `sources` en fpga.toml).")
    # el .tcl se ejecuta dentro de build/: las fuentes se citan relativas a esa carpeta (../src/x.v)
    rel = lambda s: Path(os.path.relpath(p.dir / s, p.build_dir)).as_posix()
    lines += [f"set_global_assignment -name {QUARTUS_FILE_TYPE.get(Path(s).suffix, 'VERILOG_FILE')} {rel(s)}" for s in srcs]
    lines += [
        f"set_global_assignment -name SDC_FILE {p.name}.sdc",
        "set_global_assignment -name PROJECT_OUTPUT_DIRECTORY output_files",
        'set_global_assignment -name NUM_PARALLEL_PROCESSORS "ALL"',
    ]
    if board.get("io_standard"):
        lines.append(f'set_global_assignment -name STRATIX_DEVICE_IO_STANDARD "{board["io_standard"]}"')
    for name, value in board.get("assignments", []):
        lines.append(f'set_global_assignment -name {name} "{value}"')
    # En Tcl los corchetes ejecutan un comando: `seg[7]` fallaría con «invalid command name "7"». Las llaves lo evitan.
    lines += [f"set_location_assignment {pin} -to {{{port}}}" if re.search(r"[\[\]$\s]", port) else f"set_location_assignment {pin} -to {port}"
              for port, pin in p.pins.items()]
    lines.append("project_close")
    return "\n".join(lines) + "\n"


def make_sdc(p: Project, board: dict) -> str:
    period = 1000.0 / p.clock_mhz
    lines = [f"create_clock -name clk -period {period:.3f} [get_ports clk]"]
    lines += list(board.get("sdc_extra", []))
    return "\n".join(lines) + "\n"


def write_generated(p: Project, board: dict) -> None:
    p.build_dir.mkdir(parents=True, exist_ok=True)
    (p.build_dir / f"{p.name}.tcl").write_text(make_tcl(p, board), encoding="utf-8")
    (p.build_dir / f"{p.name}.sdc").write_text(make_sdc(p, board), encoding="utf-8")


def state_read(p: Project) -> dict:
    return _json_read(p.state_file, {})


def state_update(p: Project, **kv) -> None:
    data = state_read(p)
    data.update(kv)
    _json_write(p.state_file, data)


# ---------------------------------------------------------------- herramientas externas
def tools_dir() -> Path:
    return Path(os.environ.get("FPGA_CLI_TOOLS") or ROOT / "tools")


def _free_gb(path: Path) -> float:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free / 1e9


def find_tool(name: str) -> str | None:
    local = tools_dir() / "oss-cad-suite" / "bin" / name      # las instaladas con `fpga install-tools` van primero
    if local.exists():
        return str(local)
    found = shutil.which(name)
    if found:
        return found
    candidates = [d for d in (os.environ.get("QUARTUS_BIN"), config_get("quartus_bin")) if d]
    for pattern in ("~/intelFPGA_lite/*/quartus/bin", "~/altera_lite/*/quartus/bin", "~/intelFPGA/*/quartus/bin"):
        candidates += sorted(glob.glob(os.path.expanduser(pattern)), reverse=True)
    for directory in candidates:
        cand = Path(directory) / name
        if cand.exists():
            return str(cand)
    return None


def run(cmd: list[str], cwd: Path, dry: bool = False) -> int:
    print(_c("2", "$ " + " ".join(shlex.quote(c) for c in cmd)))
    if dry:
        print(_c("2", "  (dry-run: no se ejecuta)"))
        return 0
    exe = find_tool(cmd[0])
    if exe is None:
        hint = " Instala Quartus Prime Lite 25.1 y revisa el PATH (o QUARTUS_BIN)." if cmd[0].startswith("quartus") else ""
        die(f"No encuentro '{cmd[0]}'.{hint} Prueba: fpga doctor")
    return subprocess.call([exe] + cmd[1:], cwd=cwd)


def program_cmd(p: Project, args: list[str]) -> list[str]:
    """quartus_pgm + opciones; agrega el cable si el proyecto lo fija."""
    cmd = ["quartus_pgm"] + args
    if p.cable:
        cmd += ["-c", p.cable]
    return cmd

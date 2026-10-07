"""Pruebas de fpga-cli. Quartus se sustituye por scripts falsos (tests/fakebin) que registran las llamadas.
Ejecutar:  python3 -m unittest discover -s tests -v   (requiere iverilog para la prueba de simulación)"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FPGA = str(ROOT / "fpga")
FAKEBIN = str(ROOT / "tests" / "fakebin")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = dict(os.environ, FPGA_CLI_HOME=str(self.tmp / "cfg"), FPGA_CLI_DATA=str(self.tmp / "data"),
                        FAKE_LOG=str(self.tmp / "calls.log"), PATH=FAKEBIN + os.pathsep + os.environ["PATH"],
                        NO_COLOR="1", FPGA_CLI_TOOLS=str(self.tmp / "tools"))
        self.env.pop("FAKE_PGM_FAIL", None)
        self.repos = self.tmp / "repos"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fpga(self, *args, input="", env=None, cwd=None):
        e = dict(self.env, **(env or {}))
        return subprocess.run([sys.executable, FPGA, *args], input=input, capture_output=True, text=True, env=e, cwd=cwd)

    def calls(self):
        p = self.tmp / "calls.log"
        return p.read_text().splitlines() if p.exists() else []

    def counters(self):
        p = self.tmp / "data" / "counters.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def new(self, name, board):
        r = self.fpga("new", name, "--board", board, "--dir", str(self.repos))
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.repos / name

    def built(self, name, board):
        d = self.new(name, board)
        r = self.fpga("-C", str(d), "build")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        return d


class TestNew(Base):
    def test_boards_and_pins(self):
        out = self.fpga("boards").stdout
        self.assertIn("maxv", out); self.assertIn("cyclone4", out)
        self.assertIn("PIN_20", self.fpga("pins", "--board", "maxv").stdout)

    def test_new_maxv(self):
        d = self.new("blink_maxv", "maxv")
        for f in ("src/blink_maxv.v", "tb/tb.v", "fpga.toml", "build/blink_maxv.tcl", "build/blink_maxv.sdc", "Makefile", ".gitignore", "README.md"):
            self.assertTrue((d / f).exists(), f)
        # la raíz queda limpia: solo las carpetas por tipo de archivo y los archivos del proyecto
        self.assertEqual(sorted(p.name for p in d.iterdir() if p.name != ".git"), [".gitignore", "Makefile", "README.md", "build", "fpga.toml", "src", "tb"])
        toml = (d / "fpga.toml").read_text()
        self.assertIn('layout = "dirs"', toml); self.assertIn('sources = ["src/blink_maxv.v"]', toml); self.assertIn('testbenches = ["tb/tb.v"]', toml)
        self.assertIn("../src/blink_maxv.v", (d / "build" / "blink_maxv.tcl").read_text())      # el .tcl corre dentro de build/
        tcl = (d / "build" / "blink_maxv.tcl").read_text()
        self.assertIn('FAMILY "MAX V"', tcl); self.assertIn("DEVICE 5M240ZT144C5\n", tcl)       # sin la N final: Quartus no la acepta
        self.assertIn("set_location_assignment PIN_72 -to led", tcl); self.assertIn("PIN_20 -to clk", tcl)
        self.assertIn("-period 20.000", (d / "build" / "blink_maxv.sdc").read_text())
        self.assertIn("100 ciclos", (d / "README.md").read_text())
        self.assertIn("\tiverilog", (d / "Makefile").read_text())

    def test_legacy_ordering_code_is_translated(self):
        d = self.new("viejo", "maxv")
        toml = (d / "fpga.toml").read_text().replace('device = "5M240ZT144C5"', 'device = "5M240ZT144C5N"')
        (d / "fpga.toml").write_text(toml)
        self.assertEqual(self.fpga("-C", str(d), "project", "--dry-run").returncode, 0)
        tcl = (d / "build" / "viejo.tcl").read_text()
        self.assertIn("DEVICE 5M240ZT144C5\n", tcl); self.assertNotIn("C5N", tcl)

    def test_unused_pins_are_not_driven_low_on_maxv(self):
        d = self.new("seguro", "maxv")
        tcl = (d / "build" / "seguro.tcl").read_text()
        self.assertIn('RESERVE_ALL_UNUSED_PINS "AS INPUT TRI-STATED"', tcl)
        self.assertIn("NUM_PARALLEL_PROCESSORS", tcl)

    def test_new_cyclone(self):
        d = self.new("blink_cy4", "cyclone4")
        tcl = (d / "build" / "blink_cy4.tcl").read_text()
        self.assertIn('FAMILY "Cyclone IV E"', tcl); self.assertIn("DEVICE EP4CE6E22C8", tcl)
        self.assertIn("PIN_87 -to led", tcl); self.assertIn("PIN_23 -to clk", tcl)
        self.assertIn("derive_clock_uncertainty", (d / "build" / "blink_cy4.sdc").read_text())
        self.assertIn("activo en bajo", (d / "src" / "blink_cy4.v").read_text())

    def test_new_asks_for_folder(self):
        r = self.fpga("new", "trabajo", "--board", "maxv", input=f"{self.repos}\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.repos / "trabajo" / "fpga.toml").exists())

    def test_hyphenated_folder_gets_valid_module_name(self):
        d = self.new("blink-maxv", "maxv")
        self.assertTrue((d / "src" / "blink_maxv.v").exists())
        self.assertIn("module blink_maxv", (d / "src" / "blink_maxv.v").read_text())
        self.assertIn("project_new blink_maxv", (d / "build" / "blink_maxv.tcl").read_text())
        self.assertIn('name = "blink_maxv"', (d / "fpga.toml").read_text())

    def test_new_rejects_bad_name_and_existing(self):
        self.assertNotEqual(self.fpga("new", "1mal", "--board", "maxv", "--dir", str(self.repos)).returncode, 0)
        self.new("ok", "maxv")
        self.assertNotEqual(self.fpga("new", "ok", "--board", "maxv", "--dir", str(self.repos)).returncode, 0)

    def test_unknown_led(self):
        r = self.fpga("new", "x", "--board", "maxv", "--dir", str(self.repos), "--led", "nope")
        self.assertNotEqual(r.returncode, 0)

    def test_no_project_here(self):
        r = self.fpga("prog", cwd=str(self.tmp))
        self.assertNotEqual(r.returncode, 0); self.assertIn("fpga.toml", r.stderr)


@unittest.skipUnless(shutil.which("iverilog"), "requiere iverilog")
class TestSim(Base):
    def test_sim_produces_waveform(self):
        for board in ("maxv", "cyclone4"):
            d = self.new("s_" + board, board)
            r = self.fpga("-C", str(d), "sim")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            vcd = (d / "waves" / "dump.vcd").read_text()
            self.assertIn("led", vcd)
            self.assertIn("sims", (d / ".fpga" / "state.json").read_text())


class TestBuild(Base):
    def test_build_calls_quartus(self):
        d = self.built("b", "maxv")
        log = "\n".join(self.calls())
        self.assertIn("quartus_sh -t b.tcl", log); self.assertIn("quartus_sh --flow compile b", log)
        self.assertTrue((d / "build" / "output_files" / "b.pof").exists())


class TestMaxV(Base):
    def prog(self, d, input="", *extra, env=None):
        return self.fpga("-C", str(d), "prog", *extra, input=input, env=env)

    def test_wrong_word_cancels(self):
        d = self.built("m", "maxv")
        r = self.prog(d, "no\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(any("quartus_pgm" in c for c in self.calls()))
        self.assertEqual(self.counters(), {})
        self.assertIn("ATENCIÓN", r.stdout); self.assertIn("100 ciclos", r.stdout)

    def test_confirm_programs_and_counts(self):
        d = self.built("m", "maxv")
        r = self.prog(d, "GRABAR\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("quartus_pgm -m jtag -o p;build/output_files/m.pof", "\n".join(self.calls()))
        self.assertEqual(self.counters()["maxv-1"]["count"], 1)
        self.prog(d, "GRABAR\n")
        self.assertEqual(self.counters()["maxv-1"]["count"], 2)

    def test_lowercase_is_not_enough(self):
        d = self.built("m", "maxv")
        self.assertNotEqual(self.prog(d, "grabar\n").returncode, 0)
        self.assertEqual(self.counters(), {})

    def test_stale_pof_refused(self):
        d = self.built("m", "maxv")
        future = (d / "build" / "output_files" / "m.pof").stat().st_mtime + 100
        os.utime(d / "src" / "m.v", (future, future))
        r = self.prog(d, "GRABAR\n")
        self.assertNotEqual(r.returncode, 0); self.assertIn("más viejo", r.stderr)
        self.assertFalse(any("quartus_pgm" in c for c in self.calls()))

    def test_failed_load_not_counted(self):
        d = self.built("m", "maxv")
        r = self.prog(d, "GRABAR\n", env={"FAKE_PGM_FAIL": "1"})
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.counters(), {})
        self.assertIn("NO se contó", r.stdout)

    def test_dry_run_neither_asks_nor_counts(self):
        d = self.built("m", "maxv")
        r = self.prog(d, "", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("quartus_pgm -m jtag", r.stdout)
        self.assertFalse(any("quartus_pgm" in c for c in self.calls()))
        self.assertEqual(self.counters(), {})

    def test_limit_needs_extra_confirmation(self):
        d = self.built("m", "maxv")
        r = self.fpga("counter", "--unit", "maxv-1", "--set", "100", input="CONFIRMO\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.prog(d, "GRABAR\n")                       # responde al aviso de límite con otra palabra
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.counters()["maxv-1"]["count"], 100)
        r = self.prog(d, "SUPERAR LIMITE\nGRABAR\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.counters()["maxv-1"]["count"], 101)

    def test_warns_near_limit_and_unsimulated(self):
        d = self.built("m", "maxv")
        self.fpga("counter", "--unit", "maxv-1", "--add", "85")
        out = self.prog(d, "no\n").stdout
        self.assertIn("cerca del límite", out); self.assertIn("No has simulado", out)

    @unittest.skipUnless(shutil.which("iverilog"), "requiere iverilog")
    def test_sim_freshness_follows_sources_and_testbench_only(self):
        d = self.built("m", "maxv")
        self.assertEqual(self.fpga("-C", str(d), "sim").returncode, 0)
        self.assertNotIn("No has simulado", self.prog(d, "no\n").stdout)
        future = (d / "build" / "output_files" / "m.pof").stat().st_mtime - 1      # tb.v cambia después de simular, pero el .pof sigue vigente
        os.utime(d / "tb" / "tb.v", (future + 100, future + 100))
        os.utime(d / "build" / "output_files" / "m.pof", (future + 200, future + 200))
        self.assertIn("No has simulado", self.prog(d, "no\n").stdout)

    def test_units_are_independent(self):
        d1 = self.built("m1", "maxv")
        r = self.fpga("new", "m2", "--board", "maxv", "--dir", str(self.repos), "--unit", "maxv-2")
        self.assertEqual(r.returncode, 0)
        d2 = self.repos / "m2"; self.fpga("-C", str(d2), "build")
        self.prog(d1, "GRABAR\n"); self.prog(d2, "GRABAR\n"); self.prog(d2, "GRABAR\n")
        c = self.counters()
        self.assertEqual((c["maxv-1"]["count"], c["maxv-2"]["count"]), (1, 2))


class TestCyclone(Base):
    def prog(self, d, *args, input=""):
        return self.fpga("-C", str(d), "prog", *args, input=input)

    def test_ram(self):
        d = self.built("c", "cyclone4")
        r = self.prog(d, "--target", "ram")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("quartus_pgm -m jtag -o p;build/output_files/c.sof", "\n".join(self.calls()))
        self.assertEqual(self.counters(), {})

    def test_flash_converts_then_programs_by_as(self):
        d = self.built("c", "cyclone4")
        r = self.prog(d, "--target", "flash", input="s\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = self.calls()
        cpf = [i for i, c in enumerate(log) if c.startswith("quartus_cpf")]
        pgm = [i for i, c in enumerate(log) if c.startswith("quartus_pgm")]
        self.assertTrue(cpf and pgm and cpf[0] < pgm[0])
        self.assertIn("quartus_cpf -c -d EPCS16 -s EP4CE6 build/output_files/c.sof build/output_files/c.jic", log[cpf[0]])
        self.assertIn("quartus_pgm -m jtag -o p;build/output_files/c.jic", log[pgm[0]])
        self.assertIn("por JTAG", r.stdout); self.assertNotIn("conector AS", r.stdout)

    def test_flash_declined(self):
        d = self.built("c", "cyclone4")
        self.assertNotEqual(self.prog(d, "--target", "flash", input="n\n").returncode, 0)
        self.assertFalse(any(c.startswith(("quartus_cpf", "quartus_pgm")) for c in self.calls()))

    def test_flash_conversion_failure_stops(self):
        d = self.built("c", "cyclone4")
        e = dict(self.env, FAKE_CPF_FAIL="1")
        r = subprocess.run([sys.executable, FPGA, "-C", str(d), "prog", "--target", "flash"], input="s\n",
                           capture_output=True, text=True, env=e)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(any(c.startswith("quartus_pgm") for c in self.calls()))

    def test_flash_convert_only_never_touches_the_board(self):
        d = self.built("c", "cyclone4")
        r = self.prog(d, "--target", "flash", "--convert-only")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = self.calls()
        self.assertTrue(any(c.startswith("quartus_cpf") for c in log))
        self.assertFalse(any(c.startswith("quartus_pgm") for c in log))
        self.assertTrue((d / "build" / "output_files" / "c.jic").exists())

    def test_convert_only_rejected_elsewhere(self):
        d = self.built("c", "cyclone4")
        self.assertNotEqual(self.prog(d, "--target", "ram", "--convert-only").returncode, 0)
        m = self.built("m", "maxv")
        self.assertNotEqual(self.fpga("-C", str(m), "prog", "--convert-only", input="GRABAR\n").returncode, 0)
        self.assertFalse(any(c.startswith("quartus_pgm") for c in self.calls()))

    def test_target_required_without_terminal(self):
        d = self.built("c", "cyclone4")
        r = self.prog(d)
        self.assertNotEqual(r.returncode, 0); self.assertIn("--target", r.stderr)

    def test_dry_run_flash(self):
        d = self.built("c", "cyclone4")
        r = self.prog(d, "--target", "flash", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(any(c.startswith(("quartus_cpf", "quartus_pgm")) for c in self.calls()))
        self.assertIn("quartus_pgm -m jtag -o 'p;build/output_files/c.jic'", r.stdout)


if __name__ == "__main__":
    unittest.main()

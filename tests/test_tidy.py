"""fpga tidy y la estructura de carpetas (src/ tb/ sim/ waves/ build/), con proyectos planos como los antiguos."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FPGA = str(ROOT / "fpga")
FAKEBIN = str(ROOT / "tests" / "fakebin")
HAS_IVERILOG = bool(shutil.which("iverilog"))
GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]

CONTADOR = "module contador (input wire clk, input wire rst_n, output reg [3:0] q);\n always @(posedge clk) q <= rst_n ? q + 1'b1 : 4'd0;\nendmodule\n"
TOP = "module mi_top (input wire clk, input wire rst_n, output wire [3:0] led);\n contador c(.clk(clk), .rst_n(rst_n), .q(led));\nendmodule\n"
TB = ("`timescale 1ns/1ps\nmodule %(n)s; reg clk = 0, rst_n = 0; wire [3:0] q;\n contador uut(.clk(clk), .rst_n(rst_n), .q(q));\n"
      " always #10 clk = ~clk;\n initial begin $dumpfile(\"%(n)s.vcd\"); $dumpvars(0, %(n)s); #25 rst_n = 1; #200 $finish; end\nendmodule\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = dict(os.environ, FPGA_CLI_HOME=str(self.tmp / "cfg"), FPGA_CLI_DATA=str(self.tmp / "data"),
                        FPGA_CLI_TOOLS=str(self.tmp / "tools"), FAKE_LOG=str(self.tmp / "calls.log"),
                        PATH=FAKEBIN + os.pathsep + os.environ["PATH"], NO_COLOR="1")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fpga(self, *args, input="", cwd=None):
        return subprocess.run([sys.executable, FPGA, *args], input=input, capture_output=True, text=True, env=self.env, cwd=cwd)

    def calls(self):
        p = self.tmp / "calls.log"
        return p.read_text().splitlines() if p.exists() else []

    def flat_project(self, git=False):
        """Un proyecto como los de antes: todo suelto en la raíz, con lo que generan simulación y Quartus."""
        d = self.tmp / "viejo"; d.mkdir()
        (d / "contador.v").write_text(CONTADOR); (d / "mi_top.v").write_text(TOP)
        (d / "tb_a.v").write_text(TB % {"n": "tb_a"}); (d / "tb_b.v").write_text(TB % {"n": "tb_b"})
        r = self.fpga("-C", str(d), "init", "--board", "cyclone4", "--top", "mi_top"); self.assertEqual(r.returncode, 0, r.stderr)
        self.fpga("-C", str(d), "pin", "rst_n", "reset"); self.fpga("-C", str(d), "pin", "led[3:0]", "led4", "led3", "led2", "led1")
        if HAS_IVERILOG:
            for tb in ("tb_a", "tb_b"):
                self.assertEqual(self.fpga("-C", str(d), "sim", tb).returncode, 0)
        self.assertEqual(self.fpga("-C", str(d), "build").returncode, 0)
        for extra in ("db/x.tmp", "incremental_db/y.tmp"):
            (d / extra).parent.mkdir(exist_ok=True); (d / extra).write_text("q")
        for f in ("mi_top.qpf", "mi_top.qsf", "Makefile"):
            (d / f).write_text("generado")
        if git:
            subprocess.run(["git", "init", "-q"], cwd=d); subprocess.run(["git", "add", "-A"], cwd=d)
            subprocess.run(GIT + ["commit", "-q", "-m", "plano"], cwd=d)
        return d


class TestLegacyFlat(Base):
    def test_flat_projects_keep_working_without_tidy(self):
        d = self.flat_project()
        self.assertTrue((d / "output_files" / "mi_top.sof").exists())          # sin carpeta build/
        self.assertTrue((d / "mi_top.tcl").exists()); self.assertFalse((d / "build").exists())
        self.assertIn("VERILOG_FILE contador.v", (d / "mi_top.tcl").read_text())   # rutas sin ../
        if HAS_IVERILOG:
            self.assertTrue((d / "tb_a.vcd").exists())
        r = self.fpga("-C", str(d), "prog", "--target", "ram"); self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("quartus_pgm -m jtag -o p;output_files/mi_top.sof", "\n".join(self.calls()))

    def test_the_panel_suggests_tidy_for_flat_projects(self):
        from fpga_cli import tui
        d = self.flat_project()
        self.assertEqual(tui.project_info(d)["layout"], "flat")


class TestTidy(Base):
    def test_dry_run_shows_the_plan_and_moves_nothing(self):
        d = self.flat_project()
        before = sorted(p.name for p in d.iterdir())
        r = self.fpga("-C", str(d), "tidy", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        for linea in ("contador.v", "src/contador.v", "tb/tb_a.v", "build/mi_top.tcl", "build/db", "build/output_files"):
            self.assertIn(linea, r.stdout)
        self.assertEqual(sorted(p.name for p in d.iterdir()), before)

    def test_needs_confirmation(self):
        d = self.flat_project()
        r = self.fpga("-C", str(d), "tidy")                                       # sin terminal y sin --yes
        self.assertNotEqual(r.returncode, 0); self.assertIn("--yes", r.stderr)
        self.assertTrue((d / "contador.v").exists())

    def test_tidy_orders_everything(self):
        d = self.flat_project()
        r = self.fpga("-C", str(d), "tidy", "--yes"); self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        # la raíz queda solo con las carpetas y los archivos del proyecto
        self.assertEqual(sorted(p.name for p in d.iterdir() if p.name != ".fpga"),
                         [".gitignore", "Makefile", "Makefile.bak", "build", "fpga.toml", "sim", "src", "tb", "waves"])   # sin git, se respalda el Makefile
        self.assertEqual(sorted(p.name for p in (d / "src").iterdir()), ["contador.v", "mi_top.v"])
        self.assertEqual(sorted(p.name for p in (d / "tb").iterdir()), ["tb_a.v", "tb_b.v"])
        if HAS_IVERILOG:
            self.assertEqual(sorted(p.name for p in (d / "sim").iterdir()), ["sim_tb_a.vvp", "sim_tb_b.vvp"])
            self.assertEqual(sorted(p.name for p in (d / "waves").iterdir()), ["tb_a.vcd", "tb_b.vcd"])
        for nombre in ("mi_top.qpf", "mi_top.qsf", "mi_top.tcl", "mi_top.sdc", "db", "incremental_db", "output_files"):
            self.assertTrue((d / "build" / nombre).exists(), nombre)
        toml = (d / "fpga.toml").read_text()
        self.assertIn('layout = "dirs"', toml); self.assertIn('sources = ["src/*.v"]', toml)
        self.assertIn('testbenches = ["tb/tb_a.v", "tb/tb_b.v"]', toml)
        self.assertIn("VERILOG_FILE ../src/contador.v", (d / "build" / "mi_top.tcl").read_text())
        self.assertIn("{led[3]}", (d / "build" / "mi_top.tcl").read_text())
        self.assertIn("sim/", (d / ".gitignore").read_text()); self.assertIn("!build/*.tcl", (d / ".gitignore").read_text())
        self.assertIn("cd build &&", (d / "Makefile").read_text())

    def test_everything_still_works_after_tidy(self):
        d = self.flat_project()
        self.fpga("-C", str(d), "tidy", "--yes")
        if HAS_IVERILOG:
            r = self.fpga("-C", str(d), "sim", "tb_a"); self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue((d / "waves" / "tb_a.vcd").exists()); self.assertTrue((d / "sim" / "sim_tb_a.vvp").exists())
            self.assertEqual(self.fpga("-C", str(d), "sim", "tb/tb_b.v").returncode, 0)        # también por ruta
        r = self.fpga("-C", str(d), "build"); self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((d / "build" / "output_files" / "mi_top.sof").exists())
        self.assertFalse((d / "output_files").exists())
        r = self.fpga("-C", str(d), "prog", "--target", "ram"); self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("quartus_pgm -m jtag -o p;build/output_files/mi_top.sof", "\n".join(self.calls()))

    def test_state_survives_tidy_and_is_idempotent(self):
        from fpga_cli import tui
        d = self.flat_project()
        antes = tui.project_info(d)["simulated"]
        self.fpga("-C", str(d), "tidy", "--yes")
        info = tui.project_info(d)
        self.assertEqual(info["layout"], "dirs"); self.assertEqual(info["simulated"], antes)     # conserva «simulado»
        r = self.fpga("-C", str(d), "tidy", "--yes")
        self.assertEqual(r.returncode, 0); self.assertIn("ya está ordenado", r.stdout)

    def test_git_history_is_kept_with_renames(self):
        if not shutil.which("git"):
            self.skipTest("requiere git")
        d = self.flat_project(git=True)
        self.fpga("-C", str(d), "tidy", "--yes")
        estado = subprocess.run(["git", "status", "--short"], cwd=d, capture_output=True, text=True).stdout.splitlines()
        renombres = [l for l in estado if l.startswith("R")]
        self.assertTrue(any("contador.v -> src/contador.v" in l for l in renombres), estado)
        self.assertTrue(any("tb_a.v -> tb/tb_a.v" in l for l in renombres), estado)
        self.assertFalse([l for l in estado if l.startswith("D") or l.startswith(" D")], "nada debe aparecer como borrado")
        self.assertFalse((d / "Makefile.bak").exists())                          # el Makefile ya está en git: no hace falta respaldo

    def test_makefile_is_backed_up_when_not_in_git(self):
        d = self.flat_project()
        (d / "Makefile").write_text("# el mío\n")
        self.fpga("-C", str(d), "tidy", "--yes")
        self.assertEqual((d / "Makefile.bak").read_text(), "# el mío\n")
        self.assertIn("Makefile.bak", (d / ".gitignore").read_text())                     # el respaldo no ensucia el repo

    def test_stray_files_and_collisions_are_reported_not_moved(self):
        d = self.flat_project()
        (d / "suelto.v").write_text("module suelto; endmodule\n")
        toml = (d / "fpga.toml").read_text().replace('sources = ["*.v"]', 'sources = ["contador.v", "mi_top.v"]')
        (d / "fpga.toml").write_text(toml)
        (d / "src").mkdir(); (d / "src" / "contador.v").write_text("// ya había uno\n")
        r = self.fpga("-C", str(d), "tidy", "--dry-run")
        self.assertIn("suelto.v", r.stdout); self.assertIn("no son fuentes", r.stdout)
        self.assertIn("src/contador.v ya existe", r.stdout)
        self.fpga("-C", str(d), "tidy", "--yes")
        self.assertTrue((d / "suelto.v").exists())                                       # no se toca
        self.assertEqual((d / "src" / "contador.v").read_text(), "// ya había uno\n")   # no se pisa
        self.assertTrue((d / "contador.v").exists())

    def test_explicit_source_lists_are_rewritten(self):
        d = self.flat_project()
        toml = (d / "fpga.toml").read_text().replace('sources = ["*.v"]', 'sources = ["contador.v", "mi_top.v"]')
        (d / "fpga.toml").write_text(toml)
        self.fpga("-C", str(d), "tidy", "--yes")
        self.assertIn('sources = ["src/contador.v", "src/mi_top.v"]', (d / "fpga.toml").read_text())


@unittest.skipUnless(HAS_IVERILOG and shutil.which("make"), "requiere iverilog y make")
class TestMakefile(Base):
    def test_the_generated_makefile_works_without_fpga(self):
        r = self.fpga("new", "conmake", "--board", "maxv", "--dir", str(self.tmp / "r"), "--template", "blink"); self.assertEqual(r.returncode, 0, r.stderr)
        d = self.tmp / "r" / "conmake"
        m = subprocess.run(["make", "sim"], cwd=d, capture_output=True, text=True)
        self.assertEqual(m.returncode, 0, m.stdout + m.stderr)
        self.assertTrue((d / "waves" / "dump.vcd").exists()); self.assertTrue((d / "sim" / "tb.vvp").exists())
        self.assertEqual(sorted(p.name for p in d.iterdir() if p.name != ".git"), [".gitignore", "Makefile", "README.md", "build", "fpga.toml", "sim", "src", "tb", "waves"])
        n = subprocess.run(["make", "-n", "build"], cwd=d, capture_output=True, text=True).stdout
        self.assertIn("cd build && quartus_sh -t conmake.tcl", n); self.assertIn("quartus_sh --flow compile conmake", n)


if __name__ == "__main__":
    unittest.main()

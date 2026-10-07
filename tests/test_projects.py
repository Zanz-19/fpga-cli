"""Pruebas de proyectos propios: plantilla vacía, init, add, new-tb, varios testbenches y pin."""
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
from fpga_cli import scaffold  # noqa: E402

HAS_IVERILOG = bool(shutil.which("iverilog"))
COUNTER = """module contador #(parameter W = 4, parameter [3:0] INI = 4'd0) (
    input  wire         clk,   // reloj
    input  wire         rst,
    output reg  [W-1:0] q
);
    always @(posedge clk) q <= rst ? INI : q + 1'b1;
endmodule
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = dict(os.environ, FPGA_CLI_HOME=str(self.tmp / "cfg"), FPGA_CLI_DATA=str(self.tmp / "data"),
                        FPGA_CLI_TOOLS=str(self.tmp / "tools"), FAKE_LOG=str(self.tmp / "calls.log"),
                        PATH=FAKEBIN + os.pathsep + os.environ["PATH"], NO_COLOR="1")
        self.repos = self.tmp / "repos"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fpga(self, *args, input="", cwd=None):
        return subprocess.run([sys.executable, FPGA, *args], input=input, capture_output=True, text=True, env=self.env, cwd=cwd)

    def new(self, name, board="maxv", template="empty"):
        r = self.fpga("new", name, "--board", board, "--dir", str(self.repos), "--template", template)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        return self.repos / name

    def tcl(self, d, name):
        r = self.fpga("-C", str(d), "project", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return (d / "build" / f"{name}.tcl").read_text()


class TestParser(unittest.TestCase):
    def test_ansi_with_params_widths_and_comments(self):
        info = scaffold.parse_module(COUNTER, "contador")
        self.assertEqual(info["params"], [("W", "4"), ("INI", "4'd0")])
        self.assertEqual(info["ports"], [("input", "", "clk"), ("input", "", "rst"), ("output", "[W-1:0]", "q")])

    def test_direction_is_inherited(self):
        info = scaffold.parse_module("module m (input a, b, output [1:0] y, z);\nendmodule", "m")
        self.assertEqual([(p[0], p[2]) for p in info["ports"]], [("input", "a"), ("input", "b"), ("output", "y"), ("output", "z")])

    def test_old_style_header_is_reported(self):
        info = scaffold.parse_module("module m (a, b);\n input a; output b;\nendmodule", "m")
        self.assertFalse(info["ansi"])

    def test_missing_module(self):
        self.assertIsNone(scaffold.parse_module(COUNTER, "otro"))

    def test_nested_parentheses_in_params(self):
        info = scaffold.parse_module("module m #(parameter A = (2+3)*2) (input clk);\nendmodule", "m")
        self.assertEqual(info["params"], [("A", "(2+3)*2")]); self.assertEqual(info["ports"][0][2], "clk")


class TestTemplates(Base):
    def test_empty_template_has_no_blink_and_only_the_clock(self):
        d = self.new("mio-vacio")
        top = (d / "src" / "mio_vacio.v").read_text()
        self.assertIn("module mio_vacio", top); self.assertNotIn("cuenta", top); self.assertNotIn("led", top)
        toml = (d / "fpga.toml").read_text()
        self.assertIn('pins]\nclk = "PIN_20"', toml.replace("\r", "")); self.assertNotIn("led", toml)
        self.assertIn("always #10 clk", (d / "tb" / "tb_mio_vacio.v").read_text())      # 50 MHz -> semiperiodo de 10 ns
        self.assertFalse((d / "tb" / "tb.v").exists()); self.assertIn('testbenches = ["tb/tb_mio_vacio.v"]', toml)

    @unittest.skipUnless(HAS_IVERILOG, "requiere iverilog")
    def test_empty_template_simulates_and_builds(self):
        d = self.new("vacio")
        self.assertEqual(self.fpga("-C", str(d), "sim").returncode, 0)
        r = self.fpga("-C", str(d), "build"); self.assertEqual(r.returncode, 0, r.stderr)

    def test_template_is_asked_when_missing_and_defaults_to_blink_without_terminal(self):
        r = self.fpga("new", "x", "--board", "maxv", "--dir", str(self.repos))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("cuenta", (self.repos / "x" / "src" / "x.v").read_text())

    def test_cyclone_empty_uses_its_own_clock_pin(self):
        d = self.new("cy", "cyclone4")
        self.assertIn('clk = "PIN_23"', (d / "fpga.toml").read_text())


class TestInit(Base):
    def make(self):
        d = self.tmp / "disenos" / "mi-chip"; d.mkdir(parents=True)
        (d / "top.v").write_text("module mi_chip (input wire clk, output wire [3:0] q);\n contador c(.clk(clk), .rst(1'b0), .q(q));\nendmodule\n")
        (d / "contador.v").write_text(COUNTER)
        (d / "tb_contador.v").write_text("module tb_contador; endmodule\n")
        return d

    def test_init_adopts_existing_files(self):
        d = self.make()
        r = self.fpga("-C", str(d), "init", "--board", "maxv", "--top", "mi_chip")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        toml = (d / "fpga.toml").read_text()
        self.assertIn('sources = ["*.v"]', toml); self.assertIn('testbenches = ["tb_contador.v"]', toml)
        self.assertIn('clk = "PIN_20"', toml)                                   # el top declara clk
        tcl = (d / "mi_chip.tcl").read_text()
        self.assertIn("VERILOG_FILE contador.v", tcl); self.assertIn("VERILOG_FILE top.v", tcl)
        self.assertNotIn("tb_contador", tcl)                                    # los testbenches nunca van a Quartus
        self.assertIn("TOP_LEVEL_ENTITY mi_chip", tcl)

    def test_init_refuses_existing_project_and_empty_folder(self):
        d = self.make()
        self.fpga("-C", str(d), "init", "--board", "maxv", "--top", "mi_chip")
        self.assertNotEqual(self.fpga("-C", str(d), "init", "--board", "maxv", "--top", "mi_chip").returncode, 0)
        vacio = self.tmp / "vacio"; vacio.mkdir()
        self.assertNotEqual(self.fpga("-C", str(vacio), "init", "--board", "maxv", "--top", "x").returncode, 0)


class TestSourcesAndModules(Base):
    def test_add_creates_module_and_updates_explicit_sources(self):
        d = self.new("p")
        r = self.fpga("-C", str(d), "add", "contador"); self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((d / "src" / "contador.v").exists())
        self.assertIn('sources = ["src/p.v", "src/contador.v"]', (d / "fpga.toml").read_text())
        self.assertIn("VERILOG_FILE ../src/contador.v", self.tcl(d, "p"))
        self.assertNotEqual(self.fpga("-C", str(d), "add", "contador").returncode, 0)        # ya existe
        self.assertNotEqual(self.fpga("-C", str(d), "add", "1mal").returncode, 0)

    def test_globs_cover_subfolders_and_skip_hidden_and_testbenches(self):
        d = self.new("g")
        (d / "src" / "nivel2").mkdir(parents=True); (d / ".fpga").mkdir(exist_ok=True)
        (d / "src" / "a.v").write_text("module a; endmodule\n"); (d / "src" / "nivel2" / "b.v").write_text("module b; endmodule\n")
        (d / ".fpga" / "netlist.v").write_text("module n; endmodule\n"); (d / "tb" / "tb_x.v").write_text("module tb_x; endmodule\n")
        from fpga_cli import core
        toml = (d / "fpga.toml").read_text().replace('sources = ["src/g.v"]', 'sources = ["src/**/*.v", "tb/*.v"]')
        (d / "fpga.toml").write_text(toml)
        got = core.load_project(d).resolved_sources()
        self.assertIn("src/g.v", got); self.assertIn("src/a.v", got); self.assertIn("src/nivel2/b.v", got)
        self.assertNotIn(".fpga/netlist.v", got)                                      # los ocultos nunca
        self.assertNotIn("tb/tb_g.v", got)                                            # los testbenches listados nunca, aunque un comodín los alcance
        (d / "fpga.toml").write_text(toml.replace('testbenches = ["tb/tb_g.v"]', 'testbenches = ["tb/tb_g.v", "tb/tb_x.v"]'))
        self.assertNotIn("tb/tb_x.v", core.load_project(d).resolved_sources())

    def test_add_with_globs_leaves_sources_alone(self):
        d = self.new("g2")
        toml = (d / "fpga.toml").read_text().replace('sources = ["src/g2.v"]', 'sources = ["src/*.v"]')
        (d / "fpga.toml").write_text(toml)
        self.assertEqual(self.fpga("-C", str(d), "add", "nuevo").returncode, 0)
        self.assertIn('sources = ["src/*.v"]', (d / "fpga.toml").read_text())
        self.assertIn("VERILOG_FILE ../src/nuevo.v", self.tcl(d, "g2"))

    def test_systemverilog_and_vhdl_reach_quartus_but_vhdl_not_the_simulator(self):
        d = self.new("mix")
        (d / "src" / "x.sv").write_text("module x; endmodule\n"); (d / "src" / "y.vhd").write_text("-- vhdl\n")
        toml = (d / "fpga.toml").read_text().replace('sources = ["src/mix.v"]', 'sources = ["src/mix.v", "src/x.sv", "src/y.vhd"]')
        (d / "fpga.toml").write_text(toml)
        tcl = self.tcl(d, "mix")
        self.assertIn("SYSTEMVERILOG_FILE ../src/x.sv", tcl); self.assertIn("VHDL_FILE ../src/y.vhd", tcl)
        from fpga_cli import core
        self.assertEqual(core.load_project(d).sim_sources(), ["src/mix.v", "src/x.sv"])


@unittest.skipUnless(HAS_IVERILOG, "requiere iverilog")
class TestTestbenches(Base):
    def project_with_counter(self):
        d = self.new("t")
        (d / "src" / "contador.v").write_text(COUNTER)
        self.assertEqual(self.fpga("-C", str(d), "add", "otro").returncode, 0)
        toml = (d / "fpga.toml").read_text().replace('"src/otro.v"', '"src/contador.v"')
        (d / "fpga.toml").write_text(toml)
        return d

    def test_new_tb_connects_ports_and_simulates(self):
        d = self.project_with_counter()
        (d / "src" / "otro.v").unlink()
        r = self.fpga("-C", str(d), "new-tb", "contador"); self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        tb = (d / "tb" / "tb_contador.v").read_text()
        for frag in ("localparam W = 4;", "localparam INI = 4'd0;", "contador #(.W(W), .INI(INI)) uut (", "reg clk = 0;", "reg rst = 0;", "wire [W-1:0] q;", ".clk(clk)", ".q(q)", "always #10 clk = ~clk;", '$dumpfile("tb_contador.vcd")'):
            self.assertIn(frag, tb)
        self.assertIn('testbenches = ["tb/tb_t.v", "tb/tb_contador.v"]', (d / "fpga.toml").read_text())
        s = self.fpga("-C", str(d), "sim", "tb_contador")
        self.assertEqual(s.returncode, 0, s.stdout + s.stderr)
        self.assertTrue((d / "waves" / "tb_contador.vcd").exists())          # el vcd cae en waves/ sin tocar el testbench

    def test_several_testbenches_need_a_name_without_terminal(self):
        d = self.project_with_counter()
        self.fpga("-C", str(d), "new-tb", "contador")
        r = self.fpga("-C", str(d), "sim")
        self.assertNotEqual(r.returncode, 0); self.assertIn("varios testbenches", r.stderr)
        self.assertEqual(self.fpga("-C", str(d), "sim", "tb_t").returncode, 0)
        self.assertEqual(self.fpga("-C", str(d), "sim", "tb_contador.v").returncode, 0)
        self.assertNotEqual(self.fpga("-C", str(d), "sim", "no_existe").returncode, 0)

    def test_new_tb_force_regenerates_after_ports_change(self):
        d = self.new("rg")
        tb = d / "tb" / "tb_rg.v"
        self.assertNotIn(".rst_n(rst_n)", tb.read_text())                       # el top vacío solo tiene clk
        (d / "src" / "rg.v").write_text("module rg (input wire clk, input wire rst_n, output wire [3:0] q);\nendmodule\n")
        r = self.fpga("-C", str(d), "new-tb", "rg")                                  # sin --force no sobrescribe
        self.assertNotEqual(r.returncode, 0); self.assertIn("--force", r.stderr)
        self.assertNotIn(".rst_n(rst_n)", tb.read_text())
        r = self.fpga("-C", str(d), "new-tb", "rg", "--force"); self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(".rst_n(rst_n)", tb.read_text()); self.assertIn("wire [3:0] q;", tb.read_text())
        self.assertEqual((d / "fpga.toml").read_text().count("tb/tb_rg.v"), 1)          # no se duplica en testbenches

    def test_new_tb_reports_the_connected_ports_and_params(self):
        d = self.project_with_counter()
        r = self.fpga("-C", str(d), "new-tb", "contador")
        self.assertIn("entradas: clk, rst", r.stdout); self.assertIn("salidas: q", r.stdout)
        self.assertIn("W = 4", r.stdout)

    def test_new_tb_warns_about_a_duplicate_module_definition(self):
        d = self.new("dup")
        (d / "src" / "dup.v").write_text("module dup (input wire clk);\nendmodule\n\nmodule dup (input wire clk, input wire rst_n, output wire y);\nendmodule\n")
        r = self.fpga("-C", str(d), "new-tb", "dup", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("definido 2 veces", r.stdout + r.stderr); self.assertIn("PRIMERA definición", r.stdout + r.stderr)
        self.assertNotIn(".rst_n(rst_n)", (d / "tb" / "tb_dup.v").read_text())          # usó la primera, la del esqueleto

    def test_new_tb_tells_you_when_the_module_is_missing_or_unsaved(self):
        d = self.new("nada")
        r = self.fpga("-C", str(d), "new-tb", "no_esta")
        self.assertNotEqual(r.returncode, 0); self.assertIn("¿Guardaste el archivo?", r.stderr)

    def test_new_tb_errors(self):
        d = self.project_with_counter()
        self.assertNotEqual(self.fpga("-C", str(d), "new-tb", "fantasma").returncode, 0)
        self.fpga("-C", str(d), "new-tb", "contador")
        self.assertNotEqual(self.fpga("-C", str(d), "new-tb", "contador").returncode, 0)     # ya existe
        self.assertEqual(self.fpga("-C", str(d), "new-tb", "contador", "--name", "tb_otro").returncode, 0)

    def test_programming_freshness_is_per_testbench(self):
        d = self.project_with_counter()
        self.fpga("-C", str(d), "new-tb", "contador")
        self.assertEqual(self.fpga("-C", str(d), "build").returncode, 0)
        from fpga_cli import core
        self.assertFalse(core.load_project(d).is_simulated())
        self.fpga("-C", str(d), "sim", "tb_contador")
        self.assertTrue(core.load_project(d).is_simulated())
        later = (d / "src" / "t.v").stat().st_mtime + 100
        os.utime(d / "src" / "t.v", (later, later))                      # cambia una fuente: toda simulación previa queda vieja
        self.assertFalse(core.load_project(d).is_simulated())

    def test_wave_picks_the_testbench_waveform(self):
        d = self.project_with_counter()
        self.fpga("-C", str(d), "new-tb", "contador"); self.fpga("-C", str(d), "sim", "tb_contador")
        from fpga_cli import cli, core
        p = core.load_project(d)
        self.assertEqual(cli._vcd_for(p, "tb_contador.v").name, "tb_contador.vcd")
        self.assertIsNone(cli._vcd_for(p, "otro.v"))

    def test_netlist_output_never_pollutes_sources(self):
        if not shutil.which("yosys"):
            self.skipTest("requiere yosys")
        d = self.new("n")
        self.assertEqual(self.fpga("-C", str(d), "netlist").returncode, 0)
        self.assertTrue((d / ".fpga" / "netlist.v").exists())
        from fpga_cli import core
        self.assertNotIn(".fpga/netlist.v", core.load_project(d).resolved_sources())


class TestPins(Base):
    def test_set_list_and_remove(self):
        d = self.new("pp")
        self.assertEqual(self.fpga("-C", str(d), "pin", "led", "led0").returncode, 0)
        self.assertIn("set_location_assignment PIN_72 -to led", self.tcl(d, "pp"))
        self.assertEqual(self.fpga("-C", str(d), "pin", "led", "led1").returncode, 0)        # reasignar reemplaza
        toml = (d / "fpga.toml").read_text()
        self.assertEqual(toml.count("led ="), 1); self.assertIn('led = "PIN_71"', toml)
        out = self.fpga("-C", str(d), "pin").stdout
        self.assertIn("led", out); self.assertIn("PIN_71", out); self.assertIn("led1", out)
        self.assertEqual(self.fpga("-C", str(d), "pin", "--remove", "led").returncode, 0)
        self.assertNotIn("led", (d / "fpga.toml").read_text().split("[pins]")[1])
        self.assertNotEqual(self.fpga("-C", str(d), "pin", "--remove", "led").returncode, 0)

    def test_bus_assignment_and_raw_pin(self):
        d = self.new("bus")
        r = self.fpga("-C", str(d), "pin", "led[3:0]", "led3", "led2", "led1", "led0")
        self.assertEqual(r.returncode, 0, r.stderr)
        tcl = self.tcl(d, "bus")
        for i, pin in ((3, 69), (2, 70), (1, 71), (0, 72)):
            self.assertIn(f"set_location_assignment PIN_{pin} -to {{led[{i}]}}", tcl)
        self.assertEqual(self.fpga("-C", str(d), "pin", "ext", "PIN_100").returncode, 0)
        self.assertIn("PIN_100 -to ext", self.tcl(d, "bus"))
        self.assertEqual(self.fpga("-C", str(d), "pin", "otro", "55").returncode, 0)
        self.assertIn("PIN_55 -to otro", self.tcl(d, "bus"))

    @unittest.skipUnless(shutil.which("tclsh"), "requiere tclsh")
    def test_generated_tcl_runs_in_a_real_tcl_interpreter(self):
        """Los corchetes de un bus (`seg[7]`) son sustitución de comandos en Tcl: el .tcl debe protegerlos con llaves."""
        d = self.new("tclok")
        r = self.fpga("-C", str(d), "pin", "bus[1:0]", "led1", "led0"); self.assertEqual(r.returncode, 0, r.stderr)
        r = self.fpga("-C", str(d), "pin", "solo[0]", "key0"); self.assertEqual(r.returncode, 0, r.stderr)
        tcl = self.tcl(d, "tclok")
        stub = ('set ::log {}\nproc project_new args {}\nproc project_close args {}\n'
                'proc set_global_assignment args {}\n'
                'proc set_location_assignment {pin to name} {lappend ::log $name}\n')
        script = d / "ejecuta.tcl"
        script.write_text(stub + tcl + 'puts [join $::log ","]\n')
        r = subprocess.run(["tclsh", str(script)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        names = r.stdout.strip().split(",")
        for expected in ("clk", "bus[1]", "bus[0]", "solo[0]"):
            self.assertIn(expected, names)

    def test_errors(self):
        d = self.new("er")
        self.assertNotEqual(self.fpga("-C", str(d), "pin", "x", "no_es_una_senal").returncode, 0)
        self.assertNotEqual(self.fpga("-C", str(d), "pin", "led[3:0]", "led0", "led1").returncode, 0)   # faltan señales
        self.assertNotEqual(self.fpga("-C", str(d), "pin", "x").returncode, 0)                           # falta la señal
        self.assertNotIn("x =", (d / "fpga.toml").read_text())

    def test_new_pins_go_with_the_other_pins_not_after_trailing_comments(self):
        d = self.new("orden")
        self.fpga("-C", str(d), "pin", "led[1:0]", "led1", "led0")
        text = (d / "fpga.toml").read_text()
        self.assertLess(text.index('"led[1]" ='), text.index("# [programmer]"))
        self.assertLess(text.index('"led[0]" ='), text.index("# [programmer]"))

    def test_toml_edit_keeps_comments_and_other_sections(self):
        d = self.new("keep")
        before = (d / "fpga.toml").read_text()
        self.fpga("-C", str(d), "pin", "led", "led0")
        after = (d / "fpga.toml").read_text()
        self.assertIn("# [programmer]", after); self.assertIn("[board]", after)
        self.assertIn('unit = "maxv-1"', after); self.assertEqual(before.count("[pins]"), after.count("[pins]"))


if __name__ == "__main__":
    unittest.main()

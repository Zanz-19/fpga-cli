"""Puertos de un módulo nuevo y estímulos de un testbench: validación, generación y ida y vuelta con la simulación."""
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
from fpga_cli import scaffold as S  # noqa: E402
from fpga_cli import tui  # noqa: E402

HAS_IVERILOG = shutil.which("iverilog") is not None


class TestPorts(unittest.TestCase):
    def test_accepted_forms(self):
        p = S.parse_port
        self.assertEqual(p("input clk"), ("input", "wire", "", "clk"))
        self.assertEqual(p("  INPUT   [3:0]   a  "), ("input", "wire", "[3:0]", "a"))
        self.assertEqual(p("output reg [7:0] q"), ("output", "reg", "[7:0]", "q"))
        self.assertEqual(p("output wire y,"), ("output", "wire", "", "y"))
        self.assertEqual(p("in b[3:0]"), ("input", "wire", "[3:0]", "b"))          # el ancho pegado al nombre, como en el editor de pines
        self.assertEqual(p("salida [N-1:0] dato"), ("output", "wire", "[N-1:0]", "dato"))
        self.assertEqual(p("inout [ 7 : 0 ] bus"), ("inout", "wire", "[7:0]", "bus"))

    def test_each_mistake_gets_its_own_message(self):
        cases = {"": "Escribe el puerto", "clk": "no es una dirección", "foo clk": "no es una dirección", "input": "Falta el nombre",
                 "input a b": "Sobra", "input [3:0] [1:0] a": "un solo ancho", "input [3-0] a": "no es válido", "input reg a": "Solo una salida",
                 "input 1a": "no es un nombre válido", "output begin": "palabra reservada", "output q-1": "no es un nombre válido"}
        for text, msg in cases.items():
            with self.assertRaises(ValueError, msg=text) as cm:
                S.parse_port(text)
            self.assertIn(msg, str(cm.exception), text)

    def test_generated_header_reads_back_exactly(self):
        ports = [S.parse_port(t) for t in ("input clk", "input [3:0] a", "output reg [7:0] q", "output y", "inout [1:0] bus")]
        info = S.parse_module(S.make_module("alu", ports), "alu")
        self.assertTrue(info["ansi"])
        self.assertEqual([(d, w, n) for d, _, w, n in ports], info["ports"])
        self.assertEqual(S.make_module("vacio", []).count("agrega aquí tus puertos"), 1)         # sin puertos: la plantilla de siempre

    def test_format_port_round_trip(self):
        for t in ("input clk", "output reg [7:0] q", "input [3:0] a"):
            self.assertEqual(S.format_port(S.parse_port(t)), t)


class TestStimuli(unittest.TestCase):
    ins = ["clk", "rst_n", "a"]

    def test_accepted_forms(self):
        p = S.parse_stimulus
        self.assertEqual(p("#100 rst_n=1", self.ins), ("100", [("rst_n", "1")]))
        self.assertEqual(p("#50 a = 4'b1010 rst_n=0", self.ins), ("50", [("a", "4'b1010"), ("rst_n", "0")]))
        self.assertEqual(p("rst_n=1;", self.ins), ("", [("rst_n", "1")]))                # sin espera
        self.assertEqual(p("#200", self.ins), ("200", []))                                  # solo esperar
        self.assertEqual(p("#12.5 a=8'hFF", self.ins), ("12.5", [("a", "8'hFF")]))
        self.assertEqual(S.format_stimulus(p("#50 a = 3", self.ins)), "#50 a=3")

    def test_each_mistake_gets_its_own_message(self):
        cases = {"": "Escribe un estímulo", "#abc a=1": "no es un tiempo válido", "zz=1": "no es una entrada", "clk=1": "lo genera el testbench",
                 "a=": "no es una asignación", "a": "no es una asignación", "a=hola": "no es válido", "a=4'b": "no es válido", "#": "no es un tiempo válido"}
        for text, msg in cases.items():
            with self.assertRaises(ValueError, msg=text) as cm:
                S.parse_stimulus(text, self.ins)
            self.assertIn(msg, str(cm.exception), text)

    def test_testbench_without_stimuli_is_unchanged(self):
        info = {"params": [], "ansi": True, "ports": [("input", "", "clk"), ("output", "", "y")]}
        tb = S.make_testbench("m", info, "tb_m", 50, "tb_m.vcd")
        self.assertIn("// TODO: estímulos", tb); self.assertIn("#2000 $finish;", tb)
        self.assertEqual(tb, S.make_testbench("m", info, "tb_m", 50, "tb_m.vcd", []))

    def test_finish_waits_after_the_last_stimulus(self):
        info = {"params": [], "ansi": True, "ports": [("input", "", "clk"), ("input", "", "a")]}
        tb = S.make_testbench("m", info, "tb_m", 50, "x.vcd", [("500", [("a", "1")]), ("1000", [("a", "0")]), ("", []) ][:2])
        self.assertIn("#500 a = 1;", tb); self.assertIn("#1000 a = 0;", tb)
        self.assertIn("#1000 $finish;", tb)                                               # 2000 - 1500 < 1000 → al menos 1000 ns de cola
        tb = S.make_testbench("m", info, "tb_m", 50, "x.vcd", [("100", [("a", "1")])])
        self.assertIn("#1900 $finish;", tb)                                               # el total sigue siendo 2000 ns
        tb = S.make_testbench("m", info, "tb_m", 50, "x.vcd", [("", [("a", "1")]), ("300", [])])
        self.assertIn("a = 1;", tb); self.assertIn("#300;", tb)


class TestPickers(unittest.TestCase):
    def test_resolve_file_choice(self):
        files = ["src/a.v", "tb/tb_a.v", "fpga.toml"]
        self.assertEqual(tui.resolve_file_choice("2", files), ("tb/tb_a.v", None))
        self.assertEqual(tui.resolve_file_choice(" fpga.toml ", files), ("fpga.toml", None))
        self.assertIn("No hay un archivo número 4", tui.resolve_file_choice("4", files)[1])
        self.assertIn("No hay un archivo número 0", tui.resolve_file_choice("0", files)[1])
        self.assertIn("no está en la lista", tui.resolve_file_choice("../../etc/passwd", files)[1])      # nunca fuera de la lista del proyecto


class TestCliRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = dict(os.environ, FPGA_CLI_HOME=str(self.tmp / "c"), FPGA_CLI_DATA=str(self.tmp / "d"), NO_COLOR="1")
        r = self.fpga("new", "demo", "--board", "cyclone4", "--dir", str(self.tmp / "r"), "--template", "empty"); self.assertEqual(r.returncode, 0, r.stderr)
        self.d = self.tmp / "r" / "demo"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fpga(self, *a):
        return subprocess.run([sys.executable, FPGA, *a], capture_output=True, text=True, env=self.env)

    def test_add_with_ports_then_testbench_with_stimuli(self):
        r = self.fpga("-C", str(self.d), "add", "alu", "--port", "input clk", "--port", "input rst_n", "--port", "input [3:0] a", "--port", "output reg [3:0] q")
        self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("Puertos: input clk", r.stdout)
        r = self.fpga("-C", str(self.d), "new-tb", "alu", "--stim", "#0 rst_n=0", "--stim", "#100 rst_n=1 a=4'b0011")
        self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("Estímulos: #0 rst_n=0 · #100 rst_n=1 a=4'b0011", r.stdout)
        tb = (self.d / "tb" / "tb_alu.v").read_text()
        self.assertIn("alu uut (", tb); self.assertIn("#100 rst_n = 1; a = 4'b0011;", tb)

    @unittest.skipUnless(HAS_IVERILOG, "requiere iverilog")
    def test_generated_module_and_testbench_compile_and_simulate(self):
        (self.d / "src").mkdir(exist_ok=True)
        self.fpga("-C", str(self.d), "add", "inv", "--port", "input clk", "--port", "input a", "--port", "output reg y")
        (self.d / "src" / "inv.v").write_text((self.d / "src" / "inv.v").read_text().replace("endmodule", "    always @(posedge clk) y <= ~a;\nendmodule"))
        self.assertEqual(self.fpga("-C", str(self.d), "new-tb", "inv", "--stim", "#100 a=1", "--stim", "#100 a=0").returncode, 0)
        r = self.fpga("-C", str(self.d), "sim", "tb_inv")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr); self.assertIn("Simulación terminada", r.stdout)
        vcd = (self.d / "waves" / "tb_inv.vcd").read_text()
        self.assertIn("$var reg 1", vcd)

    def test_bad_input_changes_nothing_on_disk(self):
        before = (self.d / "fpga.toml").read_text()
        for args in (["add", "m", "--port", "input clk", "--port", "foo bar"], ["add", "m", "--port", "input a", "--port", "output a"]):
            r = self.fpga("-C", str(self.d), *args)
            self.assertNotEqual(r.returncode, 0); self.assertFalse((self.d / "src" / "m.v").exists())
            self.assertEqual((self.d / "fpga.toml").read_text(), before)                     # ni el módulo ni `sources` cambiaron
        self.assertEqual(self.fpga("-C", str(self.d), "add", "m", "--port", "input clk", "--port", "input a").returncode, 0)
        valid = (self.d / "fpga.toml").read_text()
        for stim in ("#10 zz=1", "clk=1", "a=hola"):
            r = self.fpga("-C", str(self.d), "new-tb", "m", "--stim", stim)
            self.assertNotEqual(r.returncode, 0, stim); self.assertFalse((self.d / "tb" / "tb_m.v").exists(), stim)
            self.assertEqual((self.d / "fpga.toml").read_text(), valid, stim)                # tampoco se registró un testbench que no existe

    def test_stimuli_need_readable_ports(self):
        (self.d / "src" / "viejo.v").write_text("module viejo(a, y);\n input a; output y;\n assign y = a;\nendmodule\n")
        toml = (self.d / "fpga.toml").read_text()
        self.assertIn('sources = ["src/demo.v"]', toml)
        (self.d / "fpga.toml").write_text(toml.replace('sources = ["src/demo.v"]', 'sources = ["src/demo.v", "src/viejo.v"]'))
        r = self.fpga("-C", str(self.d), "new-tb", "viejo", "--stim", "a=1")                 # cabecera no ANSI: no se pueden validar
        self.assertNotEqual(r.returncode, 0); self.assertIn("No pude leer los puertos", r.stdout + r.stderr)
        self.assertFalse((self.d / "tb" / "tb_viejo.v").exists())
        self.assertEqual(self.fpga("-C", str(self.d), "new-tb", "viejo").returncode, 0)       # sin --stim sigue funcionando como antes


if __name__ == "__main__":
    unittest.main()

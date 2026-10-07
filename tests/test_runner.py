"""Pruebas de la ejecución dentro del menú: salida de la terminal, registro, editor de línea y el comando real en un pty."""
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FAKEBIN = str(ROOT / "tests" / "fakebin")
from fpga_cli import runner as R  # noqa: E402
from fpga_cli import tui  # noqa: E402


class TestOutputBuffer(unittest.TestCase):
    def feed(self, *chunks):
        b = R.OutputBuffer()
        for c in chunks:
            b.feed(c if isinstance(c, bytes) else c.encode())
        return b

    def test_lines_and_partial_prompt(self):
        b = self.feed("uno\r\ndos\r\nEscribe GRABAR: ")
        self.assertEqual(b.lines, ["uno", "dos"])
        self.assertEqual(b.partial, "Escribe GRABAR:")          # la pregunta sin salto de línea se ve

    def test_carriage_return_overwrites_progress(self):
        b = self.feed("10%\r50%\r100% listo\n")
        self.assertEqual(b.lines, ["100% listo"])
        b = self.feed("abcdef\rXY\n")
        self.assertEqual(b.lines, ["XYcdef"])

    def test_ansi_sequences_are_removed_even_when_split(self):
        b = self.feed("\x1b[32m✓\x1b[0m bien\n", b"mal \x1b[3", b"1mrojo\x1b[0m\n", "\x1b]0;titulo\x07fin\n")
        self.assertEqual(b.lines, ["✓ bien", "mal rojo", "fin"])

    def test_utf8_split_between_reads(self):
        raw = "Compilación ✓\n".encode()
        b = self.feed(raw[:9], raw[9:])
        self.assertEqual(b.lines, ["Compilación ✓"])

    def test_backspace_tabs_and_control_chars(self):
        b = self.feed("ab\bc\tx\x00\x07\n")
        self.assertEqual(b.lines, ["ac      x"])

    def test_view_wraps_long_lines_and_flush_keeps_last_text(self):
        b = self.feed("x" * 25 + "\nsin salto")
        self.assertEqual(b.view(10), ["x" * 10, "x" * 10, "x" * 5, "sin salto"])
        b.flush()
        self.assertEqual(b.lines[-1], "sin salto")

    def test_old_lines_are_dropped_beyond_the_limit(self):
        b = R.OutputBuffer()
        b.feed(("l\n" * (R.MAX_LINES + 50)).encode())
        self.assertEqual(len(b.lines), R.MAX_LINES)
        self.assertEqual(b.dropped, 50)


class TestEditLine(unittest.TestCase):
    def test_typing_moving_and_deleting(self):
        t, p = "", 0
        for ch in "hola":
            t, p, ev = R.edit_line(t, p, ch); self.assertIsNone(ev)
        self.assertEqual((t, p), ("hola", 4))
        t, p, _ = R.edit_line(t, p, "\x7f"); self.assertEqual((t, p), ("hol", 3))
        import curses
        t, p, _ = R.edit_line(t, p, curses.KEY_LEFT); t, p, _ = R.edit_line(t, p, "X")
        self.assertEqual((t, p), ("hoXl", 3))
        t, p, _ = R.edit_line(t, p, "\x01"); self.assertEqual(p, 0)
        t, p, _ = R.edit_line(t, p, curses.KEY_DC); self.assertEqual(t, "oXl")
        t, p, _ = R.edit_line(t, p, "\x15"); self.assertEqual((t, p), ("", 0))
        self.assertEqual(R.edit_line("a", 1, "\n")[2], "enter"); self.assertEqual(R.edit_line("a", 1, "\x1b")[2], "esc")
        self.assertEqual(R.edit_line("", 0, "\x7f"), ("", 0, None))

    def test_accents_and_non_printable(self):
        t, p, _ = R.edit_line("", 0, "ñ"); self.assertEqual(t, "ñ")
        self.assertEqual(R.edit_line("a", 1, "\x00"), ("a", 1, None))


class TestHelpers(unittest.TestCase):
    def test_resolve_project_choice(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "fpga.toml").write_text("x")
            self.assertEqual(tui.resolve_project_choice("2", ["/a", "/b"]), (Path("/b"), None))
            self.assertIn("No hay un proyecto número 3", tui.resolve_project_choice("3", ["/a", "/b"])[1])
            self.assertIn("No hay un proyecto número 0", tui.resolve_project_choice("0", ["/a"])[1])
            self.assertEqual(tui.resolve_project_choice(f" {tmp} ", [])[0], tmp.resolve())
            self.assertIn("no es un proyecto", tui.resolve_project_choice(str(tmp / "nada"), [])[1])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_pretty_log_name_and_label(self):
        self.assertEqual(tui.pretty_log_name(Path("20261006-153012-compilar.log")), "2026-10-06 15:30:12 · compilar")
        self.assertEqual(tui.pretty_log_name(Path("raro.log")), "raro.log")
        self.assertEqual(R.safe_label("Agregar módulo"), "agregar-modulo")
        self.assertEqual(R.safe_label("Instalar herramientas"), "instalar-herramientas")


class TestLogFile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_log_has_header_lines_and_exit_code(self):
        log = R.LogFile(self.tmp, "Compilar", ["-C", "x", "build"])
        log.write_lines(["uno", "dos"]); log.close(3)
        text = log.path.read_text(encoding="utf-8")
        self.assertIn("# Compilar", text); self.assertIn("# fpga -C x build", text)
        self.assertIn("uno\ndos\n", text); self.assertIn("# terminó: código 3", text)
        self.assertEqual(log.path.parent, self.tmp / ".fpga" / "logs")

    def test_same_second_never_overwrites_a_log(self):
        paths = set()
        for _ in range(12):
            log = R.LogFile(self.tmp, "Simular", []); paths.add(log.path); log.close(0)
        self.assertEqual(len(paths), 12)
        newest = R.list_logs(self.tmp, 1)[0]
        self.assertTrue(newest.name.endswith("-12.log"), newest.name)          # -12 es más nuevo que -2 aunque ordene antes por texto

    def test_old_logs_are_pruned_keeping_the_newest(self):
        d = self.tmp / ".fpga" / "logs"; d.mkdir(parents=True)
        for i in range(R.KEEP_LOGS + 8):
            f = d / f"20260101-{i // 3600:02d}{i // 60 % 60:02d}{i % 60:02d}-prueba.log"; f.write_text("x")
            os.utime(f, (1_700_000_000 + i, 1_700_000_000 + i))
        R.LogFile(self.tmp, "Nuevo", []).close(0)
        left = sorted(p.name for p in d.glob("*.log"))
        self.assertEqual(len(left), R.KEEP_LOGS)
        self.assertTrue(any("nuevo" in n for n in left))                      # el recién creado nunca se borra
        self.assertEqual(len(R.list_logs(self.tmp, 5)), 5)
        self.assertEqual(R.list_logs(self.tmp / "no_existe"), [])

    def test_without_a_project_logs_go_to_the_config_folder(self):
        self.assertEqual(R.log_dir(None), R.C.CONFIG_DIR / "logs")


class TestJob(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._env = dict(os.environ)
        os.environ.update(FPGA_CLI_HOME=str(self.tmp / "cfg"), FPGA_CLI_DATA=str(self.tmp / "data"), FPGA_CLI_TOOLS=str(self.tmp / "tools"),
                          PATH=FAKEBIN + os.pathsep + os.environ["PATH"])

    def tearDown(self):
        os.environ.clear(); os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wait(self, job, timeout=15):
        end = time.time() + timeout
        while not job.done and time.time() < end:
            job.poll(); time.sleep(0.03)
        return job.done

    def test_runs_a_command_and_logs_it(self):
        job = R.Job(["boards"], "Tarjetas", self.tmp)
        self.assertTrue(self.wait(job))
        self.assertEqual(job.rc, 0)
        self.assertTrue(any("MAX V" in l for l in job.buf.lines))
        self.assertIn("# terminó: código 0", job.log.path.read_text(encoding="utf-8"))

    def test_nonzero_exit_code_is_reported(self):
        job = R.Job(["-C", str(self.tmp / "no_hay"), "sim"], "Simular", self.tmp)
        self.assertTrue(self.wait(job))
        self.assertNotEqual(job.rc, 0)
        self.assertIn(f"código {job.rc}", job.log.path.read_text(encoding="utf-8"))

    def test_prompt_is_answered_only_by_what_is_sent(self):
        os.environ["FPGA_CLI_ASCII"] = "1"
        job = R.Job(["counter", "--set", "5", "--unit", "u1"], "Contador", None)
        end = time.time() + 10
        while "CONFIRMO" not in job.buf.partial and time.time() < end:
            job.poll(); time.sleep(0.03)
        self.assertIn("Escribe CONFIRMO", job.buf.partial)       # la pregunta está pendiente: nada la contestó solo
        self.assertFalse(job.done)
        job.send_line("no"); self.assertTrue(self.wait(job))
        from fpga_cli import prog as P
        self.assertEqual(P.counter_get("u1")["count"], 0)         # contestó otra cosa: no se fijó nada

    def test_interrupt_stops_a_long_command(self):
        slow = self.tmp / "bin"; slow.mkdir()
        (slow / "quartus_sh").write_text("#!/bin/bash\necho empezando\nsleep 60\n"); (slow / "quartus_sh").chmod(0o755)
        os.environ["PATH"] = str(slow) + os.pathsep + os.environ["PATH"]
        from fpga_cli import cli
        cli.main(["new", "lento", "--board", "maxv", "--dir", str(self.tmp / "r"), "--template", "blink"])
        job = R.Job(["-C", str(self.tmp / "r" / "lento"), "project"], "Proyecto", self.tmp / "r" / "lento")
        end = time.time() + 10
        while not any("empezando" in l for l in job.buf.view(80)) and time.time() < end:
            job.poll(); time.sleep(0.03)
        self.assertFalse(job.done)
        job.interrupt(); self.assertTrue(self.wait(job, 10))
        self.assertTrue(job.interrupted)
        self.assertIn("interrumpido", job.log.path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

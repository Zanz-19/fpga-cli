"""Pruebas del menú: la lógica sin pantalla y el menú real dentro de una terminal virtual (pty)."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
FPGA = str(ROOT / "fpga")
FAKEBIN = str(ROOT / "tests" / "fakebin")
from fpga_cli import tui  # noqa: E402
from ptyharness import PtySession  # noqa: E402


class TestLogic(unittest.TestCase):
    def test_parse_pin_line(self):
        p = tui.parse_pin_line
        self.assertEqual(p(""), ("empty",))
        self.assertEqual(p("   "), ("empty",))
        self.assertEqual(p("led led0"), ("assign", "led", ["led0"]))
        self.assertEqual(p("led[3:0] led3 led2 led1 led0"), ("assign", "led[3:0]", ["led3", "led2", "led1", "led0"]))
        self.assertEqual(p("quitar led"), ("remove", "led"))
        self.assertEqual(p("rm led"), ("remove", "led"))
        self.assertEqual(p("salir"), ("exit",)); self.assertEqual(p("q"), ("exit",))
        self.assertEqual(p("?led"), ("list", "led")); self.assertEqual(p("?"), ("list", ""))
        self.assertEqual(p("ayuda key"), ("list", "key"))
        self.assertEqual(p("solo_puerto")[0], "error"); self.assertEqual(p("quitar")[0], "error")

    def test_board_signals_filter(self):
        from fpga_cli import core
        board = core.load_board("maxv")
        leds = tui.board_signals(board, "LED")
        self.assertEqual(len(leds), 10); self.assertIn("led0 PIN_72", leds)
        self.assertGreater(len(tui.board_signals(board)), 60)
        self.assertEqual(tui.board_signals(board, "no_existe"), [])

    def test_strip_ansi_and_plural(self):
        self.assertEqual(tui.strip_ansi("\x1b[32m✓\x1b[0m listo"), "✓ listo")
        self.assertEqual(tui.plural(1, "fuente", "fuentes"), "1 fuente"); self.assertEqual(tui.plural(3, "pin", "pines"), "3 pines")

    def test_suggested_next_step_follows_the_workflow(self):
        s = tui.suggest_action
        self.assertEqual(s(None), "new")
        self.assertEqual(s({"simulated": False, "built": False}), "sim")
        self.assertEqual(s({"simulated": True, "built": False}), "build")
        self.assertEqual(s({"simulated": True, "built": True}), "prog")
        self.assertEqual(s({"simulated": False, "built": True}), "sim")      # simular siempre va antes de programar

    def test_project_info_missing_or_broken(self):
        self.assertIsNone(tui.project_info(None))
        tmp = Path(tempfile.mkdtemp())
        try:
            self.assertIsNone(tui.project_info(tmp))
            (tmp / "fpga.toml").write_text("esto no es toml [[")
            self.assertIsNone(tui.project_info(tmp))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = dict(os.environ, FPGA_CLI_HOME=str(self.tmp / "cfg"), FPGA_CLI_DATA=str(self.tmp / "data"),
                        FPGA_CLI_TOOLS=str(self.tmp / "tools"), FAKE_LOG=str(self.tmp / "calls.log"),
                        PATH=FAKEBIN + os.pathsep + os.environ["PATH"], NO_COLOR="1", FPGA_NO_SPLASH="1")
        self.repos = self.tmp / "repos"
        self.sessions = []

    def tearDown(self):
        for s in self.sessions:
            s.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *args):
        r = subprocess.run([sys.executable, FPGA, *args], capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)

    def project(self, name="demo", board="maxv"):
        self.cli("new", name, "--board", board, "--dir", str(self.repos), "--template", "blink")
        return self.repos / name

    def open_menu(self, *extra, rows=32, cols=100):
        s = PtySession([sys.executable, FPGA, *extra], self.env, rows=rows, cols=cols)
        self.sessions.append(s)
        return s

    def calls(self):
        p = self.tmp / "calls.log"
        return p.read_text().splitlines() if p.exists() else []


class TestMenu(Base):
    def test_opens_the_project_of_the_current_folder_not_the_last_used(self):
        uno = self.project("uno"); self.project("dos")            # el último creado (dos) es el «último usado»
        s = PtySession([sys.executable, FPGA], self.env, cwd=str(uno)); self.sessions.append(s)
        self.assertTrue(s.wait_for("Salir"))
        self.assertIn("uno", s.buffer); self.assertNotIn("dos", s.buffer)
        s.send("x"); self.assertEqual(s.wait_exit(), 0)
        s2 = PtySession([sys.executable, FPGA], self.env, cwd=str(self.tmp)); self.sessions.append(s2)   # fuera de un proyecto: el último usado
        self.assertTrue(s2.wait_for("Salir")); self.assertIn("dos", s2.buffer)
        s2.send("x"); s2.wait_exit()

    def test_renders_and_exits_cleanly(self):
        self.project()
        s = self.open_menu()
        self.assertTrue(s.wait_for("Salir"), s.buffer[-300:])
        for text in ("fpga-cli", "PROYECTO", "ENTORNO", "ACCIONES", "demo", "Nuevo proyecto", "Compilar", "Programar la tarjeta"):
            self.assertIn(text, s.buffer)
        s.send("x")
        self.assertEqual(s.wait_exit(), 0)

    def test_maxv_flash_counter_is_on_the_panel(self):
        self.project()
        s = self.open_menu(); s.wait_for("Salir")
        self.assertIn("0 de 100 cargas", s.buffer)
        s.send("x"); s.wait_exit()

    def test_project_actions_are_blocked_without_a_project(self):
        s = self.open_menu(); s.wait_for("Salir")
        self.assertIn("Sin proyecto abierto", s.buffer)
        s.send("s")
        self.assertTrue(s.wait_for("Primero abre o crea un proyecto"))
        s.send("x"); self.assertEqual(s.wait_exit(), 0)

    def test_everything_fits_in_a_default_80x24_terminal(self):
        self.project()
        s = self.open_menu(rows=24, cols=80); s.wait_for("Salir")
        for text in ("Nuevo proyecto", "Iniciar con mis .v", "Abrir otro proyecto", "Agregar módulo", "Nuevo testbench", "Asignar pines",
                     "Simular", "Ver ondas", "Compilar", "Programar la tarjeta", "Convertir para flash", "Contador de cargas", "Diagnóstico",
                     "Instalar Quartus", "Instalar simulación", "Ver registros", "Editar con nano", "Salir"):
            self.assertIn(text, s.buffer)
        self.assertNotIn("Agranda", s.buffer)
        s.send("x"); self.assertEqual(s.wait_exit(), 0)

    def test_cursor_starts_on_the_suggested_next_step(self):
        self.project()                                   # recién creado: sin simular
        s = self.open_menu(); s.wait_for("Salir")
        self.assertIn("Simula con iverilog", s.buffer)           # ayuda de «Simular», no la de «Nuevo proyecto»
        self.assertNotIn("Crea una carpeta de trabajo nueva", s.buffer)
        s.send("x"); s.wait_exit()

    def test_convert_for_flash_from_the_menu_never_touches_the_board(self):
        d = self.project("cy", "cyclone4")
        self.cli("-C", str(d), "build")
        s = self.open_menu(); s.wait_for("Salir")
        s.send("f"); self.assertTrue(s.wait_for("vuelven al menú", 15))
        self.assertIn("Conversión lista", s.buffer); self.assertIn("No se tocó la tarjeta", s.buffer)
        log = self.calls()
        self.assertTrue(any(c.startswith("quartus_cpf") for c in log))
        self.assertFalse(any(c.startswith("quartus_pgm") for c in log))
        self.assertTrue((d / "build" / "output_files" / "cy.jic").exists())
        s.buffer = ""; s.send("\r"); self.assertTrue(s.wait_for("Siguiente paso sugerido"))
        s.send("x"); self.assertEqual(s.wait_exit(), 0)

    def test_convert_for_flash_is_refused_on_the_maxv(self):
        self.project("mx", "maxv")
        s = self.open_menu(); s.wait_for("Salir")
        s.send("f"); self.assertTrue(s.wait_for("solo a la Cyclone IV"))
        self.assertNotIn("vuelven al menú", s.buffer)
        self.assertEqual(self.calls(), [])
        s.send("x"); s.wait_exit()

    def test_regenerating_a_testbench_asks_before_overwriting(self):
        d = self.project("rg", "cyclone4")
        self.cli("-C", str(d), "add", "otro")
        self.cli("-C", str(d), "new-tb", "otro")
        mine = d / "tb" / "tb_otro.v"; mine.write_text(mine.read_text() + "// MI TRABAJO\n")
        s = self.open_menu(); s.wait_for("Salir")
        s.send("t"); self.assertTrue(s.wait_for("Módulo a probar"))
        s.send("otro\r"); self.assertTrue(s.wait_for("¿Regenerarlo?"))
        self.assertIn("BORRA lo que le hayas escrito", s.buffer)
        s.send("n\r"); self.assertTrue(s.wait_for("cancelado, no se tocó nada"))
        self.assertIn("// MI TRABAJO", mine.read_text())                      # dijo que no: intacto
        s.send("t"); s.wait_for("Módulo a probar"); s.send("otro\r"); s.wait_for("¿Regenerarlo?")
        s.send("s\r"); self.assertTrue(s.wait_for("Estímulo 1"))             # el asistente de estímulos; vacío = sin estímulos
        s.send("\r"); self.assertTrue(s.wait_for("vuelven al menú"))
        self.assertNotIn("// MI TRABAJO", mine.read_text())                   # dijo que sí: regenerado
        s.buffer = ""; s.send("\r"); s.wait_for("ACCIONES"); s.send("x"); s.wait_exit()

    def test_prog_menu_offers_convert_only_as_option_3(self):
        d = self.project("cy", "cyclone4")
        self.cli("-C", str(d), "build")
        s = PtySession([sys.executable, FPGA, "-C", str(d), "prog"], self.env); self.sessions.append(s)
        self.assertTrue(s.wait_for("Elige 1, 2 o 3"))
        self.assertIn("SOBRESCRIBE", s.buffer); self.assertIn("Solo convertir", s.buffer); self.assertIn("por JTAG", s.buffer)
        s.send("3\r"); self.assertTrue(s.wait_for("Conversión lista"))
        self.assertEqual(s.wait_exit(), 0)
        log = self.calls()
        self.assertTrue(any(c.startswith("quartus_cpf") for c in log)); self.assertFalse(any(c.startswith("quartus_pgm") for c in log))

    def test_choosing_flash_warns_that_it_overwrites_and_can_be_declined(self):
        d = self.project("cy", "cyclone4")
        self.cli("-C", str(d), "build")
        s = PtySession([sys.executable, FPGA, "-C", str(d), "prog"], self.env); self.sessions.append(s)
        s.wait_for("Elige 1, 2 o 3"); s.send("2\r")
        self.assertTrue(s.wait_for("¿Convertir y grabar la flash ahora?"))
        self.assertIn("SOBRESCRIBE lo que tenga", s.buffer)
        s.send("n\r"); self.assertNotEqual(s.wait_exit(), 0)
        log = self.calls()
        self.assertFalse(any(c.startswith(("quartus_cpf", "quartus_pgm")) for c in log))

    def test_too_small_terminal_asks_to_enlarge(self):
        s = self.open_menu(rows=12, cols=50)
        self.assertTrue(s.wait_for("Agranda la terminal"))
        s.send("x"); self.assertEqual(s.wait_exit(), 0)

    def test_arrows_sent_as_plain_csi_never_trigger_actions(self):
        self.project()
        s = self.open_menu(); s.wait_for("Salir")
        for seq in ("\x1b[B", "\x1b[C", "\x1b[A", "\x1b[H"):
            s.send(seq); s.pump(0.4)
        self.assertNotIn("vuelven al menú", s.buffer)
        self.assertEqual(self.calls(), [])
        s.send("x"); self.assertEqual(s.wait_exit(), 0)

    def test_application_mode_arrows_move_the_selection(self):
        self.project()
        s = self.open_menu(); s.wait_for("Salir")
        s.buffer = ""
        s.send("\x1bOB"); s.send("\x1bOB"); s.pump(0.6)
        # el cursor arranca en «Simular»; dos pasos abajo: Ver ondas → Compilar, y la ayuda sigue a la opción elegida
        self.assertIn("Genera el proyecto de Quartus", s.buffer)
        s.send("x"); s.wait_exit()

    def test_running_a_command_returns_to_the_menu(self):
        self.project()
        s = self.open_menu(); s.wait_for("Salir")
        s.send("d")
        self.assertTrue(s.wait_for("vuelven al menú"))
        self.assertIn("fpga-cli 0.1.0", s.buffer)
        s.buffer = ""; s.send("\r")
        self.assertTrue(s.wait_for("ACCIONES"))
        s.send("x"); self.assertEqual(s.wait_exit(), 0)

    def test_build_from_the_menu_runs_the_same_flow(self):
        d = self.project("cy", "cyclone4")
        s = self.open_menu(); s.wait_for("Salir")
        s.send("b"); self.assertTrue(s.wait_for("vuelven al menú", 20))
        self.assertTrue(any("--flow compile cy" in c for c in self.calls()))
        s.buffer = ""; s.send("\r"); self.assertTrue(s.wait_for("compilado"))
        s.send("x"); s.wait_exit()
        self.assertTrue((d / "build" / "output_files" / "cy.sof").exists())

    def test_new_project_from_the_menu_then_open_another(self):
        self.repos.mkdir()
        s = self.open_menu(); s.wait_for("Salir")
        s.send("n"); self.assertTrue(s.wait_for("Nombre del proyecto"))
        s.send("desde-menu\r"); self.assertTrue(s.wait_for("Elige el número"))        # a partir de aquí responde el comando, dentro de la ventana     # tarjeta: 1) cyclone4 2) maxv
        s.send("2\r"); self.assertTrue(s.wait_for("Elige 1 o 2"))
        s.send("2\r"); self.assertTrue(s.wait_for("Ruta de la carpeta"))
        s.send(f"{self.repos}\r"); self.assertTrue(s.wait_for("vuelven al menú", 15))
        s.buffer = ""; s.send("\r"); self.assertTrue(s.wait_for("desde_menu"))     # el panel ya muestra el proyecto nuevo
        self.assertIn("MAX V", s.buffer)
        self.assertIn("module desde_menu", (self.repos / "desde-menu" / "src" / "desde_menu.v").read_text())   # plantilla vacía
        s.send("x"); self.assertEqual(s.wait_exit(), 0)

    def test_open_other_project_by_path(self):
        d = self.project("otro")
        s = self.open_menu(); s.wait_for("Salir")
        self.assertIn("otro", s.buffer)                          # el último creado se abre solo
        d2 = self.project("segundo")
        s.send("o"); self.assertTrue(s.wait_for("Número de la lista o ruta"))
        s.send(f"{d2}\r"); self.assertTrue(s.wait_for("Proyecto abierto"))
        self.assertTrue(s.wait_for("segundo"))
        s.send("x"); s.wait_exit()


class TestWindow(Base):
    """Las acciones corren en una ventana dentro del menú: salida con desplazamiento, respuestas escritas, registro."""

    def fake(self, name, body):
        d = self.tmp / "fake2"; d.mkdir(exist_ok=True)
        f = d / name; f.write_text("#!/bin/bash\n" + body); f.chmod(0o755)
        self.env["PATH"] = str(d) + os.pathsep + self.env["PATH"]

    def logs(self, d):
        from fpga_cli import runner
        return list(reversed(runner.list_logs(d, 100)))               # del más viejo al más nuevo

    def test_the_command_runs_inside_the_menu_and_leaves_a_log(self):
        d = self.project("cy", "cyclone4")
        s = self.open_menu(); s.wait_for("Salir")
        s.send("b"); self.assertTrue(s.wait_for("vuelven al menú", 20))
        self.assertIn("Compilar · cy", s.buffer); self.assertIn("Compilación terminada", s.buffer)
        self.assertNotIn("Enter para volver", s.buffer)              # ya no se sale a la terminal
        self.assertIn(".fpga/logs/", s.buffer)
        s.send("\r"); self.assertTrue(s.wait_for("Siguiente paso sugerido"))
        s.send("x"); self.assertEqual(s.wait_exit(), 0)
        log = self.logs(d); self.assertEqual(len(log), 1)
        text = log[0].read_text(encoding="utf-8")
        self.assertIn("Compilación terminada", text); self.assertIn("# terminó: código 0", text)
        self.assertIn("quartus_sh --flow compile cy", text)

    def test_a_failing_command_shows_its_code_and_is_logged(self):
        self.fake("quartus_sh", "echo 'Error: fitter falló'; exit 3")
        d = self.project("cy", "cyclone4")
        s = self.open_menu(); s.wait_for("Salir")
        s.send("b"); self.assertTrue(s.wait_for("error (código 3)", 20))
        s.send("\r"); self.assertTrue(s.wait_for("terminó con error (código 3)"))
        s.send("x"); s.wait_exit()
        self.assertIn("# terminó: código 3", self.logs(d)[0].read_text(encoding="utf-8"))

    def test_maxv_programming_needs_GRABAR_typed_inside_the_window(self):
        d = self.project("mx", "maxv")
        self.cli("-C", str(d), "build")
        s = self.open_menu(); s.wait_for("Salir")
        s.send("g"); self.assertTrue(s.wait_for("Escribe GRABAR"))
        self.assertIn("responder", s.buffer); self.assertEqual([c for c in self.calls() if c.startswith("quartus_pgm")], [])
        s.send("grabar\r"); self.assertTrue(s.wait_for("Cancelado"))          # en minúsculas no vale
        self.assertEqual([c for c in self.calls() if c.startswith("quartus_pgm")], [])
        s.buffer = ""; s.send("\r"); s.wait_for("terminó con error")
        s.buffer = ""; s.send("g"); s.wait_for("Escribe GRABAR"); s.buffer = ""; s.send("\r"); self.assertTrue(s.wait_for("Cancelado"))    # Enter solo cancela
        self.assertEqual([c for c in self.calls() if c.startswith("quartus_pgm")], [])
        s.buffer = ""; s.send("\r"); s.wait_for("terminó con error")
        s.buffer = ""; s.send("g"); s.wait_for("Escribe GRABAR"); s.pump(0.3); s.buffer = ""
        s.send("GRABAR\r"); self.assertTrue(s.wait_for("Contador de 'maxv-1': 1 de 100"))
        self.assertEqual(len([c for c in self.calls() if c.startswith("quartus_pgm")]), 1)
        s.send("\r"); s.wait_for("ACCIONES"); s.send("x"); s.wait_exit()
        self.assertEqual(R_counter(self.env, "maxv-1"), 1)
        confirm = [l for l in self.logs(d)[-1].read_text(encoding="utf-8").splitlines() if "Escribe GRABAR" in l]
        self.assertTrue(confirm and confirm[0].rstrip().endswith("GRABAR"))     # el registro deja constancia de lo que se escribió

    def test_keys_typed_before_the_window_opens_never_answer_a_confirmation(self):
        d = self.project("mx", "maxv")
        self.cli("-C", str(d), "build")
        s = self.open_menu(); s.wait_for("Salir")
        s.send("gGRABAR\r")                                                    # todo junto, antes de que exista la ventana
        self.assertTrue(s.wait_for("Escribe GRABAR")); s.pump(0.8)
        self.assertEqual([c for c in self.calls() if c.startswith("quartus_pgm")], [])
        s.send("\x03"); s.wait_for("interrumpido")                            # se sale sin grabar
        s.send("\r"); s.wait_for("ACCIONES"); s.send("x"); s.wait_exit()
        self.assertEqual(R_counter(self.env, "maxv-1"), 0)

    def test_ctrl_c_interrupts_a_running_command_and_the_menu_stays_usable(self):
        self.fake("quartus_sh", "echo empezando; sleep 60")
        d = self.project("cy", "cyclone4")
        s = self.open_menu(); s.wait_for("Salir")
        s.send("b"); self.assertTrue(s.wait_for("empezando", 15)); self.assertIn("en curso", s.buffer)
        s.send("\x1b"); self.assertTrue(s.wait_for("sigue en marcha"))          # Esc no abandona un comando vivo
        s.send("\x03"); self.assertTrue(s.wait_for("interrumpido", 10))
        s.buffer = ""; s.send("\r"); self.assertTrue(s.wait_for("ACCIONES")); self.assertTrue(s.wait_for("interrumpido"))
        s.send("x"); self.assertEqual(s.wait_exit(), 0)
        self.assertIn("# terminó: interrumpido", self.logs(d)[0].read_text(encoding="utf-8"))

    def test_long_output_scrolls_inside_the_window(self):
        self.fake("quartus_sh", "for i in $(seq 1 120); do echo \"linea numero $i\"; done")
        self.project("cy", "cyclone4")
        s = self.open_menu(rows=24, cols=80); s.wait_for("Salir")
        s.send("b"); self.assertTrue(s.wait_for("vuelven al menú", 20))
        pantalla = s.screen()
        self.assertIn("linea numero 120", pantalla); self.assertNotIn("linea numero 1\n", pantalla)
        s.send("\x1b[5~"); s.send("\x1b[5~"); s.pump(0.5)                   # RePág dos veces
        pantalla = s.screen()
        self.assertNotIn("linea numero 120", pantalla); self.assertIn("líneas más arriba", pantalla)
        s.send("\x1bOF"); s.pump(0.4)                                        # Fin: vuelve al final
        self.assertIn("linea numero 120", s.screen())
        s.send("\x1bOH"); s.pump(0.4)                                        # Inicio
        self.assertIn("$ quartus_sh", s.screen())
        s.send("\r"); s.wait_for("ACCIONES"); s.send("x"); s.wait_exit()

    def test_data_entry_dialogs_are_typed_and_escape_cancels(self):
        d = self.project("cy", "cyclone4")
        s = self.open_menu(); s.wait_for("Salir")
        s.send("a"); self.assertTrue(s.wait_for("Nombre del módulo nuevo"))
        s.send("\x1b"); self.assertTrue(s.wait_for("cancelado, no se tocó nada"))
        self.assertFalse((d / "src" / "contador.v").exists())
        s.send("a"); s.wait_for("Nombre del módulo nuevo"); s.send("contadox\x7fr\r")   # con un borrado de por medio
        self.assertTrue(s.wait_for("Puerto 1")); s.send("\r")                  # sin puertos: queda la plantilla
        self.assertTrue(s.wait_for("vuelven al menú", 10))
        self.assertTrue((d / "src" / "contador.v").exists())
        self.assertFalse((d / "src" / "contadox.v").exists())
        s.send("\r"); s.wait_for("ACCIONES"); s.send("x"); s.wait_exit()

    def test_view_logs_lists_and_shows_a_past_run(self):
        d = self.project("cy", "cyclone4")
        s = self.open_menu(); s.wait_for("Salir")
        s.send("l"); self.assertTrue(s.wait_for("Todavía no hay registros"))
        s.send("b"); s.wait_for("vuelven al menú", 20); s.send("\r"); s.wait_for("Siguiente paso sugerido")
        s.send("l"); self.assertTrue(s.wait_for("Número del registro"))
        self.assertIn("compilar", s.buffer)
        s.buffer = ""; s.send("\r"); self.assertTrue(s.wait_for("registro guardado"))
        self.assertIn("Compilación terminada", s.buffer); self.assertIn("terminó: código 0", s.buffer)
        s.send("\x1b"); self.assertTrue(s.wait_for("ACCIONES"))
        s.send("x"); self.assertEqual(s.wait_exit(), 0)

    def test_waves_open_in_their_own_window_and_the_menu_stays_free(self):
        self.fake("gtkwave", 'echo "gtkwave $*" >> "$FAKE_LOG"; sleep 4')
        d = self.project("cy", "cyclone4")
        (d / "waves").mkdir(exist_ok=True); (d / "waves" / "dump.vcd").write_text("$date x $end\n")
        s = self.open_menu(); s.wait_for("Salir")
        t0 = __import__("time").time()
        s.send("w"); self.assertTrue(s.wait_for("vuelven al menú", 6))
        self.assertLess(__import__("time").time() - t0, 3.5)                     # no esperó a que se cerrara GTKWave
        self.assertIn("Puedes seguir usando el menú", s.buffer)
        self.assertTrue(any(c.startswith("gtkwave") for c in self.calls()))
        s.send("\r"); s.wait_for("ACCIONES"); s.send("x"); self.assertEqual(s.wait_exit(), 0)

    def test_new_project_adopts_the_project_created_in_the_window(self):
        self.repos.mkdir()
        s = self.open_menu(); s.wait_for("Salir")
        s.send("n"); s.wait_for("Nombre del proyecto"); s.send("nuevo\r")
        s.wait_for("Elige el número"); s.send("1\r"); s.wait_for("Elige 1 o 2"); s.send("1\r")
        s.wait_for("Ruta de la carpeta"); s.send(f"{self.repos}\r"); self.assertTrue(s.wait_for("vuelven al menú", 15))
        s.buffer = ""; s.send("\r"); self.assertTrue(s.wait_for("Cyclone IV"))
        self.assertTrue((self.repos / "nuevo" / "fpga.toml").exists())
        s.send("x"); self.assertEqual(s.wait_exit(), 0)


def R_counter(env, unit):
    r = subprocess.run([sys.executable, FPGA, "counter"], capture_output=True, text=True, env=env)
    import re
    m = re.search(rf"{re.escape(unit)}: (\d+) cargas", r.stdout)
    return int(m.group(1)) if m else 0


class TestAssistants(Base):
    """Punto 2: nano desde el menú y los asistentes que se escriben (puertos de un módulo, estímulos de un testbench)."""

    def fake(self, name, body):
        d = self.tmp / "fake2"; d.mkdir(exist_ok=True)
        f = d / name; f.write_text("#!/bin/bash\n" + body); f.chmod(0o755)
        self.env["PATH"] = str(d) + os.pathsep + self.env["PATH"]

    def test_nano_opens_the_chosen_file_and_the_menu_comes_back(self):
        self.fake("nano", 'echo "nano $*" >> "$FAKE_LOG"; echo "// editado en nano" >> "$1"')
        d = self.project("cy", "cyclone4")
        s = self.open_menu(); s.wait_for("Salir")
        s.send("e"); self.assertTrue(s.wait_for("Número del archivo"))
        self.assertIn("1) src/cy.v", s.buffer); self.assertIn("fpga.toml", s.buffer)
        s.buffer = ""; s.send("\r")                                              # Enter = el primero de la lista
        self.assertTrue(s.wait_for("Guardaste cambios en src/cy.v"))
        self.assertIn("Siguiente paso sugerido: Simular", s.buffer)
        self.assertIn("ACCIONES", s.buffer)                                       # el menú se repintó entero
        self.assertIn("// editado en nano", (d / "src" / "cy.v").read_text())
        self.assertEqual([c for c in self.calls() if c.startswith("nano")], [f"nano {d / 'src' / 'cy.v'}"])
        s.send("x"); self.assertEqual(s.wait_exit(), 0)

    def test_editing_a_source_invalidates_the_simulated_flag(self):
        self.fake("nano", 'echo "// otro cambio" >> "$1"')
        d = self.project("cy", "cyclone4")
        self.cli("-C", str(d), "sim"); 
        from fpga_cli import tui
        if not tui.project_info(d)["simulated"]:
            self.skipTest("este entorno no tiene iverilog")
        s = self.open_menu(); s.wait_for("Salir"); self.assertIn("✓ simulado", s.buffer)
        s.buffer = ""; s.send("e"); s.wait_for("Número del archivo"); s.send("\r"); self.assertTrue(s.wait_for("Guardaste cambios"))
        self.assertIn("✗ simulado", s.buffer)
        s.send("x"); s.wait_exit()

    def test_closing_nano_without_saving_changes_nothing(self):
        self.fake("nano", "exit 0")
        self.project("cy", "cyclone4")
        s = self.open_menu(); s.wait_for("Salir")
        s.send("e"); s.wait_for("Número del archivo"); s.send("\r"); self.assertTrue(s.wait_for("Sin cambios en src/cy.v"))
        s.send("x"); self.assertEqual(s.wait_exit(), 0)

    def test_picker_accepts_a_path_and_rejects_nonsense_without_opening_nano(self):
        self.fake("nano", 'echo "nano $*" >> "$FAKE_LOG"')
        self.project("cy", "cyclone4")
        s = self.open_menu(); s.wait_for("Salir")
        s.send("e"); s.wait_for("Número del archivo"); s.send("\x15" + "99\r"); self.assertTrue(s.wait_for("No hay un archivo número 99"))
        s.send("e"); s.wait_for("Número del archivo"); s.send("\x15" + "/etc/passwd\r"); self.assertTrue(s.wait_for("no está en la lista"))
        s.send("e"); s.wait_for("Número del archivo"); s.send("\x1b"); self.assertTrue(s.wait_for("cancelado, no se tocó nada"))
        self.assertEqual(self.calls(), [])
        s.buffer = ""; s.send("e"); s.wait_for("Número del archivo"); s.send("\x15" + "fpga.toml\r"); self.assertTrue(s.wait_for("Sin cambios en fpga.toml"))
        self.assertEqual(len(self.calls()), 1)
        s.send("x"); s.wait_exit()

    def test_missing_nano_says_how_to_install_it(self):
        self.env["PATH"] = FAKEBIN                                                # sin nano en ninguna parte
        self.project("cy", "cyclone4")
        s = self.open_menu(); s.wait_for("Salir")
        s.send("e"); s.wait_for("Número del archivo"); s.send("\r"); self.assertTrue(s.wait_for("sudo apt install nano"))
        s.send("x"); self.assertEqual(s.wait_exit(), 0)

    def test_a_broken_fpga_toml_after_nano_is_reported(self):
        self.fake("nano", 'echo "esto no es toml [[" > "$1"')
        self.project("cy", "cyclone4")
        s = self.open_menu(); s.wait_for("Salir")
        s.send("e"); s.wait_for("Número del archivo"); s.send("\x15fpga.toml\r"); self.assertTrue(s.wait_for("ya no se puede leer"))
        s.send("x"); self.assertEqual(s.wait_exit(), 0)

    def test_module_wizard_validates_each_port_as_you_type(self):
        d = self.project("cy", "cyclone4")
        s = self.open_menu(); s.wait_for("Salir")
        s.send("a"); s.wait_for("Nombre del módulo nuevo"); s.send("alu\r"); self.assertTrue(s.wait_for("Puerto 1"))
        self.assertIn("input [3:0] a", s.buffer)                                 # los ejemplos están a la vista
        s.buffer = ""; s.send("foo clk\r"); self.assertTrue(s.wait_for("no es una dirección"))
        self.assertIn("foo clk", s.buffer)                                        # lo escrito se conserva para corregirlo
        s.send("\x15input clk\r"); self.assertTrue(s.wait_for("Puerto 2"))
        s.send("input [3:0] a\r"); s.wait_for("Puerto 3")
        s.send("output reg [7:0] q\r"); s.wait_for("Puerto 4")
        s.send("output a\r"); s.wait_for("Puerto 5")                             # un nombre repetido lo rechaza la herramienta al final
        s.send("borrar\r"); s.wait_for("Puerto 4")                               # «borrar» quita el último
        s.buffer = ""; s.send("\r"); self.assertTrue(s.wait_for("vuelven al menú", 10))
        text = (d / "src" / "alu.v").read_text()
        for frag in ("input  wire       clk,", "input  wire [3:0] a,", "output reg  [7:0] q"):
            self.assertIn(frag, text)
        self.assertIn("src/alu.v", (d / "fpga.toml").read_text())
        s.send("\r"); s.wait_for("ACCIONES"); s.send("x"); s.wait_exit()

    def test_module_wizard_escape_creates_nothing(self):
        d = self.project("cy", "cyclone4")
        s = self.open_menu(); s.wait_for("Salir")
        s.send("a"); s.wait_for("Nombre del módulo nuevo"); s.send("alu\r"); s.wait_for("Puerto 1")
        s.send("input clk\r"); s.wait_for("Puerto 2"); s.buffer = ""
        s.send("\x1b"); self.assertTrue(s.wait_for("cancelado, no se tocó nada"))
        self.assertFalse((d / "src" / "alu.v").exists())
        s.send("a"); s.wait_for("Nombre del módulo nuevo"); s.send("1mal\r"); self.assertTrue(s.wait_for("identificador de Verilog"))
        s.send("x"); s.wait_exit()

    def test_testbench_wizard_writes_the_stimuli_and_the_simulation_uses_them(self):
        d = self.project("cy", "cyclone4")
        self.cli("-C", str(d), "add", "alu", "--port", "input clk", "--port", "input rst_n", "--port", "input [3:0] a", "--port", "output y")
        s = self.open_menu(); s.wait_for("Salir")
        s.send("t"); s.wait_for("Módulo a probar"); s.send("alu\r"); self.assertTrue(s.wait_for("Estímulo 1"))
        self.assertIn("Entradas del módulo: rst_n, a", s.buffer); self.assertIn("clk la genera el testbench", s.buffer)
        s.buffer = ""; s.send("#100 zz=1\r"); self.assertTrue(s.wait_for("«zz» no es una entrada"))
        s.send("\x15#100 rst_n=1\r"); s.wait_for("Estímulo 2")
        s.send("#50 a = 4'b1010\r"); s.wait_for("Estímulo 3")
        self.assertIn("#50 a=4'b1010", s.buffer)                                  # se muestra normalizado
        s.send("#10 clk=1\r"); self.assertTrue(s.wait_for("clk lo genera el testbench"))
        s.buffer = ""; s.send("\x15\r"); self.assertTrue(s.wait_for("vuelven al menú", 10))
        tb = (d / "tb" / "tb_alu.v").read_text()
        self.assertIn("#100 rst_n = 1;", tb); self.assertIn("#50 a = 4'b1010;", tb); self.assertNotIn("TODO: estímulos", tb)
        self.assertIn("tb/tb_alu.v", (d / "fpga.toml").read_text())
        s.send("\r"); s.wait_for("ACCIONES"); s.send("x"); s.wait_exit()

    def test_testbench_wizard_escape_creates_nothing(self):
        d = self.project("cy", "cyclone4")
        self.cli("-C", str(d), "add", "beta", "--port", "input clk", "--port", "input rst_n")
        s = self.open_menu(); s.wait_for("Salir")
        s.send("t"); s.wait_for("Módulo a probar"); s.send("beta\r"); s.wait_for("Estímulo 1")
        s.send("rst_n=1\r"); s.wait_for("Estímulo 2"); s.buffer = ""
        s.send("\x1b"); self.assertTrue(s.wait_for("cancelado, no se tocó nada"))
        self.assertFalse((d / "tb" / "tb_beta.v").exists())
        s.send("x"); s.wait_exit()

    def test_everything_still_fits_in_80x24_with_the_new_option(self):
        self.project("cy", "cyclone4")
        s = PtySession([sys.executable, FPGA], self.env, rows=24, cols=80); self.sessions.append(s)
        s.wait_for("Salir")
        pantalla = s.screen()
        for text in ("Agregar módulo", "Nuevo testbench", "Asignar pines", "Editar con nano", "Simular", "Convertir para flash", "Contador de cargas",
                     "Ver registros", "Salir"):
            self.assertIn(text, pantalla)
        filas = pantalla.splitlines()
        self.assertLess(max(i for i, l in enumerate(filas) if "Editar con nano" in l), next(i for i, l in enumerate(filas) if l.lstrip().startswith("─") and i > 9))
        s.send("x"); s.wait_exit()


class TestSplash(Base):
    """Logo de Pocket Labs: pantalla de inicio breve y gnomo pequeño en el encabezado."""

    def start(self, rows=24, cols=80, **extra):
        env = dict(self.env); env.pop("FPGA_NO_SPLASH", None); env.update(extra)
        s = PtySession([sys.executable, FPGA], env, rows=rows, cols=cols, term="linux"); self.sessions.append(s)
        return s

    def test_splash_shows_the_logo_and_any_key_skips_it(self):
        self.project("cy", "cyclone4")
        s = self.start()
        self.assertTrue(s.wait_for("P O C K E T"))
        pantalla = s.screen()
        self.assertIn("█", pantalla); self.assertIn("fpga-cli v0.1.0", pantalla)
        self.assertNotIn("ACCIONES", pantalla)                                    # todavía no se ve el menú
        s.send("z"); self.assertTrue(s.wait_for("ACCIONES"))                     # una tecla la salta y NO se cuela como atajo
        self.assertNotIn("Simular: listo", s.screen()); s.send("x"); self.assertEqual(s.wait_exit(), 0)

    def test_splash_ends_by_itself_and_fits_in_80x24(self):
        s = self.start()
        s.wait_for("P O C K E T")
        filas = s.screen().splitlines()
        self.assertLessEqual(len(filas), 24); self.assertLessEqual(max(len(l) for l in filas), 80)
        self.assertTrue(s.wait_for("ACCIONES", 5))                               # sin tocar nada pasa sola
        s.send("x"); self.assertEqual(s.wait_exit(), 0)

    def test_taller_terminals_get_a_bigger_logo(self):
        s24 = self.start(rows=24); s24.wait_for("P O C K E T"); a24 = sum(1 for l in s24.screen().splitlines() if "█" in l or "▀" in l or "▄" in l)
        s40 = self.start(rows=40); s40.wait_for("P O C K E T"); a40 = sum(1 for l in s40.screen().splitlines() if "█" in l or "▀" in l or "▄" in l)
        self.assertEqual(a24, 18); self.assertEqual(a40, 22)
        for s in (s24, s40):
            s.send("x"); s.wait_exit()

    def test_ascii_terminals_get_a_pure_ascii_logo(self):
        s = self.start(FPGA_CLI_ASCII="1")
        self.assertTrue(s.wait_for("P O C K E T"))
        dibujo = set("█▀▄▲▐▝▌▘•─│╭╮╰╯├┤┬┴┼●▸▎✓✗")                                # nada de bloques ni líneas de dibujo
        pantalla = s.screen(); self.assertEqual(dibujo & set(pantalla), set())
        self.assertIn("#", pantalla); s.send("z"); s.wait_for("ACCIONES")
        menu = s.screen(); self.assertEqual(dibujo & set(menu), set())            # y el encabezado tampoco
        self.assertIn("(oo)", menu); self.assertIn("Pocket Labs", menu)
        s.send("x"); s.wait_exit()

    def test_can_be_turned_off_and_the_header_keeps_a_small_gnome(self):
        s = PtySession([sys.executable, FPGA], self.env, rows=24, cols=80, term="linux"); self.sessions.append(s)
        self.assertTrue(s.wait_for("ACCIONES")); pantalla = s.screen()
        self.assertNotIn("P O C K E T", pantalla)
        self.assertIn("· Pocket Labs", pantalla); self.assertIn("▲", pantalla); self.assertIn("▐••▌", pantalla)
        s.send("x"); s.wait_exit()


class TestLogoData(unittest.TestCase):
    def test_art_is_consistent(self):
        from fpga_cli import logo
        for name, arts in (("utf", logo.ART_UTF), ("ascii", logo.ART_ASCII)):
            self.assertEqual(sorted(arts), [12, 18, 22], name)
            for rows, art in arts.items():
                self.assertEqual(len(art), rows, (name, rows))
                self.assertLessEqual(max(len(l) for l in art), 40)
                self.assertTrue(all(l.strip() for l in art[:3]), "las primeras filas (la punta del sombrero) no pueden estar vacías")
        for art in logo.ART_ASCII.values():
            self.assertTrue(all(ord(c) < 128 for l in art for c in l))
        self.assertEqual(set("".join("".join(a) for a in logo.ART_UTF.values())) - set(" █▀▄"), set())

    def test_regenerating_gives_the_same_logo(self):
        try:
            import numpy, PIL  # noqa: F401
        except ImportError:
            self.skipTest("regenerar el logo necesita Pillow y numpy")
        import subprocess, tempfile
        out = Path(tempfile.mkdtemp()) / "logo.py"
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "make_logo.py"), str(ROOT / "assets" / "pocket-labs.jpeg"), "--salida", str(out)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(out.read_text(encoding="utf-8"), (ROOT / "fpga_cli" / "logo.py").read_text(encoding="utf-8"))


class TestPinEditor(Base):
    def test_typed_assignments_errors_listing_and_escape(self):
        d = self.project()
        s = self.open_menu(); s.wait_for("Salir")
        s.send("p"); self.assertTrue(s.wait_for("ASIGNACIONES ACTUALES"))
        for line in ("led led1\r", "led nada_de_esto\r"):
            s.send(line); s.pump(0.7)
        self.assertIn("no es una señal", s.buffer)                # el error lo produce lo escrito, no la herramienta
        s.send("?led\r"); s.pump(0.7)
        self.assertIn("led0 PIN_72", s.buffer)
        s.send("led[1:0] led1 led0\r"); self.assertTrue(s.wait_for("Pines asignados"))
        s.send("quitar led[1]\r"); s.pump(0.7)
        s.buffer = ""; s.send("\x1b"); self.assertTrue(s.wait_for("ACCIONES"))   # Esc vuelve al menú
        s.send("x"); self.assertEqual(s.wait_exit(), 0)
        toml = (d / "fpga.toml").read_text()
        self.assertIn('led = "PIN_71"', toml); self.assertIn('"led[0]" = "PIN_72"', toml)
        self.assertNotIn('"led[1]"', toml)

    def test_the_editor_always_says_how_to_leave(self):
        self.project()
        s = self.open_menu(); s.wait_for("Salir")
        s.send("p"); self.assertTrue(s.wait_for("ASIGNACIONES ACTUALES"))
        self.assertIn("volver al menú", s.buffer)
        s.send("nada\r"); s.pump(0.6)                         # un error no debe quitar la pista
        s.buffer = ""; s.send("salir\r"); self.assertTrue(s.wait_for("ACCIONES"))   # y escribir «salir» sí sale
        s.send("x"); self.assertEqual(s.wait_exit(), 0)

    def test_all_19_assignments_are_visible_in_a_default_80x24_terminal(self):
        self.cli("new", "muchos", "--board", "cyclone4", "--dir", str(self.repos), "--template", "empty")     # solo trae clk
        s = PtySession([sys.executable, FPGA], self.env, rows=24, cols=80, term="linux"); self.sessions.append(s)
        s.wait_for("Salir"); s.send("p"); s.wait_for("ASIGNACIONES ACTUALES")
        for line in ("a[7:0] seg7 seg6 seg5 seg4 seg3 seg2 seg1 seg0", "b[3:0] dig4 dig3 dig2 dig1", "c[3:0] led4 led3 led2 led1", "r reset", "k key1"):
            s.send(line + "\r"); s.pump(0.7)
        pantalla = s.screen()                                      # lo que se ve de verdad, no el flujo de bytes
        self.assertIn("ASIGNACIONES ACTUALES (19)", pantalla)
        for puerto in ("clk", "a[7]", "a[0]", "b[3]", "b[0]", "c[3]", "c[0]", "r ", "k "):
            self.assertIn(puerto, pantalla)
        self.assertIn("volver al menú", pantalla)                  # y la pista de salida sigue visible
        s.send("salir\r"); s.wait_for("ACCIONES"); s.send("x"); s.wait_exit()

    def test_a_mistyped_line_never_touches_the_file(self):
        d = self.project()
        before = (d / "fpga.toml").read_text()
        s = self.open_menu(); s.wait_for("Salir")
        s.send("p"); s.wait_for("ASIGNACIONES")
        for line in ("solo_un_puerto\r", "led[3:0] led0\r", "x pin_que_no_existe\r"):
            s.send(line); s.pump(0.6)
        s.buffer = ""; s.send("salir\r"); self.assertTrue(s.wait_for("ACCIONES"))
        s.send("x"); s.wait_exit()
        self.assertEqual((d / "fpga.toml").read_text(), before)


if __name__ == "__main__":
    unittest.main()

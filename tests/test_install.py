"""Pruebas de `fpga install-quartus` con un instalador falso y un manifiesto de prueba."""
import hashlib
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

FAKE_RUN = """#!/bin/bash
echo "installer $*" >> "$FAKE_LOG"
while [ $# -gt 0 ]; do [ "$1" = "--installdir" ] && dir="$2"; shift; done
[ -n "$dir" ] && mkdir -p "$dir/quartus/bin" && printf '#!/bin/sh\\necho quartus_sh fake\\n' > "$dir/quartus/bin/quartus_sh" && chmod +x "$dir/quartus/bin/quartus_sh"
exit ${FAKE_INSTALL_RC:-0}
"""


class TestInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.dl = self.tmp / "dl"; self.dl.mkdir()
        self.prefix = self.tmp / "opt" / "quartus"
        run = self.dl / "Setup-fake.run"; run.write_text(FAKE_RUN)
        q1 = self.dl / "cyclone-fake.qdz"; q1.write_bytes(b"cyclone")
        q2 = self.dl / "max-fake.qdz"; q2.write_bytes(b"max")
        sha = lambda p: hashlib.sha1(p.read_bytes()).hexdigest()
        self.sha = {p.name: sha(p) for p in (run, q1, q2)}
        self.write_manifest()
        self.env = dict(os.environ, FPGA_CLI_HOME=str(self.tmp / "cfg"), FPGA_CLI_DATA=str(self.tmp / "data"),
                        FAKE_LOG=str(self.tmp / "calls.log"), NO_COLOR="1", FPGA_CLI_TOOLS=str(self.tmp / "tools"),
                        FPGA_CLI_QUARTUS_MANIFEST=str(self.tmp / "q.toml"))
        self.env.pop("QUARTUS_BIN", None)

    def write_manifest(self, min_disk=0, bad=None):
        files = [("Setup-fake.run", "installer"), ("cyclone-fake.qdz", "device"), ("max-fake.qdz", "device")]
        body = f'version = "test"\npage = "https://example.invalid/q"\nlicense = "https://example.invalid/lic"\nmin_disk_gb = {min_disk}\nprefix = "{self.prefix}"\n'
        for name, role in files:
            sha = "0" * 40 if name == bad else self.sha[name]
            body += f'\n[[files]]\nname = "{name}"\nrole = "{role}"\nsize = "1 KB"\nsha1 = "{sha}"\nwhat = "fake"\n'
        (self.tmp / "q.toml").write_text(body)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fpga(self, *args, input=""):
        return subprocess.run([sys.executable, FPGA, *args], input=input, capture_output=True, text=True, env=self.env)

    def calls(self):
        p = self.tmp / "calls.log"
        return p.read_text().splitlines() if p.exists() else []

    def test_missing_files_listed(self):
        r = self.fpga("install-quartus", "--dir", str(self.tmp), "--no-open")
        self.assertEqual(r.returncode, 2)
        self.assertIn("FALTA", r.stdout); self.assertIn("Setup-fake.run", r.stdout); self.assertIn("example.invalid/q", r.stdout)
        self.assertEqual(self.calls(), [])

    def test_sha_mismatch_blocks(self):
        self.write_manifest(bad="cyclone-fake.qdz")
        r = self.fpga("install-quartus", "--dir", str(self.dl), "--no-open", input="ACEPTO\n")
        self.assertNotEqual(r.returncode, 0); self.assertIn("SHA1 distinto", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_without_consent_nothing_runs(self):
        r = self.fpga("install-quartus", "--dir", str(self.dl), "--no-open", input="no\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_install_runs_unattended_and_registers_path(self):
        r = self.fpga("install-quartus", "--dir", str(self.dl), "--no-open", input="ACEPTO\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = "\n".join(self.calls())
        self.assertIn("--mode unattended", log); self.assertIn(f"--installdir {self.prefix}", log); self.assertIn("--accept_eula 1", log)
        cfg = json.loads((self.tmp / "cfg" / "config.json").read_text())
        self.assertEqual(cfg["quartus_bin"], str(self.prefix / "quartus" / "bin"))
        # fpga doctor lo encuentra por la configuración, sin tocar el PATH
        d = self.fpga("doctor")
        self.assertIn(str(self.prefix / "quartus" / "bin" / "quartus_sh"), d.stdout)
        # y una segunda corrida avisa que ya está
        again = self.fpga("install-quartus", "--dir", str(self.dl), "--no-open")
        self.assertIn("ya está instalado", again.stdout)

    def test_dry_run(self):
        r = self.fpga("install-quartus", "--dir", str(self.dl), "--no-open", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--installdir", r.stdout); self.assertEqual(self.calls(), [])

    def test_interactive_passes_no_flags(self):
        r = self.fpga("install-quartus", "--dir", str(self.dl), "--no-open", "--interactive", "--prefix", str(self.prefix))
        self.assertEqual(self.calls() and self.calls()[0], "installer ")  # sin banderas; el falso no crea nada
        self.assertNotEqual(r.returncode, 0)                             # no hay quartus_sh que registrar

    def test_failed_installer_reports(self):
        self.env["FAKE_INSTALL_RC"] = "7"
        r = self.fpga("install-quartus", "--dir", str(self.dl), "--no-open", input="ACEPTO\n")
        self.assertNotEqual(r.returncode, 0); self.assertIn("--interactive", r.stderr)
        self.assertFalse((self.tmp / "cfg" / "config.json").exists())

    def test_not_enough_disk(self):
        self.write_manifest(min_disk=10 ** 9)
        r = self.fpga("install-quartus", "--dir", str(self.dl), "--no-open", input="ACEPTO\n")
        self.assertNotEqual(r.returncode, 0); self.assertIn("insuficiente", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_already_installed_in_path(self):
        self.env["PATH"] = FAKEBIN + os.pathsep + self.env["PATH"]
        r = self.fpga("install-quartus", "--dir", str(self.dl), "--no-open")
        self.assertEqual(r.returncode, 0); self.assertIn("ya está instalado", r.stdout)
        self.assertEqual(self.calls(), [])


FAKE_QINST = """#!/bin/bash
echo "qinst $*" >> "$FAKE_LOG"
if [ -z "$FAKE_QINST_SKIP" ]; then
  mkdir -p "$HOME/altera_lite/25.1std/quartus/bin"
  printf '#!/bin/sh\\necho fake\\n' > "$HOME/altera_lite/25.1std/quartus/bin/quartus_sh"
  chmod +x "$HOME/altera_lite/25.1std/quartus/bin/quartus_sh"
fi
exit ${FAKE_INSTALL_RC:-0}
"""


class TestQinst(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"; self.home.mkdir()
        self.dl = self.tmp / "dl"; self.dl.mkdir()
        q = self.dl / "qinst-fake.run"; q.write_text(FAKE_QINST)
        self.sha = hashlib.sha1(q.read_bytes()).hexdigest()
        self.write_manifest()
        self.env = dict(os.environ, HOME=str(self.home), FPGA_CLI_HOME=str(self.tmp / "cfg"), FPGA_CLI_DATA=str(self.tmp / "data"),
                        FAKE_LOG=str(self.tmp / "calls.log"), NO_COLOR="1", FPGA_CLI_TOOLS=str(self.tmp / "tools"), FPGA_CLI_QUARTUS_MANIFEST=str(self.tmp / "q.toml"))
        for k in ("QUARTUS_BIN", "FAKE_QINST_SKIP", "FAKE_INSTALL_RC"):
            self.env.pop(k, None)

    def write_manifest(self, sha=None):
        (self.tmp / "q.toml").write_text(
            'version = "test"\npage = "https://example.invalid/q"\nlicense = "https://example.invalid/lic"\nmin_disk_gb = 0\n'
            f'prefix = "{self.home}/altera_lite/25.1std"\n\n'
            '[[files]]\nname = "Setup-fake.run"\nrole = "installer"\nsize = "1 KB"\nsha1 = "' + "0" * 40 + '"\nwhat = "fake"\n\n'
            f'[qinst]\nname = "qinst-fake.run"\nsize = "1 KB"\nsha1 = "{sha or self.sha}"\n')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fpga(self, *args, input=""):
        return subprocess.run([sys.executable, FPGA, *args], input=input, capture_output=True, text=True, env=self.env)

    def calls(self):
        p = self.tmp / "calls.log"
        return p.read_text().splitlines() if p.exists() else []

    def cfg(self):
        p = self.tmp / "cfg" / "config.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def test_missing_offers_both_options(self):
        (self.dl / "qinst-fake.run").unlink()
        r = self.fpga("install-quartus", "--dir", str(self.dl), "--no-open")
        self.assertEqual(r.returncode, 2)
        self.assertIn("Opción 1", r.stdout); self.assertIn("qinst-fake.run", r.stdout); self.assertIn("Opción 2", r.stdout)

    def test_qinst_runs_and_registers(self):
        r = self.fpga("install-quartus", "--dir", str(self.dl), "--no-open")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.calls(), ["qinst "])
        self.assertEqual(self.cfg()["quartus_bin"], str(self.home / "altera_lite" / "25.1std" / "quartus" / "bin"))
        self.assertIn("Cyclone IV device support", r.stdout); self.assertIn("MAX II, MAX V device support", r.stdout)
        self.assertIn("Questa", r.stdout)

    def test_qinst_sha_mismatch(self):
        self.write_manifest(sha="1" * 40)
        r = self.fpga("install-quartus", "--dir", str(self.dl), "--no-open")
        self.assertNotEqual(r.returncode, 0); self.assertIn("SHA1 distinto", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_qinst_dry_run(self):
        r = self.fpga("install-quartus", "--dir", str(self.dl), "--no-open", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr); self.assertEqual(self.calls(), [])

    def test_qinst_installed_elsewhere_points_to_register(self):
        self.env["FAKE_QINST_SKIP"] = "1"
        r = self.fpga("install-quartus", "--dir", str(self.dl), "--no-open")
        self.assertNotEqual(r.returncode, 0); self.assertIn("--register", r.stderr)
        self.assertEqual(self.cfg(), {})

    def test_register_accepts_install_dir_or_bin(self):
        base = self.tmp / "custom" / "quartus_here"
        (base / "quartus" / "bin").mkdir(parents=True)
        (base / "quartus" / "bin" / "quartus_sh").write_text("#!/bin/sh\n")
        r = self.fpga("install-quartus", "--register", str(base))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.cfg()["quartus_bin"], str(base / "quartus" / "bin"))
        self.assertNotEqual(self.fpga("install-quartus", "--register", str(self.tmp / "nada")).returncode, 0)


if __name__ == "__main__":
    unittest.main()

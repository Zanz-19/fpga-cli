"""Pruebas de `fpga install-tools` con un paquete falso servido por file://."""
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FPGA = str(ROOT / "fpga")


def make_tgz(path: Path, names=("iverilog", "vvp", "gtkwave", "yosys"), top="oss-cad-suite"):
    with tarfile.open(path, "w:gz") as tar:
        for n in names:
            data = b"#!/bin/sh\necho fake-" + n.encode() + b"\n"
            ti = tarfile.TarInfo(f"{top}/bin/{n}"); ti.size = len(data); ti.mode = 0o755
            tar.addfile(ti, io.BytesIO(data))
        v = b"OSS CAD Suite fake 2026-01-01\n"
        ti = tarfile.TarInfo(f"{top}/VERSION"); ti.size = len(v); ti.mode = 0o644
        tar.addfile(ti, io.BytesIO(v))


class TestTools(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.tgz = self.tmp / "suite.tgz"; make_tgz(self.tgz)
        self.tools = self.tmp / "tools"
        self.env = dict(os.environ, FPGA_CLI_HOME=str(self.tmp / "cfg"), FPGA_CLI_DATA=str(self.tmp / "data"),
                        FPGA_CLI_TOOLS=str(self.tools), NO_COLOR="1")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fpga(self, *args, input=""):
        return subprocess.run([sys.executable, FPGA, *args], input=input, capture_output=True, text=True, env=self.env)

    def install(self, *extra, input=""):
        return self.fpga("install-tools", "--url", self.tgz.as_uri(), *extra, input=input)

    def test_installs_and_doctor_prefers_local(self):
        r = self.install("--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for n in ("iverilog", "vvp", "gtkwave", "yosys"):
            p = self.tools / "oss-cad-suite" / "bin" / n
            self.assertTrue(p.exists() and os.access(p, os.X_OK), n)
        self.assertFalse((self.tools / ".oss-cad-suite.partial").exists())
        d = self.fpga("doctor").stdout
        self.assertIn(str(self.tools / "oss-cad-suite" / "bin" / "iverilog"), d)

    def test_second_run_reports_installed_and_force_reinstalls(self):
        self.install("--yes")
        again = self.install("--yes")
        self.assertIn("Ya están instaladas", again.stdout)
        self.assertEqual(self.install("--yes", "--force").returncode, 0)

    def test_declined_installs_nothing(self):
        r = self.install(input="n\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.tools / "oss-cad-suite").exists())

    def test_dry_run_downloads_nothing(self):
        r = self.install("--dry-run")
        self.assertEqual(r.returncode, 0)
        self.assertFalse(self.tools.exists())

    def test_unexpected_package_cleans_up(self):
        make_tgz(self.tgz, names=("otra_cosa",))
        r = self.install("--yes")
        self.assertNotEqual(r.returncode, 0); self.assertIn("estructura", r.stderr)
        self.assertFalse((self.tools / "oss-cad-suite").exists())
        self.assertFalse((self.tools / ".oss-cad-suite.partial").exists())

    def test_missing_package(self):
        self.tgz.unlink()
        r = self.install("--yes")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.tools / "oss-cad-suite").exists())

    def test_asset_url(self):
        from fpga_cli import tools
        with mock.patch("platform.machine", return_value="x86_64"):
            self.assertEqual(tools.asset_url("2026-10-05"),
                "https://github.com/YosysHQ/oss-cad-suite-build/releases/download/2026-10-05/oss-cad-suite-linux-x64-20261005.tgz")
        with mock.patch("platform.machine", return_value="aarch64"):
            self.assertIn("linux-arm64-20261005", tools.asset_url("2026-10-05"))


if __name__ == "__main__":
    unittest.main()

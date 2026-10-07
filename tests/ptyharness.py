"""Ejecuta un comando dentro de una terminal virtual (pty) para probar la TUI sin pantalla real."""
import fcntl
import os
import pty
import select
import struct
import subprocess
import termios
import time


class PtySession:
    def __init__(self, args, env, cwd=None, rows=32, cols=100, term="xterm-256color"):
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        env = dict(env, TERM=term, LANG="C.UTF-8", LC_ALL="C.UTF-8", ESCDELAY="25")
        self.proc = subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=cwd,
                                     close_fds=True, start_new_session=True,
                                     preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))   # terminal de control: ^C llega como SIGINT, como en una terminal real
        os.close(slave)
        self.buffer = ""
        self.rows, self.cols = rows, cols

    def pump(self, timeout=0.3):
        """Lee lo disponible durante `timeout` segundos y lo agrega al búfer."""
        end = time.time() + timeout
        while time.time() < end:
            r, _, _ = select.select([self.master], [], [], 0.05)
            if r:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    return
                if not data:
                    return
                self.buffer += data.decode("utf-8", errors="replace")

    def wait_for(self, text, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if text in self.buffer:
                return True
            self.pump(0.1)
        return text in self.buffer

    def send(self, text):
        os.write(self.master, text.encode())

    def wait_exit(self, timeout=10):
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return None

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        os.close(self.master)

    def screen(self, wait=0.6):
        """Texto de la pantalla tal como la vería una persona (requiere el paquete opcional `pyte`)."""
        import unittest
        try:
            import pyte
        except ImportError:
            raise unittest.SkipTest("requiere pyte (pip install pyte) para leer la pantalla")
        self.pump(wait)
        scr = pyte.Screen(self.cols, self.rows)
        pyte.Stream(scr).feed(self.buffer)
        return "\n".join(line.rstrip() for line in scr.display)

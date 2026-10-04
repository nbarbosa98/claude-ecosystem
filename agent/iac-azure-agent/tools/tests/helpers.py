"""Shared test setup. No test touches the network, Azure or GitHub: every store lives in a
temporary directory selected through IAC_AZURE_AGENT_HOME."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PLUGIN = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TOOLS = os.path.join(PLUGIN, "tools")
CONFIG_CLI = os.path.join(TOOLS, "config", "cli.py")
STATE_CLI = os.path.join(TOOLS, "state", "cli.py")
HOOK = os.path.join(PLUGIN, "hooks", "azure_guard.py")

if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

# Obviously fake identifiers.
TENANT = "00000000-0000-0000-0000-00000000000a"
SUB = "00000000-0000-0000-0000-00000000000b"


class StoreCase(unittest.TestCase):
    """A fresh store root and a fresh project (a directory containing .git) per test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.home = os.path.join(self.tmp, "store")
        self.project = os.path.join(self.tmp, "project")
        os.makedirs(os.path.join(self.project, ".git"))
        self._env = mock.patch.dict(os.environ, {"IAC_AZURE_AGENT_HOME": self.home})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def cli(self, script, *args, env=None, cwd=None):
        """Runs a CLI in a separate process. Returns (exit_code, parsed JSON)."""
        e = dict(os.environ, IAC_AZURE_AGENT_HOME=self.home)
        e.update(env or {})
        p = subprocess.run([sys.executable, script, "--project-dir", self.project] + list(args),
                           capture_output=True, text=True, env=e, cwd=cwd or self.tmp, timeout=60)
        try:
            out = json.loads(p.stdout)
        except ValueError:
            self.fail("non-JSON output (exit %d): %s %s" % (p.returncode, p.stdout, p.stderr))
        return p.returncode, out

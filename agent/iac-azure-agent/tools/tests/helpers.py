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

DISCOVERY_CLI = os.path.join(TOOLS, "discovery", "cli.py")
REPO_CLI = os.path.join(TOOLS, "repo", "cli.py")
SETUP_CLI = os.path.join(TOOLS, "setup", "cli.py")

# A complete architecture proposal (discovery/proposal.py) and a simple request profile.
ARCH = {
    "objective": "Keep application logs for 90 days.",
    "scope": "One storage account in the dev environment. Nothing else is changed.",
    "resources": [{"name": "st-logs", "type": "Microsoft.Storage/storageAccounts",
                   "purpose": "Holds application logs"}],
    "overview": "A single storage account reached through a private endpoint.",
    "dependencies": "The private endpoint depends on the storage account and the subnet.",
    "naming_tagging_region": "CAF names; tags environment, workload, owner; westeurope.",
    "identity_access": "Managed identity of the application gets Storage Blob Data Contributor.",
    "network_security": "Public network access disabled; private endpoint only.",
    "monitoring": "Diagnostic settings to the environment's Log Analytics workspace.",
    "cost": {"estimate": "Under 5 EUR per month at 50 GB, LRS."},
    "repository_changes": "infra/main.bicep, infra/modules/storage.bicep, infra/parameters/dev.bicepparam",
    "deployment_strategy": "Resource-group deployment after what-if and approval.",
    "risks": ["Private endpoint needs an existing subnet."],
    "unresolved": [],
}
PROFILE = {"kind": "new", "categories": ["storage"], "environments": ["dev"],
           "restatement": "A private storage account for application logs in dev."}

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
        self.claude_home = os.path.join(self.tmp, "claude-home")
        self._env = mock.patch.dict(os.environ, {"IAC_AZURE_AGENT_HOME": self.home,
                                                 "CLAUDE_CONFIG_DIR": self.claude_home})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def write_settings(self, rules=None, scope="project", extra=None):
        """Writes a Claude Code settings file with the given ask rules (default: the
        required ones) so the deployment gate's permission-rule check passes."""
        from setup import permissions
        if rules is None:
            rules = [v["rule"] for v in permissions.REQUIRED.values()]
        base = self.claude_home if scope == "user" else os.path.join(self.project, ".claude")
        os.makedirs(base, exist_ok=True)
        name = "settings.local.json" if scope == "local" else "settings.json"
        perms = dict({"ask": rules}, **(extra or {}))
        with open(os.path.join(base, name), "w") as f:
            json.dump({"permissions": perms}, f)

    def cli(self, script, *args, env=None, cwd=None):
        """Runs a CLI in a separate process. Returns (exit_code, parsed JSON)."""
        e = dict(os.environ, IAC_AZURE_AGENT_HOME=self.home, CLAUDE_CONFIG_DIR=self.claude_home)
        e.update(env or {})
        p = subprocess.run([sys.executable, script, "--project-dir", self.project] + list(args),
                           capture_output=True, text=True, env=e, cwd=cwd or self.tmp, timeout=60)
        try:
            out = json.loads(p.stdout)
        except ValueError:
            self.fail("non-JSON output (exit %d): %s %s" % (p.returncode, p.stdout, p.stderr))
        return p.returncode, out

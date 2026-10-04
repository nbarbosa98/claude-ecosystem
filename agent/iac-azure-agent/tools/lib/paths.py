"""Where config and state live, and how a project is identified.

Store root (first match wins):
  1. IAC_AZURE_AGENT_HOME (must be an absolute path; used by tests and by users who
     want the store elsewhere)
  2. Windows: %APPDATA%\\iac-azure-agent
  3. $XDG_CONFIG_HOME/iac-azure-agent when XDG_CONFIG_HOME is absolute
  4. ~/.config/iac-azure-agent

Per project: <root>/projects/<key>/ with config.json and requests/<id>.json.
The key is derived from the project's resolved root path (docs/decisions.md ADR-004).
The store is outside the plugin directory on purpose: ${CLAUDE_PLUGIN_ROOT} changes on
every plugin update (ADR-003).
"""
import hashlib
import os
import re

from lib.errors import InvalidInput

APP = "iac-azure-agent"
WORK_DIR = ".iac-azure-agent"      # inside the project; holds the working copy (ADR-021)
ENV_HOME = "IAC_AZURE_AGENT_HOME"


def store_root(environ=None):
    env = os.environ if environ is None else environ
    override = env.get(ENV_HOME)
    if override:
        if not os.path.isabs(override):
            raise InvalidInput("%s must be an absolute path" % ENV_HOME)
        return os.path.normpath(override)
    if os.name == "nt" and env.get("APPDATA"):
        return os.path.join(env["APPDATA"], APP)
    xdg = env.get("XDG_CONFIG_HOME")
    if xdg and os.path.isabs(xdg):
        return os.path.join(xdg, APP)
    return os.path.join(os.path.expanduser("~"), ".config", APP)


def resolve_project_root(start=None):
    """Nearest ancestor of `start` (default: cwd) that contains .git, else `start` itself."""
    here = os.path.realpath(start or os.getcwd())
    probe = here
    while True:
        if os.path.exists(os.path.join(probe, ".git")):
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            return here
        probe = parent


def project_key(project_root):
    real = os.path.realpath(project_root)
    ident = os.path.normcase(real)
    digest = hashlib.sha256(ident.encode("utf-8")).hexdigest()[:16]
    base = re.sub(r"[^A-Za-z0-9._-]", "-", os.path.basename(real) or "root")[:40]
    return "%s-%s" % (base, digest)


def project_dir(project_root, environ=None):
    return os.path.join(store_root(environ), "projects", project_key(project_root))


def work_root(project_root):
    return os.path.join(os.path.realpath(project_root), WORK_DIR)


def workspace_dir(project_root, repo):
    """The working copy of the configured repository, under the project."""
    return os.path.join(work_root(project_root), "workspace",
                        "%s--%s" % (repo["owner"], repo["name"]))

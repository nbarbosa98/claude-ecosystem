"""Working copy of the configured repository, kept under the project (ADR-021).

  <project>/.iac-azure-agent/workspace/<owner>--<name>/

Rules:
  - Local git only, plus clone and fetch. Nothing here commits or pushes (Milestone 4).
  - Never discards work: no reset, no clean, no forced checkout. A dirty tree or a branch
    that cannot fast-forward is refused and reported.
  - The clone must point at the configured repository; anything else is refused.
  - Authentication is whatever git already has (for example `gh auth setup-git`). No token
    is read, passed or stored. Prompts are disabled, so a missing credential fails fast.
"""
import hashlib
import os
import subprocess

from lib import paths, repo_id
from lib.errors import ExternalUnavailable, InvalidInput, NotFound, Refused

TIMEOUT = 180
BRANCH_PREFIX = "iac/"


def run_git(args, cwd=None, timeout=TIMEOUT):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", LC_ALL="C")
    try:
        p = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=env)
    except FileNotFoundError:
        raise ExternalUnavailable("git_missing: git is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        raise ExternalUnavailable("network: git %s did not finish within %d seconds"
                                  % (args[0], timeout))
    return p.returncode, p.stdout, p.stderr


def _remote_failure(action, err):
    low = err.lower()
    if "could not read username" in low or "authentication failed" in low or "terminal prompts disabled" in low:
        kind = ("not_authenticated: git has no credential for GitHub. Run `gh auth login` and "
                "`gh auth setup-git` yourself; never paste a token here.")
    elif "not found" in low or "does not appear to be a git repository" in low:
        kind = "not_found_or_no_access: the repository does not exist or this login cannot see it."
    elif "could not resolve" in low or "unable to access" in low or "timed out" in low or "connection" in low:
        kind = "network: GitHub could not be reached. Safe to retry."
    else:
        kind = "error: %s" % " ".join(err.split())[:300]
    return ExternalUnavailable("%s failed. %s Nothing was changed." % (action, kind))


class Workspace:
    def __init__(self, project_root, config):
        repo = (config or {}).get("repository")
        if not repo:
            raise Refused("no repository is configured for this project; run /iac-setup first")
        self.project_root = os.path.realpath(project_root)
        self.repo = repo
        self.default_branch = repo.get("default_branch")
        self.infra_root = (config or {}).get("infra_root")
        self.dir = paths.workspace_dir(self.project_root, repo)

    # ------------------------------------------------------------ basics

    def exists(self):
        return os.path.isdir(os.path.join(self.dir, ".git"))

    def _git(self, *args):
        return run_git(list(args), cwd=self.dir)

    def _ok(self, *args):
        code, out, err = self._git(*args)
        if code != 0:
            raise ExternalUnavailable("error: git %s failed: %s" % (args[0], " ".join(err.split())[:300]))
        return out

    def require(self):
        if not self.exists():
            raise NotFound("no working copy yet at %s; run workspace/cli.py clone" % self.dir)
        code, out, _ = self._git("config", "--get", "remote.origin.url")
        try:
            origin = repo_id.parse(out.strip())["slug"]
        except InvalidInput:
            origin = None
        if origin != self.repo["slug"]:
            raise Refused("the working copy at %s points at %r, not the configured repository "
                          "%s. It was left untouched; move it away or fix the configuration."
                          % (self.dir, out.strip(), self.repo["slug"]))

    def infra_dir(self):
        if not self.infra_root:
            raise Refused("infra_root is not configured; run /iac-setup")
        return os.path.join(self.dir, *self.infra_root.split("/"))

    def _protect_from_project_git(self):
        """Keeps the project's own repository from picking up the working copy."""
        root = paths.work_root(self.project_root)
        os.makedirs(root, exist_ok=True)
        ignore = os.path.join(root, ".gitignore")
        if not os.path.exists(ignore):
            with open(ignore, "w", encoding="utf-8") as f:
                f.write("# iac-azure-agent working copy; not part of this project's history\n*\n")

    # ------------------------------------------------------------ state

    def branch(self):
        code, out, _ = self._git("symbolic-ref", "--short", "-q", "HEAD")
        return out.strip() or None

    def head(self):
        code, out, _ = self._git("rev-parse", "-q", "--verify", "HEAD")
        return out.strip() if code == 0 else None

    def dirty(self):
        """[(path, action)] for uncommitted changes, untracked files included."""
        out = self._ok("status", "--porcelain=v1", "-z", "--untracked-files=all")
        items, parts, i = [], out.split("\0"), 0
        while i < len(parts):
            entry = parts[i]
            i += 1
            if len(entry) < 4:
                continue
            code, path = entry[:2], entry[3:]
            if code[0] in "RC":
                i += 1  # the original path follows a rename or copy
            if "D" in code:
                action = "deleted"
            elif code == "??" or "A" in code:
                action = "created"
            else:
                action = "modified"
            items.append((path, action))
        return items

    def _remote_ref(self):
        if not self.default_branch:
            return None
        ref = "refs/remotes/origin/%s" % self.default_branch
        code, _, _ = self._git("rev-parse", "-q", "--verify", ref)
        return ref if code == 0 else None

    def committed_changes(self):
        """[(path, action)] committed on the current branch since it left the default branch."""
        ref = self._remote_ref()
        if not ref or not self.head():
            return []
        code, out, _ = self._git("diff", "--name-status", "-z", "--no-renames", "%s...HEAD" % ref)
        if code != 0:
            return []
        parts, items = out.split("\0"), []
        for k in range(0, len(parts) - 1, 2):
            status, path = parts[k], parts[k + 1]
            items.append((path, {"A": "created", "D": "deleted"}.get(status[:1], "modified")))
        return items

    def changes(self):
        merged = dict(self.committed_changes())
        merged.update(dict(self.dirty()))
        return sorted(merged.items())

    def inside_infra(self, path):
        root = (self.infra_root or "").rstrip("/")
        return bool(root) and (path == root or path.startswith(root + "/"))

    def status(self):
        if not self.exists():
            return {"exists": False, "path": self.dir, "repository": self.repo["slug"]}
        self.require()
        dirty = self.dirty()
        out = {"exists": True, "path": self.dir, "repository": self.repo["slug"],
               "branch": self.branch(), "head": self.head(),
               "default_branch": self.default_branch, "infra_root": self.infra_root,
               "clean": not dirty,
               "uncommitted": [{"path": p, "action": a} for p, a in dirty[:200]],
               "on_work_branch": (self.branch() or "").startswith(BRANCH_PREFIX)}
        ref = self._remote_ref()
        if ref and self.head():
            code, counts, _ = self._git("rev-list", "--left-right", "--count", "HEAD...%s" % ref)
            if code == 0 and len(counts.split()) == 2:
                out["ahead_of_default"], out["behind_default"] = (int(x) for x in counts.split())
                out["note"] = "Counts are against the last fetch; run sync to refresh."
        return out

    # ------------------------------------------------------------ changes to the clone

    def clone(self):
        if os.path.exists(self.dir):
            if self.exists():
                self.require()
                raise Refused("a working copy already exists at %s; use sync" % self.dir)
            raise Refused("%s exists but is not a git working copy; it was left untouched" % self.dir)
        self._protect_from_project_git()
        os.makedirs(os.path.dirname(self.dir), exist_ok=True)
        code, _, err = run_git(["clone", "--quiet", self.repo["url"] + ".git", self.dir])
        if code != 0:
            raise _remote_failure("clone", err)
        return self.status()

    def fetch(self):
        self.require()
        code, _, err = self._git("fetch", "--quiet", "--prune", "origin")
        if code != 0:
            raise _remote_failure("fetch", err)

    def sync(self):
        """Fetches. Fast-forwards the default branch when it is checked out and clean."""
        self.fetch()
        result = {"fetched": True, "fast_forwarded": False}
        ref = self._remote_ref()
        if ref and self.branch() == self.default_branch:
            if self.dirty():
                raise Refused("the working copy has uncommitted changes on %s; nothing was "
                              "updated. Review them before continuing." % self.default_branch)
            before = self.head()
            code, _, err = self._git("merge", "--ff-only", "--quiet", ref)
            if code != 0:
                raise Refused("local %s has diverged from GitHub and cannot fast-forward; "
                              "nothing was changed. This needs a person to look at it."
                              % self.default_branch)
            result["fast_forwarded"] = before != self.head()
        result.update(self.status())
        return result

    def begin(self, rec_id):
        """Creates (or returns to) the local work branch for a request. Not pushed."""
        self.fetch()
        name = BRANCH_PREFIX + rec_id
        current = self.branch()
        if current == name:
            return dict(self.status(), created=False)
        if self.dirty():
            raise Refused("the working copy has uncommitted changes on %s; they would be "
                          "carried onto another branch. Nothing was changed." % current)
        code, _, _ = self._git("rev-parse", "-q", "--verify", "refs/heads/%s" % name)
        if code == 0:
            self._ok("checkout", "--quiet", name)
            return dict(self.status(), created=False)
        ref = self._remote_ref()
        if ref:
            self._ok("checkout", "--quiet", "--no-track", "-b", name, ref)
        elif self.head() is None:
            self._ok("checkout", "--quiet", "-b", name)   # empty repository: unborn branch
        else:
            raise Refused("the default branch %r was not found on GitHub; check "
                          "repository.default_branch in the config" % self.default_branch)
        return dict(self.status(), created=True)

    # ------------------------------------------------------------ content

    def infra_files(self):
        base = self.infra_dir()
        out = []
        for folder, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d != ".git")
            for name in sorted(files):
                full = os.path.join(folder, name)
                if os.path.islink(full):
                    continue
                out.append(os.path.relpath(full, self.dir).replace(os.sep, "/"))
        return out

    def tree_hash(self):
        """sha256 over the path and content of every file under the infrastructure root."""
        h = hashlib.sha256()
        for rel in self.infra_files():
            with open(os.path.join(self.dir, *rel.split("/")), "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
            h.update(("%s\0%s\n" % (rel, digest)).encode("utf-8"))
        return h.hexdigest()


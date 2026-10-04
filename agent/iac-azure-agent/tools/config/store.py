"""Load, change and save one project's config. See schema.py for the fields."""
import copy
import datetime
import os

from config import schema
from lib import paths, repo_id, storage
from lib.errors import InvalidInput, NotFound, Refused, StorageUnavailable

FILENAME = "config.json"


def now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


class ConfigStore:
    def __init__(self, project_root, environ=None):
        self.project_root = os.path.realpath(project_root)
        self.dir = paths.project_dir(self.project_root, environ)
        self.path = os.path.join(self.dir, FILENAME)

    def load(self):
        """Returns the stored config, or None on first run (no file yet)."""
        cfg = storage.read_json(self.path)
        if cfg is not None:
            schema.validate_loaded(cfg, self.path)
        return cfg

    def _base(self):
        cfg = self.load()
        if cfg is None:
            cfg = {"schema_version": schema.SCHEMA_VERSION, "project_root": self.project_root}
        return cfg

    def _save(self, cfg):
        cfg = copy.deepcopy(cfg)
        cfg["updated_at"] = now()
        schema.validate(cfg)
        storage.write_json_atomic(self.path, cfg)
        return cfg

    def set_value(self, key, value):
        """key is a top-level field, 'repository.default_branch', or '<map>.<name>'."""
        cfg = self._base()
        if key == "repository.default_branch":
            if "repository" not in cfg:
                raise Refused("set the repository first (set-repo)")
            cfg["repository"]["default_branch"] = schema.check_branch(value)
        elif key in schema.SCALAR_FIELDS:
            cfg[key] = schema.check_scalar(key, value)
        elif key in schema.LIST_FIELDS:
            items = value if isinstance(value, list) else [s.strip() for s in str(value).split(",") if s.strip()]
            cfg[key] = schema.check_env_list(key, items)
        elif "." in key and key.split(".", 1)[0] in schema.MAP_FIELDS:
            field, name = key.split(".", 1)
            m = dict(cfg.get(field, {}))
            m[name] = value
            cfg[field] = schema.check_map(field, m)
        elif key == "repository":
            raise Refused("use set-repo to change the repository")
        else:
            raise InvalidInput("unknown or read-only key %r" % key)
        return self._save(cfg)

    def unset_value(self, key):
        cfg = self.load()
        if cfg is None:
            raise NotFound("no config saved for this project")
        if key in schema.SCALAR_FIELDS or key in schema.LIST_FIELDS:
            cfg.pop(key, None)
        elif "." in key and key.split(".", 1)[0] in schema.MAP_FIELDS:
            field, name = key.split(".", 1)
            cfg.get(field, {}).pop(name, None)
            if field in cfg and not cfg[field]:
                cfg.pop(field)
        else:
            raise InvalidInput("cannot unset %r (the repository is changed with set-repo)" % key)
        return self._save(cfg)

    def set_repo(self, new_id, confirm_from=None, default_branch=None):
        """Sets the repository. Switching away from an already-configured repository needs
        confirm_from equal to the current repository (any accepted spelling)."""
        new = repo_id.parse(new_id)
        cfg = self._base()
        current = cfg.get("repository")
        if current and current["slug"] != new["slug"]:
            if confirm_from is None:
                raise Refused("this project is configured for %s. Switching to %s needs "
                              "explicit confirmation: repeat with --confirm-switch-from %s"
                              % (current["slug"], new["slug"], current["slug"]))
            try:
                confirmed = repo_id.parse(confirm_from)["slug"]
            except InvalidInput:
                confirmed = None
            if confirmed != current["slug"]:
                raise Refused("--confirm-switch-from does not match the configured repository; "
                              "nothing was changed")
        elif confirm_from is not None and not current:
            raise Refused("--confirm-switch-from given but no repository is configured")
        repo = dict(new)
        branch = default_branch
        if not branch and current and current["slug"] == new["slug"]:
            branch = current.get("default_branch")
        if branch:
            repo["default_branch"] = schema.check_branch(branch)
        cfg["repository"] = repo
        return self._save(cfg), (current["slug"] if current else None)

    def clear(self, confirm_project_root):
        if os.path.realpath(confirm_project_root or "") != self.project_root:
            raise Refused("clear needs --confirm-project equal to the project root %s"
                          % self.project_root)
        self.load()  # refuses on a corrupt file rather than deleting evidence
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            return False
        except OSError as e:
            raise StorageUnavailable("could not delete %s: %s" % (self.path, e.strerror or e))
        return True

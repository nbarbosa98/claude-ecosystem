"""Persistence for request records: one JSON file per request, per project.

Every change goes through mutate(): load, apply, reconcile approvals, run the secret guard
over the whole record, bump the revision, save atomically. A save fails if the file changed
on disk since it was loaded (optimistic concurrency; there is no lock).
"""
import datetime
import os
import re
import secrets

from config.store import ConfigStore
from lib import paths, secret_guard, storage
from lib.errors import CorruptRecord, NotFound, StorageUnavailable
from state import machine

ID_RE = re.compile(r"^req-\d{8}-\d{6}-[0-9a-f]{6}$")
REQUIRED = ("schema_version", "id", "state", "status", "intent", "requirements", "questions",
            "assumptions", "approvals", "history", "revision")


class StateStore:
    def __init__(self, project_root, environ=None):
        self.project_root = os.path.realpath(project_root)
        self.environ = environ
        self.dir = os.path.join(paths.project_dir(self.project_root, environ), "requests")

    def production_envs(self):
        """Production environment names from config, plus the built-in defaults. A corrupt
        or unreadable config raises: risk flags must not be computed on a guess."""
        cfg = ConfigStore(self.project_root, self.environ).load() or {}
        return tuple(cfg.get("production_environments", ())) + machine.DEFAULT_PRODUCTION_ENVS

    def path(self, rec_id):
        if not isinstance(rec_id, str) or not ID_RE.match(rec_id):
            raise NotFound("invalid request id %r" % rec_id)
        return os.path.join(self.dir, rec_id + ".json")

    def load(self, rec_id):
        p = self.path(rec_id)
        rec = storage.read_json(p)
        if rec is None:
            raise NotFound("no request %s in this project" % rec_id)
        if rec.get("schema_version") != machine.SCHEMA_VERSION or any(k not in rec for k in REQUIRED) \
                or rec.get("state") not in machine.IDX or rec.get("id") != rec_id:
            raise CorruptRecord("%s is not a valid version-%d request record. It was left "
                                "untouched." % (p, machine.SCHEMA_VERSION))
        return rec

    def _save(self, rec, expected_revision):
        p = self.path(rec["id"])
        on_disk = storage.read_json(p)
        if on_disk is not None and on_disk.get("revision") != expected_revision:
            raise StorageUnavailable("request %s changed on disk while this command ran "
                                     "(revision %s, expected %s); nothing was saved, retry"
                                     % (rec["id"], on_disk.get("revision"), expected_revision))
        secret_guard.check(rec, "request record")
        rec["revision"] = expected_revision + 1
        rec["updated_at"] = machine.now()
        storage.write_json_atomic(p, rec)
        return rec

    def create(self, intent):
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
        rec_id = "req-%s-%s" % (stamp, secrets.token_hex(3))
        rec = machine.new_record(rec_id, self.project_root, intent)
        return self._save(rec, 0)

    def mutate(self, rec_id, fn):
        """fn(record, production_envs) changes the record in place and returns a result."""
        rec = self.load(rec_id)
        rev = rec["revision"]
        envs = self.production_envs()
        result = fn(rec, envs)
        machine.reconcile(rec, envs)
        self._save(rec, rev)
        return rec, result

    def list(self):
        out = []
        for name in storage.list_json(self.dir):
            rec_id = name[:-5]
            try:
                rec = self.load(rec_id)
                out.append({"id": rec_id, "state": rec["state"], "status": rec["status"],
                            "intent": rec["intent"], "updated_at": rec.get("updated_at")})
            except (CorruptRecord, NotFound) as e:
                out.append({"id": rec_id, "error": str(e)})
        return out

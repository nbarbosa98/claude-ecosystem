"""JSON file storage: atomic writes, owner-only permissions, honest failures.

- A missing file reads as None. Any other read problem raises: StorageUnavailable when the
  file cannot be read, CorruptRecord when it can be read but is not a valid JSON object.
  A corrupt file is never replaced silently; the caller decides.
- Writes go to a temporary file in the same directory, are flushed and fsynced, set to
  mode 0600, then os.replace()d over the target. The written file is read back and
  compared before the write is reported as saved.
- Directories are created with mode 0700.
"""
import errno
import json
import os
import tempfile

from lib.canonical import canonical_json
from lib.errors import CorruptRecord, StorageUnavailable


def ensure_dir(path):
    # os.makedirs applies `mode` only to the leaf, so each missing level is created here.
    missing = []
    probe = os.path.abspath(path)
    while not os.path.exists(probe):
        missing.append(probe)
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        for d in reversed(missing):
            try:
                os.mkdir(d, 0o700)
            except FileExistsError:
                pass
    except OSError as e:
        raise StorageUnavailable("cannot create storage directory %s: %s" % (path, e.strerror or e))
    if not os.path.isdir(path):
        raise StorageUnavailable("storage path %s is not a directory" % path)


def read_json(path):
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        if e.errno == errno.ENOENT:
            return None
        if e.errno == errno.ENOTDIR:
            raise StorageUnavailable("cannot read %s: a parent path is not a directory" % path)
        raise StorageUnavailable("cannot read %s: %s" % (path, e.strerror or e))
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise CorruptRecord("%s is not valid JSON (%s). It was left untouched; inspect or "
                            "move it aside, then retry." % (path, e))
    if not isinstance(data, dict):
        raise CorruptRecord("%s does not contain a JSON object. It was left untouched." % path)
    return data


def write_json_atomic(path, obj):
    directory = os.path.dirname(path)
    ensure_dir(directory)
    payload = (json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("ascii")
    tmp = None
    try:
        # mkstemp creates the file with mode 0600; the chmod below covers odd umasks.
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        tmp = None
        if hasattr(os, "O_DIRECTORY"):
            dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
    except OSError as e:
        raise StorageUnavailable("could not save %s: %s. Nothing was saved." % (path, e.strerror or e))
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    back = read_json(path)
    if back is None or canonical_json(back) != canonical_json(obj):
        raise StorageUnavailable("read-back of %s did not match what was written; treat the "
                                 "save as failed" % path)
    return path


def list_json(directory):
    try:
        names = os.listdir(directory)
    except OSError as e:
        if e.errno == errno.ENOENT:
            return []
        raise StorageUnavailable("cannot list %s: %s" % (directory, e.strerror or e))
    return sorted(n for n in names if n.endswith(".json") and not n.startswith(".tmp-"))

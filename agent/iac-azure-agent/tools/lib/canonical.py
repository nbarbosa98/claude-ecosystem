"""Canonical JSON and the content hash that approvals are bound to."""
import hashlib
import json

SHORT_LEN = 12


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False)


def content_hash(obj):
    return hashlib.sha256(canonical_json(obj).encode("ascii")).hexdigest()


def short(h):
    return h[:SHORT_LEN]

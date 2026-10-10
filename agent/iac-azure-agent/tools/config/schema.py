"""Config schema, version 1.

{
  "schema_version": 1,
  "project_root": "/abs/path/of/project",           # informational; the key is derived from it
  "repository": {"owner", "name", "slug", "url", "default_branch"} | absent,
  "infra_root": "infra",                            # relative path inside the repository
  "region": "westeurope",
  "environments": ["dev", "test", "prod"],
  "default_environment": "dev",
  "production_environments": ["prod"],
  "naming": {"<name>": "<convention>"},             # confirmed naming conventions
  "tagging": {"<tag>": "<rule or default>"},        # confirmed tagging conventions
  "deployment_auth": "azure-cli-user" | "github-actions-oidc" | "managed-identity",
  "preferences": {"<name>": "<value>"},             # non-secret preferences and defaults
  "accepted_findings": {"<check id>@<file>:<resource>": "<reason>"},
                                                    # scanner findings the user accepted, one
                                                    # resource each (ADR-028). A bare
                                                    # "<check id>" key is the pre-0.6.0 form:
                                                    # still readable, no longer applied.
  "updated_at": "<UTC ISO 8601>"
}

Every field except schema_version and project_root is optional: an unset field means the
user has not confirmed it yet. Nothing here may hold a secret (lib/secret_guard.py).
"""
import re

from lib import secret_guard
from lib.errors import CorruptRecord, InvalidInput
from lib.repo_id import NAME, OWNER

SCHEMA_VERSION = 1

AUTH_METHODS = ("azure-cli-user", "github-actions-oidc", "managed-identity")
ENV_NAME = re.compile(r"^[a-z][a-z0-9-]{0,23}$")
REGION = re.compile(r"^[a-z][a-z0-9]{2,30}$")
PATH_PART = re.compile(r"^[A-Za-z0-9._-]+$")
MAP_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
FINDING_KEY = re.compile(r"^[A-Z][A-Z0-9_]{1,39}@[A-Za-z0-9._/-]{1,200}:[A-Za-z0-9._/-]{1,120}$")
MAX_MAP = 50
MAX_VALUE = 500

TOP_KEYS = {"schema_version", "project_root", "repository", "infra_root", "region",
            "environments", "default_environment", "production_environments", "naming",
            "tagging", "deployment_auth", "preferences", "accepted_findings", "updated_at"}
MAP_FIELDS = ("naming", "tagging", "preferences", "accepted_findings")
LIST_FIELDS = ("environments", "production_environments")
SCALAR_FIELDS = ("infra_root", "region", "default_environment", "deployment_auth")


def check_branch(v):
    if (not isinstance(v, str) or not re.match(r"^[A-Za-z0-9._/-]{1,100}$", v) or ".." in v
            or v.startswith(("-", "/", ".")) or v.endswith(("/", ".lock", "."))
            or "//" in v):
        raise InvalidInput("invalid branch name")
    return v


def check_infra_root(v):
    if not isinstance(v, str) or not v or len(v) > 200:
        raise InvalidInput("infra_root must be a non-empty relative path")
    v = v.replace("\\", "/")
    if v.startswith("/") or re.match(r"^[A-Za-z]:", v):
        raise InvalidInput("infra_root must be relative to the repository root")
    parts = v.rstrip("/").split("/")
    if any(p in ("", ".", "..") or not PATH_PART.match(p) for p in parts):
        raise InvalidInput("infra_root may contain only letters, digits, '.', '_', '-' and '/', "
                           "and no '.' or '..' segments")
    return "/".join(parts)


def check_scalar(field, v):
    if field == "infra_root":
        return check_infra_root(v)
    if field == "region":
        if not isinstance(v, str) or not REGION.match(v):
            raise InvalidInput("region must be an Azure region name such as 'westeurope' "
                               "(lower-case letters and digits)")
        return v
    if field == "default_environment":
        if not isinstance(v, str) or not ENV_NAME.match(v):
            raise InvalidInput("invalid environment name")
        return v
    if field == "deployment_auth":
        if v not in AUTH_METHODS:
            raise InvalidInput("deployment_auth must be one of: %s" % ", ".join(AUTH_METHODS))
        return v
    raise InvalidInput("unknown field %s" % field)


def check_env_list(field, v):
    if not isinstance(v, list) or not v or len(v) > 20:
        raise InvalidInput("%s must be a list of 1 to 20 environment names" % field)
    for e in v:
        if not isinstance(e, str) or not ENV_NAME.match(e):
            raise InvalidInput("invalid environment name in %s" % field)
    if len(set(v)) != len(v):
        raise InvalidInput("duplicate environment name in %s" % field)
    return v


def finding_key(code, file, resource):
    """The accepted_findings key for one finding on one resource, or None when the file or
    resource name cannot be written as a key (suppress that one in code instead)."""
    key = "%s@%s:%s" % (code, file, resource)
    return key if FINDING_KEY.match(key) and ".." not in key else None


def is_scoped_finding(key):
    return bool(FINDING_KEY.match(key))


def check_map(field, v):
    if not isinstance(v, dict) or len(v) > MAX_MAP:
        raise InvalidInput("%s must be an object with at most %d entries" % (field, MAX_MAP))
    for k, val in v.items():
        if field == "accepted_findings" and is_scoped_finding(k):
            pass
        elif not MAP_KEY.match(k):
            raise InvalidInput("invalid key in %s (letters, digits, '_', '.', '-'; starts "
                               "with a letter)" % field)
        if field == "accepted_findings" and (not isinstance(val, str) or len(val.strip()) < 10):
            raise InvalidInput("accepted_findings.%s needs the reason it was accepted "
                               "(at least 10 characters)" % k)
        if not isinstance(val, str) or len(val) > MAX_VALUE:
            raise InvalidInput("%s.%s must be a string of at most %d characters"
                               % (field, k, MAX_VALUE))
    return v


def check_repository(v):
    if not isinstance(v, dict):
        raise InvalidInput("repository must be an object")
    owner, name = v.get("owner"), v.get("name")
    if (not isinstance(owner, str) or not OWNER.match(owner) or owner != owner.lower()
            or not isinstance(name, str) or not NAME.match(name) or name != name.lower()):
        raise InvalidInput("repository owner/name invalid or not normalised")
    if v.get("slug") != "%s/%s" % (owner, name) or v.get("url") != "https://github.com/%s/%s" % (owner, name):
        raise InvalidInput("repository slug/url do not match owner/name")
    if "default_branch" in v:
        check_branch(v["default_branch"])
    extra = set(v) - {"owner", "name", "slug", "url", "default_branch"}
    if extra:
        raise InvalidInput("unknown repository fields: %s" % ", ".join(sorted(extra)))


def validate(cfg):
    """Validates a whole config document. Raises InvalidInput."""
    if not isinstance(cfg, dict):
        raise InvalidInput("config must be an object")
    extra = set(cfg) - TOP_KEYS
    if extra:
        raise InvalidInput("unknown config fields: %s" % ", ".join(sorted(extra)))
    if cfg.get("schema_version") != SCHEMA_VERSION:
        raise InvalidInput("unsupported schema_version %r" % cfg.get("schema_version"))
    if not isinstance(cfg.get("project_root"), str):
        raise InvalidInput("project_root missing")
    if "repository" in cfg:
        check_repository(cfg["repository"])
    for f in SCALAR_FIELDS:
        if f in cfg:
            check_scalar(f, cfg[f])
    for f in LIST_FIELDS:
        if f in cfg:
            check_env_list(f, cfg[f])
    for f in MAP_FIELDS:
        if f in cfg:
            check_map(f, cfg[f])
    envs = cfg.get("environments")
    if envs is not None:
        if cfg.get("default_environment") and cfg["default_environment"] not in envs:
            raise InvalidInput("default_environment is not one of environments")
        bad = [e for e in cfg.get("production_environments", []) if e not in envs]
        if bad:
            raise InvalidInput("production_environments not in environments: %s" % ", ".join(bad))
    secret_guard.check(cfg, "config")


def validate_loaded(cfg, path):
    """Validation for a document read from disk: failures mean the file is corrupt."""
    if isinstance(cfg, dict) and isinstance(cfg.get("schema_version"), int) \
            and cfg["schema_version"] > SCHEMA_VERSION:
        raise CorruptRecord("%s has schema_version %d, newer than this tool supports (%d); "
                            "update the plugin" % (path, cfg["schema_version"], SCHEMA_VERSION))
    try:
        validate(cfg)
    except InvalidInput as e:
        raise CorruptRecord("%s failed validation: %s. It was left untouched." % (path, e))

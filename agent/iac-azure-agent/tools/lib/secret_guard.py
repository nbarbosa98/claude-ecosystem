"""Rejects secret-shaped keys and values before anything is stored.

Used by the config store and the state store on every write. It is a pattern check,
not a secret scanner: it catches the common shapes listed below and nothing else.
See docs/security-model.md ("Secret guard") for what it does not catch.

Findings never include the matched value, so an error message cannot leak the secret.
"""
import re

from lib.errors import InvalidInput

# Key names are compared after lower-casing and removing everything but letters and digits,
# so "client_secret", "Client-Secret" and "clientSecret" all become "clientsecret".
KEY_SUBSTRINGS = (
    "password", "passwd", "passphrase", "secret", "token", "apikey", "accesskey",
    "accountkey", "sharedaccesskey", "privatekey", "connectionstring", "connstr",
    "credential", "sastoken", "sasurl", "pfx",
)
KEY_EXACT = ("pwd", "pat", "sas", "key", "sig", "signature", "cert", "certificate")

VALUE_PATTERNS = (
    ("pem_block", re.compile(r"-----BEGIN [A-Z0-9 ]+-----")),
    ("private_key_marker", re.compile(r"PRIVATE KEY", re.I)),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("github_fine_grained_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")),
    # Azure storage account keys are 64 random bytes, base64: 86 characters plus "==".
    ("storage_account_key", re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{86}==")),
    ("connection_string_key", re.compile(
        r"\b(AccountKey|SharedAccessKey|SharedAccessSignature|Password|Pwd)\s*=\s*[^;\s]+", re.I)),
    ("sas_signature", re.compile(r"(?:^|[?&\s])sig=[A-Za-z0-9%+/=]{10,}", re.I)),
    ("url_credentials", re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@", re.I)),
    # Entra ID client secrets created since 2021 carry "Q~" after a 3-character prefix and
    # a digit. Heuristic: the format is not formally documented.
    ("entra_client_secret", re.compile(r"(?<![A-Za-z0-9_~.-])[A-Za-z0-9_~.-]{3}\dQ~[A-Za-z0-9_~.-]{31,34}(?![A-Za-z0-9_~.-])")),
)


def normalise_key(key):
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def key_is_secret(key):
    k = normalise_key(key)
    if k in KEY_EXACT:
        return True
    return any(s in k for s in KEY_SUBSTRINGS)


def value_findings(value):
    return [name for name, pat in VALUE_PATTERNS if pat.search(value)]


def scan(obj, path="$"):
    """Returns a list of (path, reason) for every secret-shaped key or value."""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            child = "%s.%s" % (path, k)
            if key_is_secret(k):
                found.append((child, "secret-shaped key name"))
            for name in value_findings(str(k)):
                found.append((child, "key looks like a secret (%s)" % name))
            found.extend(scan(v, child))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            found.extend(scan(v, "%s[%d]" % (path, i)))
    elif isinstance(obj, str):
        for name in value_findings(obj):
            found.append((path, "value looks like a secret (%s)" % name))
    return found


def check(obj, what="input"):
    """Raises InvalidInput listing where secret-shaped data was found (never the data)."""
    found = scan(obj)
    if found:
        detail = "; ".join("%s: %s" % (p, r) for p, r in found)
        raise InvalidInput("refusing to store %s: %s. Secrets are never stored by this "
                           "agent; reference them from Key Vault or the pipeline's secret "
                           "store instead." % (what, detail))

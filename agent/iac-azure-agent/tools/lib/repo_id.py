"""GitHub repository identifiers: parse, validate, normalise to one form.

Accepted inputs (github.com only):
  owner/name
  https://github.com/owner/name[.git][/]
  git@github.com:owner/name[.git]
  ssh://git@github.com/owner/name[.git]

Normal form: lower-case "owner/name" plus the URL https://github.com/owner/name.
GitHub resolves owner and repository names case-insensitively, so lower-casing makes two
spellings of the same repository compare equal.
"""
import re

from lib.errors import InvalidInput

OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

_FORMS = (
    re.compile(r"^(?P<owner>[^/\s:@]+)/(?P<name>[^/\s:@]+)$"),
    re.compile(r"^https://(?:www\.)?github\.com/(?P<owner>[^/\s:@]+)/(?P<name>[^/\s:@?#]+?)/?$", re.I),
    re.compile(r"^git@github\.com:(?P<owner>[^/\s:@]+)/(?P<name>[^/\s:@]+?)/?$", re.I),
    re.compile(r"^ssh://git@github\.com/(?P<owner>[^/\s:@]+)/(?P<name>[^/\s:@]+?)/?$", re.I),
)


def parse(text):
    if not isinstance(text, str) or not text.strip():
        raise InvalidInput("repository identifier is empty")
    s = text.strip()
    if re.match(r"^[a-z][a-z0-9+.-]*://[^/]*@", s, re.I) and not s.lower().startswith("ssh://git@"):
        raise InvalidInput("repository URL must not contain credentials")
    if re.match(r"^(https?|ssh)://", s, re.I) or s.lower().startswith("git@"):
        if not re.search(r"(^|[/@])(www\.)?github\.com[:/]", s, re.I):
            raise InvalidInput("only github.com repositories are supported")
        if s.lower().startswith("http://"):
            raise InvalidInput("use https:// for repository URLs")
    for form in _FORMS:
        m = form.match(s)
        if m:
            owner, name = m.group("owner"), m.group("name")
            break
    else:
        raise InvalidInput("not a GitHub repository identifier; use owner/name or "
                           "https://github.com/owner/name")
    if name.lower().endswith(".git"):
        name = name[:-4]
    if not OWNER.match(owner):
        raise InvalidInput("invalid GitHub owner name")
    if not NAME.match(name) or name in (".", ".."):
        raise InvalidInput("invalid GitHub repository name")
    owner, name = owner.lower(), name.lower()
    return {"owner": owner, "name": name, "slug": "%s/%s" % (owner, name),
            "url": "https://github.com/%s/%s" % (owner, name)}


def same(a, b):
    return parse(a)["slug"] == parse(b)["slug"]

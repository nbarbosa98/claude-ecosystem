"""Common CLI plumbing: JSON on stdout, exit codes from lib.errors."""
import json
import sys

from lib.errors import EXIT_OK, InvalidInput, ToolError


def emit(obj):
    sys.stdout.write(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=True) + "\n")


def run(handler, args):
    try:
        result = handler(args)
    except ToolError as e:
        emit({"ok": False, "error": e.code, "message": str(e)})
        return e.exit_code
    emit(dict({"ok": True}, **result))
    return EXIT_OK


def load_json_arg(text=None, path=None):
    """Reads a JSON value from --json TEXT or --file PATH ('-' means stdin)."""
    if (text is None) == (path is None):
        raise InvalidInput("pass exactly one of --json or --file")
    try:
        if path is not None:
            if path == "-":
                text = sys.stdin.read()
            else:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
        return json.loads(text)
    except OSError as e:
        raise InvalidInput("cannot read %s: %s" % (path, e.strerror or e))
    except ValueError as e:
        raise InvalidInput("input is not valid JSON: %s" % e)

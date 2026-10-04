"""Error types and the exit codes the CLIs map them to."""

EXIT_OK = 0
EXIT_INVALID = 1      # bad input, failed validation
EXIT_REFUSED = 2      # a workflow or safety rule refused the action
EXIT_STORAGE = 3      # storage unavailable (cannot read dir, cannot write)
EXIT_CORRUPT = 4      # stored file exists but is unreadable or invalid
EXIT_EXTERNAL = 5     # an external tool or service (gh, GitHub) is missing or failed


class ToolError(Exception):
    code = "invalid"
    exit_code = EXIT_INVALID


class InvalidInput(ToolError):
    code = "invalid"
    exit_code = EXIT_INVALID


class Refused(ToolError):
    code = "refused"
    exit_code = EXIT_REFUSED


class StorageUnavailable(ToolError):
    code = "storage_unavailable"
    exit_code = EXIT_STORAGE


class CorruptRecord(ToolError):
    code = "corrupt"
    exit_code = EXIT_CORRUPT


class NotFound(ToolError):
    code = "not_found"
    exit_code = EXIT_INVALID


class ExternalUnavailable(ToolError):
    """The check could not be made. It says nothing about the thing being checked."""
    code = "external_unavailable"
    exit_code = EXIT_EXTERNAL

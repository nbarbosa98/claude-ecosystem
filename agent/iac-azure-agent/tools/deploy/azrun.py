"""Runs the Azure CLI and turns its failures into causes a person can act on.

Sign-in is whatever `az login` left behind; this module never signs in, never reads a
token and never passes a credential. Every failure is classified so the report can say
what happened and whether a retry is safe:

  az_missing            az is not installed
  not_signed_in         no session, or the session expired
  wrong_context         signed in to a different tenant or subscription than the target
  authorization         the identity lacks the role for this operation
  policy                Azure Policy rejected the request
  quota                 not enough quota for the size or region
  sku_unavailable       the size or SKU is not offered to this subscription in this region
  invalid_template      the template or parameters are invalid
  conflict              the resource is busy or in a conflicting state
  transient             throttling, time-outs, service unavailable (safe to retry reads)
  error                 anything else

Only read operations are retried, and only for `transient`, at most RETRIES times.
"""
import json
import re
import subprocess
import time

from lib.errors import ExternalUnavailable, Refused

TIMEOUT_READ = 180
TIMEOUT_DEPLOY = 3600
RETRIES = 2
RETRY_WAIT = 5

PATTERNS = (
    ("not_signed_in", r"az login|AADSTS\d+|refresh token has expired|No subscription found|"
                      r"Interactive authentication is needed|token.*expired"),
    ("authorization", r"AuthorizationFailed|does not have authorization|LinkedAuthorizationFailed|"
                      r"AuthorizationPermissionMismatch|Forbidden"),
    ("policy", r"RequestDisallowedByPolicy|disallowed by policy|PolicyViolation"),
    ("quota", r"QuotaExceeded|OperationNotAllowed.*quota|exceeding approved .* quota|"
              r"Insufficient.*quota"),
    ("sku_unavailable", r"SkuNotAvailable|NotAvailableForSubscription|"
                        r"is currently not available in location|AllocationFailed|"
                        r"ZonalAllocationFailed|OverconstrainedAllocationRequest"),
    ("invalid_template", r"InvalidTemplate|InvalidTemplateDeployment|InvalidParameter|BCP\d{3}|"
                         r"InvalidRequestContent|InvalidResourceName|NoRegisteredProviderFound|"
                         r"InvalidApiVersionParameter|LocationNotAvailableForResourceType|"
                         r"MissingSubscriptionRegistration|BadRequest"),
    ("conflict", r"\bConflict\b|AnotherOperationInProgress|DeploymentActive|InUseSubnetCannotBeDeleted"),
    ("transient", r"TooManyRequests|\b429\b|ServiceUnavailable|GatewayTimeout|\b50[234]\b|"
                  r"timed out|Connection (reset|aborted)|Temporary failure|Max retries exceeded"),
)
ADVICE = {
    "az_missing": "Install the Azure CLI. Nothing was checked or changed.",
    "not_signed_in": "Run `az login` yourself (never paste a credential here), then try again.",
    "authorization": "The signed-in identity lacks permission. Granting a role is a change to "
                     "access that only you can decide; the agent will not do it.",
    "policy": "Azure Policy blocks this. The policy is not bypassed or weakened; change the "
              "design to comply, or ask the policy owner for an exemption.",
    "quota": "The subscription has no quota left for this. Choose another size or region, or "
             "request a quota increase.",
    "sku_unavailable": "That size is not offered to this subscription in this region, or Azure "
                       "has no capacity for it now. Choose another size, zone or region.",
    "invalid_template": "The template or its parameters are invalid. Fix the Bicep; this goes "
                        "back to IMPLEMENTATION and needs validation again.",
    "conflict": "The resource is busy or in a conflicting state. Check what else is running "
                "against it before retrying.",
    "transient": "A temporary service problem. Reading again is safe. A deployment is not "
                 "retried automatically: check its real state first.",
    "error": "Unclassified failure. Read the message; do not retry a change blindly.",
}
GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def classify(text):
    for kind, pattern in PATTERNS:
        if re.search(pattern, text, re.I):
            return kind
    return "error"


def clean_message(text, limit=600):
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("WARNING:")]
    return " ".join(" ".join(lines).split())[:limit]


class AzFailure(ExternalUnavailable):
    def __init__(self, kind, message):
        self.kind = kind
        super().__init__("%s: %s %s" % (kind, clean_message(message), ADVICE[kind]))


class Az:
    """`run` is injectable for tests: run(args, timeout) -> (exit_code, stdout, stderr)."""

    def __init__(self, run=None, cwd=None, sleep=time.sleep):
        self.cwd = cwd
        self._run = run or self._subprocess
        self._sleep = sleep

    def _subprocess(self, args, timeout):
        try:
            p = subprocess.run(["az"] + args, cwd=self.cwd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise AzFailure("az_missing", "the Azure CLI (az) is not installed or not on PATH.")
        except subprocess.TimeoutExpired:
            raise AzFailure("transient", "az did not finish within %d seconds." % timeout)
        return p.returncode, p.stdout, p.stderr

    def read(self, args, timeout=TIMEOUT_READ):
        """A read-only call returning parsed JSON. Retried on transient failures only."""
        last = None
        for attempt in range(RETRIES + 1):
            try:
                return self._json(args, timeout)
            except AzFailure as e:
                last = e
                if e.kind != "transient" or attempt == RETRIES:
                    raise
                self._sleep(RETRY_WAIT * (attempt + 1))
        raise last

    def change(self, args, timeout=TIMEOUT_DEPLOY):
        """A call that changes Azure. Never retried."""
        return self._json(args, timeout)

    def _json(self, args, timeout):
        code, out, err = self._run(args + ["--output", "json"], timeout)
        if code != 0:
            raise AzFailure(classify(err + " " + out), err or out)
        if not out.strip():
            return None
        try:
            return json.loads(out)
        except ValueError:
            raise AzFailure("error", "az returned output that is not JSON.")

    def context(self):
        acct = self.read(["account", "show"])
        if not isinstance(acct, dict) or not acct.get("id") or not acct.get("tenantId"):
            raise AzFailure("not_signed_in", "az account show returned no subscription.")
        return {"tenant_id": acct["tenantId"].lower(), "subscription_id": acct["id"].lower(),
                "subscription_name": acct.get("name"), "user": (acct.get("user") or {}).get("name"),
                "user_type": (acct.get("user") or {}).get("type"), "state": acct.get("state")}

    def require_context(self, tenant_id, subscription_id):
        """Refuses unless az is signed in to exactly the approved tenant and subscription."""
        ctx = self.context()
        if ctx["tenant_id"] != tenant_id.lower() or ctx["subscription_id"] != subscription_id.lower():
            raise Refused("wrong_context: az is signed in to subscription %s in tenant %s, but the "
                          "target is subscription %s in tenant %s. Nothing was changed. Switch "
                          "with `az account set` yourself; the agent does not switch context."
                          % (ctx["subscription_id"], ctx["tenant_id"], subscription_id, tenant_id))
        if ctx.get("state") and ctx["state"] != "Enabled":
            raise Refused("the subscription is %s, not Enabled" % ctx["state"])
        return ctx

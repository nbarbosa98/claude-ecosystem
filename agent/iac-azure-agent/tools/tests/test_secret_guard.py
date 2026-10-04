"""Secret guard: secret-shaped keys and values are rejected; identifiers are not.

Every "secret" below is synthetic: random-looking filler in the documented shape."""
import unittest

from helpers import SUB, TENANT  # noqa: F401 (sets sys.path)

from lib import secret_guard as G
from lib.errors import InvalidInput

FAKE_STORAGE_KEY = ("Ab0" * 28) + "Ab=="           # 86 base64 chars + "=="
FAKE_JWT = ".".join(("eyJhbGciOiJSUzI1NiJ9", "eyJzdWIiOiJ0ZXN0LXVzZXIifQ", "c2lnbmF0dXJlLWZha2U"))
FAKE_GH = "ghp_" + "Zz9" * 12
FAKE_GH_PAT = "github_pat_" + "11ABCDEFG0" + "_" + "x" * 30
FAKE_ENTRA = "abc" + "8Q~" + "Zz0_" * 8 + "Zz"


class Keys(unittest.TestCase):
    def test_secret_key_names(self):
        for k in ("password", "Password", "admin_password", "adminPassword", "secret",
                  "client_secret", "clientSecret", "CLIENT-SECRET", "token", "access_token",
                  "github_token", "private_key", "privateKey", "connection_string",
                  "ConnectionString", "storage_account_key", "accountKey", "api_key",
                  "sas_token", "pwd", "pat", "key", "certificate", "credentials", "pfx_path"):
            self.assertTrue(G.key_is_secret(k), k)

    def test_ordinary_key_names(self):
        for k in ("region", "environment", "subscription_id", "tenant_id", "resource_group",
                  "keyvault_name", "key_vault_sku", "monkey", "owner", "cost_center",
                  "naming", "default_branch", "public_network_access"):
            self.assertFalse(G.key_is_secret(k), k)


class Values(unittest.TestCase):
    def assertSecret(self, value, kind):
        self.assertIn(kind, G.value_findings(value), value)

    def test_positives(self):
        self.assertSecret("-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----", "pem_block")
        self.assertSecret("-----BEGIN CERTIFICATE-----", "pem_block")
        self.assertSecret("token is %s" % FAKE_GH, "github_token")
        self.assertSecret(FAKE_GH_PAT, "github_fine_grained_pat")
        self.assertSecret("Bearer " + FAKE_JWT, "jwt")
        self.assertSecret(FAKE_STORAGE_KEY, "storage_account_key")
        self.assertSecret("DefaultEndpointsProtocol=https;AccountName=stx;AccountKey=abc/def==;"
                          "EndpointSuffix=core.windows.net", "connection_string_key")
        self.assertSecret("Endpoint=sb://ns.servicebus.windows.net/;SharedAccessKeyName=root;"
                          "SharedAccessKey=abcdef", "connection_string_key")
        self.assertSecret("Server=tcp:x.database.windows.net;Password=hunter2;", "connection_string_key")
        self.assertSecret("https://stx.blob.core.windows.net/c?sv=2022-11-02&ss=b&sig=abcdefghijKLMNOP%2B%3D",
                          "sas_signature")
        self.assertSecret("https://user:hunter2@example.com/repo", "url_credentials")
        self.assertSecret(FAKE_ENTRA, "entra_client_secret")

    def test_negatives(self):
        for v in (TENANT, SUB, TENANT.upper(),
                  "/subscriptions/%s/resourceGroups/rg-app-dev" % SUB,
                  "0123456789abcdef0123456789abcdef01234567",               # git SHA
                  "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",  # sha256
                  "https://github.com/example-org/infra", "git@github.com:example-org/infra.git",
                  "westeurope", "st{workload}{env}001", "Standard_LRS",
                  "Use Key Vault references for secrets", "keyvault/secrets are referenced by name",
                  "https://stx.blob.core.windows.net/container", "sig-team-infra",
                  "Microsoft.Storage/storageAccounts@2023-05-01"):
            self.assertEqual(G.value_findings(v), [], v)


class Scan(unittest.TestCase):
    def test_nested_paths_reported_without_values(self):
        doc = {"a": [{"b": FAKE_GH}], "admin_password": "x", "fine": TENANT}
        found = G.scan(doc)
        paths = sorted(p for p, _ in found)
        self.assertEqual(paths, ["$.a[0].b", "$.admin_password"])
        with self.assertRaises(InvalidInput) as cm:
            G.check(doc)
        self.assertNotIn(FAKE_GH, str(cm.exception))
        self.assertNotIn("Zz9", str(cm.exception))

    def test_guid_document_passes(self):
        G.check({"tenant_id": TENANT, "subscription_id": SUB, "ids": [TENANT, SUB]})


if __name__ == "__main__":
    unittest.main()

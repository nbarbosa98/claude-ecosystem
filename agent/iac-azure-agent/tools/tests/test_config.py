"""Config store and CLI: first run, persistence, repository ids, switching, failures."""
import json
import os
import stat
import unittest

from helpers import CONFIG_CLI, StoreCase

from config.store import ConfigStore
from lib import paths, repo_id
from lib.errors import (EXIT_CORRUPT, EXIT_INVALID, EXIT_REFUSED, EXIT_STORAGE, CorruptRecord,
                        InvalidInput, Refused, StorageUnavailable)


class FirstRunAndPersistence(StoreCase):
    def test_first_run_reports_unconfigured(self):
        code, out = self.cli(CONFIG_CLI, "show")
        self.assertEqual(code, 0)
        self.assertFalse(out["configured"])
        self.assertIsNone(out["config"])
        self.assertFalse(os.path.exists(out["config_path"]), "show must not create files")

    def test_values_survive_separate_processes(self):
        steps = [("set-repo", "https://github.com/Example-Org/Infra-Live.git", "--default-branch", "main"),
                 ("set", "infra_root", "infra/bicep"),
                 ("set", "region", "westeurope"),
                 ("set", "environments", "dev, test, prod"),
                 ("set", "default_environment", "dev"),
                 ("set", "production_environments", "prod"),
                 ("set", "naming.pattern", "{type}-{workload}-{env}-{region}"),
                 ("set", "tagging.owner", "required"),
                 ("set", "deployment_auth", "github-actions-oidc"),
                 ("set", "preferences.private_endpoints", "preferred")]
        for s in steps:
            code, out = self.cli(CONFIG_CLI, *s)
            self.assertEqual(code, 0, out)
            self.assertTrue(out["saved"])
        code, out = self.cli(CONFIG_CLI, "show")
        cfg = out["config"]
        self.assertTrue(out["configured"])
        self.assertEqual(cfg["repository"]["slug"], "example-org/infra-live")
        self.assertEqual(cfg["repository"]["url"], "https://github.com/example-org/infra-live")
        self.assertEqual(cfg["repository"]["default_branch"], "main")
        self.assertEqual(cfg["infra_root"], "infra/bicep")
        self.assertEqual(cfg["environments"], ["dev", "test", "prod"])
        self.assertEqual(cfg["naming"]["pattern"], "{type}-{workload}-{env}-{region}")
        self.assertEqual(cfg["deployment_auth"], "github-actions-oidc")
        self.assertEqual(cfg["schema_version"], 1)
        self.assertEqual(out["unset_fields"], [])

    def test_file_and_directory_modes(self):
        if os.name != "posix":
            self.skipTest("POSIX permissions only")
        self.cli(CONFIG_CLI, "set", "region", "westeurope")
        path = ConfigStore(self.project).path
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.home).st_mode), 0o700)
        leftovers = [n for n in os.listdir(os.path.dirname(path)) if n.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    def test_config_is_per_project(self):
        other = os.path.join(self.tmp, "other")
        os.makedirs(os.path.join(other, ".git"))
        ConfigStore(self.project).set_value("region", "westeurope")
        self.assertIsNone(ConfigStore(other).load())
        self.assertNotEqual(paths.project_key(self.project), paths.project_key(other))

    def test_subdirectory_resolves_to_project_root(self):
        sub = os.path.join(self.project, "infra", "modules")
        os.makedirs(sub)
        self.assertEqual(paths.resolve_project_root(sub), self.project)

    def test_unset_and_unknown_keys(self):
        s = ConfigStore(self.project)
        s.set_value("region", "westeurope")
        self.assertNotIn("region", s.unset_value("region"))
        with self.assertRaises(InvalidInput):
            s.set_value("colour", "blue")
        with self.assertRaises(Refused):
            s.set_value("repository", "a/b")

    def test_invalid_values_rejected(self):
        s = ConfigStore(self.project)
        for key, value in (("region", "West Europe"), ("infra_root", "../outside"),
                           ("infra_root", "/abs"), ("deployment_auth", "client-secret"),
                           ("environments", "Dev"), ("naming.1bad", "x")):
            with self.assertRaises(InvalidInput, msg=(key, value)):
                s.set_value(key, value)
        s.set_value("environments", "dev,prod")
        with self.assertRaises(InvalidInput):
            s.set_value("default_environment", "test")

    def test_secret_in_config_rejected_and_not_echoed(self):
        code, out = self.cli(CONFIG_CLI, "set", "preferences.note",
                             "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8")
        self.assertEqual(code, EXIT_INVALID)
        self.assertNotIn("a1B2c3D4", out["message"])
        code, _ = self.cli(CONFIG_CLI, "set", "preferences.client_secret", "x")
        self.assertEqual(code, EXIT_INVALID)


class RepositoryIdentifiers(unittest.TestCase):
    def test_accepted_forms_normalise_to_one(self):
        for text in ("example-org/infra", "Example-Org/Infra",
                     "https://github.com/example-org/infra",
                     "https://github.com/Example-Org/infra.git",
                     "https://github.com/example-org/infra/",
                     "git@github.com:example-org/infra.git",
                     "ssh://git@github.com/example-org/infra.git"):
            r = repo_id.parse(text)
            self.assertEqual(r["slug"], "example-org/infra", text)
            self.assertEqual(r["url"], "https://github.com/example-org/infra", text)

    def test_rejected_forms(self):
        for text in ("", "infra", "a/b/c", "https://gitlab.com/a/b", "http://github.com/a/b",
                     "https://user:tok@github.com/a/b", "-bad/repo", "bad--owner/repo",
                     "owner/..", "owner/re po", "https://github.com/a", "github.com/a/b",
                     "o" * 40 + "/repo"):
            with self.assertRaises(InvalidInput, msg=text):
                repo_id.parse(text)

    def test_same(self):
        self.assertTrue(repo_id.same("a/b", "https://github.com/A/B.git"))
        self.assertFalse(repo_id.same("a/b", "a/c"))


class RepositorySwitch(StoreCase):
    def setUp(self):
        super().setUp()
        code, _ = self.cli(CONFIG_CLI, "set-repo", "example-org/infra", "--default-branch", "main")
        self.assertEqual(code, 0)

    def current(self):
        return self.cli(CONFIG_CLI, "show")[1]["config"]["repository"]["slug"]

    def test_switch_without_confirmation_refused(self):
        code, out = self.cli(CONFIG_CLI, "set-repo", "example-org/other")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertEqual(self.current(), "example-org/infra")

    def test_switch_with_wrong_confirmation_refused(self):
        for wrong in ("example-org/other", "example-org/infra2", "not a repo"):
            code, _ = self.cli(CONFIG_CLI, "set-repo", "example-org/other",
                               "--confirm-switch-from", wrong)
            self.assertEqual(code, EXIT_REFUSED, wrong)
        self.assertEqual(self.current(), "example-org/infra")

    def test_switch_with_exact_confirmation(self):
        code, out = self.cli(CONFIG_CLI, "set-repo", "https://github.com/example-org/other",
                             "--confirm-switch-from", "https://github.com/Example-Org/infra.git")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["previous_repository"], "example-org/infra")
        self.assertEqual(self.current(), "example-org/other")
        self.assertNotIn("default_branch", out["repository"], "branch of the old repo must not carry over")

    def test_same_repository_needs_no_confirmation(self):
        code, out = self.cli(CONFIG_CLI, "set-repo", "git@github.com:Example-Org/infra.git")
        self.assertEqual(code, 0)
        self.assertEqual(out["repository"]["default_branch"], "main")

    def test_confirmation_without_configured_repo_refused(self):
        s = ConfigStore(os.path.join(self.tmp, "fresh"))
        with self.assertRaises(Refused):
            s.set_repo("a/b", confirm_from="a/b")

    def test_clear_needs_project_root(self):
        code, _ = self.cli(CONFIG_CLI, "clear", "--confirm-project", self.tmp)
        self.assertEqual(code, EXIT_REFUSED)
        code, out = self.cli(CONFIG_CLI, "clear", "--confirm-project", self.project)
        self.assertEqual(code, 0)
        self.assertTrue(out["deleted"])
        self.assertFalse(self.cli(CONFIG_CLI, "show")[1]["configured"])


class Failures(StoreCase):
    def write_raw(self, text):
        s = ConfigStore(self.project)
        os.makedirs(os.path.dirname(s.path), exist_ok=True)
        with open(s.path, "w") as f:
            f.write(text)
        return s.path

    def test_corrupt_json_reported_and_left_untouched(self):
        path = self.write_raw("{not json")
        code, out = self.cli(CONFIG_CLI, "show")
        self.assertEqual(code, EXIT_CORRUPT)
        self.assertEqual(out["error"], "corrupt")
        code, _ = self.cli(CONFIG_CLI, "set", "region", "westeurope")
        self.assertEqual(code, EXIT_CORRUPT)
        with open(path) as f:
            self.assertEqual(f.read(), "{not json")

    def test_invalid_schema_is_corrupt(self):
        self.write_raw(json.dumps({"schema_version": 1, "project_root": "/x", "region": "Bad Region"}))
        with self.assertRaises(CorruptRecord):
            ConfigStore(self.project).load()
        self.write_raw(json.dumps([1, 2]))
        with self.assertRaises(CorruptRecord):
            ConfigStore(self.project).load()

    def test_newer_schema_version_is_refused(self):
        self.write_raw(json.dumps({"schema_version": 99, "project_root": "/x"}))
        with self.assertRaises(CorruptRecord) as cm:
            ConfigStore(self.project).load()
        self.assertIn("newer", str(cm.exception))

    def test_unwritable_store_reported_as_failure(self):
        blocker = os.path.join(self.tmp, "blocker")
        with open(blocker, "w") as f:
            f.write("a file where the store directory should be")
        env = {"IAC_AZURE_AGENT_HOME": os.path.join(blocker, "store")}
        code, out = self.cli(CONFIG_CLI, "set", "region", "westeurope", env=env)
        self.assertEqual(code, EXIT_STORAGE)
        self.assertFalse(out["ok"])
        self.assertNotIn("saved", out)

    def test_read_only_directory_reported_as_failure(self):
        if os.name != "posix" or os.geteuid() == 0:
            self.skipTest("needs a non-root POSIX user (root ignores directory permissions)")
        s = ConfigStore(self.project)
        os.makedirs(os.path.dirname(s.path))
        os.chmod(os.path.dirname(s.path), 0o500)
        try:
            with self.assertRaises(StorageUnavailable):
                s.set_value("region", "westeurope")
        finally:
            os.chmod(os.path.dirname(s.path), 0o700)

    def test_relative_home_override_rejected(self):
        code, out = self.cli(CONFIG_CLI, "show", env={"IAC_AZURE_AGENT_HOME": "relative/dir"})
        self.assertEqual(code, EXIT_INVALID)


if __name__ == "__main__":
    unittest.main()

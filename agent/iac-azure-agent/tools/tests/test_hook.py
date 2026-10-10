"""hooks/azure_guard.py: allow and deny cases, ask on approvals, fail-closed behaviour.

The hook runs in a subprocess exactly as Claude Code runs it: JSON on stdin, exit 2 blocks.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

from helpers import HOOK, PLUGIN

STORE = os.path.join(tempfile.gettempdir(), "iac-guard-test-store")


def run(payload, raw=False, env=None):
    data = payload if raw else json.dumps(payload)
    e = dict(os.environ, IAC_AZURE_AGENT_HOME=STORE, CLAUDE_PLUGIN_ROOT=PLUGIN)
    e.update(env or {})
    p = subprocess.run([sys.executable, HOOK], input=data, capture_output=True, text=True,
                       env=e, timeout=30)
    return p.returncode, p.stdout, p.stderr


def bash(command, **kw):
    return run({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": command}, "cwd": "/tmp"}, **kw)


class Allowed(unittest.TestCase):
    def test_read_only_and_local_commands_pass(self):
        for cmd in (
            "az account show",
            "az account show --query id -o tsv",
            "az account set --subscription 00000000-0000-0000-0000-00000000000b",
            "az login",
            "az login --use-device-code",
            "az group list -o table",
            "az group show -n rg-app-dev",
            "az resource list -g rg-app-dev",
            "az deployment group what-if -g rg-app-dev -f infra/main.bicep -p infra/dev.bicepparam",
            "az deployment sub what-if -l westeurope -f main.bicep",
            "az deployment group validate -g rg-app-dev --template-file main.bicep",
            "az deployment group show -g rg -n dep1",
            "az deployment operation group list -g rg -n dep1",
            "az bicep build --file infra/main.bicep",
            "az bicep lint --file infra/main.bicep",
            "az bicep version",
            "bicep build infra/main.bicep",
            "bicep lint infra/main.bicep",
            "bicep --version",
            "az rest --method get --url https://management.azure.com/subscriptions?api-version=2022-12-01",
            "az rest -m GET --uri /subscriptions/x/resourceGroups?api-version=2021-04-01",
            "az role assignment list --assignee 00000000-0000-0000-0000-00000000000c",
            "az --version",
            "pwsh -NoProfile -Command Get-AzContext",
            "pwsh -c 'Get-AzResourceGroup | Select-Object Name'",
            "pwsh -c Test-AzResourceGroupDeployment -ResourceGroupName rg -TemplateFile main.bicep",
            "git status && git diff --stat",
            "ls -la infra/",
            "python3 -m unittest discover -s tools/tests -v",
            "cd agent/iac-azure-agent && git log --oneline",
            "python3 /plugins/iac-azure-agent/tools/state/cli.py show req-20261004-000000-abcdef",
            "python3 /plugins/iac-azure-agent/tools/config/cli.py set region westeurope",
        ):
            code, out, err = bash(cmd)
            self.assertEqual(code, 0, "%s -> %s" % (cmd, err))
            self.assertEqual(out.strip(), "", cmd)

    def test_other_tools_pass(self):
        code, _, _ = run({"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}})
        self.assertEqual(code, 0)
        code, _, _ = run({"tool_name": "Write", "tool_input": {"file_path": "/tmp/project/infra/main.bicep", "content": ""}})
        self.assertEqual(code, 0)


class Denied(unittest.TestCase):
    def assertDenied(self, cmd):
        code, out, err = bash(cmd)
        self.assertEqual(code, 2, "%s was not blocked" % cmd)
        self.assertIn("iac-azure-agent guard", err)

    def test_azure_cli_mutations(self):
        for cmd in (
            "az deployment group create -g rg -f main.bicep",
            "az deployment sub create -l westeurope -f main.bicep",
            "az deployment mg create -m mg1 -l westeurope -f main.bicep",
            "az deployment tenant create -l westeurope -f main.bicep",
            "az deployment group create -g rg -f main.bicep --what-if",
            "az deployment group delete -g rg -n dep1",
            "az stack group create -g rg -n s1 -f main.bicep",
            "az group create -n rg -l westeurope",
            "az group delete -n rg --yes",
            "az resource delete --ids /subscriptions/x/resourceGroups/rg/providers/a/b/c",
            "az role assignment create --assignee x --role Owner --scope /subscriptions/x",
            "az role assignment delete --assignee x",
            "az ad sp create-for-rbac -n app",
            "az rest --method put --url https://management.azure.com/x",
            "az rest --method=post --url /x",
            "az rest --url https://management.azure.com/x",
            "az storage account update -n st -g rg --public-network-access Enabled",
            "az vm start -n vm -g rg",
            "az some-new-command do-something",
            "AZ GROUP DELETE -n rg",
            "/usr/bin/az group delete -n rg",
            "sudo az group delete -n rg",
            "echo ok; az group delete -n rg",
            "true && az group delete -n rg",
            "bash -c 'az group delete -n rg --yes'",
            "sh -c \"az deployment group create -g rg -f main.bicep\"",
            "$(az group delete -n rg)",
            "x=`az group delete -n rg`",
        ):
            self.assertDenied(cmd)

    def test_secret_revealing_reads(self):
        for cmd in ("az account get-access-token",
                    "az keyvault secret show --vault-name kv -n s",
                    "az keyvault secret list --vault-name kv",
                    "az storage account keys list -n st",
                    "az storage account show-connection-string -n st",
                    "az webapp deployment list-publishing-credentials -n app -g rg",
                    "az acr credential show -n reg",
                    "az login --service-principal -u app -p secretvalue --tenant t",
                    "pwsh -c Get-AzAccessToken"):
            self.assertDenied(cmd)

    def test_azure_powershell_mutations(self):
        for cmd in ("pwsh -c New-AzResourceGroupDeployment -ResourceGroupName rg -TemplateFile main.bicep",
                    "pwsh -c 'new-azresourcegroupdeployment -ResourceGroupName rg'",
                    "pwsh -Command \"Remove-AzResourceGroup -Name rg -Force\"",
                    "powershell -c New-AzRoleAssignment -ObjectId x -RoleDefinitionName Owner",
                    "pwsh -c New-AzSubscriptionDeployment -Location westeurope -TemplateFile main.bicep",
                    "pwsh -c Set-AzStorageAccount -Name st",
                    "pwsh -c Invoke-AzRestMethod -Method PUT -Path /x",
                    "pwsh -EncodedCommand ZQBjAGgAbwAgAGgAaQA=",
                    "powershell.exe -enc ZQBjAGgAbwA="):
            self.assertDenied(cmd)

    def test_raw_arm_calls_and_bicep_publish(self):
        for cmd in ("curl -X PUT https://management.azure.com/subscriptions/x?api-version=1",
                    "curl https://management.azure.com/subscriptions",
                    "wget --method=DELETE https://management.azure.com/x",
                    "pwsh -c Invoke-RestMethod -Method Put -Uri https://management.azure.com/x",
                    "python3 -c \"import urllib.request; urllib.request.urlopen('https://management.azure.com/x')\"",
                    "bicep publish main.bicep --target br:reg.azurecr.io/m:v1",
                    "az bicep publish --file main.bicep --target br:reg.azurecr.io/m:v1"):
            self.assertDenied(cmd)

    def test_store_access_blocked(self):
        for cmd in ("cat %s/projects/x/config.json" % STORE,
                    "echo '{}' > %s/projects/x/requests/r.json" % STORE,
                    "rm -rf ~/.config/iac-azure-agent/projects"):
            self.assertDenied(cmd)
        for tool in ("Write", "Edit"):
            code, _, err = run({"tool_name": tool, "tool_input": {
                "file_path": os.path.join(STORE, "projects", "x", "config.json")}})
            self.assertEqual(code, 2, tool)


class AskOnApproval(unittest.TestCase):
    def assertAsk(self, cmd):
        code, out, err = bash(cmd)
        self.assertEqual(code, 0, err)
        d = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(d["hookEventName"], "PreToolUse")
        self.assertEqual(d["permissionDecision"], "ask")
        self.assertTrue(d["permissionDecisionReason"])

    def test_approval_commands_prompt_the_user(self):
        root = "/home/u/.claude/plugins/cache/claude-agents/iac-azure-agent/0.1.0"
        self.assertAsk('python3 "%s/tools/state/cli.py" approve req-20261004-000000-abcdef '
                       '--kind architecture --confirm 0123456789ab' % root)
        self.assertAsk('python3 %s/tools/state/cli.py confirm-risk req-20261004-000000-abcdef '
                       '--phrase "ACCEPT-RISK 0123456789ab deletion"' % root)
        self.assertAsk('python3 %s/tools/config/cli.py set-repo a/b --confirm-switch-from c/d' % root)
        self.assertAsk('python3 "%s/tools/config/cli.py" set-repo a/b --default-branch main' % root)
        self.assertAsk('python3 %s/tools/config/cli.py clear --confirm-project /tmp/p' % root)


class FailClosed(unittest.TestCase):
    def test_bad_input_blocks(self):
        for raw in ("", "not json", "[]", "null", '"str"',
                    json.dumps({"tool_input": {"command": "ls"}}),
                    json.dumps({"tool_name": "Bash"}),
                    json.dumps({"tool_name": "Bash", "tool_input": {"command": 5}}),
                    json.dumps({"tool_name": "Write", "tool_input": {}})):
            code, _, err = run(raw, raw=True)
            self.assertEqual(code, 2, raw)
            self.assertIn("failing closed", err)

    def test_internal_error_blocks(self):
        code, _, err = bash("ls", env={"IAC_AZURE_AGENT_HOME": "relative/path"})
        self.assertEqual(code, 2)
        self.assertIn("failing closed", err)

    def test_missing_shared_module_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "hooks"))
            copy = os.path.join(d, "hooks", "azure_guard.py")
            with open(HOOK) as src, open(copy, "w") as dst:
                dst.write(src.read())
            p = subprocess.run([sys.executable, copy], capture_output=True, text=True,
                               input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}))
            self.assertEqual(p.returncode, 2)


class HooksJson(unittest.TestCase):
    def test_hooks_json_wires_the_guard(self):
        with open(os.path.join(PLUGIN, "hooks", "hooks.json")) as f:
            cfg = json.load(f)
        entries = cfg["hooks"]["PreToolUse"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["matcher"], "Bash|Write|Edit|NotebookEdit")
        cmd = entries[0]["hooks"][0]["command"]
        self.assertIn('"${CLAUDE_PLUGIN_ROOT}/hooks/azure_guard.py"', cmd)


if __name__ == "__main__":
    unittest.main()


class WorkingCopyLimits(unittest.TestCase):
    """Write limits and the git guard for <project>/.iac-azure-agent/workspace/..."""

    AGENT = "iac-azure-agent:iac-azure-agent"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.home = os.path.join(self.tmp, "store")
        self.project = os.path.join(self.tmp, "project")
        os.makedirs(os.path.join(self.project, ".git"))
        self.ws = os.path.join(self.project, ".iac-azure-agent", "workspace", "example-org--infra")
        os.makedirs(os.path.join(self.ws, "infra", "modules"))
        os.makedirs(os.path.join(self.ws, ".git"))
        self.env = {"IAC_AZURE_AGENT_HOME": self.home}
        self.configure("infra")

    def tearDown(self):
        self._tmp.cleanup()

    def configure(self, infra_root):
        from unittest import mock
        from config.store import ConfigStore
        with mock.patch.dict(os.environ, self.env):
            store = ConfigStore(self.project)
            store.set_repo("example-org/infra", default_branch="main")
            if infra_root:
                store.set_value("infra_root", infra_root)

    def write(self, path, agent=None, tool="Write"):
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool,
                   "tool_input": {"file_path": path}, "cwd": self.project}
        if agent:
            payload["agent_type"] = agent
        return run(payload, env=self.env)

    def git(self, command, cwd=None, agent=None):
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                   "tool_input": {"command": command}, "cwd": cwd or self.project}
        if agent:
            payload["agent_type"] = agent
        return run(payload, env=self.env)

    def test_infrastructure_root_is_writable(self):
        for rel in ("infra/main.bicep", "infra/modules/storage.bicep", "infra/README.md",
                    "infra/parameters/dev.bicepparam", "infra/docs/architecture.md"):
            for agent in (None, self.AGENT, "iac-azure-agent"):
                for tool in ("Write", "Edit"):
                    code, _, err = self.write(os.path.join(self.ws, rel), agent, tool)
                    self.assertEqual(code, 0, (rel, agent, err))

    def test_rest_of_the_working_copy_is_not(self):
        for rel in ("README.md", "src/app.py", ".github/workflows/deploy.yml", ".git/config",
                    "infra/.git/hooks/pre-commit", "infrastructure/main.bicep", "infra",
                    "infra/../src/app.py"):
            for agent in (None, self.AGENT):
                code, _, err = self.write(os.path.join(self.ws, rel), agent)
                self.assertEqual(code, 2, (rel, agent))
                self.assertIn("blocked", err)

    def test_another_repositorys_working_copy_is_not_writable(self):
        other = os.path.join(self.project, ".iac-azure-agent", "workspace", "example-org--other")
        code, _, _ = self.write(os.path.join(other, "infra", "main.bicep"))
        self.assertEqual(code, 2)

    def test_symlink_out_of_the_infrastructure_root_is_followed(self):
        os.symlink(os.path.join(self.ws, "src"), os.path.join(self.ws, "infra", "link"))
        code, _, _ = self.write(os.path.join(self.ws, "infra", "link", "app.py"))
        self.assertEqual(code, 2)

    def test_no_infrastructure_root_fails_closed(self):
        from unittest import mock
        from config.store import ConfigStore
        with mock.patch.dict(os.environ, self.env):
            ConfigStore(self.project).unset_value("infra_root")
        code, _, _ = self.write(os.path.join(self.ws, "infra", "main.bicep"))
        self.assertEqual(code, 2)

    def test_agent_cannot_write_outside_the_working_copy(self):
        for path in (os.path.join(self.project, "src", "app.py"),
                     os.path.join(self.project, ".claude", "settings.json"),
                     os.path.join(self.tmp, "elsewhere.txt")):
            code, _, err = self.write(path, self.AGENT)
            self.assertEqual(code, 2, path)
            self.assertIn("outside it", err)

    def test_other_sessions_keep_normal_write_access(self):
        for agent in (None, "general-purpose", "other-plugin:reviewer"):
            code, _, err = self.write(os.path.join(self.project, "src", "app.py"), agent)
            self.assertEqual(code, 0, (agent, err))

    def test_git_writes_in_the_working_copy_are_blocked(self):
        for cmd in ("git -C %s commit -m x" % self.ws, "git -C %s push origin HEAD" % self.ws,
                    "cd %s && git add -A && git commit -m x" % self.ws,
                    "git -C %s reset --hard" % self.ws, "git -C %s checkout -b other" % self.ws,
                    "git -C %s clean -fd" % self.ws, "git -C %s branch -D iac/x" % self.ws,
                    "git -C %s branch newbranch" % self.ws, "git -C %s stash" % self.ws,
                    "git -C %s remote set-url origin https://github.com/a/b" % self.ws,
                    "git -C %s config user.name x" % self.ws,
                    "git -c core.editor=true -C %s rebase main" % self.ws):
            code, _, err = self.git(cmd)
            self.assertEqual(code, 2, cmd)
            self.assertIn("blocked", err)
        for cmd in ("git commit -m x", "git push"):  # cwd inside the working copy
            self.assertEqual(self.git(cmd, cwd=os.path.join(self.ws, "infra"))[0], 2, cmd)

    def test_git_reads_in_the_working_copy_pass(self):
        for cmd in ("git -C %s status --porcelain" % self.ws, "git -C %s diff" % self.ws,
                    "git -C %s log --oneline -5" % self.ws, "git -C %s branch --show-current" % self.ws,
                    "git -C %s remote -v" % self.ws, "git -C %s rev-parse HEAD" % self.ws,
                    "git -C %s config --get remote.origin.url" % self.ws,
                    "git --no-pager -C %s show HEAD" % self.ws):
            code, _, err = self.git(cmd)
            self.assertEqual(code, 0, (cmd, err))

    def test_git_elsewhere_is_untouched_for_other_sessions_but_not_for_the_agent(self):
        self.assertEqual(self.git("git commit -m 'my own work' && git push")[0], 0)
        self.assertEqual(self.git("git commit -m x", agent=self.AGENT)[0], 2)
        self.assertEqual(self.git("git status", agent=self.AGENT)[0], 0)


class PublishingGuards(unittest.TestCase):
    AGENT = "iac-azure-agent:iac-azure-agent"
    ROOT = "/home/u/.claude/plugins/cache/claude-agents/iac-azure-agent/0.4.0"

    def call(self, command, agent=None):
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                   "tool_input": {"command": command}, "cwd": "/tmp"}
        if agent:
            payload["agent_type"] = agent
        return run(payload)

    def test_publish_and_accepting_a_finding_prompt_the_user(self):
        for cmd in ('python3 "%s/tools/github/cli.py" publish req-20261004-000000-abcdef --message "Add storage"' % self.ROOT,
                    'python3 %s/tools/github/cli.py --project-dir /p publish req-1 --message x' % self.ROOT,
                    'python3 "%s/tools/github/cli.py" install-workflow' % self.ROOT,
                    'python3 %s/tools/github/cli.py --project-dir /p install-workflow' % self.ROOT,
                    'python3 "%s/tools/config/cli.py" set accepted_findings.CKV_AZURE_206@infra/main.bicep:Microsoft.Storage/storageAccounts.sa "LRS is fine in dev"' % self.ROOT):
            for agent in (None, self.AGENT):
                code, out, err = self.call(cmd, agent)
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "ask", cmd)

    def test_reading_the_workflow_state_does_not_prompt(self):
        code, out, err = self.call('python3 "%s/tools/github/cli.py" workflow-status' % self.ROOT, self.AGENT)
        self.assertEqual((code, out.strip()), (0, ""), err)

    def test_deploy_prompts_and_planning_does_not(self):
        code, out, err = self.call('python3 "%s/tools/deploy/cli.py" deploy req-20261004-000000-abcdef' % self.ROOT, self.AGENT)
        self.assertEqual(code, 0, err)
        d = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(d["permissionDecision"], "ask")
        self.assertIn("DEPLOYS to Azure", d["permissionDecisionReason"])
        for cmd in ('python3 "%s/tools/deploy/cli.py" plan req-1 --environment dev --tenant t --subscription s' % self.ROOT,
                    'python3 "%s/tools/deploy/cli.py" verify req-1' % self.ROOT,
                    'python3 "%s/tools/deploy/cli.py" status req-1' % self.ROOT,
                    'python3 "%s/tools/deploy/cli.py" context' % self.ROOT):
            code, out, err = self.call(cmd, self.AGENT)
            self.assertEqual((code, out), (0, ""), cmd)

    def test_direct_azure_deployment_stays_blocked(self):
        for cmd in ("az deployment sub create -l westeurope -f infra/main.bicep",
                    "az deployment group create -g rg -f main.bicep", "az group delete -n rg --yes",
                    "az vm delete -g rg -n vm --yes"):
            self.assertEqual(self.call(cmd, self.AGENT)[0], 2, cmd)
            self.assertEqual(self.call(cmd)[0], 2, cmd)

    def test_preview_verify_and_ordinary_settings_do_not_prompt(self):
        for cmd in ('python3 "%s/tools/github/cli.py" preview req-1' % self.ROOT,
                    'python3 "%s/tools/github/cli.py" verify req-1' % self.ROOT,
                    'python3 "%s/tools/config/cli.py" set region westeurope' % self.ROOT):
            code, out, err = self.call(cmd, self.AGENT)
            self.assertEqual((code, out), (0, ""), cmd)

    def test_agent_cannot_use_gh_to_write(self):
        for cmd in ("gh pr merge 12 --squash", "gh pr create --title x --body y", "gh pr close 3",
                    "gh repo delete example-org/infra --yes", "gh repo edit --visibility public",
                    "gh api -X DELETE repos/a/b/git/refs/heads/main",
                    "gh api repos/a/b/pulls/1/merge --method PUT",
                    "gh api repos/a/b/issues -f title=x", "gh secret set TOKEN", "gh auth token",
                    "gh workflow run deploy.yml", "gh release create v1"):
            code, _, err = self.call(cmd, self.AGENT)
            self.assertEqual(code, 2, cmd)
            self.assertIn("blocked", err)

    def test_agent_can_use_gh_to_read(self):
        for cmd in ("gh auth status", "gh pr view 12 --json state", "gh pr list --head iac/x",
                    "gh pr checks 12", "gh repo view example-org/infra", "gh api repos/a/b",
                    "gh api repos/a/b/pulls --method GET", "gh --version"):
            code, _, err = self.call(cmd, self.AGENT)
            self.assertEqual(code, 0, (cmd, err))

    def test_other_sessions_keep_gh(self):
        for cmd in ("gh pr merge 12 --squash", "gh pr create --title x --body y"):
            self.assertEqual(self.call(cmd)[0], 0, cmd)

# Recovery procedures

What to do when a step fails or is interrupted. Every procedure starts by reading the
real state; none of them deletes anything or forces a push. `<cli>` is
`python3 <plugin>/tools`. Commands that change a record, GitHub or Azure prompt you first.

The rule behind all of them: a step whose outcome is unknown is resolved by reading
GitHub or Azure, never by running the step again blindly.

## Find out where a request stands

```text
<cli>/state/cli.py list
<cli>/state/cli.py resume <id>
```

`resume` reports the state, the next action and the last step. A step that was in progress
when the session ended is marked `interrupted`: its outcome is unknown until you read it
from the service it was talking to (below).

## Validation failed, or the files changed after validation

1. `<cli>/state/cli.py back <id> --to IMPLEMENTATION --reason "<why>"`. Earlier check
   results are marked stale; nothing is deleted.
2. Fix the Bicep, then `<cli>/workspace/cli.py record-files <id>`.
3. Advance to VALIDATION and run `<cli>/validate/cli.py run <id>`.

A finding you decide to accept is accepted for one resource: copy `accept_key` from the
validation output and run `<cli>/config/cli.py set <accept_key> "<reason>"`.

Acceptances made before 0.6.0 (a bare check ID such as `accepted_findings.CKV_AZURE_35`)
are no longer applied, so those findings fail again. Accept each one for the resource it
concerns, then remove the old entry with `<cli>/config/cli.py unset accepted_findings.<check id>`.

## Publishing stopped part-way

`publish` records each stage that was read back. Run `<cli>/github/cli.py verify <id>`
to see what GitHub has, then:

| Message | Meaning | Do |
| --- | --- | --- |
| `git has no author identity` | Nothing was committed | Set `user.name` and `user.email` yourself, publish again |
| `GitHub rejected the push: the remote branch ... has commits` | Someone else changed `iac/<id>`. Nothing was forced | Look at the branch on GitHub. This needs a person; the tool will not overwrite it |
| `GitHub refused the push (permission or branch protection)` | Nothing was pushed | Fix access or protection yourself, publish again |
| `the branch was pushed but the pull request was not created` | Branch is on GitHub, no pull request | Publish again: it finds the branch at the same commit and only opens the pull request |
| `the push returned success but the remote branch is at ...` | Not published | Check the network and GitHub's status, then publish again |

Publishing again is always safe: it commits nothing new when the files are unchanged,
never forces, and reuses an open pull request.

## The workflow installer stopped part-way

`<cli>/github/cli.py workflow-status` shows whether the default branch holds the template.
`install-workflow` can be run again: the branch name is derived from the file content, so
a rerun continues on the same branch and reuses the open pull request.

| Message | Do |
| --- | --- |
| `it lacks the workflow scope` | Run `gh auth refresh -s workflow` yourself, then install again |
| `the working copy has uncommitted changes` | Finish or record the request in progress first; the installer does not carry work onto another branch |
| `the workflow branch ... holds other changes` | A branch with that name was changed by hand. Inspect it; delete it yourself if it is not wanted |

If the working copy was left on `iac/validation-workflow-...` (the process was killed),
check out the branch you were on with `git checkout <branch>` in the working copy
yourself: the agent does not switch branches outside its tools.

## Planning (what-if) failed

Nothing was changed. The message classifies the failure (sign-in, authorization, policy,
quota, SKU, template, conflict, temporary) and says what can be done. Fix the cause and
run `<cli>/deploy/cli.py plan <id> ...` again. A template problem goes back to
IMPLEMENTATION as in "Validation failed".

## Deployment was refused

Nothing was deployed. The refusal names the check that failed:

| Message | Do |
| --- | --- |
| `no valid deployment approval` | The target, change set, commit or inputs changed since approval. Plan again, review, approve again |
| `what-if now differs from the approved change set` | Azure changed since approval. Plan again and approve the new change set |
| `the compiled template or parameters differ` | An environment variable a parameter file reads changed. Restore it, or plan and approve again |
| `a production deployment needs the pull request to be merged first` | Review and merge the pull request yourself, then deploy again. The approval stays valid |
| `merged at ..., not at the published and approved commit` | Commits were added to the pull request after publishing. What was merged was not validated by this request: cancel it and start a new request from the merged code |
| `could not read the pull request from GitHub` | Whether it is merged is unknown. Check `gh auth status` and the network, deploy again |
| `the working copy is not at the published commit` | Something changed the working copy. Inspect it; do not deploy until it is the published commit, clean |
| `wrong_context: az is signed in to subscription ...` | Sign in or switch yourself (`az login`, `az account set`); the tool never does |

## Deployment failed or was partial

The record says `failed` or `partial` and lists what Azure reports as created before the
failure. Nothing was rolled back or deleted, and the create is never retried.

1. `<cli>/deploy/cli.py status <id>` to read the deployment and its operations from Azure.
2. Decide what to do with the resources that were created. Removing them is yours to do
   (`az group delete`, the portal); the agent does not delete.
3. To try again after fixing the cause: `<cli>/state/cli.py back <id> --to IMPLEMENTATION`
   (or `--to GIT_REVIEW` when the code is right and the cause was outside it), then plan,
   review the new change set, approve and deploy. A new what-if shows the resources that
   already exist as unchanged or modified, not as new.

## Deployment was interrupted (session closed, machine slept, network dropped)

The step is `interrupted` and its outcome is unknown. Deploying again is refused.

```text
<cli>/deploy/cli.py status <id> --record
```

| Azure says | Result |
| --- | --- |
| No deployment with this name | Recorded as not started. It is safe to deploy |
| Succeeded | Recorded as succeeded. Advance to VERIFICATION |
| Failed or Canceled | Recorded as failed, or partial with the created resources. See above |
| Running | Nothing is recorded. Wait and run the command again |

## Verification failed

A planned resource is missing, a VM is not running, or resources exist that were not in
the plan. The deployment is not complete until verification passes.

1. Read the `problems` and `also_present` lists in the output.
2. A stopped VM: start it yourself, then run `<cli>/deploy/cli.py verify <id>` again.
3. A missing resource: read the deployment with `status`. If Azure reports it as created,
   wait and verify again; if not, treat it as a failed deployment.
4. An unplanned resource: find out who created it. The agent reports it and does not
   remove it.

## A record or the config is corrupt (exit code 4)

The file was left untouched. Do not edit it by hand: approvals are hash-checked.

- Config: move the file away yourself (`config/cli.py path` prints where it is) and run
  `/iac-setup` again.
- Request: start a new request. What the old one did on GitHub (branch, pull request) and
  in Azure (deployment `iac-<id>`) is still there and can be read with `gh` and `az`.

## Two sessions wrote the same request (exit code 3, "changed on disk")

Nothing was saved by the second writer. Run the command again.

## Give up on a request

`<cli>/state/cli.py cancel <id> --reason "<why>"`. The record is kept. The branch, the
pull request and anything deployed stay as they are: close the pull request and remove
resources yourself.

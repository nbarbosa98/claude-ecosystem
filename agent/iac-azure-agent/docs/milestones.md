# Milestones

One pull request per milestone. Each ends with a report (what was built, how it was
verified, what is open; facts and assumptions kept apart) and stops for owner review.

| # | Milestone | Scope | Status |
| --- | --- | --- | --- |
| 1 | Foundation | Plugin manifest and marketplace entry; orchestrator agent definition; per-user, per-project config store and CLI; workflow state machine with hash-bound approvals, high-risk confirmation and resume; secret guard; PreToolUse shell guard; unit tests; ADR log, security model, README | DONE (merged, PR #8) |
| 2 | Discovery | First-run repository setup with read-only inspection through `gh`; `/iac-setup`, `/iac-repo`, `/iac-discover`; request profile; adaptive question rounds from a topic catalog; must-confirm topics; assumptions review; architecture proposal sections, rendering and approval; required permission rules checked before deployment approval | DONE (merged, PR #9) |
| 3 | Bicep engineering | Working copy under the project (clone, sync, local work branch); inventory of existing Bicep; `/iac-implement` and the Bicep standards; hook write limits and git guard; changed files recorded from git; validation (structure, Bicep-only, secret scan, `bicep build`, `build-params`, `lint`, Checkov) recorded as check results bound to a file hash | DONE (merged, PR #10) |
| 4 | GitHub integration | Publish preview; commit of the infrastructure directory on `iac/<id>`; push without force; remote head read back; pull request created or reused and fetched; branch, commit and PR recorded; partial failures recorded and resumable; accepted scanner findings; `gh` limited to reads for the agent | DONE (merged, PR #11) |
| 5 | Azure integration | Context check against the confirmed tenant and subscription; Azure validation and what-if; change set, risk flags and uncertain changes; deployment through one tool that re-checks approval, inputs and a fresh what-if; classified failures; failed and partial outcomes; interrupted runs resolved from Azure; verification by reading resources back | DONE (merged, PRs #12 and #13) |
| 6 | Automation and hardening | GitHub Actions validation workflow installed by a tool from a fixed template; production deploys only from a merged pull request; scanner findings accepted per resource; security analysis; mocked end-to-end test of the production path; recovery procedures; final documentation | IN REVIEW |

## Milestone 1 exit criteria

- `python3 -m unittest discover -s tools/tests -v` passes with no network, Azure or GitHub access.
- `claude plugin validate .` passes from the repository root.
- No file for a later milestone exists as a stub.

## Milestone 2 exit criteria

- The unit tests pass with no network, Azure or GitHub access (GitHub is a fake `gh`).
- `claude plugin validate .` passes from the repository root.
- A first run asks for the repository once, saves it only after confirmation, and a new
  process finds it again.
- A request cannot leave DISCOVERY with an unanswered must-confirm topic or unreviewed
  assumptions, and cannot reach APPROVAL with an incomplete proposal.
- Not verified in this milestone: the skills and the hook inside a live Claude Code session.

## Milestone 3 exit criteria

- The unit tests pass with no network: GitHub is a local bare repository, `bicep` and
  `checkov` are fakes; the two real-tool tests pass where the tools are installed.
- `claude plugin validate .` passes from the repository root.
- A request cannot leave VALIDATION with a failed check, and cannot leave it with a
  skipped or unavailable check unless that is accepted explicitly.
- The agent cannot write outside the infrastructure root of the working copy with Write
  or Edit, and a change made there any other way is refused when files are recorded.
- Not verified in this milestone: the skill and the hook inside a live Claude Code
  session, and the quality of Bicep the model writes (there are no prompt evals yet).

## Milestone 4 exit criteria

- The unit tests pass with no network: pushes go to a local bare repository, pull
  requests to a fake `gh`.
- Files that changed after validation, or that lie outside the infrastructure root, are
  never committed.
- The default branch of the remote is unchanged by every test.
- A push or pull request is reported as done only after it was read back.
- Not verified in this milestone: a publish against GitHub itself, and `/iac-publish`
  inside a live Claude Code session.

## Milestone 5 exit criteria

- The unit tests pass with no Azure access (a fake `az`), including one test that drives
  the whole workflow from request to completion through the CLIs.
- No deployment happens without a valid approval, with a forged record state, after the
  what-if result changed, after the files or inputs changed, or against another subscription.
- A failed create is recorded as failed or partial and is never retried; nothing is deleted.
- Verified against the real services once (2026-10-05): a request for a small Linux VM
  went from discovery to a published pull request, Azure what-if, an approved
  subscription-scope deployment and verification.
- Not verified: the skill, prompts and hook inside a live Claude Code session; high-risk
  and failure paths against real Azure; resource-group scope against real Azure.

## Milestone 6 exit criteria

- The unit tests pass with no network, Azure or GitHub access, including one test that
  drives the production path through the CLIs: workflow pull request, publish, what-if,
  approval and high-risk confirmation, refusal while unmerged, merge, deployment,
  verification, completion.
- A production deployment is refused while the pull request is open, closed, merged into
  another branch, merged with later commits, or when GitHub cannot be read.
- The workflow installer changes one file, never touches the default branch, never
  forces, and hands the working copy back on the branch it was on.
- An acceptance covers one resource; an acceptance from an earlier version is not applied.
- The workflow's shell steps were run locally with the real Bicep CLI and Checkov against
  the first real repository (2026-10-10).
- Not verified: the workflow on GitHub Actions; the installer and the merged-first rule
  against GitHub itself; the skills, prompts and hook inside a live Claude Code session;
  high-risk and failure paths and resource-group scope against real Azure.

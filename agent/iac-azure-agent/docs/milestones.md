# Milestones

One pull request per milestone. Each ends with a report (what was built, how it was
verified, what is open; facts and assumptions kept apart) and stops for owner review.

| # | Milestone | Scope | Status |
| --- | --- | --- | --- |
| 1 | Foundation | Plugin manifest and marketplace entry; orchestrator agent definition; per-user, per-project config store and CLI; workflow state machine with hash-bound approvals, high-risk confirmation and resume; secret guard; PreToolUse shell guard; unit tests; ADR log, security model, README | IN REVIEW |
| 2 | Discovery | First-run repository setup; adaptive question rounds; recording requirements, questions and assumptions; architecture proposals presented for approval | NOT STARTED |
| 3 | Bicep engineering | Inspection of the configured repository; generation standards (modules, naming, tagging, parameters); writing Bicep; `bicep build`, lint and static validation recorded as check results | NOT STARTED |
| 4 | GitHub integration | Branch, commit and push; verification that the remote head matches; pull request creation; recording branch, commit and PR on the request | NOT STARTED |
| 5 | Azure integration | Identity and context checks; read-only inventory; what-if and change-set extraction with risk flags; deployment approval; deployment through one approval-checking tool; post-deployment verification | NOT STARTED |
| 6 | Automation and hardening | GitHub Actions validation workflow; security analysis; mocked end-to-end test; recovery procedures; final documentation | NOT STARTED |

## Milestone 1 exit criteria

- `python3 -m unittest discover -s tools/tests -v` passes with no network, Azure or GitHub access.
- `claude plugin validate .` passes from the repository root.
- No file for a later milestone exists as a stub.

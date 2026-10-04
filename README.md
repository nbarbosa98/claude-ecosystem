# claude-plugins

My personal library of Claude Code **agents**, **skills** and **MCP servers**, packaged as **plugins** and published from a single **plugin marketplace**. Add this repository to Claude Code once, then install, update, or remove any of them on demand.

The repository is also a working portfolio of applied agentic AI: each plugin is scoped to a real task, has an explicit tool budget, and documents the design and security decisions behind it.

---

## Contents

- [Why this repo exists](#why-this-repo-exists)
- [Quick start](#quick-start)
- [Catalog](#catalog)
- [Repository layout](#repository-layout)
- [How an agent is built](#how-an-agent-is-built)
- [Design principles](#design-principles)
- [Adding a new plugin](#adding-a-new-plugin)
- [Versioning and syncing](#versioning-and-syncing)
- [Security](#security)
- [Roadmap](#roadmap)
- [License](#license)

---

## Why this repo exists

| Goal | How it is achieved |
| --- | --- |
| **One source of truth** | Every agent, skill and MCP server I use lives here, versioned in Git, instead of being copied between machines and projects. |
| **Install on demand** | Each one is a self-contained plugin. Install only what a project needs. |
| **Sync anywhere** | Pull the latest versions into any Claude Code environment with a single command. |
| **Show the work** | Each plugin documents its purpose, tool permissions, limits, and trade-offs. |

---

## Quick start

Requires [Claude Code](https://code.claude.com/docs) with plugin support.

**1. Add the marketplace** (one time per machine):

```text
/plugin marketplace add nbarbosa98/claude-plugins
```

**2. Browse and install a plugin:**

```text
/plugin                                   # interactive browser
/plugin install <plugin-name>@claude-agents
```

**3. Pull the latest versions at any time:**

```text
/plugin marketplace update claude-agents
```

The repository is `claude-plugins`; the marketplace it publishes is named `claude-agents`, which is the name you type after `@`.

After installing, the plugin's subagents, skills, commands and MCP servers are available in Claude Code. Subagents can be invoked by name or picked automatically by Claude based on their `description`.

### Enable for a whole team or project

To make the marketplace available to everyone who works in a project, commit this to that project's `.claude/settings.json`:

```json
{
  "extraKnownMarketplaces": {
    "claude-agents": {
      "source": { "source": "github", "repo": "nbarbosa98/claude-plugins" }
    }
  },
  "enabledPlugins": {
    "<plugin-name>@claude-agents": true
  }
}
```

---

## Catalog

Each plugin folder contains its own `README.md` covering usage, required tools, example prompts, and known limitations.

### Agents — [`agent/`](agent)

| Plugin | What it does | Components | Status |
| --- | --- | --- | --- |
| [`email-orchestrator`](agent/email-orchestrator) | Briefs, categorizes, tracks unanswered important emails, flags phishing, cleans up the inbox, and drafts and sends email (each after you confirm) across Gmail, Outlook, and other connected mail services | 1 subagent, 7 skills (`/mail`, `/mail-*`) | `0.3.1` — beta |
| [`iac-azure-agent`](agent/iac-azure-agent) | Plain-English Azure infrastructure in Bicep: repository setup, adaptive requirements discovery, architecture proposals for approval, modular Bicep written and validated (Bicep CLI, secret scan, Checkov) in a local working copy, approval-gated resumable workflow, guards against direct Azure changes. Milestone 3 of 6; no GitHub publishing or Azure deployment yet | 1 subagent, 4 skills (`/iac-setup`, `/iac-repo`, `/iac-discover`, `/iac-implement`), 1 PreToolUse hook, 7 Python CLIs | `0.3.0` - in development |
| [`intune-rmd-scr-agent`](agent/intune-rmd-scr-agent) | Orchestrator for building, testing and deploying Intune Remediation scripts on a Windows fleet (lab tenant only) | Project opened as its own root, not an installable plugin (see its [ADR-001](agent/intune-rmd-scr-agent/docs/decisions.md)) | In development |

### Skills — [`skill/`](skill)

| Plugin | What it does | Components | Status |
| --- | --- | --- | --- |
| [`doc-skill`](skill/doc-skill) | Documents the process just carried out, or any topic you name, in a fixed structure (Title, Summary, Scope, Details or Step by step, Additional considerations, References) and saves it as PDF, HTML, MD, TXT or DOCX | 1 skill (`/doc-skill`) | `0.1.0` — beta |

### MCP servers — [`mcp/`](mcp)

None published yet.

---

## Repository layout

Every plugin lives in a folder named after its **type** and its **name**: `<type>/<plugin-name>/`.

```text
claude-plugins/
├── .claude-plugin/
│   └── marketplace.json        # Marketplace manifest: lists every installable plugin in this repo
├── agent/<plugin-name>/        # Main component is a subagent (may bundle the skills it depends on)
├── skill/<plugin-name>/        # Main component is one or more skills
├── mcp/<plugin-name>/          # Main component is an MCP server configuration
├── hook/<plugin-name>/         # Main component is a set of hooks (created when the first one is added)
├── SECURITY.md                 # Security policy and recommendations for this repo
└── README.md
```

Inside each plugin folder:

```text
<type>/<plugin-name>/
├── .claude-plugin/
│   └── plugin.json             # Plugin metadata (name, version, description, author)
├── agents/                     # Subagents: Markdown files with YAML frontmatter
├── skills/                     # Agent Skills (<skill>/SKILL.md)
├── commands/                   # Optional: slash commands
├── hooks/                      # Optional: hooks.json for lifecycle automation
├── .mcp.json                   # Optional: MCP servers the plugin ships
└── README.md                   # Usage, safety model and changelog for this plugin
```

Only `plugin.json` is required per plugin. Every other folder is included when the plugin needs it.

### Naming rules

- **Type folder** = the plugin's main component: `agent`, `skill`, `mcp`, or `hook`.
- **Plugin name** is kebab-case and identical in three places: the folder (`agent/email-orchestrator`), the `name` in its `plugin.json`, and its entry in `marketplace.json`.
- **Bundles stay together.** A plugin that ships an agent plus the skills it relies on goes under `agent/`, not split across folders: its skills reference the agent through `${CLAUDE_PLUGIN_ROOT}` and are namespaced by the plugin name (`email-orchestrator:mail-brief`).

### Marketplace manifest

`.claude-plugin/marketplace.json` registers each installable plugin, with `source` pointing at its type folder:

```json
{
  "name": "claude-agents",
  "owner": { "name": "Nelson Barbosa" },
  "plugins": [
    {
      "name": "example-agent",
      "source": "./agent/example-agent",
      "description": "One-line summary of what the agent does."
    },
    {
      "name": "example-skill",
      "source": "./skill/example-skill",
      "description": "One-line summary of what the skill does."
    }
  ]
}
```

---

## How an agent is built

A subagent is a Markdown file in a plugin's `agents/` folder. The frontmatter defines **when** it runs and **what it may do**; the body is its system prompt.

```markdown
---
name: example-agent
description: Use this agent when <specific trigger>. It <specific outcome>.
tools: Read, Grep, Glob
model: sonnet
---

You are a specialist in <domain>.

## Goal
<The single outcome this agent is responsible for.>

## Process
1. <Step>
2. <Step>

## Output
<Exact format of the result returned to the caller.>

## Boundaries
- <What the agent must not do.>
- <When to stop and hand back to the user.>
```

| Field | Purpose |
| --- | --- |
| `name` | Unique identifier used to invoke the agent. |
| `description` | Routing signal. Claude uses it to decide when to delegate, so it must state the trigger clearly. |
| `tools` | Allowlist of tools. Omit to inherit all tools; restrict it wherever possible. |
| `model` | Optional model override (for example `sonnet`, `opus`, `haiku`, or `inherit`). |

Skills and MCP servers follow the templates in [`skill/README.md`](skill/README.md) and [`mcp/README.md`](mcp/README.md).

---

## Design principles

These are the rules every plugin in this repository follows.

1. **Single responsibility.** One plugin, one clearly bounded job. Broad "do everything" agents route poorly and are hard to evaluate.
2. **Least privilege.** Grant the smallest tool set that completes the task. Read-only agents do not get `Edit`, `Write`, or `Bash`.
3. **Precise routing.** The `description` states when to use the agent or skill and when not to, so automatic delegation stays predictable.
4. **Structured output.** Each agent defines the exact shape of its result so the calling agent or user can act on it without re-parsing.
5. **Explicit stop conditions.** Agents say when to halt and hand back instead of guessing, especially before destructive or outward-facing actions.
6. **Right-sized model.** Use a smaller, faster model for narrow, high-volume tasks and a larger one for reasoning-heavy work.
7. **Documented trade-offs.** Every plugin README records known limitations and failure modes, not only the happy path.
8. **Tested before release.** Each plugin is exercised against representative prompts, including edge cases and prompts that should *not* trigger it, before its version is bumped.
9. **Secure by default.** Untrusted content is data, not instructions; secrets never enter the repo; MCP servers are version-pinned. See [SECURITY.md](SECURITY.md).

---

## Adding a new plugin

1. Pick the type folder (`agent`, `skill`, `mcp`, `hook`) and create the plugin folder:
   ```text
   <type>/<plugin-name>/.claude-plugin/plugin.json
   <type>/<plugin-name>/agents/<agent-name>.md        # agent
   <type>/<plugin-name>/skills/<skill-name>/SKILL.md  # skill
   <type>/<plugin-name>/.mcp.json                     # mcp
   <type>/<plugin-name>/README.md
   ```
2. Fill in `plugin.json`:
   ```json
   {
     "name": "<plugin-name>",
     "version": "0.1.0",
     "description": "One-line summary.",
     "author": { "name": "Nelson Barbosa" },
     "repository": "https://github.com/nbarbosa98/claude-plugins"
   }
   ```
3. Register the plugin in `.claude-plugin/marketplace.json` with `"source": "./<type>/<plugin-name>"`.
4. Validate and test locally from the repository root:
   ```text
   claude plugin validate .
   /plugin marketplace add ./
   /plugin install <plugin-name>@claude-agents
   ```
5. Add the plugin to the [Catalog](#catalog), run through the [SECURITY.md review checklist](SECURITY.md#pull-request-checklist), and open a pull request.

---

## Versioning and syncing

- Plugins follow [Semantic Versioning](https://semver.org/): **MAJOR** for breaking changes to behavior or output format, **MINOR** for new capabilities, **PATCH** for fixes and prompt tuning.
- Bump the version in `plugin.json` on every release so installed copies can detect updates.
- The default branch is the published channel. Anything merged there is what `/plugin marketplace update claude-agents` delivers.
- To roll back, revert the change on the default branch (or pin a project to an earlier commit or tag) and run the update command again.

---

## Security

Read [SECURITY.md](SECURITY.md) before adding a plugin or installing one. It covers how to report a vulnerability, the repository settings this marketplace should run with, the rules for each component type (agents, skills, MCP servers, hooks), supply-chain pinning, the pull request checklist, and incident response.

---

## Roadmap

- [x] Marketplace manifest and first plugin
- [x] Type-based layout (`agent/`, `skill/`, `mcp/`) and security policy
- [ ] Per-plugin evaluation prompts (should-trigger and should-not-trigger cases)
- [ ] CI validation of `marketplace.json`, `plugin.json`, agent frontmatter, secrets and hidden characters
- [ ] First published skill and MCP server
- [ ] Multi-agent workflows that compose several agents from this catalog

---

## License

No license has been chosen yet. Until one is added, all rights are reserved by the author.

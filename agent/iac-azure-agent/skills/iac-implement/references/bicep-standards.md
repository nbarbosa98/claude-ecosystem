# Bicep standards for iac-azure-agent

Follow the repository's existing conventions first. Where it has none, use these.

Facts about Azure change. Before relying on a resource type, API version, property or
Azure Verified Module version, check it: `bicep build` reports unknown types and
properties, and the linter rule `use-recent-api-versions` reports stale versions. If you
cannot check something, say it is unverified in the report. Do not state it as fact.

## Layout

```text
<infra_root>/
  README.md                 required: the documentation for this deployment
  main.bicep                entry point; composes modules, declares no resources of its own
                            unless the deployment is a single small resource
  bicepconfig.json          linter configuration (see below), if the repository has none
  parameters/<env>.bicepparam   one per environment actually used
  modules/<component>.bicep     one per logical component
```

- Create only the modules and environments the request needs. No empty files, no examples.
- A module is a component (networking, storage, monitoring, identity, compute), not a
  single resource. Do not wrap one resource in a module unless it is reused.
- Shared architecture lives in modules. Anything that differs per environment is a
  parameter set in the `.bicepparam` file.

## Azure Verified Modules

Prefer an Azure Verified Module (`br/public:avm/res/<provider>/<type>:<version>`) when one
covers the resource and the repository does not already define it another way. Pin an
exact version; never a floating tag. `bicep build` must be able to restore it, which needs
network access; if the restore fails, report that and do not claim the module was checked.
Write the resource directly when the module would hide a setting the user decided, or
when the repository's own modules already cover it.

## Parameters

- `@description` on every parameter. `@allowed`, `@minLength`, `@maxLength`, `@minValue`,
  `@maxValue` where they catch mistakes.
- `@secure()` on every parameter that could carry a secret. Never give it a default.
- Use typed parameters (user-defined types) for structured input instead of loose objects.
- `param location string = resourceGroup().location` at resource-group scope; never a
  literal region inside a module.
- Never hard-code a subscription ID, tenant ID, object ID or resource ID. Pass them in.

## Names and tags

- Use the naming convention saved in the project config. Without one, Cloud Adoption
  Framework abbreviations: `<type>-<workload>-<environment>-<region>`; for types that
  allow no hyphens (storage accounts, container registries), the same parts concatenated.
- Names must be stable across deployments. Use `uniqueString(resourceGroup().id)` only as
  a suffix for globally unique names, never a value that changes per deployment.
- One `tags` object parameter applied to every resource that supports tags. At least
  `environment`, `workload`, `owner`; `costCenter` when the user has one.

## Resources

- Explicit API version on every resource. Let Bicep infer dependencies from symbolic
  references; use `dependsOn` only when there is no reference to infer from.
- `existing` only for resources that are really outside this deployment. Never declare
  the same resource twice.
- Outputs: resource IDs, names and endpoints that another deployment needs. Never a key,
  connection string, password or token. Do not output the result of a `list*` function.
- Comments: one line where a design decision or a security-sensitive setting is not
  obvious from the code. No comments that repeat the code.

## Security defaults

Set these explicitly. A default that is safe today may not stay the default.

- Managed identity for every workload that calls another Azure service. No access keys,
  no shared secrets, no connection strings with embedded keys.
- RBAC role assignments scoped to the single resource that needs them, with built-in
  roles by ID, to named principals passed as parameters. A grant at resource-group or
  subscription scope is a high-risk change the user must have approved.
- `publicNetworkAccess: 'Disabled'` and private endpoints for data services, unless the
  user confirmed public exposure for that resource.
- TLS 1.2 or later; HTTPS only; no anonymous or public blob access; shared-key access
  disabled where the service allows it.
- Key Vault: RBAC authorization, soft delete, purge protection.
- Diagnostic settings on every resource that supports them, to the Log Analytics
  workspace named in the architecture.
- Resource locks only when the architecture calls for them; say what they will block.

## Linter configuration

If the infrastructure root has no `bicepconfig.json`, add this one. If it has one, leave
it and do not lower any rule.

```json
{
  "analyzers": {
    "core": {
      "rules": {
        "use-recent-api-versions": { "level": "warning" },
        "no-hardcoded-location": { "level": "error" },
        "no-hardcoded-env-urls": { "level": "error" },
        "secure-secrets-in-params": { "level": "error" },
        "outputs-should-not-contain-secrets": { "level": "error" },
        "protect-commandtoexecute-secrets": { "level": "error" },
        "use-secure-value-for-secure-inputs": { "level": "error" },
        "admin-username-should-not-be-literal": { "level": "error" },
        "no-unused-params": { "level": "warning" },
        "no-unused-vars": { "level": "warning" }
      }
    }
  }
}
```

## What is not allowed

- Terraform, ARM JSON, Pulumi or deployment scripts as the definition of infrastructure.
- A `checkov:skip` comment or `#disable-next-line` without the user's agreement and a
  reason in the comment.
- Changing files outside the infrastructure root.
- Weakening a security setting, a linter rule or a check so that validation passes.

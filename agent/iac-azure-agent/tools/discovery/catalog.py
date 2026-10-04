"""Discovery topics: what the agent may need to know before proposing an architecture.

This is data, not a script to read aloud. planner.py picks the few topics that apply to a
request and skips the rest; the agent words the questions and may add its own. Fields:

  round         2 scope and environment, 3 architecture-specific (round 1 is the profile,
                round 4 is the assumptions review; both are enforced by state/machine.py)
  applies       resource categories the topic belongs to, or "*" for every request
  kinds         request kinds it applies to (default: all)
  must_confirm  the user must answer; it can never be recorded as an assumption
  config        config field that already answers it for this project
  optional      asked only for a full-depth request; otherwise offered as a default
  when          extra condition: "production", "non_production", "integrates_existing",
                "destructive"
  default       conventional default the agent may propose as an assumption
"""

CATEGORIES = ("networking", "compute", "storage", "database", "app_hosting", "security",
              "monitoring", "identity")

MAX_BATCH = 4

TOPICS = {
    # ---------------------------------------------------------------- round 2
    "target_subscription": {
        "round": 2, "applies": "*", "must_confirm": True,
        "text": "Which Azure tenant and subscription is this for? Confirm the one the "
                "tools report as signed in, or name another.",
        "why": "A wrong subscription means resources, cost and access land in the wrong place."},
    "environments": {
        "round": 2, "applies": "*", "config": "environments",
        "text": "Which environments does this project use (for example dev, test, prod)?",
        "why": "Each environment gets its own parameter file."},
    "region": {
        "round": 2, "applies": "*", "config": "region",
        "text": "Which Azure region should this be deployed to?",
        "why": "Region affects latency, data residency, price and which services exist."},
    "resource_group": {
        "round": 2, "applies": "*",
        "text": "Should this go into an existing resource group (which one) or a new one?",
        "why": "The resource group is the deployment scope and the unit of cleanup.",
        "default": "A new resource group per workload and environment."},
    "naming": {
        "round": 2, "applies": "*", "config": "naming",
        "text": "Is there a naming convention to follow?",
        "why": "Many Azure names cannot be changed after creation.",
        "default": "Microsoft Cloud Adoption Framework abbreviations: "
                   "<type>-<workload>-<environment>-<region>."},
    "tagging": {
        "round": 2, "applies": "*", "config": "tagging",
        "text": "Which tags are mandatory (for example environment, workload, owner, cost center)?",
        "why": "Tags drive cost reporting and ownership.",
        "default": "environment, workload and owner on every resource."},
    "existing_dependencies": {
        "round": 2, "applies": "*", "when": "integrates_existing",
        "text": "Which existing resources must this connect to or reuse (names or resource IDs)?",
        "why": "Existing resources are referenced, not recreated."},
    "destructive_scope": {
        "round": 2, "applies": "*", "when": "destructive", "must_confirm": True,
        "text": "Exactly which existing resources may be deleted or replaced, and is any data "
                "in them still needed?",
        "why": "Deletion and replacement can lose data and cannot always be undone."},
    "criticality": {
        "round": 2, "applies": "*", "when": "production",
        "text": "How critical is this workload? What happens if it is down for an hour, or a day?",
        "why": "Criticality decides redundancy, backup and how much to spend on them."},
    "availability": {
        "round": 2, "applies": "*", "when": "production", "optional": True,
        "text": "Are there availability or recovery targets (uptime, acceptable data loss, "
                "time to recover)?",
        "why": "Zone redundancy and geo-replication cost more and add complexity.",
        "default": "Zone-redundant where the service offers it at no large extra cost; "
                   "no cross-region failover."},
    "budget": {
        "round": 2, "applies": "*", "optional": True,
        "text": "Is there a monthly budget or cost ceiling?",
        "why": "It decides SKUs and whether premium features are worth it.",
        "default": "Lowest-cost SKUs that meet the stated requirements."},
    "policy_constraints": {
        "round": 2, "applies": "*", "optional": True,
        "text": "Are there Azure Policy rules or organisational restrictions (allowed regions, "
                "SKUs, mandatory private endpoints)?",
        "why": "A deployment that violates policy is rejected by Azure.",
        "default": "No known restrictions; policy violations will surface in validation."},
    # ---------------------------------------------------------------- round 3: networking
    "vnet_existing_or_new": {
        "round": 3, "applies": ["networking"],
        "text": "Existing virtual network (which one) or a new one?",
        "why": "A new network needs an address plan; an existing one constrains it."},
    "address_space": {
        "round": 3, "applies": ["networking"], "kinds": ["new", "update"], "must_confirm": True,
        "text": "Which address space and subnet ranges should be used? They must not overlap "
                "with networks this one will ever be connected to.",
        "why": "Overlapping ranges break peering and VPN, and ranges are hard to change later."},
    "connectivity": {
        "round": 3, "applies": ["networking"],
        "text": "What must this network reach: other virtual networks (peering), on-premises "
                "(VPN or ExpressRoute), the internet?",
        "why": "It decides gateways, routing and firewall needs.",
        "default": "No peering or hybrid connectivity."},
    "private_dns": {
        "round": 3, "applies": ["networking"], "optional": True,
        "text": "Are private DNS zones needed, and do any already exist?",
        "why": "Private endpoints resolve through private DNS zones; duplicates cause conflicts.",
        "default": "Create the private DNS zones the private endpoints need, linked to this network."},
    "network_filtering": {
        "round": 3, "applies": ["networking"], "optional": True,
        "text": "What traffic filtering is required (network security groups, Azure Firewall, "
                "forced routing)?",
        "why": "Azure Firewall is a significant fixed monthly cost.",
        "default": "Network security groups on every subnet, deny by default inbound; no Azure Firewall."},
    # ---------------------------------------------------------------- round 3: exposure
    "public_exposure": {
        "round": 3, "applies": ["networking", "compute", "storage", "database", "app_hosting"],
        "must_confirm": True,
        "text": "Should anything be reachable from the public internet? If so, what, and by whom?",
        "why": "Public exposure is the main security decision; the safe default is none, but "
               "it must be your decision."},
    # ---------------------------------------------------------------- round 3: compute
    "compute_os": {
        "round": 3, "applies": ["compute"],
        "text": "Linux or Windows, and which distribution or version?",
        "why": "It decides the image, licensing cost and patching."},
    "compute_sizing_production": {
        "round": 3, "applies": ["compute", "database", "app_hosting"], "when": "production",
        "must_confirm": True,
        "text": "What capacity does production need (users, requests, CPU and memory, data size)?",
        "why": "Production sizing drives cost and reliability and is never guessed."},
    "compute_sizing": {
        "round": 3, "applies": ["compute"], "when": "non_production",
        "text": "Roughly how large should it be (CPU, memory, disk)?",
        "why": "Size is the main cost driver.",
        "default": "A small burstable size (B-series) suitable for development."},
    "compute_admin_access": {
        "round": 3, "applies": ["compute"],
        "text": "How will administrators reach it (Azure Bastion, VPN, just-in-time access)?",
        "why": "Open SSH or RDP from the internet is the most common VM compromise path.",
        "default": "No public IP; Microsoft Entra ID login; access through Azure Bastion."},
    "compute_scaling": {
        "round": 3, "applies": ["compute"], "optional": True,
        "text": "Single instance, or several with scaling or availability zones?",
        "why": "More instances cost more but survive failures.",
        "default": "A single instance."},
    # ---------------------------------------------------------------- round 3: storage
    "storage_redundancy": {
        "round": 3, "applies": ["storage"],
        "text": "How durable must the data be: one datacenter (LRS), zone-redundant (ZRS) or "
                "geo-redundant (GRS)?",
        "why": "Redundancy changes price and what failures the data survives.",
        "default": "LRS outside production, ZRS in production."},
    "data_classification": {
        "round": 3, "applies": ["storage", "database"],
        "text": "What kind of data is this (public, internal, personal, regulated), and must it "
                "stay in a particular country or region?",
        "why": "It decides encryption, access, retention and where it may be stored."},
    "storage_lifecycle": {
        "round": 3, "applies": ["storage"], "optional": True,
        "text": "How long must data be kept, and should older data move to cheaper tiers or be deleted?",
        "why": "Lifecycle rules cut cost; deletion rules are irreversible.",
        "default": "Soft delete for 7 days; no automatic tiering or deletion."},
    # ---------------------------------------------------------------- round 3: database
    "database_engine": {
        "round": 3, "applies": ["database"],
        "text": "Which database engine and service (Azure SQL, PostgreSQL flexible server, "
                "Cosmos DB, other)?",
        "why": "Engines differ in features, price model and operations."},
    "backup_retention": {
        "round": 3, "applies": ["database"],
        "text": "How long must backups be kept, and how much data loss is acceptable?",
        "why": "Retention and geo-redundant backup affect cost and recovery.",
        "default": "The service default: 7 days of point-in-time restore, locally redundant backups."},
    # ---------------------------------------------------------------- round 3: app hosting
    "hosting_platform": {
        "round": 3, "applies": ["app_hosting"],
        "text": "Which hosting platform: App Service, Container Apps, AKS, Functions or virtual "
                "machines? If unsure, describe the application.",
        "why": "They differ widely in operational effort and cost."},
    "app_runtime": {
        "round": 3, "applies": ["app_hosting"],
        "text": "Which runtime or container image, and where do build artifacts come from?",
        "why": "It decides the plan, registry and identity needs."},
    "app_vnet_integration": {
        "round": 3, "applies": ["app_hosting"], "when": "integrates_existing",
        "text": "Which existing virtual network and subnet should it be connected to?",
        "why": "Some platforms need a dedicated, empty subnet of a minimum size."},
    "app_scaling": {
        "round": 3, "applies": ["app_hosting"], "optional": True,
        "text": "What are the scaling needs (minimum and maximum instances, scale to zero)?",
        "why": "Always-on instances cost money while idle.",
        "default": "Scale to zero where supported outside production; minimum one instance in production."},
    # ---------------------------------------------------------------- round 3: security, identity, monitoring
    "identity_model": {
        "round": 3, "applies": ["compute", "app_hosting", "identity", "database", "storage"],
        "optional": True,
        "text": "Which identities need access, and to what? (Workload identities, users, groups.)",
        "why": "Access is granted with least privilege to named identities.",
        "default": "Managed identities for workloads; no access keys or shared secrets; RBAC "
                   "roles scoped to the single resource."},
    "rbac_scope": {
        "round": 3, "applies": ["identity"],
        "text": "Which roles, for whom, at which scope (resource, resource group, subscription)?",
        "why": "Broad grants at subscription scope are high risk and need separate confirmation."},
    "key_vault": {
        "round": 3, "applies": ["security", "app_hosting", "compute", "database"], "optional": True,
        "text": "Is there an existing Key Vault for secrets, keys and certificates, or should one be created?",
        "why": "Secrets are referenced from Key Vault by name and never placed in code.",
        "default": "A new Key Vault with RBAC authorization and purge protection."},
    "compliance": {
        "round": 3, "applies": ["security"],
        "text": "Which compliance controls or standards apply?",
        "why": "They can mandate encryption keys, logging, retention and network isolation."},
    "diagnostics": {
        "round": 3, "applies": ["monitoring", "compute", "storage", "database", "app_hosting",
                                "networking", "security"], "optional": True,
        "text": "Is there a central Log Analytics workspace for diagnostics, or should one be created?",
        "why": "Without diagnostics there is nothing to investigate after an incident.",
        "default": "Diagnostic settings on every resource that supports them, sent to one Log "
                   "Analytics workspace per environment with 30 days retention."},
    "alerting": {
        "round": 3, "applies": ["monitoring"],
        "text": "Which conditions should raise alerts, and who receives them?",
        "why": "Alerts need an owner to be useful."},
}


def is_production(profile, production_envs):
    return any(e in production_envs for e in profile["environments"])


def is_destructive(profile):
    return profile["kind"] == "remove"


def applies(topic_id, profile, production_envs):
    t = TOPICS[topic_id]
    if t["applies"] != "*" and not set(t["applies"]) & set(profile["categories"]):
        return False
    if "kinds" in t and profile["kind"] not in t["kinds"]:
        return False
    when = t.get("when")
    prod = is_production(profile, production_envs)
    if when == "production":
        return prod
    if when == "non_production":
        return not prod
    if when == "integrates_existing":
        return bool(profile.get("integrates_existing")) or profile["kind"] != "new"
    if when == "destructive":
        return is_destructive(profile)
    return True


def must_confirm(profile, production_envs):
    return sorted(t for t in TOPICS
                  if TOPICS[t].get("must_confirm") and applies(t, profile, production_envs))


def depth(profile, production_envs):
    """simple: one category, new, not production, nothing existing to integrate. Optional
    topics are then offered as defaults instead of asked. Everything else is full."""
    if (profile["kind"] == "new" and len(profile["categories"]) == 1
            and not is_production(profile, production_envs)
            and not profile.get("integrates_existing")):
        return "simple"
    return "full"

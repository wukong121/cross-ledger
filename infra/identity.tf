# -----------------------------------------------------------------------------
# Human identities: Entra ID security groups. Membership is the only thing
# IT ops manages day-to-day; every privilege hangs off a group.
# -----------------------------------------------------------------------------

data "azuread_client_config" "current" {}

locals {
  groups = {
    it_ops = {
      name        = "${var.prefix}-it-ops"
      description = "cross-ledger: IT operations — Owner of solution scopes, approver of all data access requests."
      members     = var.it_ops_members
    }
    data_team = {
      name        = "${var.prefix}-data-team"
      description = "cross-ledger: R&D / algorithm / data staff — eligible (not standing) for data roles via PIM."
      members     = var.data_team_members
    }
    auditors = {
      name        = "${var.prefix}-auditors"
      description = "cross-ledger: compliance / audit — read-only access to the audit ledger."
      members     = var.auditor_members
    }
  }

  group_member_pairs = merge([
    for gk, g in local.groups : {
      for upn in g.members : "${gk}/${upn}" => { group = gk, upn = upn }
    }
  ]...)
}

resource "azuread_group" "this" {
  for_each         = local.groups
  display_name     = "${each.value.name}-${local.sfx}"
  description      = each.value.description
  security_enabled = true
  owners           = [data.azuread_client_config.current.object_id]
}

data "azuread_user" "members" {
  for_each            = toset(distinct(flatten([for g in local.groups : g.members])))
  user_principal_name = each.value
}

resource "azuread_group_member" "this" {
  for_each         = local.group_member_pairs
  group_object_id  = azuread_group.this[each.value.group].object_id
  member_object_id = data.azuread_user.members[each.value.upn].object_id
}

# -----------------------------------------------------------------------------
# Workload identities: user-assigned managed identities, no secrets anywhere.
# -----------------------------------------------------------------------------

resource "azurerm_user_assigned_identity" "transfer" {
  name                = "id-${local.name}-transfer"
  resource_group_name = azurerm_resource_group.gov.name
  location            = azurerm_resource_group.gov.location
  tags                = var.tags
}

# PoC only: identity of the job that generates sample source data.
resource "azurerm_user_assigned_identity" "seed" {
  name                = "id-${local.name}-seed"
  resource_group_name = azurerm_resource_group.gov.name
  location            = azurerm_resource_group.gov.location
  tags                = var.tags
}

resource "azurerm_user_assigned_identity" "collector" {
  name                = "id-${local.name}-collector"
  resource_group_name = azurerm_resource_group.gov.name
  location            = azurerm_resource_group.gov.location
  tags                = var.tags
}

locals {
  rgs = {
    source = azurerm_resource_group.source.id
    gov    = azurerm_resource_group.gov.id
  }

  # Standing (non-PIM) role assignments — workloads and read-only audiences only.
  standing_assignments = merge(local.base_assignments, var.enable_managed_transfer ? local.managed_transfer_assignments : {})

  # Only with enable_managed_transfer.
  managed_transfer_assignments = {
    # transfer worker: read source, consume its queue, read destination credentials, emit events
    xfer_src_read = { scope = azurerm_storage_account.source.id, role = "Storage Blob Data Reader", principal = azurerm_user_assigned_identity.transfer.principal_id }
    # Data Owner on the queue only: KEDA's Service Bus scaler needs to read queue runtime properties.
    xfer_queue     = { scope = local.bus_queue_id, role = "Azure Service Bus Data Owner", principal = azurerm_user_assigned_identity.transfer.principal_id }
    xfer_kv        = { scope = local.key_vault_id, role = "Key Vault Secrets User", principal = azurerm_user_assigned_identity.transfer.principal_id }
    ops_kv_officer = { scope = local.key_vault_id, role = "Key Vault Secrets Officer", principal = azuread_group.this["it_ops"].object_id }
    # data team: list registered destinations (names + tags via ARM), never secret values
    team_kv_reader    = { scope = local.key_vault_id, role = "Reader", principal = azuread_group.this["data_team"].object_id }
    xfer_pim_read     = { scope = azurerm_resource_group.gov.id, role = "Reader", principal = azurerm_user_assigned_identity.transfer.principal_id }
    xfer_audit_append = { scope = azurerm_storage_account.audit.id, role = "Storage Blob Data Contributor", principal = azurerm_user_assigned_identity.transfer.principal_id }
    xfer_dcr          = { scope = azurerm_monitor_data_collection_rule.ledger.id, role = "Monitoring Metrics Publisher", principal = azurerm_user_assigned_identity.transfer.principal_id }
    xfer_acr          = { scope = azurerm_container_registry.this.id, role = "AcrPull", principal = azurerm_user_assigned_identity.transfer.principal_id }
  }

  base_assignments = {
    # collector: read PIM state + logs on all three scopes, append to ledger
    col_src_read = { scope = azurerm_resource_group.source.id, role = "Reader", principal = azurerm_user_assigned_identity.collector.principal_id }
    col_gov_read = { scope = azurerm_resource_group.gov.id, role = "Reader", principal = azurerm_user_assigned_identity.collector.principal_id }
    col_logs     = { scope = azurerm_log_analytics_workspace.this.id, role = "Log Analytics Reader", principal = azurerm_user_assigned_identity.collector.principal_id }
    col_audit    = { scope = azurerm_storage_account.audit.id, role = "Storage Blob Data Contributor", principal = azurerm_user_assigned_identity.collector.principal_id }
    col_state    = { scope = azurerm_storage_account.ctl.id, role = "Storage Blob Data Contributor", principal = azurerm_user_assigned_identity.collector.principal_id }
    col_dcr      = { scope = azurerm_monitor_data_collection_rule.ledger.id, role = "Monitoring Metrics Publisher", principal = azurerm_user_assigned_identity.collector.principal_id }
    col_acr      = { scope = azurerm_container_registry.this.id, role = "AcrPull", principal = azurerm_user_assigned_identity.collector.principal_id }

    # seed (PoC): generates sample data in the source account
    seed_src_write = { scope = azurerm_storage_account.source.id, role = "Storage Blob Data Contributor", principal = azurerm_user_assigned_identity.seed.principal_id }
    seed_audit     = { scope = azurerm_storage_account.audit.id, role = "Storage Blob Data Contributor", principal = azurerm_user_assigned_identity.seed.principal_id }
    seed_dcr       = { scope = azurerm_monitor_data_collection_rule.ledger.id, role = "Monitoring Metrics Publisher", principal = azurerm_user_assigned_identity.seed.principal_id }
    seed_acr       = { scope = azurerm_container_registry.this.id, role = "AcrPull", principal = azurerm_user_assigned_identity.seed.principal_id }

    # auditors: read the whole ledger (LA) + WORM archive
    aud_logs  = { scope = azurerm_log_analytics_workspace.this.id, role = "Log Analytics Reader", principal = azuread_group.this["auditors"].object_id }
    aud_audit = { scope = azurerm_storage_account.audit.id, role = "Storage Blob Data Reader", principal = azuread_group.this["auditors"].object_id }
    aud_rg    = { scope = azurerm_resource_group.gov.id, role = "Reader", principal = azuread_group.this["auditors"].object_id }

    # data team: may read ONLY the ledger table (to track their own tickets) — table-level RBAC
    team_ledger_table = { scope = "${azurerm_log_analytics_workspace.this.id}/tables/${local.ledger_table}", role = "Log Analytics Data Reader", principal = azuread_group.this["data_team"].object_id }
  }
}

resource "azurerm_role_assignment" "standing" {
  for_each             = local.standing_assignments
  scope                = each.value.scope
  role_definition_name = each.value.role
  principal_id         = each.value.principal
  # Explicit principal type: lets ARM skip the Graph lookup, so freshly created
  # managed identities / groups that have not replicated yet still work.
  principal_type = startswith(each.key, "aud_") || startswith(each.key, "team_") || startswith(each.key, "ops_") ? "Group" : "ServicePrincipal"
  depends_on     = [azapi_resource.ledger_table]
}

# IT ops — Owner on every solution scope (standing or PIM-eligible).
resource "azurerm_role_assignment" "it_ops_owner" {
  for_each             = var.it_ops_owner_mode == "active" ? local.rgs : {}
  scope                = each.value
  role_definition_name = "Owner"
  principal_id         = azuread_group.this["it_ops"].object_id
  principal_type       = "Group"
}

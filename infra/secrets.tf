# -----------------------------------------------------------------------------
# OPTIONAL (enable_managed_transfer). Destination credentials for the managed
# transfer worker live in Key Vault — never in code, images or job definitions.
#
#   * RBAC-only; the worker's managed identity can read secrets, IT ops manage them
#   * network: Network Security Perimeter profile "secrets" — only managed identities
#     of this subscription (the worker) and the operators' admin IPs may connect
#   * every secret read / write is logged (AuditEvent -> Log Analytics)
#
# Destinations: one secret per destination, `dest-<name>`, registered by IT ops with
# `xl dest add` (through ARM — no data-plane network access needed). Users list them
# (names + tags only, never values) with `xl dest list` and pick one per transfer.
# The worker reads the secret at the start of every shard, so updates need no redeploy.
# -----------------------------------------------------------------------------


resource "azurerm_key_vault" "this" {
  count                         = local.managed_transfer ? 1 : 0
  name                          = "kv-${local.name}"
  resource_group_name           = azurerm_resource_group.gov.name
  location                      = azurerm_resource_group.gov.location
  tenant_id                     = data.azurerm_client_config.current.tenant_id
  sku_name                      = "standard"
  rbac_authorization_enabled    = true
  purge_protection_enabled      = false # PoC: allow clean teardown; enable in production
  soft_delete_retention_days    = 7
  public_network_access_enabled = false
  tags                          = var.tags

  lifecycle {
    # publicNetworkAccess is set to SecuredByPerimeter below (azurerm can only express true/false).
    ignore_changes = [public_network_access_enabled]
  }
}

resource "azurerm_network_security_perimeter_profile" "secrets" {
  count                         = local.managed_transfer ? 1 : 0
  name                          = "secrets"
  network_security_perimeter_id = azurerm_network_security_perimeter.this[0].id
}

resource "azurerm_network_security_perimeter_access_rule" "secrets_workloads" {
  count                                 = local.managed_transfer ? 1 : 0
  name                                  = "allow-subscription-identities"
  network_security_perimeter_profile_id = azurerm_network_security_perimeter_profile.secrets[0].id
  direction                             = "Inbound"
  subscription_ids                      = [data.azurerm_subscription.current.id]
}

resource "azurerm_network_security_perimeter_access_rule" "secrets_admins" {
  count                                 = local.managed_transfer && length(var.secrets_admin_ips) > 0 ? 1 : 0
  name                                  = "allow-admin-ips"
  network_security_perimeter_profile_id = azurerm_network_security_perimeter_profile.secrets[0].id
  direction                             = "Inbound"
  address_prefixes                      = var.secrets_admin_ips
}

resource "azurerm_network_security_perimeter_association" "key_vault" {
  count                                 = local.managed_transfer ? 1 : 0
  name                                  = "key-vault"
  network_security_perimeter_profile_id = azurerm_network_security_perimeter_profile.secrets[0].id
  resource_id                           = azurerm_key_vault.this[0].id
  access_mode                           = "Enforced"
}

resource "azapi_update_resource" "key_vault_public_access" {
  count       = local.managed_transfer ? 1 : 0
  type        = "Microsoft.KeyVault/vaults@2024-11-01"
  resource_id = azurerm_key_vault.this[0].id
  body = {
    properties = { publicNetworkAccess = "SecuredByPerimeter" }
  }
  depends_on = [azurerm_network_security_perimeter_association.key_vault, azurerm_network_security_perimeter_access_rule.secrets_workloads]
}

resource "azurerm_monitor_diagnostic_setting" "key_vault" {
  count                      = local.managed_transfer ? 1 : 0
  name                       = "${local.name}-kv"
  target_resource_id         = azurerm_key_vault.this[0].id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id

  enabled_log { category = "AuditEvent" }
}

locals {
  key_vault_uri = local.managed_transfer ? azurerm_key_vault.this[0].vault_uri : ""
  key_vault_id  = local.managed_transfer ? azurerm_key_vault.this[0].id : ""
}

# Feishu App Secret. Terraform only creates the (placeholder) secret so that read access can be
# granted on this one secret; IT sets the real value with `xl feishu set-secret` (through ARM),
# so it never appears in tfvars or Terraform state.
resource "azapi_resource" "feishu_secret" {
  count     = local.ticket_intake ? 1 : 0
  type      = "Microsoft.KeyVault/vaults/secrets@2023-07-01"
  name      = "feishu-app-secret"
  parent_id = azurerm_key_vault.this[0].id
  body = {
    tags       = { "xl-kind" = "feishu" }
    properties = { value = "unset", contentType = "text/plain" }
  }
  lifecycle {
    ignore_changes = [body]
  }
}

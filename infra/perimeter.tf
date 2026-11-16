# -----------------------------------------------------------------------------
# Public access to the source dataset — only when explicitly chosen.
#
# Recommended: keep the source private (Private Endpoint + cross-border private
# line). If transfers must use the public internet, do NOT open the storage
# firewall. Put the account in a Network Security Perimeter (NSP) instead:
#   * publicNetworkAccess = SecuredByPerimeter
#   * inbound rule = allow-list of the destination's egress IPs only
#   * access mode Enforced; every allowed/denied connection is logged
# Entra ID-only auth, TLS 1.2 and PIM still apply on top.
# -----------------------------------------------------------------------------

locals {
  perimeter_enabled = var.source_public_access == "perimeter"
  perimeter_ips     = distinct(var.perimeter_allowed_ips)
}

resource "azapi_update_resource" "source_public_access" {
  type        = "Microsoft.Storage/storageAccounts@2023-05-01"
  resource_id = azurerm_storage_account.source.id
  body = {
    properties = {
      publicNetworkAccess = local.perimeter_enabled ? "SecuredByPerimeter" : "Disabled"
    }
  }
  depends_on = [azurerm_network_security_perimeter_association.source, azurerm_network_security_perimeter_access_rule.inbound_ips]
}

resource "azurerm_network_security_perimeter" "this" {
  count               = local.perimeter_enabled ? 1 : 0
  name                = "nsp-${local.name}"
  resource_group_name = azurerm_resource_group.gov.name
  location            = azurerm_resource_group.gov.location
  tags                = var.tags
}

resource "azurerm_network_security_perimeter_profile" "source" {
  count                         = local.perimeter_enabled ? 1 : 0
  name                          = "source-dataset"
  network_security_perimeter_id = azurerm_network_security_perimeter.this[0].id
}

resource "azurerm_network_security_perimeter_access_rule" "inbound_ips" {
  count                                 = local.perimeter_enabled && length(var.perimeter_allowed_ips) > 0 ? 1 : 0
  name                                  = "allow-destination-egress"
  network_security_perimeter_profile_id = azurerm_network_security_perimeter_profile.source[0].id
  direction                             = "Inbound"
  address_prefixes                      = local.perimeter_ips
}

resource "azurerm_network_security_perimeter_association" "source" {
  count                                 = local.perimeter_enabled ? 1 : 0
  name                                  = "source-account"
  network_security_perimeter_profile_id = azurerm_network_security_perimeter_profile.source[0].id
  resource_id                           = azurerm_storage_account.source.id
  access_mode                           = "Enforced"
}

# Perimeter access logs (allowed + denied connections) -> Log Analytics.
resource "azurerm_monitor_diagnostic_setting" "perimeter" {
  count                      = local.perimeter_enabled ? 1 : 0
  name                       = "${local.name}-nsp"
  target_resource_id         = azurerm_network_security_perimeter.this[0].id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id

  enabled_log { category_group = "allLogs" }
}

# IMPORTANT: a resource in an Enforced perimeter can only send traffic to resources in
# the same perimeter — including its diagnostic logs. Without this association the
# source account's StorageBlobLogs silently stop flowing to Log Analytics, and the
# ledger loses the "who read what" evidence. Learning mode keeps the workspace's own
# public query / ingestion behaviour unchanged.
resource "azurerm_network_security_perimeter_association" "workspace" {
  count                                 = local.perimeter_enabled ? 1 : 0
  name                                  = "log-analytics"
  network_security_perimeter_profile_id = azurerm_network_security_perimeter_profile.source[0].id
  resource_id                           = azurerm_log_analytics_workspace.this.id
  access_mode                           = "Learning"
}

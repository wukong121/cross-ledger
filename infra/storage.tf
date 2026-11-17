# -----------------------------------------------------------------------------
# Data storage. All accounts: Entra ID auth only (shared keys disabled),
# TLS 1.2+, no anonymous access. Containers are created through the ARM
# control plane so the deployer never needs standing data-plane rights.
# -----------------------------------------------------------------------------

locals {
  # Data plane is private-endpoint only (see network.tf).
  public_network_access_enabled = false

  storage_common = {
    account_tier                    = "Standard"
    account_kind                    = "StorageV2"
    min_tls_version                 = "TLS1_2"
    shared_access_key_enabled       = false
    allow_nested_items_to_be_public = false
    https_traffic_only_enabled      = true
  }

}

# Overseas source — the business data to be transferred.
resource "azurerm_storage_account" "source" {
  name                            = "${local.sa_name}src"
  resource_group_name             = azurerm_resource_group.source.name
  location                        = azurerm_resource_group.source.location
  account_tier                    = local.storage_common.account_tier
  account_kind                    = local.storage_common.account_kind
  account_replication_type        = "LRS"
  min_tls_version                 = local.storage_common.min_tls_version
  shared_access_key_enabled       = local.storage_common.shared_access_key_enabled
  allow_nested_items_to_be_public = local.storage_common.allow_nested_items_to_be_public
  https_traffic_only_enabled      = local.storage_common.https_traffic_only_enabled
  public_network_access_enabled   = local.public_network_access_enabled
  tags                            = merge(var.tags, { role = "source" })

  blob_properties {
    versioning_enabled = true
    delete_retention_policy { days = 7 }
    container_delete_retention_policy { days = 7 }
  }

  lifecycle {
    # publicNetworkAccess is owned by perimeter.tf (Disabled | SecuredByPerimeter);
    # azurerm can only express true/false.
    ignore_changes = [public_network_access_enabled]
  }
}

resource "azurerm_storage_container" "source_datasets" {
  name                  = "datasets"
  storage_account_id    = azurerm_storage_account.source.id
  container_access_type = "private"
}

# Audit archive — WORM copy of every ledger event (append blobs, one per day).
resource "azurerm_storage_account" "audit" {
  name                            = "${local.sa_name}audit"
  resource_group_name             = azurerm_resource_group.gov.name
  location                        = azurerm_resource_group.gov.location
  account_tier                    = local.storage_common.account_tier
  account_kind                    = local.storage_common.account_kind
  account_replication_type        = "ZRS"
  min_tls_version                 = local.storage_common.min_tls_version
  shared_access_key_enabled       = local.storage_common.shared_access_key_enabled
  allow_nested_items_to_be_public = local.storage_common.allow_nested_items_to_be_public
  https_traffic_only_enabled      = local.storage_common.https_traffic_only_enabled
  public_network_access_enabled   = local.public_network_access_enabled
  tags                            = merge(var.tags, { role = "audit" })
}

resource "azurerm_storage_container" "ledger" {
  name                  = "ledger"
  storage_account_id    = azurerm_storage_account.audit.id
  container_access_type = "private"
}


resource "azurerm_storage_container_immutability_policy" "ledger" {
  storage_container_resource_manager_id = azurerm_storage_container.ledger.id
  immutability_period_in_days           = var.ledger_retention_days
  protected_append_writes_all_enabled   = true # append-only: new events may be appended, nothing may be modified/deleted
  locked                                = var.ledger_immutability_locked
}

# Control-plane account: collector state.
resource "azurerm_storage_account" "ctl" {
  name                            = "${local.sa_name}ctl"
  resource_group_name             = azurerm_resource_group.gov.name
  location                        = azurerm_resource_group.gov.location
  account_tier                    = local.storage_common.account_tier
  account_kind                    = local.storage_common.account_kind
  account_replication_type        = "LRS"
  min_tls_version                 = local.storage_common.min_tls_version
  shared_access_key_enabled       = local.storage_common.shared_access_key_enabled
  allow_nested_items_to_be_public = local.storage_common.allow_nested_items_to_be_public
  https_traffic_only_enabled      = local.storage_common.https_traffic_only_enabled
  public_network_access_enabled   = local.public_network_access_enabled
  tags                            = merge(var.tags, { role = "control" })
}

# Collector bookkeeping (cursor / dedupe state). Not immutable.
resource "azurerm_storage_container" "state" {
  name                  = "collector-state"
  storage_account_id    = azurerm_storage_account.ctl.id
  container_access_type = "private"
}

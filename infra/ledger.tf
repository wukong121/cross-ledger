# -----------------------------------------------------------------------------
# Azure Confidential Ledger — the tamper-evident half of the ledger.
# Every event is appended into a collection named after its ticket; the ledger
# returns a transaction id whose receipt can be verified offline (Merkle proof
# signed by the TEE-backed service), so neither IT ops nor Microsoft can
# silently rewrite history.
# -----------------------------------------------------------------------------

resource "azurerm_confidential_ledger" "this" {
  count               = var.enable_confidential_ledger ? 1 : 0
  name                = "acl-${local.name}"
  resource_group_name = azurerm_resource_group.gov.name
  location            = local.ledger_location
  ledger_type         = "Private"
  tags                = var.tags

  azuread_based_service_principal {
    principal_id     = data.azurerm_client_config.current.object_id
    tenant_id        = data.azurerm_client_config.current.tenant_id
    ledger_role_name = "Administrator"
  }

  azuread_based_service_principal {
    principal_id     = azurerm_user_assigned_identity.collector.principal_id
    tenant_id        = data.azurerm_client_config.current.tenant_id
    ledger_role_name = "Contributor"
  }

  azuread_based_service_principal {
    principal_id     = azurerm_user_assigned_identity.seed.principal_id
    tenant_id        = data.azurerm_client_config.current.tenant_id
    ledger_role_name = "Contributor"
  }

  azuread_based_service_principal {
    principal_id     = azurerm_user_assigned_identity.transfer.principal_id
    tenant_id        = data.azurerm_client_config.current.tenant_id
    ledger_role_name = "Contributor"
  }

}

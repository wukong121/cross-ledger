# -----------------------------------------------------------------------------
# Private networking. Data never touches a public endpoint:
#   * every storage account is reachable only through Private Endpoints
#   * the Container Apps environment (transfer + collector jobs) is VNet-injected
# Human-facing control endpoints (ARM/PIM, Service Bus, Log Analytics,
# Confidential Ledger) stay public but accept Entra ID tokens only.
# -----------------------------------------------------------------------------

resource "azurerm_virtual_network" "this" {
  name                = "vnet-${local.name}"
  resource_group_name = azurerm_resource_group.gov.name
  location            = azurerm_resource_group.gov.location
  address_space       = [var.vnet_cidr]
  tags                = var.tags
}

resource "azurerm_subnet" "cae" {
  name                 = "snet-cae"
  resource_group_name  = azurerm_resource_group.gov.name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [cidrsubnet(var.vnet_cidr, 7, 0)] # /23 for a /16

  delegation {
    name = "aca"
    service_delegation {
      name    = "Microsoft.App/environments"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

resource "azurerm_subnet" "pe" {
  name                 = "snet-pe"
  resource_group_name  = azurerm_resource_group.gov.name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [cidrsubnet(var.vnet_cidr, 8, 4)] # /24
}

resource "azurerm_private_dns_zone" "blob" {
  name                = "privatelink.blob.core.windows.net"
  resource_group_name = azurerm_resource_group.gov.name
  tags                = var.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "blob" {
  name                  = "link-${local.name}"
  resource_group_name   = azurerm_resource_group.gov.name
  private_dns_zone_name = azurerm_private_dns_zone.blob.name
  virtual_network_id    = azurerm_virtual_network.this.id
}

locals {
  blob_endpoints = {
    source = azurerm_storage_account.source.id
    audit  = azurerm_storage_account.audit.id
    ctl    = azurerm_storage_account.ctl.id
  }
}

resource "azurerm_private_endpoint" "blob" {
  for_each            = local.blob_endpoints
  name                = "pe-${local.name}-${each.key}-blob"
  resource_group_name = azurerm_resource_group.gov.name
  location            = azurerm_resource_group.gov.location
  subnet_id           = azurerm_subnet.pe.id
  tags                = var.tags

  private_service_connection {
    name                           = "${each.key}-blob"
    private_connection_resource_id = each.value
    subresource_names              = ["blob"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "blob"
    private_dns_zone_ids = [azurerm_private_dns_zone.blob.id]
  }
}

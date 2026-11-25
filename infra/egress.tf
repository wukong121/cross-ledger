# -----------------------------------------------------------------------------
# OPTIONAL (enable_managed_transfer). Fixed egress IP for the transfer worker.
# The domestic object storage can then allow-list exactly one Azure address
# (bucket policy / firewall), instead of the whole Azure public range.
# -----------------------------------------------------------------------------

resource "azurerm_public_ip" "egress" {
  count               = local.managed_transfer ? 1 : 0
  name                = "pip-${local.name}-egress"
  resource_group_name = azurerm_resource_group.gov.name
  location            = azurerm_resource_group.gov.location
  allocation_method   = "Static"
  sku                 = "Standard"
  tags                = var.tags

  lifecycle {
    ignore_changes = [ip_tags] # platform-managed tags in some subscriptions
  }
}

resource "azurerm_nat_gateway" "egress" {
  count               = local.managed_transfer ? 1 : 0
  name                = "ng-${local.name}"
  resource_group_name = azurerm_resource_group.gov.name
  location            = azurerm_resource_group.gov.location
  sku_name            = "Standard"
  tags                = var.tags
}

resource "azurerm_nat_gateway_public_ip_association" "egress" {
  count                = local.managed_transfer ? 1 : 0
  nat_gateway_id       = azurerm_nat_gateway.egress[0].id
  public_ip_address_id = azurerm_public_ip.egress[0].id
}

resource "azurerm_subnet_nat_gateway_association" "cae" {
  count          = local.managed_transfer ? 1 : 0
  subnet_id      = azurerm_subnet.cae.id
  nat_gateway_id = azurerm_nat_gateway.egress[0].id
}

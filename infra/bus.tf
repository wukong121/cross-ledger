# -----------------------------------------------------------------------------
# OPTIONAL (enable_managed_transfer). Transfer request bus. Submitting a transfer == sending a message here, which
# requires an *activated* PIM role (Azure Service Bus Data Sender). Entra ID
# only (local SAS auth disabled); poison messages dead-letter natively.
# -----------------------------------------------------------------------------

resource "azurerm_servicebus_namespace" "this" {
  count               = var.enable_managed_transfer ? 1 : 0
  name                = "sb-${local.name}"
  resource_group_name = azurerm_resource_group.gov.name
  location            = azurerm_resource_group.gov.location
  sku                 = "Standard"
  local_auth_enabled  = false
  minimum_tls_version = "1.2"
  tags                = var.tags
}

resource "azurerm_servicebus_queue" "transfer_requests" {
  count                                = var.enable_managed_transfer ? 1 : 0
  name                                 = "transfer-requests"
  namespace_id                         = azurerm_servicebus_namespace.this[0].id
  max_delivery_count                   = 3 # then -> $DeadLetterQueue
  lock_duration                        = "PT5M"
  dead_lettering_on_message_expiration = true
  default_message_ttl                  = "P7D"
}

locals {
  bus_namespace_name = var.enable_managed_transfer ? azurerm_servicebus_namespace.this[0].name : ""
  bus_namespace_id   = var.enable_managed_transfer ? azurerm_servicebus_namespace.this[0].id : ""
  bus_queue_name     = var.enable_managed_transfer ? azurerm_servicebus_queue.transfer_requests[0].name : ""
  bus_queue_id       = var.enable_managed_transfer ? azurerm_servicebus_queue.transfer_requests[0].id : ""
}

moved {
  from = azurerm_servicebus_namespace.this
  to   = azurerm_servicebus_namespace.this[0]
}

moved {
  from = azurerm_servicebus_queue.transfer_requests
  to   = azurerm_servicebus_queue.transfer_requests[0]
}

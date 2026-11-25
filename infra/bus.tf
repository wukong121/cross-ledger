# -----------------------------------------------------------------------------
# Managed transfer bus (on with intake = "feishu" or enable_managed_transfer). Shards are queued by the connector
# (approved tickets) or, in self-service mode, by users holding an *activated* PIM role (Azure Service Bus Data Sender). Entra ID
# only (local SAS auth disabled); poison messages dead-letter natively.
# -----------------------------------------------------------------------------

resource "azurerm_servicebus_namespace" "this" {
  count               = local.managed_transfer ? 1 : 0
  name                = "sb-${local.name}"
  resource_group_name = azurerm_resource_group.gov.name
  location            = azurerm_resource_group.gov.location
  sku                 = "Standard"
  local_auth_enabled  = false
  minimum_tls_version = "1.2"
  tags                = var.tags
}

resource "azurerm_servicebus_queue" "transfer_requests" {
  count                                = local.managed_transfer ? 1 : 0
  name                                 = "transfer-requests"
  namespace_id                         = azurerm_servicebus_namespace.this[0].id
  max_delivery_count                   = 3 # then -> $DeadLetterQueue
  lock_duration                        = "PT5M"
  dead_lettering_on_message_expiration = true
  default_message_ttl                  = "P7D"
  # One shard per approved ticket: the connector uses a deterministic message id.
  requires_duplicate_detection            = true
  duplicate_detection_history_time_window = "P1D"
}

# Ticket intake: approval events (connector, long connection) and reconciliation (collector)
# land here; the connector's intake thread consumes them.
resource "azurerm_servicebus_queue" "ticket_events" {
  count                                   = local.ticket_intake ? 1 : 0
  name                                    = "ticket-events"
  namespace_id                            = azurerm_servicebus_namespace.this[0].id
  max_delivery_count                      = 10
  lock_duration                           = "PT2M"
  dead_lettering_on_message_expiration    = true
  default_message_ttl                     = "P14D"
  requires_duplicate_detection            = true
  duplicate_detection_history_time_window = "P1D"
}

locals {
  bus_namespace_name = local.managed_transfer ? azurerm_servicebus_namespace.this[0].name : ""
  bus_namespace_id   = local.managed_transfer ? azurerm_servicebus_namespace.this[0].id : ""
  bus_queue_name     = local.managed_transfer ? azurerm_servicebus_queue.transfer_requests[0].name : ""
  bus_queue_id       = local.managed_transfer ? azurerm_servicebus_queue.transfer_requests[0].id : ""
  ticket_queue_name  = local.ticket_intake ? azurerm_servicebus_queue.ticket_events[0].name : ""
  ticket_queue_id    = local.ticket_intake ? azurerm_servicebus_queue.ticket_events[0].id : ""
}

moved {
  from = azurerm_servicebus_namespace.this
  to   = azurerm_servicebus_namespace.this[0]
}

moved {
  from = azurerm_servicebus_queue.transfer_requests
  to   = azurerm_servicebus_queue.transfer_requests[0]
}

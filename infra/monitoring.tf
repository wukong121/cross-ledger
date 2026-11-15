# -----------------------------------------------------------------------------
# Observability + the queryable half of the ledger.
#   XLedger_CL           normalized lifecycle events (request→approve→operate→revoke)
#   AzureActivity        control-plane operations (subscription diagnostic setting)
#   StorageBlobLogs      every data-plane read/write on source & landing
#   ContainerAppConsole* transfer / collector job logs
# -----------------------------------------------------------------------------

locals {
  ledger_table  = "XLedger_CL"
  ledger_stream = "Custom-XLedger"

  ledger_columns = [
    { name = "TimeGenerated", type = "datetime", description = "When the event happened (source system time)." },
    { name = "EventId", type = "string", description = "Deterministic id — idempotency key across all sinks." },
    { name = "EventType", type = "string", description = "e.g. access.requested, access.approved, transfer.completed." },
    { name = "Stage", type = "string", description = "request | approve | operate | revoke" },
    { name = "TicketId", type = "string", description = "Business ticket that ties the whole lifecycle together." },
    { name = "ActorId", type = "string", description = "Entra object id of who did it." },
    { name = "ActorName", type = "string", description = "UPN / display name of who did it." },
    { name = "ActorType", type = "string", description = "User | Group | ServicePrincipal | ManagedIdentity | System" },
    { name = "Role", type = "string", description = "Azure role involved, if any." },
    { name = "Scope", type = "string", description = "Azure resource scope involved." },
    { name = "Route", type = "string", description = "Data route, e.g. source/datasets/a/ -> landing/datasets/a/." },
    { name = "Status", type = "string", description = "Outcome / state reported by the source system." },
    { name = "Message", type = "string", description = "Human-readable one-liner." },
    { name = "Details", type = "dynamic", description = "Raw, source-specific payload." },
    { name = "Origin", type = "string", description = "Which component produced the event." },
  ]
}

resource "azurerm_log_analytics_workspace" "this" {
  name                = "log-${local.name}"
  resource_group_name = azurerm_resource_group.gov.name
  location            = azurerm_resource_group.gov.location
  sku                 = "PerGB2018"
  retention_in_days   = var.log_retention_days
  tags                = var.tags
}

resource "azapi_resource" "ledger_table" {
  type      = "Microsoft.OperationalInsights/workspaces/tables@2023-09-01"
  name      = local.ledger_table
  parent_id = azurerm_log_analytics_workspace.this.id
  body = {
    properties = {
      plan                 = "Analytics"
      retentionInDays      = var.log_retention_days
      totalRetentionInDays = var.ledger_table_total_retention_days
      schema = {
        name    = local.ledger_table
        columns = local.ledger_columns
      }
    }
  }
}

resource "azurerm_monitor_data_collection_endpoint" "this" {
  name                = "dce-${local.name}"
  resource_group_name = azurerm_resource_group.gov.name
  location            = azurerm_resource_group.gov.location
  tags                = var.tags
}

resource "azurerm_monitor_data_collection_rule" "ledger" {
  name                        = "dcr-${local.name}-ledger"
  resource_group_name         = azurerm_resource_group.gov.name
  location                    = azurerm_resource_group.gov.location
  data_collection_endpoint_id = azurerm_monitor_data_collection_endpoint.this.id
  tags                        = var.tags

  destinations {
    log_analytics {
      workspace_resource_id = azurerm_log_analytics_workspace.this.id
      name                  = "ledger-ws"
    }
  }

  data_flow {
    streams       = [local.ledger_stream]
    destinations  = ["ledger-ws"]
    output_stream = "Custom-${local.ledger_table}"
    transform_kql = "source"
  }

  stream_declaration {
    stream_name = local.ledger_stream
    dynamic "column" {
      for_each = local.ledger_columns
      content {
        name = column.value.name
        type = column.value.type
      }
    }
  }

  depends_on = [azapi_resource.ledger_table]
}

# Control plane (who started / changed / deleted what) for the whole subscription.
resource "azurerm_monitor_diagnostic_setting" "activity" {
  name                       = "${local.name}-activity"
  target_resource_id         = data.azurerm_subscription.current.id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id

  enabled_log { category = "Administrative" }
  enabled_log { category = "Security" }
  enabled_log { category = "Policy" }
}

# Data plane: every blob read/write/delete on source and landing.
resource "azurerm_monitor_diagnostic_setting" "blob" {
  for_each = {
    source  = azurerm_storage_account.source.id
    landing = azurerm_storage_account.landing.id
    audit   = azurerm_storage_account.audit.id
  }
  name                       = "${local.name}-blob"
  target_resource_id         = "${each.value}/blobServices/default"
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id

  enabled_log { category = "StorageRead" }
  enabled_log { category = "StorageWrite" }
  enabled_log { category = "StorageDelete" }
}

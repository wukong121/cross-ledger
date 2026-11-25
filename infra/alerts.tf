# -----------------------------------------------------------------------------
# Operations: alert on anything in the ledger that needs a human.
# Fires within ~10 minutes of a failed / rejected transfer, a failed verification,
# missing evidence or access without a PIM activation.
# -----------------------------------------------------------------------------

resource "azurerm_monitor_action_group" "ops" {
  count               = length(var.alert_emails) > 0 ? 1 : 0
  name                = "ag-${local.name}-ops"
  resource_group_name = azurerm_resource_group.gov.name
  short_name          = "xl-ops"
  tags                = var.tags

  dynamic "email_receiver" {
    for_each = var.alert_emails
    content {
      name                    = "ops-${email_receiver.key}"
      email_address           = email_receiver.value
      use_common_alert_schema = true
    }
  }
}

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "ledger" {
  name                 = "alert-${local.name}-ledger"
  resource_group_name  = azurerm_resource_group.gov.name
  location             = azurerm_resource_group.gov.location
  scopes               = [azurerm_log_analytics_workspace.this.id]
  severity             = 2
  evaluation_frequency = "PT5M"
  window_duration      = "PT15M"
  description          = "cross-ledger: transfer failed / rejected, verification failed, audit gap, data accessed without activation, ticket withdrawn/deleted after approval, or approval form drift"
  tags                 = var.tags

  criteria {
    query                   = <<-KQL
      XLedger_CL
      | where EventType in ("transfer.failed", "transfer.rejected", "verify.failed", "audit.gap", "data.accessed.outside-window",
          "ticket.withdrawn.after-start", "ticket.source.deleted", "catalog.drift")
      | project TimeGenerated, TicketId, EventType, ActorName, Message
    KQL
    time_aggregation_method = "Count"
    operator                = "GreaterThan"
    threshold               = 0
    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  dynamic "action" {
    for_each = length(var.alert_emails) > 0 ? [1] : []
    content {
      action_groups = [azurerm_monitor_action_group.ops[0].id]
    }
  }

  depends_on = [azapi_resource.ledger_table]
}

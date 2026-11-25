# Azure Monitor Workbook: "who asked, who approved, what moved, when it was revoked" per ticket.

resource "random_uuid" "workbook" {}

locals {
  wb_q_tickets = <<-KQL
    XLedger_CL
    | summarize FirstSeen = min(TimeGenerated), LastSeen = max(TimeGenerated),
        Serial    = take_anyif(tostring(Details.serial), isnotempty(tostring(Details.serial))),
        Requester = take_anyif(ActorName, EventType in ("access.requested", "ticket.submitted")),
        Approver  = take_anyif(ActorName, EventType in ("access.approved", "ticket.approved") and ActorType != "System"),
        Dataset   = take_anyif(tostring(Details.dataset), isnotempty(tostring(Details.dataset))),
        Destination = take_anyif(tostring(Details.destination), isnotempty(tostring(Details.destination))),
        Stages    = make_set(Stage), Events = count(),
        BytesMoved = sumif(tolong(Details.bytesTransferred), EventType == "transfer.completed"),
        Failures  = countif(EventType in ("transfer.failed", "transfer.rejected", "verify.failed", "access.denied"))
        by TicketId
    | extend Closed = set_has_element(Stages, "revoke") or set_has_element(Stages, "close")
    | order by LastSeen desc
  KQL

  wb_q_timeline = <<-KQL
    XLedger_CL
    | where TicketId == '{TicketId}'
    | project TimeGenerated, Stage, EventType, ActorName, Role, Route, Status, Message, Origin, EventId
    | order by TimeGenerated asc
  KQL

  wb_q_stage = <<-KQL
    XLedger_CL
    | where TicketId == '{TicketId}'
    | summarize Events = count() by Stage
  KQL

  wb_q_anomalies = <<-KQL
    XLedger_CL
    | where EventType in ("transfer.rejected", "verify.failed", "access.denied", "transfer.failed", "data.accessed.outside-window", "data.access.denied", "rbac.changed", "audit.gap", "audit.partial",
        "ticket.withdrawn.after-start", "ticket.source.deleted", "catalog.drift", "destination.changed")
    | project TimeGenerated, TicketId, EventType, ActorName, Message
    | order by TimeGenerated desc
  KQL

  workbook = {
    version = "Notebook/1.0"
    items = [
      { type = 1, name = "title", content = { json = "# cross-ledger — data movement ledger\nSubmit → Approve → Transfer → Close, per ticket (self-service mode: Request → Approve → Operate → Revoke). Source: `XLedger_CL` (also mirrored to WORM storage and Azure Confidential Ledger)." } },
      {
        type = 3, name = "tickets"
        content = {
          version         = "KqlItem/1.0", query = local.wb_q_tickets, size = 0, title = "Tickets"
          timeContext     = { durationMs = 2592000000 }, queryType = 0, resourceType = "microsoft.operationalinsights/workspaces"
          exportFieldName = "TicketId", exportParameterName = "TicketId"
        }
      },
      {
        type = 3, name = "stages"
        content = {
          version      = "KqlItem/1.0", query = local.wb_q_stage, size = 4, title = "Lifecycle coverage for {TicketId}"
          timeContext  = { durationMs = 2592000000 }, queryType = 0, resourceType = "microsoft.operationalinsights/workspaces", visualization = "tiles"
          tileSettings = { titleContent = { columnMatch = "Stage" }, leftContent = { columnMatch = "Events" } }
        }
        conditionalVisibility = { parameterName = "TicketId", comparison = "isNotEqualTo", value = "" }
      },
      {
        type = 3, name = "timeline"
        content = {
          version     = "KqlItem/1.0", query = local.wb_q_timeline, size = 0, title = "Timeline for {TicketId}"
          timeContext = { durationMs = 2592000000 }, queryType = 0, resourceType = "microsoft.operationalinsights/workspaces"
        }
        conditionalVisibility = { parameterName = "TicketId", comparison = "isNotEqualTo", value = "" }
      },
      {
        type = 3, name = "anomalies"
        content = {
          version     = "KqlItem/1.0", query = local.wb_q_anomalies, size = 0, title = "Rejections, failures, anomalies and registry changes"
          timeContext = { durationMs = 2592000000 }, queryType = 0, resourceType = "microsoft.operationalinsights/workspaces"
        }
      },
    ]
    fallbackResourceIds = [azurerm_log_analytics_workspace.this.id]
  }
}

resource "azurerm_application_insights_workbook" "ledger" {
  name                = random_uuid.workbook.result
  resource_group_name = azurerm_resource_group.gov.name
  location            = azurerm_resource_group.gov.location
  display_name        = "cross-ledger — data movement ledger (${local.sfx})"
  source_id           = lower(azurerm_log_analytics_workspace.this.id)
  category            = "workbook"
  data_json           = jsonencode(local.workbook)
  tags                = var.tags
}

# -----------------------------------------------------------------------------
# Just-in-time data access with Microsoft Entra Privileged Identity Management
# (PIM for Azure resources).
#
#   data-team group  --eligible-->  role @ scope
#   member activates (justification + ticket no.)  -> PendingApproval
#   any it-ops member approves / denies            -> Provisioned / Denied
#   assignment auto-expires after <= max_activation_hours, or is revoked early
#
# Every step is an ARM object (roleAssignmentScheduleRequest / approval) which
# the collector reads and turns into ledger events keyed by the ticket number.
# -----------------------------------------------------------------------------

locals {
  role_ids = {
    blob_reader      = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/2a2b9908-6ea1-4ae2-8e65-a410df84e7d1"
    blob_contributor = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/ba92f5b4-2d11-453d-a403-e96b0029c9fe"
    bus_sender       = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/69a216fc-b8fb-44d8-bc22-1f3c2cd27a39"
    owner            = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/8e3af657-a8ff-443c-a75c-2fe8c4bcb635"
  }

  # What the data team can ask for. Keys are the short names used by the CLI.
  data_roles = merge(local.base_data_roles, var.enable_managed_transfer ? {
    transfer-operator = {
      scope       = local.bus_namespace_id
      role        = local.role_ids.bus_sender
      description = "Submit transfer requests (source -> landing)"
    }
  } : {})

  base_data_roles = {
    source-reader = {
      scope       = azurerm_storage_account.source.id
      role        = local.role_ids.blob_reader
      description = "Browse / sample the overseas source dataset"
    }
    landing-writer = {
      scope       = azurerm_storage_account.landing.id
      role        = local.role_ids.blob_contributor
      description = "Read / write / curate data in the landing zone"
    }
  }
}

resource "time_static" "eligibility_start" {}

resource "azurerm_role_management_policy" "data" {
  for_each           = local.data_roles
  scope              = each.value.scope
  role_definition_id = each.value.role

  activation_rules {
    maximum_duration                   = "PT${var.max_activation_hours}H"
    require_approval                   = true
    require_justification              = true
    require_ticket_info                = true
    require_multifactor_authentication = false # enforce via Conditional Access authentication context in production

    approval_stage {
      primary_approver {
        object_id = azuread_group.this["it_ops"].object_id
        type      = "Group"
      }
    }
  }

  eligible_assignment_rules {
    expiration_required = true
    expire_after        = "P${var.eligibility_days}D"
  }

  active_assignment_rules {
    expiration_required                = true
    expire_after                       = "P15D"
    require_justification              = true
    require_multifactor_authentication = false
  }

  notification_rules {
    eligible_activations {
      approver_notifications {
        notification_level    = "All"
        default_recipients    = true
        additional_recipients = []
      }
    }
  }
}

locals {
  # role x principal pairs according to eligibility_mode
  data_eligibility = var.eligibility_mode == "group" ? {
    for k, r in local.data_roles : k => merge(r, { principal = azuread_group.this["data_team"].object_id })
    } : merge([
      for k, r in local.data_roles : {
        for upn in var.data_team_members : "${k}/${upn}" => merge(r, { principal = data.azuread_user.members[upn].object_id })
      }
  ]...)
}

resource "azurerm_pim_eligible_role_assignment" "data_team" {
  for_each           = local.data_eligibility
  scope              = each.value.scope
  role_definition_id = each.value.role
  principal_id       = each.value.principal
  justification      = "cross-ledger baseline: ${each.value.description}"

  schedule {
    start_date_time = time_static.eligibility_start.rfc3339
    expiration {
      duration_days = var.eligibility_days
    }
  }

  ticket {
    number = "BASELINE"
    system = "cross-ledger"
  }

  depends_on = [azurerm_role_management_policy.data]
}

# Optional: IT ops Owner via PIM (self-activation, no approval) instead of standing.
resource "azurerm_pim_eligible_role_assignment" "it_ops_owner" {
  for_each           = var.it_ops_owner_mode == "eligible" ? local.rgs : {}
  scope              = each.value
  role_definition_id = local.role_ids.owner
  principal_id       = azuread_group.this["it_ops"].object_id
  justification      = "cross-ledger baseline: IT ops owner"

  schedule {
    start_date_time = time_static.eligibility_start.rfc3339
    expiration {
      duration_days = 365
    }
  }

  ticket {
    number = "BASELINE"
    system = "cross-ledger"
  }
}

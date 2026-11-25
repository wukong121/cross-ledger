variable "subscription_id" {
  description = "Azure subscription that hosts the overseas side (source data, transfer plane, governance plane)."
  type        = string
}

variable "prefix" {
  description = "Short name prefix for every resource (lowercase letters/digits, <= 6 chars)."
  type        = string
  default     = "xl"
  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,5}$", var.prefix))
    error_message = "prefix must be 2-6 lowercase alphanumerics starting with a letter."
  }
}

variable "source_location" {
  description = "Region of the overseas source data (where the business data is stored)."
  type        = string
}

variable "governance_location" {
  description = "Region for the governance plane (Log Analytics, ledger, transfer jobs). Defaults to source_location."
  type        = string
  default     = null
}

variable "confidential_ledger_location" {
  description = "Region for Azure Confidential Ledger (only available in some regions). Defaults to governance location."
  type        = string
  default     = null
}

variable "enable_confidential_ledger" {
  description = "Write every ledger event to Azure Confidential Ledger (tamper-evident, receipts verifiable)."
  type        = bool
  default     = true
}

# ---------------------------------------------------------------- identities --

variable "it_ops_members" {
  description = "UPNs of IT operations staff: Owner of the solution scopes and approvers of every access request."
  type        = list(string)
  default     = []
}

variable "data_team_members" {
  description = "UPNs of R&D / algorithm / data staff who may request (not hold) data access."
  type        = list(string)
  default     = []
}

variable "auditor_members" {
  description = "UPNs of compliance / audit staff with read-only access to the ledger."
  type        = list(string)
  default     = []
}

variable "it_ops_owner_mode" {
  description = "\"active\" = standing Owner for IT ops; \"eligible\" = Owner via PIM activation (no approval, MFA) — recommended for production."
  type        = string
  default     = "active"
  validation {
    condition     = contains(["active", "eligible"], var.it_ops_owner_mode)
    error_message = "it_ops_owner_mode must be \"active\" or \"eligible\"."
  }
}

variable "max_activation_hours" {
  description = "Maximum duration of a single PIM activation for data roles."
  type        = number
  default     = 8
}

variable "eligibility_days" {
  description = "How long a data-team eligibility lasts before it must be renewed (re-certified)."
  type        = number
  default     = 180
}

# ------------------------------------------------------------------ transfer --

variable "container_image" {
  description = "Image for the transfer & collector jobs. Leave null on the first apply; deploy.sh builds and sets it."
  type        = string
  default     = null
}

variable "transfer_parallelism" {
  description = "How many replicas (shards) one transfer execution may run in parallel."
  type        = number
  default     = 4
}

variable "collector_cron" {
  description = "Cron expression (UTC) for the ledger collector."
  type        = string
  default     = "*/5 * * * *"
}

variable "ledger_retention_days" {
  description = "WORM (time-based immutability) retention for the ledger archive container."
  type        = number
  default     = 2555 # ~7 years
}

variable "ledger_immutability_locked" {
  description = "Lock the immutability policy. Locked policies cannot be shortened or removed — keep false for PoC."
  type        = bool
  default     = false
}

variable "log_retention_days" {
  description = "Interactive retention of the Log Analytics workspace."
  type        = number
  default     = 90
}

variable "ledger_table_total_retention_days" {
  description = "Total (interactive + long-term) retention of the ledger table in Log Analytics."
  type        = number
  default     = 2556
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default = {
    solution = "cross-ledger"
  }
}

variable "vnet_cidr" {
  description = "Address space of the solution VNet (Container Apps subnet + private endpoints)."
  type        = string
  default     = "10.60.0.0/16"
}

variable "seed_batches" {
  description = "PoC only: synthetic dataset batches the seed job writes into the source account."
  type        = string
  default     = "batch-001,batch-002,batch-003"
}

variable "eligibility_mode" {
  description = <<-EOT
    "group": data roles are eligible to the data-team group (recommended; membership changes need no redeploy,
             but PIM can take a while to pick up brand-new members).
    "user":  eligibility is expanded to each data_team_members UPN (effective immediately; redeploy on changes).
  EOT
  type        = string
  default     = "group"
  validation {
    condition     = contains(["group", "user"], var.eligibility_mode)
    error_message = "eligibility_mode must be \"group\" or \"user\"."
  }
}

# ------------------------------------------------------------- public access --

variable "source_public_access" {
  description = <<-EOT
    How the source dataset can be reached from outside Azure.
    "private"   (default, recommended): Private Endpoint only — pair with a cross-border private line.
    "perimeter": public endpoint governed by a Network Security Perimeter: only perimeter_allowed_ips
                 (e.g. the egress IPs of the domestic clouds / IDC) may connect, Entra ID auth only.
  EOT
  type        = string
  default     = "private"
  validation {
    condition     = contains(["private", "perimeter"], var.source_public_access)
    error_message = "source_public_access must be \"private\" or \"perimeter\"."
  }
}

variable "perimeter_allowed_ips" {
  description = "Public CIDRs allowed through the perimeter (only used when source_public_access = \"perimeter\")."
  type        = list(string)
  default     = []
}

variable "enable_managed_transfer" {
  description = <<-EOT
    Managed transfer component (always on when intake = "feishu"): transfer jobs are submitted to a Service Bus queue and
    executed by a KEDA-triggered Container Apps Job that runs rclone under its managed identity —
    reading the source over its Private Endpoint and writing to the destination object storage
    (S3-compatible) whose credentials are kept in Key Vault. Egress uses a fixed IP (NAT Gateway).
    The default data path (self-service rclone with the user's own identity) needs none of this.
  EOT
  type        = bool
  default     = false
}

variable "known_service_principals" {
  description = <<-EOT
    Object id -> label of platform services that legitimately read data outside PIM windows
    (e.g. Microsoft Defender for Storage sensitive data discovery). Their access is still recorded
    in the ledger, as `data.accessed.platform-service` (Info) instead of a high-severity anomaly.
  EOT
  type        = map(string)
  default     = {}
}

variable "secrets_admin_ips" {
  description = "Public CIDRs from which IT ops may manage Key Vault secrets (managed transfer only)."
  type        = list(string)
  default     = []
}

variable "alert_emails" {
  description = "E-mail addresses notified when the ledger records failures, rejections or anomalies."
  type        = list(string)
  default     = []
}

variable "rclone_transfers" {
  description = "Parallel file transfers per shard in the managed transfer worker (rclone --transfers)."
  type        = number
  default     = 16
}

# ------------------------------------------------------------- ticket intake --

variable "intake" {
  description = <<-EOT
    Where transfers come from.
    "feishu" (default): a Feishu (Lark) approval form. IT approves in Feishu, the connector re-checks the
                        ticket on the Azure side and the worker moves the data — nobody needs an Azure account
                        to request data. Implies the managed transfer component.
    "cli":              only `xl transfer` (needs enable_self_service = true for the PIM-based flow).
  EOT
  type        = string
  default     = "feishu"
  validation {
    condition     = contains(["feishu", "cli"], var.intake)
    error_message = "intake must be \"feishu\" or \"cli\"."
  }
}

variable "enable_self_service" {
  description = <<-EOT
    Optional self-service mode (off by default): data-team members with Azure accounts get PIM-eligible
    data roles (read the source with their own identity, submit transfers with `xl transfer`).
  EOT
  type        = bool
  default     = false
}

variable "datasets" {
  description = "Datasets that may be requested: name (as shown in the approval form) -> prefix in the source container."
  type = map(object({
    prefix      = string
    description = optional(string, "")
  }))
  default = {}
  validation {
    condition     = alltrue([for k in keys(var.datasets) : can(regex("^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$", k))])
    error_message = "dataset names: lowercase letters, digits and '-' (max 32)."
  }
}

variable "feishu_app_id" {
  description = "Feishu self-built app ID (App Secret: `xl feishu set-secret`, never in tfvars)."
  type        = string
  default     = ""
}

variable "feishu_approval_code" {
  description = "Approval definition code of the data request form (only this definition is processed)."
  type        = string
  default     = ""
}

variable "feishu_domain" {
  description = "Feishu Open Platform base URL."
  type        = string
  default     = "https://open.feishu.cn"
}

variable "feishu_fields" {
  description = "Form widget (custom_id, id or name) -> meaning: dataset | destination | purpose."
  type        = map(string)
  default = {
    "数据集" = "dataset"
    "目的地" = "destination"
    "用途"  = "purpose"
  }
}

variable "it_approver_emails" {
  description = "E-mail addresses whose approval counts as IT approval (matched against the ticketing system). Defaults to it_ops_members."
  type        = list(string)
  default     = null
}

variable "ticket_reconcile_days" {
  description = "How far back the collector re-checks tickets (missed events, deleted records)."
  type        = number
  default     = 7
}

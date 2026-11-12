variable "account_id" {
  type        = string
  description = "Cloudflare account ID (the 32-character account tag)."

  validation {
    condition     = can(regex("^[0-9a-f]{32}$", var.account_id))
    error_message = "account_id is the 32-character hex account tag, not the account name."
  }
}

variable "domain" {
  type        = string
  description = "Apex zone whose subdomains are exposed."

  validation {
    condition     = can(regex("^[a-z0-9.-]+$", var.domain))
    error_message = "domain is the bare apex, lowercase, without a trailing dot."
  }
}

variable "email" {
  type        = string
  description = "Owner identity: the only principal the Access default-deny admits, and the alert recipient."

  validation {
    condition     = can(regex("^[^@]+@[^@]+$", var.email))
    error_message = "email is the address the IdP asserts, not a group name."
  }
}

variable "host_tunnel_id" {
  type        = string
  description = "UUID of the human-owned host tunnel (nix-config `webProxy`, cloudflared on smortress). Read it from the tunnel credentials JSON's TunnelID."

  validation {
    condition     = can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", var.host_tunnel_id))
    error_message = "host_tunnel_id is the tunnel UUID, not the tunnel name."
  }
}

variable "cluster_tunnel_id" {
  type        = string
  description = "UUID of the cluster tunnel (cloudflared in the ingress namespace, T6). Empty until the tunnel is minted; only required once cluster_hosts is non-empty."
  default     = ""

  validation {
    condition     = var.cluster_tunnel_id == "" || can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", var.cluster_tunnel_id))
    error_message = "cluster_tunnel_id is either empty or the tunnel UUID, not the tunnel name."
  }
}

variable "cluster_hosts" {
  type        = set(string)
  description = "Subdomains routed to the cluster tunnel instead of the host tunnel. Defaults to the keys of `local.cluster_ingress`, so a cutover is one edit in tofu/cluster.tf and DNS cannot drift from the tunnel's ingress. Set it explicitly only to pin a subset, which is the rollback for a single name."
  default     = null
  nullable    = true

  validation {
    condition = var.cluster_hosts == null || alltrue([
      for host in var.cluster_hosts : can(regex("^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", host))
    ])
    error_message = "cluster_hosts holds bare one-label subdomains (no dots, lowercase, no wildcard)."
  }
}

variable "acme_account_id" {
  type        = string
  description = "Numeric Let's Encrypt ACME account id behind the in-VM CSR client. Pinned into the CAA accounturi so no other account of that CA can issue for the zone. Empty until that client has registered: the pin is applied later, and no CAA record is written meanwhile."
  default     = ""

  validation {
    condition     = var.acme_account_id == "" || can(regex("^[0-9]+$", var.acme_account_id))
    error_message = "acme_account_id is the numeric account number from the ACME account URL (/acme/acct/<id>), or empty until that account exists."
  }
}

variable "acme_directory" {
  type        = string
  description = "ACME directory URL of that account, used to derive its account URI. Staging here pins staging: a CAA accounturi never mixes the two."
  default     = "https://acme-v02.api.letsencrypt.org/directory"
}

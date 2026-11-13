# Remote state for the Cloudflare zone (SDLC #622). The operator's local file was
# the only copy: a lost checkout lost the mapping between config and the live
# objects, and CI could not plan against the real state at all.
#
# R2 speaks the S3 API, so the S3 backend with an R2 endpoint is the whole
# configuration. Nothing here is a credential: the access key pair comes from the
# environment (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`), which is the
# `cloudflare` environment's R2 token in CI and the SOPS-decrypted pair locally
# (scripts/tofu-cloudflare.sh).
#
# The identifiers are inline rather than passed with `-backend-config` on
# purpose: the account tag is already committed in `terraform.tfvars`, and a
# backend that initialises identically from a CI runner and from the operator's
# checkout is worth more than the indirection. `tests/tofu/check.sh` asserts the
# endpoint carries the same account tag as `var.account_id`.
#
# State contents: resource addresses and attributes, which are Cloudflare object
# ids and settings — no token, no secret (the provider credential is environment
# only). R2 encrypts objects at rest; there is no client-side state encryption
# because there is no key to lose that way.
terraform {
  backend "s3" {
    bucket = "homelabs-tofu-state"
    key    = "cloudflare/terraform.tfstate"
    # R2 has no regions; the SDK still requires a value.
    region = "auto"

    endpoints = {
      s3 = "https://385f7d47e2e8438fbda5d6f76d760434.r2.cloudflarestorage.com"
    }

    # R2 implements the object API, not the account API: every validation call
    # the backend makes by default (STS, account id, region discovery) would
    # fail, and the checksum trailer is not supported by every R2 client path.
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    skip_metadata_api_check     = true
    skip_s3_checksum            = true
    use_path_style              = true
  }
}

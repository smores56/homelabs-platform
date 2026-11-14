# Cloudflare zone, as code

This directory is the whole Cloudflare account's edge config: DNS records, the
cluster tunnel's ingress rules, Access applications and policies, and the
recovery pages project. `docs/cloudflare.md` explains the shape; this file only
says how a change travels.

1. Branch, change `tofu/`, open a PR. `tofu plan` is a required check: it plans
   against the live state with read-only credentials and comments the plan on
   the PR. Read the plan, not only the diff — a renamed `for_each` key is a
   replacement.
2. Merge. `tofu-apply.yml` plans again and applies the saved plan. A plan that
   replaces or destroys anything stops instead, and waits for an owner dispatch.
3. Outside changes (dashboard edits) show up as a weekly `tofu-drift` issue.

State lives in the `homelabs-tofu-state` R2 bucket (`backend.tf`), never in git.
The write credential exists only in the `cloudflare` environment; the read-only
pair exists only in `cloudflare-plan`.

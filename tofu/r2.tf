# restic keeps its repository in this bucket and the recovery page reads it straight from
# the browser. Every read is signed, so it carries an Authorization header and the browser
# sends a preflight first: without a policy here R2 answers the OPTIONS with "CORS not
# configured for this bucket" and the page can only report "Failed to fetch".
#
# The bucket itself predates tofu and is not managed here; only its browser policy is.

resource "cloudflare_r2_bucket_cors" "restic" {
  account_id  = var.account_id
  bucket_name = "restic-backup"

  rules = [{
    id = "recovery page"

    allowed = {
      # The letter's URL, plus the Pages default host so a deployment can be tested
      # before the custom domain moves to it. Preview deployments get their own
      # <hash>.sam-mohr-data-recovery.pages.dev host, which the wildcard covers.
      origins = [
        "https://recovery.${var.domain}",
        "https://sam-mohr-data-recovery.pages.dev",
        "https://*.sam-mohr-data-recovery.pages.dev",
      ]
      methods = ["GET", "HEAD"]
      # Exactly what Chrome asks for on the SDK's signed GET, captured from a
      # real preflight. R2 hides the origin header as soon as one requested
      # header is missing, and the browser only reports "Failed to fetch", so
      # this list has to be a superset. Recapture with:
      #   curl -X OPTIONS -H 'Origin: https://recovery.sammohr.dev' \
      #     -H 'Access-Control-Request-Method: GET' \
      #     -H 'Access-Control-Request-Headers: <the list Chrome sends>' \
      #     .../r2.cloudflarestorage.com/restic-backup/config
      headers = [
        "amz-sdk-invocation-id",
        "amz-sdk-request",
        "authorization",
        "x-amz-checksum-mode",
        "x-amz-content-sha256",
        "x-amz-date",
        "x-amz-security-token",
        "x-amz-user-agent",
        "range",
        "content-type",
      ]
    }

    # Response headers the SDK reads back; the body arrives without them, but a ranged
    # read checks its length and restic compares the ETag.
    expose_headers  = ["content-length", "content-range", "etag", "last-modified"]
    max_age_seconds = 3600
  }]
}

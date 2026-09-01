variable "region" {
  description = "Where the user pool lives. The pool's issuer URL contains this, so moving it later invalidates every token."
  type        = string
  default     = "us-west-2"
}

variable "domain_prefix" {
  description = <<-EOT
    The label in <prefix>.auth.<region>.amazoncognito.com. Must be unique across
    every AWS account in the region, which is why it is not derived from
    anything: a collision has to be resolved by a person picking another word.

    This string goes into the Google OAuth client's authorised origin and
    redirect URI, so changing it later means editing the Google console too.
  EOT
  type        = string
}

variable "callback_urls" {
  description = <<-EOT
    Every address Cognito is allowed to send the authorization code to. Cognito
    refuses any redirect_uri not listed here exactly, which is what stops a
    stolen link from delivering the code somewhere else.

    Two kinds go in: the screen's CloudFront URL, and a handful of
    http://localhost:<port>/callback for `ddpsrun login`. The CLI picks a free
    port from the ones listed rather than any free port, because a port that is
    not registered here cannot receive the code (docs/16-login.md 16.4).
  EOT
  type        = list(string)
}

variable "logout_urls" {
  description = "Where Cognito sends the browser after signing out."
  type        = list(string)
  default     = []
}

variable "google_client_id" {
  description = <<-EOT
    From the Google Cloud Console. Empty means no Google button: the pool still
    works with its own email/password users, and adding Google later changes
    nothing in the screen or the CLI (docs/16-login.md 16.6).
  EOT
  type        = string
  default     = ""
}

variable "google_client_secret" {
  description = <<-EOT
    From the same place. NEVER commit this. terraform.tfvars is gitignored;
    terraform.tfvars.example carries a placeholder.

    It does end up in terraform state, which is why the state file is gitignored
    too. Treat the state file as a secret.
  EOT
  type        = string
  default     = ""
  sensitive   = true
}

variable "tags" {
  description = "Applied to everything here."
  type        = map(string)
  default = {
    Project   = "ddpsrun"
    ManagedBy = "terraform"
  }
}

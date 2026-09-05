# DDPSRUN-COGNITO
#
# The login system, so we do not build one. Decisions and their reasons are in
# `docs/16-login.md`; this file is the four objects that implement them.
#
# END-TO-END FLOW:
#   1. `aws_cognito_user_pool.gw` holds the people. Email is the username.
#   2. `aws_cognito_identity_provider.google` (only when a client id is given)
#      lets Cognito hand the login off to Google and come back.
#   3. `aws_cognito_user_pool_client.gw` is what the screen and the CLI talk to.
#      It has NO secret on purpose: neither of them can hide one, so both use
#      PKCE instead (docs/16-login.md 16.4).
#   4. `aws_cognito_user_pool_domain.gw` is the address of the login page
#      Cognito draws for us.
#
# WHAT IS NOT HERE. No namespace, no team, no per-user attribute carrying
# either. Cognito answers "who is this"; the token file answers "what may they
# touch" (docs/16-login.md 16.2). Keeping the second one next to the terraform
# that creates the namespaces is what stops the two drifting apart.

resource "aws_cognito_user_pool" "gw" {
  name = "ddpsrun-gw"

  # Sign in with the email address itself rather than a separate username. One
  # fewer thing for a person to remember, and it matches the key the server maps
  # on, so what a user types is what the token file lists.
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_numbers                  = true
    require_symbols                  = false
    require_uppercase                = true
    temporary_password_validity_days = 7
  }

  # Only an operator creates accounts. Self-signup would let anyone with the
  # login URL create a Cognito user; they would then get 403 from our server
  # (their email is not in the token file), but they would still be a real user
  # in the pool and would count toward the free tier.
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  tags = var.tags
}

# Only created when a Google client id was supplied. Everything else here works
# without it, which is what lets the pool be stood up before anyone has visited
# the Google console (docs/16-login.md 16.6).
resource "aws_cognito_identity_provider" "google" {
  count = var.google_client_id == "" ? 0 : 1

  user_pool_id  = aws_cognito_user_pool.gw.id
  provider_name = "Google"
  provider_type = "Google"

  provider_details = {
    client_id     = var.google_client_id
    client_secret = var.google_client_secret
    # openid gets an id_token at all; email is the claim we key on. Nothing else
    # is asked for, because anything asked for is something Google shows the
    # user on the consent screen and something we then hold.
    authorize_scopes = "openid email"
  }

  # Copy Google's email into the pool user's email attribute, so an id_token
  # minted for a Google sign-in carries the same claim as one minted for a
  # native user. Without this the server would need two code paths.
  attribute_mapping = {
    email          = "email"
    email_verified = "email_verified"
    username       = "sub"
  }
}

resource "aws_cognito_user_pool_domain" "gw" {
  domain       = var.domain_prefix
  user_pool_id = aws_cognito_user_pool.gw.id
}

resource "aws_cognito_user_pool_client" "gw" {
  name         = "ddpsrun-gw"
  user_pool_id = aws_cognito_user_pool.gw.id

  # No secret. A static page and a CLI on someone's laptop cannot hold one, and
  # a secret that ships to every user is not a secret. PKCE covers what the
  # secret would have covered (docs/16-login.md 16.4).
  generate_secret = false

  allowed_oauth_flows                  = ["code"]
  allowed_oauth_flows_user_pool_client = true
  # openid mints the id_token; email puts the claim in it; profile is not asked
  # for because nothing here uses a name or a picture.
  allowed_oauth_scopes = ["openid", "email"]

  // With no Google configured, COGNITO is the only way in and has to stay.
  // With Google configured, COGNITO is offered only if someone asked for it:
  // the goal is that a researcher signs in with the account they already have
  // and never invents a password for this service.
  supported_identity_providers = (
    var.google_client_id == ""
    ? ["COGNITO"]
    : (var.allow_password_login ? ["Google", "COGNITO"] : ["Google"])
  )

  callback_urls = var.callback_urls
  logout_urls   = var.logout_urls

  # An id_token lives an hour. Long enough that nobody re-logs-in mid-task,
  # short enough that removing someone from the pool takes effect within the
  # hour. The refresh token is what keeps a browser tab working across days.
  id_token_validity      = 60
  access_token_validity  = 60
  refresh_token_validity = 30

  token_validity_units {
    id_token      = "minutes"
    access_token  = "minutes"
    refresh_token = "days"
  }

  # Rotating the refresh token on every use means a stolen one is good until the
  # real user next refreshes, at which point Cognito rejects the stolen copy.
  enable_token_revocation = true

  # SRP and the admin flows are for a client that collects the password itself.
  # Ours never sees a password: the Hosted UI does, so only the code exchange
  # and the refresh are needed.
  explicit_auth_flows = ["ALLOW_REFRESH_TOKEN_AUTH"]

  prevent_user_existence_errors = "ENABLED"

  depends_on = [aws_cognito_identity_provider.google]
}

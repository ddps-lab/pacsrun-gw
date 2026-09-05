output "user_pool_id" {
  description = "Goes into the server's DDPSRUN_COGNITO_POOL_ID."
  value       = aws_cognito_user_pool.gw.id
}

output "client_id" {
  description = "Goes into DDPSRUN_COGNITO_CLIENT_ID, and into the screen and the CLI. Not a secret: it is in every login URL."
  value       = aws_cognito_user_pool_client.gw.id
}

output "issuer" {
  description = "The `iss` every id_token from this pool carries. The server refuses any token whose iss is not exactly this."
  value       = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.gw.id}"
}

output "jwks_uri" {
  description = "Where the server fetches the public keys it verifies signatures with."
  value       = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.gw.id}/.well-known/jwks.json"
}

output "login_domain" {
  description = "The Hosted UI. The screen and the CLI send people here."
  value       = "https://${aws_cognito_user_pool_domain.gw.domain}.auth.${var.region}.amazoncognito.com"
}

output "google_console_values" {
  description = "Paste these into the Google Cloud Console when creating the OAuth client, then put the two values it gives back into terraform.tfvars and apply again."
  value       = <<-EOT
    Authorised JavaScript origin:
      https://${aws_cognito_user_pool_domain.gw.domain}.auth.${var.region}.amazoncognito.com
    Authorised redirect URI:
      https://${aws_cognito_user_pool_domain.gw.domain}.auth.${var.region}.amazoncognito.com/oauth2/idpresponse
  EOT
}

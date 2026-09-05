// What to do with this module's results.
//
// The three repository variables below are the whole handover: paste them into
// pacsrun-gw's Settings -> Secrets and variables -> Actions -> Variables, and the release
// workflow can push. Nothing here is a credential — they are identifiers that appear in the
// trust policy as restrictions.

output "aws_region" {
  description = "Set as the ECR_REGION repository variable."
  value       = var.region
}

output "github_actions_role_arn" {
  description = "Set as the AWS_ROLE_ARN repository variable."
  value       = aws_iam_role.github_actions.arn
}

output "ecr_repository_name" {
  description = "Set as the ECR_REPOSITORY repository variable."
  value       = aws_ecr_repository.gateway.name
}

output "ecr_repository_url" {
  description = "What the image tag is prefixed with. The release workflow builds this itself."
  value       = aws_ecr_repository.gateway.repository_url
}

output "github_repository_variables" {
  description = <<-EOT
    The one command that finishes the handover. Run it once, from anywhere:
  EOT
  value       = <<-EOT
    gh variable set ECR_REGION    --repo ${var.github_owner}/${var.github_repository} --body ${var.region}
    gh variable set AWS_ROLE_ARN  --repo ${var.github_owner}/${var.github_repository} --body ${aws_iam_role.github_actions.arn}
    gh variable set ECR_REPOSITORY --repo ${var.github_owner}/${var.github_repository} --body ${aws_ecr_repository.gateway.name}
    gh variable set LAMBDA_FUNCTION --repo ${var.github_owner}/${var.github_repository} --body ${var.lambda_function_name}
    gh variable set UI_BUCKET       --repo ${var.github_owner}/${var.github_repository} --body ${var.ui_bucket_name}
    gh variable set UI_DISTRIBUTION --repo ${var.github_owner}/${var.github_repository} --body ${var.ui_distribution_id}
  EOT
}

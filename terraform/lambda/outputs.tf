// What to do after apply. Three of these are commands an operator runs once.

output "function_url" {
  description = "The address users point ddpsrun at. `ddpsrun login --server <this>`."
  value       = aws_lambda_function_url.gw.function_url
}

output "function_name" {
  description = "Set as the LAMBDA_FUNCTION repository variable so CI can publish the zip."
  value       = aws_lambda_function.gw.function_name
}

output "execution_role_arn" {
  description = "The identity registered as an EKS access entry."
  value       = aws_iam_role.gw.arn
}

output "next_steps" {
  description = "Run these once, in this order."
  value       = <<-EOT
    # 1. Give the group its permissions inside the cluster. terraform does not do
    #    this: it is a Kubernetes object, and creating it here would make every
    #    plan depend on the cluster being reachable.
    kubectl create clusterrolebinding ${var.name} \
      --clusterrole=${var.name} --group=${var.kubernetes_group}
    #    (the ClusterRole itself is in config/deploy/rbac.yaml)

    # 2. Write the token list. Its VALUE is deliberately not in terraform, because
    #    that would put every hash in the state file.
    aws secretsmanager put-secret-value \
      --secret-id ${aws_secretsmanager_secret.tokens.name} \
      --secret-string file://tokens.json

    # 3. Let CI publish the code. The function currently holds a placeholder that
    #    answers 503.
    gh variable set LAMBDA_FUNCTION --repo ddps-lab/pacsrun-gw --body ${aws_lambda_function.gw.function_name}
    gh variable set AWS_REGION      --repo ddps-lab/pacsrun-gw --body ${var.region}
  EOT
}

// What is left to do after apply.
//
// ★ `function_url` and `function_name` ARE GONE (2026-09-16). The gateway is a
// pod behind an ALB; its address is the hostname on the Gateway's HTTPS listener
// in config/deploy/hyperun-gateway.yaml, and nothing about it is in this module.

output "execution_role_arn" {
  description = <<-EOT
    The identity registered as an EKS access entry.

    ☆ NOTHING ASSUMES THIS ROLE ANY MORE. It trusts lambda.amazonaws.com and the
    Lambda is gone. The pod uses the IAM role `hyperun-gw` through an EKS Pod
    Identity association, which is not in terraform. This output is kept because
    the access entry it names still exists, and deleting either is a separate
    decision.
  EOT
  value       = aws_iam_role.gw.arn
}

output "tokens_secret_name" {
  description = "The secret the POD reads every 60 s. This is the live one."
  value       = aws_secretsmanager_secret.tokens.name
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

    # 3. Deploy the pod and the ALB. This is where the Lambda step used to be.
    kubectl apply -f config/deploy/hyperun-gw.yaml
    kubectl apply -f config/deploy/hyperun-gateway.yaml
  EOT
}

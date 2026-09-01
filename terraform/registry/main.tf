// The gateway's private image registry, and the identity GitHub Actions uses to push to it.
//
// END-TO-END FLOW of one release:
//
//   1. A push to pacsrun-gw runs `.github/workflows/release.yml`.
//   2. The job asks GitHub for an OIDC token. Its `sub` claim names this repository.
//   3. It calls sts:AssumeRoleWithWebIdentity against `aws_iam_role.github_actions` below.
//      The trust policy checks `sub`, which is THE control that stops any other repository
//      from assuming this role.
//   4. amazon-ecr-login exchanges the temporary credentials for a `docker login` password and
//      pushes to `aws_ecr_repository.gateway`.
//   5. The server pod pulls that tag. Nothing else ever does.
//
// WHY A SEPARATE ROLE FROM PACSrun's. PACSrun's role trusts `repo:ddps-lab/pacsrun:*` and
// nothing else, so this repository cannot use it as it stands. Widening that list would have
// been one line; a separate role was chosen instead because the gateway is the component that
// holds users' tokens, and an incident in one repository should not reach the other's registry.
//
// WHY NO OIDC PROVIDER RESOURCE. An AWS account may hold only ONE provider per issuer URL and
// this account already has token.actions.githubusercontent.com — PACSrun's registry module
// created it. Creating a second fails with EntityAlreadyExists; importing it here would put
// one object under two modules' control. So this module takes its ARN as an input.
//
// COST, all of it: ECR storage is $0.10 per GB-month. A roughly 0.2 GiB image kept 20 deep is
// about $0.40 a month. There is no charge for the IAM role, and pulls from a pod in the same
// region carry no data transfer charge. What this does NOT cover is the ALB or tunnel that
// would put the server in front of users (docs/08-plan.md open item 12) — that is separate and
// an ALB is about $22 a month.
//
// Grep anchor: DDPSRUN-REGISTRY

data "aws_caller_identity" "current" {}

resource "aws_ecr_repository" "gateway" {
  name = var.ecr_repository_name

  // MUTABLE because the release workflow republishes `latest` on every push to main. Making it
  // immutable would need `latest` dropped from the workflow first: an immutable repository
  // rejects the second push of a tag and the job fails.
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    // Free, and it flags known CVEs in the python:3.12-slim base the gateway builds on.
    scan_on_push = true
  }

  // ECR encrypts at rest by default; stated so it is visible in review.
  encryption_configuration {
    encryption_type = "AES256"
  }

  // The image is rebuilt from source on every push, so losing the repository is recoverable.
  // force_delete lets `terraform destroy` remove it without purging images by hand first.
  force_delete = true

  tags = var.tags
}

resource "aws_ecr_lifecycle_policy" "gateway" {
  repository = aws_ecr_repository.gateway.name

  // Two rules, evaluated in ASCENDING rulePriority order.
  //
  // THE ORDER IS A HARD REQUIREMENT, not a preference: ECR insists that a rule whose tagStatus
  // is "any" carry the HIGHEST rulePriority, so it is evaluated last. An "any" rule placed
  // first is rejected at apply time.
  //
  //   priority 1 (untagged) — drop orphaned layers, mostly abandoned buildx cache manifests,
  //                           once they are a week old.
  //   priority 2 (any)      — of whatever survives, keep only the newest N images.
  //
  // `latest` is a tag, so rule 1 can never remove it. Rule 2 counts it like any other image,
  // which is why ecr_keep_last_images must stay comfortably above 1.
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "expire untagged layers after 7 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "keep only the newest ${var.ecr_keep_last_images} images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = var.ecr_keep_last_images
        }
        action = { type = "expire" }
      },
    ]
  })
}

locals {
  // The subject claim in its name-based form. The trailing `:*` covers every ref, tag and
  // environment in this repository.
  //
  // THE OWNER SEGMENT ENDS WITH A SLASH ON PURPOSE. "repo:ddps-lab*" would also match
  // "repo:ddps-lab-someone-else/anything", which is a different organization.
  name_sub = "repo:${var.github_owner}/${var.github_repository}:*"

  // The id-based form, used only when the organization emits it. Empty when the two id
  // variables are not both set.
  id_sub = (var.github_org_id != "" && var.github_repository_id != "") ? [
    "repo:${var.github_owner}@${var.github_org_id}/${var.github_repository}@${var.github_repository_id}:*"
  ] : []

  github_subs = concat([local.name_sub], local.id_sub)
}

data "aws_iam_policy_document" "github_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [var.github_oidc_provider_arn]
    }

    // Audience check. GitHub sets `aud` to sts.amazonaws.com for tokens minted for AWS.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    // Subject check: THE control that stops any other repository from assuming this role.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = local.github_subs
    }
  }
}

resource "aws_iam_role" "github_actions" {
  name               = "${var.name_prefix}-github-actions"
  description        = "Assumed by GitHub Actions in ${var.github_owner}/${var.github_repository} to push the ddpsrun gateway image to ECR."
  assume_role_policy = data.aws_iam_policy_document.github_assume.json

  // A build and push takes minutes. One hour is ample and shortens the window a leaked
  // credential is useful in.
  max_session_duration = 3600
  tags                 = var.tags
}

data "aws_iam_policy_document" "github_permissions" {
  // GetAuthorizationToken is account-wide by design: it returns the `docker login` password for
  // the whole registry and takes no resource ARN. On its own it grants no read or write on any
  // repository — those come from the scoped statement below.
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  // The push itself, scoped to THIS repository only. BatchGetImage and
  // BatchCheckLayerAvailability are reads that buildx uses to skip layers already present.
  //
  // Note the ARN names its region. If the repository is ever moved to another region, this
  // policy denies every push until the ARN follows it, and the failure reads as an
  // AccessDeniedException naming an ARN that looks correct at a glance.
  // Publishing the Lambda package. Scoped to the one function: this role is
  // assumed by anything running in the repository, including a pull request from
  // a fork if branch protection ever slipped, so it must not be able to replace
  // arbitrary code in the account.
  dynamic "statement" {
    for_each = var.lambda_function_name == "" ? [] : [1]
    content {
      sid    = "PublishTheGatewayFunctionOnly"
      effect = "Allow"
      actions = [
        "lambda:UpdateFunctionCode",
        "lambda:GetFunction",
        "lambda:GetFunctionConfiguration",
      ]
      resources = [
        "arn:aws:lambda:${var.region}:${data.aws_caller_identity.current.account_id}:function:${var.lambda_function_name}"
      ]
    }
  }

  statement {
    sid = "EcrPushToThisRepositoryOnly"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
      "ecr:BatchGetImage",
      "ecr:DescribeImages",
      "ecr:ListImages",
    ]
    resources = [aws_ecr_repository.gateway.arn]
  }
}

resource "aws_iam_role_policy" "github_actions" {
  name   = "${var.name_prefix}-github-actions"
  role   = aws_iam_role.github_actions.id
  policy = data.aws_iam_policy_document.github_permissions.json
}

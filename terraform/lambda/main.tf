// ★ THIS MODULE NO LONGER BUILDS A LAMBDA, AND THE DIRECTORY IS STILL CALLED ONE.
//
// The gateway ran as a Lambda behind a Function URL until 2026-09-15. It now runs
// as a POD in the EKS cluster (config/deploy/hyperun-gw.yaml) behind an ALB built
// by the AWS Load Balancer Controller from config/deploy/hyperun-gateway.yaml.
// The move was made for one reason and it is worth keeping written down: a
// Function URL cannot accept an INBOUND WebSocket, so a browser terminal was
// impossible, and a 15-minute execution cap meant nothing in the gateway was
// alive between two of a person's keystrokes.
//
// `aws_lambda_function.gw` and `aws_lambda_function_url.gw` were removed from
// AWS with a targeted destroy of exactly those two resources, and the blocks that
// described them are removed from this file TODAY (2026-09-16) -- until now they
// were still here, so the next `terraform apply` would have built the Lambda
// again and put a second, stale copy of the API on the internet.
//
// WHAT IS STILL HERE, and all of it is still real:
//
//   aws_secretsmanager_secret.tokens   the token list the pod reads every 60 s.
//                                      ★ The pod reads THIS secret. It is the one
//                                      resource in this module that is on the
//                                      live path.
//   aws_iam_role.gw                    `<name>-lambda`, trusting
//                                      lambda.amazonaws.com. ☆ NOTHING ASSUMES IT
//                                      ANY MORE -- see below.
//   aws_eks_access_entry.gw            that role, as a principal the cluster
//                                      recognises. Dead with the role.
//   aws_cloudwatch_log_group.gw        /aws/lambda/<name>. Holds the old logs and
//                                      receives nothing. Kept so the history is
//                                      not thrown away by a plan.
//
// ☆ WHAT THE POD ACTUALLY USES, AND IT IS NOT IN TERRAFORM AT ALL. Measured
// 2026-09-16 with `aws eks describe-pod-identity-association`: the ServiceAccount
// `hyperun-system/hyperun-gw` is associated with the IAM role `hyperun-gw`, whose
// trust policy names `pods.eks.amazonaws.com`. That role, that association, the
// ACM certificate for the API's hostname and its Route 53 record were all made by
// hand. Bringing them under terraform is an import, not a create, and it is a
// decision to take with the operator rather than in a commit.
//
// COST of what remains. Secrets Manager is $0.40 per secret per month plus $0.05
// per 10,000 API calls; the pod reads it once every 60 s, which is 43,200 calls a
// month, so about $0.62/month all together. The log group ingests nothing now and
// bills only for what it stores. The ALB in front of the pod is $16.43/month and
// is NOT in this module -- the load balancer controller builds it from a
// Kubernetes object.
//
// WHAT THIS MODULE DELIBERATELY DOES NOT DO. It does not create the
// ClusterRoleBinding that gives `var.kubernetes_group` its permissions. That is a
// Kubernetes object in `config/deploy/rbac.yaml`, and creating it here would make
// every plan depend on the cluster being reachable.
//
// Grep anchor: DDPSRUN-LAMBDA

data "aws_eks_cluster" "target" {
  name = var.cluster_name
}

// ---------------------------------------------------------------- the identity

data "aws_iam_policy_document" "assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "gw" {
  name               = "${var.name}-lambda"
  description        = "Run identity for the ddpsrun gateway function. Registered as an EKS access entry."
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "permissions" {
  // Its own logs.
  statement {
    sid    = "OwnLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.gw.arn}:*"]
  }

  // How the function learns where the apiserver is and which CA signed it.
  // There is no kubeconfig on Lambda, so this is not optional.
  statement {
    sid       = "DescribeThisClusterOnly"
    effect    = "Allow"
    actions   = ["eks:DescribeCluster"]
    resources = [data.aws_eks_cluster.target.arn]
  }

  // The user token list. Scoped to this one secret: a compromise of the function
  // must not turn into a read of everything the account keeps here.
  statement {
    sid       = "ReadTheTokenListOnly"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.tokens.arn]
  }
}

resource "aws_iam_role_policy" "gw" {
  name   = "${var.name}-lambda"
  role   = aws_iam_role.gw.id
  policy = data.aws_iam_policy_document.permissions.json
}

// DDPSRUN-IMAGES-READ needs this account's id to build the ECR repository ARN. A data source
// rather than a variable: the id is a fact about whoever is running terraform, and asking for it
// in terraform.tfvars would be one more twelve-digit number to copy wrongly.
data "aws_caller_identity" "gw" {}

// DDPSRUN-ARTIFACTS-READ. The results bucket, read-only, results prefix only.
// GET /v1/jobs/{id}/artifacts lists a job's files and mints a presigned GET
// URL per file. S3 checks a presigned URL against the SIGNER's permission at
// the moment the URL is USED, so without GetObject here every link the server
// mints would answer AccessDenied — and without ListBucket the listing itself
// is refused. A separate resource rather than a fourth statement above so it
// can be applied and removed with -target, without touching the rest.
data "aws_iam_policy_document" "results_read" {
  statement {
    sid       = "ListResultPrefixOnly"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${var.result_bucket}"]
    // ListBucket is a bucket-level action; this condition is what narrows it
    // to the results prefix, so the function cannot enumerate anything else
    // the bucket might one day hold.
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${var.result_prefix}*"]
    }
  }

  statement {
    sid       = "ReadResultObjectsOnly"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["arn:aws:s3:::${var.result_bucket}/${var.result_prefix}*"]
  }
}

resource "aws_iam_role_policy" "results_read" {
  name   = "${var.name}-results-read"
  role   = aws_iam_role.gw.id
  policy = data.aws_iam_policy_document.results_read.json
}

// DDPSRUN-IMAGES-READ. The container registry, read-only, this account only.
//
// WHY. GET /v1/images offers the images this lab has already built, because the Image field on
// the New job screen was free text with a 70-character ECR URL in its placeholder and a typo in
// any part of it is not caught anywhere: the request is valid, the job is created, and the answer
// arrives as an ImagePullBackOff on a machine that has already been rented.
//
// TWO ACTIONS AND NO MORE. DescribeRepositories answers "what exists" and DescribeImages answers
// "which tags". Deliberately absent: ecr:GetAuthorizationToken and every Get*Layer* action, which
// are what a PULL needs -- this function reads the catalogue and never fetches an image. Also
// absent is every write action, so a compromise of the function cannot push a tag over one a
// researcher is running.
//
// SCOPED TO THIS ACCOUNT'S OWN REPOSITORIES, in this region, by ARN. Both actions take a
// repository resource, so neither needs "*" -- a repository in another account is out of reach
// even if its ARN were guessed, and the role cannot be pointed at a registry this deployment
// does not own. Verified after apply by calling GET /v1/images, which is the only way to know
// the ARN form is the one ECR actually authorises against.
//
// A SEPARATE RESOURCE, for the same reason results_read is one: it can be applied and removed
// with -target without touching the rest of the role.
//
// COST. ECR's price list bills stored bytes ($0.10/GB-month) and data transferred out, and has no
// per-request line for either of these calls, so the route's cost is the Lambda time it spends.
// (Read off the pricing page, not measured here.)
data "aws_iam_policy_document" "registry_read" {
  statement {
    sid    = "ReadThisAccountsRepositoriesOnly"
    effect = "Allow"
    actions = [
      "ecr:DescribeRepositories",
      "ecr:DescribeImages",
    ]
    resources = [
      "arn:aws:ecr:${var.region}:${data.aws_caller_identity.gw.account_id}:repository/*"
    ]
  }
}

resource "aws_iam_role_policy" "registry_read" {
  name   = "${var.name}-registry-read"
  role   = aws_iam_role.gw.id
  policy = data.aws_iam_policy_document.registry_read.json
}

// DDPSRUN-REGISTER. Email one operator, and remember who has already asked.
//
// WHY. A first-time Google visitor holds a valid Cognito token and is 403 on every route,
// because being known to Google is not being registered here. That was a dead end with nothing
// to press. POST /v1/register-request emails the operator once, with the token-file entry ready
// to paste.
//
// TWO ACTIONS. ses:SendEmail is the send. s3:PutObject is the marker that makes it happen ONCE
// per address: the route writes an empty object with `If-None-Match: *`, so S3 itself decides
// which of two simultaneous requests wins and the loser gets 412. Without it, one person
// reloading the screen mails the operator once per reload -- and this endpoint sits on a public
// Lambda URL that any Google account can reach, because `allow_admin_create_user_only` blocks
// the built-in sign-up flow and not a federated first sign-in.
//
// ★ THE PUT IS SCOPED TO A PREFIX THAT IS NOT THE RESULTS PREFIX, and that is the whole point of
// spelling the ARN out. `results_read` above deliberately gives this function no write of any
// kind on the results bucket, so a compromise of it cannot overwrite a job's output. Granting
// PutObject on `<bucket>/*` would undo that. `ddpsrun-register/*` cannot reach
// `${var.result_prefix}*`, and the two paths are disjoint by construction because the results
// prefix is validated to end in a slash and is not "ddpsrun-register/".
//
// ses:SendEmail IS NOT SCOPED BY RECIPIENT, because SES has no condition key for one. What
// bounds it instead is stronger than an IAM condition while the account is in the SES sandbox:
// SES refuses to send to any address that is not a verified identity, and this account has
// (measured 2026-09-08, `aws sesv2 list-email-identities`) exactly zero. So until somebody
// verifies an address, this action can reach no inbox at all; once the operator's address is
// verified, that inbox is the only one it can reach. The `FromEmailAddress` is scoped by
// resource to the one identity this deployment is configured with, which is the part IAM CAN
// express.
//
// COST. SES bills $0.10 per 1,000 messages. Twenty lab members registering once each is 20
// messages, $0.002. The sandbox ceiling is 200 messages a day and 1 a second; hitting 200 every
// day for a month is 6,000 messages and $0.60. The markers are PutObject requests at $0.005 per
// 1,000 ($0.0001 for those twenty) holding zero bytes, and S3 charges storage by the byte.
//
// A SEPARATE RESOURCE with a count, so a deployment that sets no notification address gets
// neither permission rather than an unused one.
data "aws_iam_policy_document" "register_notify" {
  statement {
    sid     = "SendFromTheConfiguredIdentityOnly"
    effect  = "Allow"
    actions = ["ses:SendEmail"]
    resources = [
      "arn:aws:ses:${var.region}:${data.aws_caller_identity.gw.account_id}:identity/${var.register_notify_from != "" ? var.register_notify_from : var.register_notify_to}"
    ]
  }

  statement {
    sid    = "WriteRegistrationMarkersOnly"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      // ★ DeleteObject IS FOR GIVING A FAILED CLAIM BACK, not for tidying up.
      // The marker is written BEFORE the send, because that is what stops a
      // reload mailing the operator twice while the first request is in flight.
      // A marker that outlives a send which never happened is worse than no
      // marker: the address is locked out AND the next press is told an operator
      // was already emailed.
      //
      // MEASURED 2026-09-08, minutes after this policy was first applied: that
      // was the ONLY reachable path, because the operator's address is not yet a
      // verified SES identity and every send therefore failed. The first person
      // to press the button would have got 502 and the second a 202 about an
      // email nobody sent.
      //
      // Scoped to the same prefix as the write and no wider. Verified with
      // simulate-principal-policy after the first apply: PutObject on the
      // results prefix and on the bucket root are both implicitDeny, and this
      // adds nothing outside ddpsrun-register/.
      "s3:DeleteObject",
    ]
    resources = ["arn:aws:s3:::${var.result_bucket}/ddpsrun-register/*"]
  }
}

resource "aws_iam_role_policy" "register_notify" {
  count  = var.register_notify_to != "" ? 1 : 0
  name   = "${var.name}-register-notify"
  role   = aws_iam_role.gw.id
  policy = data.aws_iam_policy_document.register_notify.json
}

// ------------------------------------------------------------- cluster access

// WHY A GROUP AND NOT A USERNAME. Measured 2026-09-01: the access entry reports
// its username as `assumed-role/<role>/{{SessionName}}`, and the session name
// differs per invocation, so a RoleBinding naming the username matches nothing.
//
// AND WHY THIS CAN FAIL ON A FIRST APPLY. EKS cannot see an IAM role the instant
// it is created; the same measurement needed three tries over about ten seconds.
// depends_on gives terraform the ordering, and a re-apply covers the rest.
resource "aws_eks_access_entry" "gw" {
  cluster_name      = var.cluster_name
  principal_arn     = aws_iam_role.gw.arn
  kubernetes_groups = [var.kubernetes_group]
  type              = "STANDARD"
  tags              = var.tags

  depends_on = [aws_iam_role.gw]
}

// ------------------------------------------------------------------ the secret

resource "aws_secretsmanager_secret" "tokens" {
  name        = "${var.name}/tokens"
  description = "ddpsrun user tokens: sha256 hashes, never the tokens themselves."
  tags        = var.tags

  // Long enough to undo a mistake, short enough that a rotated list stops being
  // recoverable fairly soon.
  recovery_window_in_days = 7
}

// The VALUE is deliberately not managed here. Putting it in terraform would put
// every token hash in the state file, which is the one place they must not be.
// An operator writes it once with:
//   aws secretsmanager put-secret-value --secret-id <name> --secret-string file://tokens.json

// ------------------------------------------------------------------- the logs

resource "aws_cloudwatch_log_group" "gw" {
  // Lambda writes here whether or not the group exists; creating it explicitly is
  // the only way to bound retention, and an unbounded group bills forever.
  name              = "/aws/lambda/${var.name}"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

// ---------------------------------------------------------------------------
// REMOVED 2026-09-16: aws_lambda_function.gw and aws_lambda_function_url.gw.
//
// Both were destroyed in AWS on 2026-09-15 with a targeted destroy that named
// exactly those two addresses, so `aws_secretsmanager_secret.tokens` -- which
// holds every token hash and cannot be rebuilt from this repository -- was never
// in the plan. The state has not listed them since. Leaving the BLOCKS behind
// meant the next unqualified `terraform apply` would create the function again,
// with the placeholder zip that answers 503, and publish a second address for an
// API that already has one.
//
// The variables they used (memory_mb, timeout_seconds, cors_allow_origins, and
// the four cognito_*) are still declared in variables.tf and still set in
// terraform.tfvars. Deleting a variable while a tfvars file still assigns it is
// an error, and the tfvars file is not in git -- so they stay until somebody
// clears both in the same change. The pod reads its own settings from
// config/deploy/hyperun-gw.yaml, not from here.
// ---------------------------------------------------------------------------

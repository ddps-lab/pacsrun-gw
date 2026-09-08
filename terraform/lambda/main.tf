// The gateway function, its identity, and the two things it reads at cold start.
//
// END-TO-END FLOW of one request, and what this module has to exist for:
//
//   1. A user's CLI or browser calls the Function URL over HTTPS with a ddpsrun
//      token. `aws_lambda_function_url.gw` is what gives the function an address
//      at all; without it a Lambda has no way to be called from outside AWS.
//   2. The function reads the token list from Secrets Manager. On a pod this was
//      a mounted file; Lambda has no mounts, so it is a call, cached for the life
//      of the execution environment.
//   3. It calls eks:DescribeCluster to learn the apiserver endpoint and CA. On a
//      pod both were files under /var/run/secrets; here neither exists.
//   4. It signs an EKS token with its own execution role and calls the apiserver.
//      `aws_eks_access_entry.gw` is what makes that role a principal the cluster
//      recognises, and `kubernetes_groups` is what carries it into RBAC.
//
// WHAT THIS MODULE DELIBERATELY DOES NOT DO. It does not create the
// ClusterRoleBinding that gives `var.kubernetes_group` its permissions. That is a
// Kubernetes object in `config/deploy/rbac.yaml`, and creating it here would make
// every plan depend on the cluster being reachable. The outputs print the command.
//
// COST, all of it. The function is free at this scale: 1M requests and 400,000
// GB-seconds a month are free, and 10,000 requests at 512 MB for 200 ms is 1,000
// GB-seconds. A Function URL costs nothing. Secrets Manager is $0.40 per secret
// per month plus $0.05 per 10,000 API calls. CloudWatch logs are $0.50 per GB
// ingested. What this does NOT cover is the EKS control plane ($73/month) or the
// node the operator runs on, neither of which this module touches.
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
    sid       = "WriteRegistrationMarkersOnly"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
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

// --------------------------------------------------------------- the function

resource "aws_lambda_function" "gw" {
  function_name = var.name
  role          = aws_iam_role.gw.arn
  runtime       = "python3.12"
  handler       = "ddpsrun_server.lambda_handler.handler"
  memory_size   = var.memory_mb
  timeout       = var.timeout_seconds
  tags          = var.tags

  // The package is built and uploaded by CI, not by terraform. Measured
  // 2026-09-01 the dependencies are 92.4 MB unzipped against Lambda's 250 MB
  // limit, so this is a zip and not a container image.
  //
  // terraform creates the function with a placeholder on the very first apply and
  // ignores the code afterwards: otherwise every plan would want to revert
  // whatever CI last published.
  filename         = "${path.module}/placeholder.zip"
  source_code_hash = filebase64sha256("${path.module}/placeholder.zip")

  // ★★★ READ BEFORE APPLYING THIS RESOURCE. `environment.variables` is a whole
  // map, so terraform reconciles EVERY key in it -- including any value that was
  // set by hand outside terraform, which it removes without singling it out.
  //
  // MEASURED 2026-09-08: an `apply -target=aws_lambda_function.gw` intended to
  // change one variable also emptied all four DDPSRUN_COGNITO_* values, because
  // they had been set by hand and terraform.tfvars still had them blank. Sign-in
  // broke. The plan DID say so -- four `~ "..." -> ""` lines -- and the mistake
  // was reading `Plan: 1 to change` as "one thing changes": that counts
  // RESOURCES, not attributes.
  //
  // So before any apply that touches this resource: check that every variable
  // below has its real value in terraform.tfvars, and read the plan's `~` lines
  // one by one rather than its summary count.
  environment {
    variables = {
      DDPSRUN_RESULT_BUCKET    = var.result_bucket
      DDPSRUN_RESULT_PREFIX    = var.result_prefix
      DDPSRUN_SERVICE_ACCOUNT  = var.service_account
      DDPSRUN_TOKENS_SECRET_ID = aws_secretsmanager_secret.tokens.name
      DDPSRUN_CLUSTER_NAME     = var.cluster_name
      DDPSRUN_SECRET_BINDINGS  = jsonencode(var.secret_bindings)

      // DDPSRUN-COGNITO-WIRING. All four empty means the server accepts static
      // tokens only, which is what it did before Cognito existed and what a
      // local run still does. They are set together or not at all: a pool id
      // with no client id would refuse every token rather than accept a wrong
      // one, but it would also be a half-configured deployment nobody meant.
      DDPSRUN_COGNITO_POOL_ID      = var.cognito_pool_id
      DDPSRUN_COGNITO_CLIENT_ID    = var.cognito_client_id
      DDPSRUN_COGNITO_REGION       = var.cognito_pool_id == "" ? "" : var.region
      DDPSRUN_COGNITO_LOGIN_DOMAIN = var.cognito_login_domain

      // DDPSRUN-REGISTER. Empty means the server reports
      // `registration_requests: false`, the screen draws no button, and no IAM
      // permission exists for it either (the policy has a count).
      DDPSRUN_REGISTER_NOTIFY_TO   = var.register_notify_to
      DDPSRUN_REGISTER_NOTIFY_FROM = var.register_notify_from != "" ? var.register_notify_from : var.register_notify_to
    }
  }

  lifecycle {
    ignore_changes = [filename, source_code_hash, layers]
  }

  depends_on = [aws_cloudwatch_log_group.gw]
}

// WHAT GIVES THE FUNCTION AN ADDRESS. Without this a Lambda can only be invoked
// through the AWS API, which needs AWS credentials — the exact thing our users do
// not have. This is what replaces the ALB, the Gateway API objects, the load
// balancer controller, the ACM certificate and the Route53 record.
resource "aws_lambda_function_url" "gw" {
  function_name = aws_lambda_function.gw.function_name

  // The function checks the ddpsrun token itself. AWS_IAM here would require the
  // caller to hold AWS credentials, which is the problem this whole service
  // exists to remove.
  authorization_type = "NONE"

  // CORS IS DECLARED ONLY WHEN THERE IS AN ORIGIN TO ALLOW. Lambda refuses a cors
  // block with an empty allow_origins:
  //
  //   InvalidParameterValueException: You can't leave AllowOrigins as empty when
  //   Cors is enabled.
  //
  // Which is the right refusal — "CORS enabled, nobody allowed" is a contradiction
  // rather than a safe default. Until the screen exists there is no browser origin,
  // and the CLI is not a browser and never sends an Origin header, so it is
  // unaffected either way.
  dynamic "cors" {
    for_each = length(var.cors_allow_origins) > 0 ? [1] : []
    content {
      allow_origins     = var.cors_allow_origins
      allow_methods     = ["GET", "POST"]
      allow_headers     = ["authorization", "content-type"]
      max_age           = 3600
      allow_credentials = false
    }
  }
}

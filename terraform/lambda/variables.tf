// Inputs for the gateway function.
//
// None of these is a credential. The user tokens live in Secrets Manager and are
// referenced by name; the secret's VALUE never appears in terraform state.

variable "region" {
  description = "Must be the cluster's region: the function calls kube-apiserver and nothing else."
  type        = string
  default     = "us-west-2"
}

variable "name" {
  description = "Function name, and the prefix for the role and the secret."
  type        = string
  default     = "ddpsrun-gw"
}

variable "cluster_name" {
  description = <<-EOT
    The EKS cluster the function submits to. Used for two things: the access entry
    that lets this function's role authenticate, and `eks:DescribeCluster`, which
    is how the function learns the endpoint and CA certificate at cold start —
    there is no kubeconfig on Lambda.
  EOT
  type        = string
  default     = "pacsrun"
}

variable "kubernetes_group" {
  description = <<-EOT
    The Kubernetes group the access entry maps this function's role into.

    BOUND TO A GROUP AND NOT TO A USERNAME, and that is not a style choice.
    Measured 2026-09-01: an access entry for a role reports its username as
    `assumed-role/<role>/{{SessionName}}`, and the session name differs per
    invocation, so a RoleBinding naming the username would match nothing.

    The ClusterRole this group needs is in `config/deploy/rbac.yaml`; terraform
    does not create the binding because it is a Kubernetes object and creating it
    here would make every plan depend on the cluster being reachable.
  EOT
  type        = string
  default     = "ddpsrun-gw"
}

variable "result_bucket" {
  description = "S3 bucket every job writes to. The function builds each job's resultPath from it."
  type        = string
}

variable "result_prefix" {
  description = <<-EOT
    Key prefix inside that bucket. MUST match the operator's
    PACSRUN_RESULT_PREFIX_TEMPLATE: the controller checks the same prefix a second
    time on the cluster side, and if the two disagree every job is refused at
    admission.
  EOT
  type        = string
  default     = "pacsrun/"
}

variable "service_account" {
  description = <<-EOT
    The ServiceAccount every job's DRIVER pod runs as. It must be the one PACSrun's own
    terraform wired to the EC2/STS role, because that role's trust policy names exactly one
    namespace/ServiceAccount pair -- PACSrun's config/deploy/README.md step 3: "role의 trust
    policy가 그 namespace/ServiceAccount 조합 하나만 신뢰하므로, 다른 SA로 돌리면 STS가
    거절한다".

    IT WAS "pacsrun-workload" UNTIL 2026-09-08 AND THAT IS AN IAM ROLE'S NAME, NOT THIS
    CLUSTER'S ServiceAccount. The EKS Pod Identity association is
    `default/pacsjob-writer -> role/pacsrun-workload`, so naming the role here produced a
    driver pod with no usable identity: every AWS job died in its own configuration check with

        configuration error: PACSRUN_AWS_ZONE is unusable: ... AccessDenied ... Not authorized
        to perform sts:AssumeRoleWithWebIdentity

    exit 10, terminal, before a machine was rented. Measured 2026-09-08 on job
    ddpsrun-24547306294e submitted from the New job screen; the same request with
    `pacsjob-writer` reached Running and rented a gr6.4xlarge.
  EOT
  type        = string
  default     = "pacsjob-writer"
}

variable "secret_bindings" {
  description = <<-EOT
    Which stored secrets a user may ask for by name, and where each one really is.
    A name absent here cannot be requested at all.

    These are NAMES, not values. The function writes a secretKeyRef into the
    PacsJob and kubelet does the reading, so neither this module nor the function
    ever holds the secret itself.

    Example:
      { "GITHUB_PAT" = { name = "slm-rca-clone", key = "token" } }
  EOT
  type = map(object({
    name = string
    key  = string
  }))
  default = {}
}

variable "memory_mb" {
  description = <<-EOT
    Lambda memory. CPU is allocated in proportion, but measured 2026-09-01 the
    cold start does not improve with it — 512 MB gave 4138/4136/4044 ms and
    1024 MB gave 1587/1554 ms on a smaller package, so the time is spent reading
    the deployment package rather than computing. 512 is enough.
  EOT
  type        = number
  default     = 512
}

variable "timeout_seconds" {
  description = <<-EOT
    Per-invocation ceiling. Every route answers in well under a second; the log
    route returns one window rather than streaming precisely so that nothing here
    ever approaches Lambda's own 15-minute cap.
  EOT
  type        = number
  default     = 30
}

variable "cors_allow_origins" {
  description = <<-EOT
    Which origins the browser may call this function from.

    THIS EXISTS BECAUSE THE PAGE AND THE API ARE DIFFERENT ORIGINS. The screen is
    static files on CloudFront and the API is this Function URL, so without these
    headers the browser refuses every request the page makes. Empty means no
    browser may call it, which is correct until the screen exists.
  EOT
  type        = list(string)
  default     = []
}

variable "log_retention_days" {
  description = "How long the function's own CloudWatch logs are kept. Unset means forever, which bills forever."
  type        = number
  default     = 14
}

variable "tags" {
  description = "Tags applied to every object this module creates."
  type        = map(string)
  default = {
    Project   = "pacsrun-gw"
    ManagedBy = "terraform"
  }
}

// DDPSRUN-COGNITO-WIRING. Outputs of `terraform/cognito`. Kept as variables
// rather than a data source or a remote state read so that the two stacks stay
// independent: the lambda can be applied when no user pool exists at all, which
// is what every deployment before 2026-09-01 did and what a fresh clone does.
//
// None of these is a credential. The client id and the login domain appear in
// every login URL a browser shows, and the pool id is in the issuer of every
// token this service hands out.

variable "cognito_pool_id" {
  description = "From `terraform -chdir=../cognito output -raw user_pool_id`. Empty disables the Cognito branch entirely."
  type        = string
  default     = ""
}

variable "cognito_client_id" {
  description = "From `terraform -chdir=../cognito output -raw client_id`. An id_token addressed to any other client is refused."
  type        = string
  default     = ""
}

variable "cognito_login_domain" {
  description = "From `terraform -chdir=../cognito output -raw login_domain`. The server never calls it; it hands the address to the screen and the CLI."
  type        = string
  default     = ""
}


// DDPSRUN-REGISTER. Where "somebody signed in and has no namespace" mail goes.
//
// EMPTY IS A SUPPORTED STATE, not a half-configured one. The server then reports
// `registration_requests: false` on /v1/login-config, the screen draws no button
// and tells the person to contact an operator directly, and no IAM permission is
// created at all (the policy above has a count).
//
// ★ ONE MANUAL STEP THIS TERRAFORM CANNOT DO. While the SES account is in the
// sandbox -- it is, measured 2026-09-08 (`ProductionAccessEnabled: false`, 200
// messages a day, 1 a second, and ZERO verified identities) -- SES refuses to
// send FROM or TO an address that has not been verified, and verification means
// the owner of the inbox clicking a link AWS emails them. So after apply:
//
//   aws sesv2 create-email-identity --region <region> --email-identity <address>
//   # AWS emails that address a confirmation link; the owner clicks it
//   aws sesv2 get-email-identity --region <region> --email-identity <address> \
//     --query VerifiedForSendingStatus
//
// Terraform cannot click the link, so `aws_ses_email_identity` would apply
// cleanly and leave the button failing at runtime with "Email address not
// verified" -- which is why the identity is NOT declared here. It is a person's
// inbox, and consenting to receive from it is theirs to give.
variable "register_notify_to" {
  description = "Operator address that registration requests are emailed to. Empty turns the feature off."
  type        = string
  default     = ""
}

// Defaults to `register_notify_to` in the server (config.py), and the same
// default is what the IAM resource ARN above falls back to. Setting them equal
// is the cheapest working configuration: in the sandbox both ends have to be
// verified, so one verification click covers both.
variable "register_notify_from" {
  description = "From address on registration emails. Empty means use register_notify_to."
  type        = string
  default     = ""
}

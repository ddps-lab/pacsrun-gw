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
  description = "The ServiceAccount every job's pods run as."
  type        = string
  default     = "pacsrun-workload"
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

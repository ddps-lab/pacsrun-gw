// Inputs for the gateway's registry module.
//
// NONE OF THESE IS A CREDENTIAL. They are identifiers that appear in the trust policy AS
// RESTRICTIONS, so knowing them grants nothing. They are still kept in a gitignored
// terraform.tfvars rather than defaulted here, so that one clone is never mistaken for
// another environment.

variable "region" {
  description = <<-EOT
    The region the ECR repository lives in. This must be the CLUSTER's region: the server pod
    pulls the image and nothing else does, so putting the repository anywhere else buys a
    cross-region transfer charge and no benefit.
  EOT
  type        = string
  default     = "us-west-2"
}

variable "name_prefix" {
  description = "Prefix for the IAM role's name. Keeps this module's objects greppable."
  type        = string
  default     = "ddpsrun-gw"
}

variable "ecr_repository_name" {
  description = <<-EOT
    Name of the ECR repository the gateway image is pushed to. It goes in the pacsrun-gw
    repository's ECR_REPOSITORY variable verbatim, and a mismatch fails the release workflow at
    the push step with a message naming a repository that does not exist.
  EOT
  type        = string
  default     = "ddpsrun/gateway"
}

variable "ecr_keep_last_images" {
  description = <<-EOT
    How many tagged images to keep. Every push adds one and the tag is the git SHA, so without
    a limit this grows forever. Storage is $0.10 per GB-month; a roughly 0.2 GiB image kept 20
    deep is about $0.40 a month.

    Keep this comfortably above 1: the lifecycle rule counts `latest` like any other image.
  EOT
  type        = number
  default     = 20
}

variable "github_owner" {
  description = "GitHub owner. Half of the `sub` claim the trust policy matches."
  type        = string
  default     = "ddps-lab"
}

variable "github_repository" {
  description = <<-EOT
    GitHub repository name. Together with github_owner this forms the `sub` claim the role's
    trust policy matches, so a typo means every workflow run fails with
    "Not authorized to perform sts:AssumeRoleWithWebIdentity".
  EOT
  type        = string
  default     = "pacsrun-gw"
}

variable "github_org_id" {
  description = <<-EOT
    The organization's numeric id, or "" to skip the id-based subject form.

    ONLY needed when the organization has enabled ID-based OIDC subject claims, i.e. when the
    token's `sub` looks like

      repo:ddps-lab@28432465/pacsrun-gw@1352229254:ref:refs/heads/main

    rather than

      repo:ddps-lab/pacsrun-gw:ref:refs/heads/main

    PACSrun's own role carries both forms, which is evidence this organization emits the id
    form at least sometimes, so both are accepted here too. Accepting an extra form the
    organization never sends costs nothing: the policy matches on either, and neither can be
    forged — GitHub signs the token.
  EOT
  type        = string
  default     = ""
}

variable "github_repository_id" {
  description = "The repository's numeric id. Paired with github_org_id; both or neither."
  type        = string
  default     = ""
}

variable "github_oidc_provider_arn" {
  description = <<-EOT
    ARN of the EXISTING token.actions.githubusercontent.com OIDC provider.

    This module does NOT create one. An AWS account may hold only a single provider per issuer
    URL, and this account already has it — PACSrun's registry module created it. Creating a
    second would fail with EntityAlreadyExists, and importing it into this state would put one
    object under two modules' control.

    Find it with:
      aws iam list-open-id-connect-providers
  EOT
  type        = string
}

variable "tags" {
  description = "Tags applied to every object this module creates."
  type        = map(string)
  default = {
    Project   = "pacsrun-gw"
    ManagedBy = "terraform"
  }
}

variable "lambda_function_name" {
  description = <<-EOT
    The Lambda function CI publishes to, or "" to grant nothing.

    WHY THIS IS HERE AND NOT IN terraform/lambda. The permission belongs to the
    GitHub Actions role, which this module owns; the function belongs to the other
    module. Naming the function by string rather than by reference keeps the two
    modules independent — neither has to be applied before the other, and neither
    holds the other's state.
  EOT
  type        = string
  default     = ""
}


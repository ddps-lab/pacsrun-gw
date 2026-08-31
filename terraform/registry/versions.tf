// Toolchain and provider for the gateway's registry module.
//
// ONE REGION, unlike PACSrun's registry module which declares two. The gateway image is pulled
// by exactly one thing — the server pod in the us-west-2 EKS cluster — so a second copy in
// another region would be paid for and never used. Pulling across regions instead would cost
// $0.02 per GB of inter-region transfer, which on a roughly 200 MB image is $0.004 per pod
// start: small, and for nothing.
terraform {
  required_version = ">= 1.5.7"

  required_providers {
    aws = {
      // Same floor as PACSrun's modules so one provider cache serves all of them.
      version = ">= 6.52"
      source  = "hashicorp/aws"
    }
  }
}

provider "aws" {
  region = var.region
}

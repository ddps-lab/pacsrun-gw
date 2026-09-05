// The gateway function and everything it needs to reach the cluster.
//
// ONE REGION, the cluster's. The function talks to kube-apiserver and to nothing
// that lives anywhere else.
terraform {
  required_version = ">= 1.5.7"
  required_providers {
    aws = {
      version = ">= 6.52"
      source  = "hashicorp/aws"
    }
  }
}

provider "aws" {
  region = var.region
}

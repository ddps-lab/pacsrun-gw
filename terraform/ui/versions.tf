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

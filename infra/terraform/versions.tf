
terraform {
  required_version = ">= 1.10, < 2.0"
  backend "s3" {
  }
  required_providers {
    aws = {
      source  = "hashicorp/aws",
      version = "~> 6.0"
    }
  }
}
provider "aws" {
  region              = "eu-west-1"
  allowed_account_ids = [var.aws_account_id]
  default_tags {
    tags = {
      Project     = "LedgerGuard",
      Environment = var.environment,
      ManagedBy   = "Terraform"
    }
  }
}
data "aws_availability_zones" "available" {
  state = "available"
}
data "aws_caller_identity" "current" {
}
data "aws_partition" "current" {
}

terraform {
  required_version = ">= 1.10, < 2.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}
variable "aws_account_id" { type = string }
variable "environment" { type = string }
provider "aws" {
  region              = "eu-west-1"
  allowed_account_ids = [var.aws_account_id]
}
locals { name = "ledgerguard-${var.environment}" }
resource "aws_kms_key" "state" {
  description             = "${local.name} Terraform state"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}
resource "aws_s3_bucket" "state" {
  bucket        = "${local.name}-${var.aws_account_id}-tfstate"
  force_destroy = false
  lifecycle { prevent_destroy = true }
}
resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.state.arn
    }
  }
}
resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Deny", Principal = "*", Action = "s3:*", Resource = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"], Condition = { Bool = { "aws:SecureTransport" = "false" } } }] })
}
resource "aws_secretsmanager_secret" "runtime" {
  for_each                = toset(["application", "migrator", "maintenance"])
  name                    = "${local.name}/${each.key}"
  recovery_window_in_days = 30
}
resource "aws_sns_topic" "operations" {
  name              = "${local.name}-operations"
  kms_master_key_id = aws_kms_key.operations.arn
}
output "state_bucket" { value = aws_s3_bucket.state.id }
output "state_key_arn" { value = aws_kms_key.state.arn }
output "secret_arns" { value = { for k, v in aws_secretsmanager_secret.runtime : k => v.arn } }
output "operations_topic_arn" { value = aws_sns_topic.operations.arn }

resource "aws_kms_key" "operations" {
  description             = "${local.name} operations notifications"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Principal = { AWS = "arn:aws:iam::${var.aws_account_id}:root" }, Action = "kms:*", Resource = "*" },
    { Effect = "Allow", Principal = { Service = "cloudwatch.amazonaws.com" }, Action = ["kms:Decrypt", "kms:GenerateDataKey*"], Resource = "*", Condition = { StringEquals = { "aws:SourceAccount" = var.aws_account_id } } }
  ] })
}
resource "aws_sns_topic_policy" "operations" {
  arn = aws_sns_topic.operations.arn
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Principal = { AWS = "arn:aws:iam::${var.aws_account_id}:root" }, Action = "sns:*", Resource = aws_sns_topic.operations.arn },
    { Effect = "Allow", Principal = { Service = "cloudwatch.amazonaws.com" }, Action = "sns:Publish", Resource = aws_sns_topic.operations.arn, Condition = { StringEquals = { "aws:SourceAccount" = var.aws_account_id }, ArnLike = { "aws:SourceArn" = "arn:aws:cloudwatch:eu-west-1:${var.aws_account_id}:alarm:ledgerguard-${var.environment}-*" } } }
  ] })
}

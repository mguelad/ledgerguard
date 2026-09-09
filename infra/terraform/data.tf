
locals {
  name          = "ledgerguard-${var.environment}"
  queue_classes = toset(["ingestion", "reconciliation", "notification", "reporting", "repair"])
}
resource "aws_kms_key" "data" {
  description             = "${local.name} data encryption"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}
resource "aws_kms_key_policy" "data" {
  key_id = aws_kms_key.data.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Sid = "AccountDelegation", Effect = "Allow", Principal = { AWS = "arn:aws:iam::${var.aws_account_id}:root" }, Action = "kms:*", Resource = "*" },
    { Sid = "ApplicationLogs", Effect = "Allow", Principal = { Service = "logs.eu-west-1.amazonaws.com" }, Action = ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:DescribeKey"], Resource = "*", Condition = { ArnLike = { "kms:EncryptionContext:aws:logs:arn" = ["arn:aws:logs:eu-west-1:${var.aws_account_id}:log-group:/ecs/${local.name}", "arn:aws:logs:eu-west-1:${var.aws_account_id}:log-group:/ledgerguard/${local.name}/*"] } } },
    { Sid = "EmailFeedback", Effect = "Allow", Principal = { Service = "ses.amazonaws.com" }, Action = ["kms:GenerateDataKey", "kms:Decrypt"], Resource = "*", Condition = { StringEquals = { "aws:SourceAccount" = var.aws_account_id } } },
    { Sid = "Trail", Effect = "Allow", Principal = { Service = "cloudtrail.amazonaws.com" }, Action = ["kms:GenerateDataKey*", "kms:DescribeKey"], Resource = "*", Condition = { StringEquals = { "aws:SourceArn" = "arn:aws:cloudtrail:eu-west-1:${var.aws_account_id}:trail/${local.name}" } } }
  ] })
}
resource "aws_kms_alias" "data" {
  name          = "alias/${local.name}"
  target_key_id = aws_kms_key.data.key_id
}
resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id
}
resource "aws_db_parameter_group" "main" {
  name_prefix = "${local.name}-"
  family      = "postgres17"
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  parameter {
    name  = "log_statement"
    value = "none"
  }
  parameter {
    name  = "log_min_error_statement"
    value = "panic"
  }
}
resource "aws_db_instance" "main" {
  identifier                      = local.name
  engine                          = "postgres"
  engine_version                  = "17"
  auto_minor_version_upgrade      = true
  instance_class                  = var.db_instance_class
  allocated_storage               = var.db_storage_gib
  max_allocated_storage           = 200
  storage_type                    = "gp3"
  storage_encrypted               = true
  kms_key_id                      = aws_kms_key.data.arn
  db_name                         = "ledgerguard"
  username                        = "ledgerguard_owner"
  manage_master_user_password     = true
  master_user_secret_kms_key_id   = aws_kms_key.data.arn
  db_subnet_group_name            = aws_db_subnet_group.main.name
  vpc_security_group_ids          = [aws_security_group.database.id]
  parameter_group_name            = aws_db_parameter_group.main.name
  publicly_accessible             = false
  multi_az                        = var.enable_multi_az
  backup_retention_period         = 35
  backup_window                   = "02:00-03:00"
  maintenance_window              = "sun:03:30-sun:04:30"
  deletion_protection             = var.deletion_protection
  skip_final_snapshot             = false
  final_snapshot_identifier       = "${local.name}-final"
  copy_tags_to_snapshot           = true
  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]
  performance_insights_enabled    = true
  performance_insights_kms_key_id = aws_kms_key.data.arn
  lifecycle {
    precondition {
      condition     = !var.ga_release || var.enable_multi_az
      error_message = "GA requires Multi-AZ."
    }
  }
}
resource "aws_sqs_queue" "dead" {
  for_each                  = local.queue_classes
  name                      = "${local.name}-${each.key}-dlq"
  message_retention_seconds = 1209600
  kms_master_key_id         = aws_kms_key.data.arn
}
resource "aws_sqs_queue" "work" {
  for_each                   = local.queue_classes
  name                       = "${local.name}-${each.key}"
  visibility_timeout_seconds = 900
  receive_wait_time_seconds  = 10
  message_retention_seconds  = 345600
  kms_master_key_id          = aws_kms_key.data.arn
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dead[each.key].arn,
    maxReceiveCount     = 8
    }
  )
}
resource "aws_sqs_queue_redrive_allow_policy" "dead" {
  for_each  = local.queue_classes
  queue_url = aws_sqs_queue.dead[each.key].id
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue",
    sourceQueueArns   = concat([aws_sqs_queue.work[each.key].arn], each.key == "notification" ? [aws_sqs_queue.email_feedback.arn] : [])
    }
  )
}
resource "aws_s3_bucket" "reports" {
  bucket_prefix = "${local.name}-reports-"
  force_destroy = false
}
resource "aws_s3_bucket_public_access_block" "reports" {
  bucket                  = aws_s3_bucket.reports.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_server_side_encryption_configuration" "reports" {
  bucket = aws_s3_bucket.reports.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.data.arn
    }
    bucket_key_enabled = true
  }
}
resource "aws_s3_bucket_versioning" "reports" {
  bucket = aws_s3_bucket.reports.id
  versioning_configuration {
    status = "Enabled"
  }
}
resource "aws_s3_bucket_lifecycle_configuration" "reports" {
  bucket = aws_s3_bucket.reports.id
  rule {
    id     = "reports-one-day"
    status = "Enabled"
    filter {
      prefix = "reports/"
    }
    expiration {
      days = 1
    }
    noncurrent_version_expiration {
      noncurrent_days = 1
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
  rule {
    id     = "imports-one-day"
    status = "Enabled"
    filter {
      prefix = "imports/"
    }
    expiration {
      days = 1
    }
    noncurrent_version_expiration {
      noncurrent_days = 1
    }
  }
}
resource "aws_s3_bucket_policy" "reports" {
  bucket = aws_s3_bucket.reports.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect    = "Deny",
      Principal = "*",
      Action    = "s3:*",
      Resource  = [aws_s3_bucket.reports.arn, "${aws_s3_bucket.reports.arn}/*"],
      Condition = {
        Bool = {
          "aws:SecureTransport" = "false"
        }
      }
      }
    ]
    }
  )
}

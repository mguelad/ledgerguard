resource "aws_cognito_user_pool" "main" {
  name              = local.name
  mfa_configuration = "ON"
  software_token_mfa_configuration {
    enabled = true
  }
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  deletion_protection      = "ACTIVE"
  password_policy {
    minimum_length                   = 14
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 1
  }
  account_recovery_setting {
    recovery_mechanism {
      name     = "admin_only"
      priority = 1
    }
  }
  user_pool_add_ons {
    advanced_security_mode = "ENFORCED"
  }
}
resource "aws_cognito_user_pool_domain" "main" {
  domain       = "${local.name}-${var.aws_account_id}"
  user_pool_id = aws_cognito_user_pool.main.id
}
resource "aws_cognito_user_pool_client" "web" {
  name                                 = "${local.name}-web"
  user_pool_id                         = aws_cognito_user_pool.main.id
  generate_secret                      = true
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = ["https://${var.domain_name}/auth/callback"]
  logout_urls                          = ["https://${var.domain_name}/login/"]
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true
  access_token_validity                = 15
  id_token_validity                    = 15
  refresh_token_validity               = 1
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
}
resource "aws_ses_domain_identity" "mail" {
  domain = var.alerts_email_domain
}
resource "aws_ses_domain_dkim" "mail" {
  domain = aws_ses_domain_identity.mail.domain
}
resource "aws_route53_record" "ses_verification" {
  zone_id = var.hosted_zone_id
  name    = "_amazonses.${var.alerts_email_domain}"
  type    = "TXT"
  ttl     = 300
  records = [aws_ses_domain_identity.mail.verification_token]
}
resource "aws_route53_record" "ses_dkim" {
  count   = 3
  zone_id = var.hosted_zone_id
  name    = "${aws_ses_domain_dkim.mail.dkim_tokens[count.index]}._domainkey.${var.alerts_email_domain}"
  type    = "CNAME"
  ttl     = 300
  records = ["${aws_ses_domain_dkim.mail.dkim_tokens[count.index]}.dkim.amazonses.com"]
}
resource "aws_ses_configuration_set" "main" {
  name = local.name
}
resource "aws_sns_topic" "email_feedback" {
  name              = "${local.name}-email-feedback"
  kms_master_key_id = aws_kms_key.data.arn
}
resource "aws_sqs_queue" "email_feedback" {
  name                       = "${local.name}-email-feedback"
  sqs_managed_sse_enabled    = true
  message_retention_seconds  = 345600
  visibility_timeout_seconds = 60
  redrive_policy             = jsonencode({ deadLetterTargetArn = aws_sqs_queue.dead["notification"].arn, maxReceiveCount = 8 })
}
resource "aws_sns_topic_policy" "email_feedback" {
  arn = aws_sns_topic.email_feedback.arn
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Principal = { Service = "ses.amazonaws.com" }, Action = "sns:Publish", Resource = aws_sns_topic.email_feedback.arn, Condition = { StringEquals = { "aws:SourceAccount" = var.aws_account_id }, ArnLike = { "aws:SourceArn" = "arn:aws:ses:eu-west-1:${var.aws_account_id}:*" } } }
  ] })
}
resource "aws_sqs_queue_policy" "email_feedback" {
  queue_url = aws_sqs_queue.email_feedback.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect = "Allow",
      Principal = {
        Service = "sns.amazonaws.com"
      },
      Action   = "sqs:SendMessage",
      Resource = aws_sqs_queue.email_feedback.arn,
      Condition = {
        ArnEquals = {
          "aws:SourceArn" = aws_sns_topic.email_feedback.arn
        }
      }
      }
    ]
    }
  )
}
resource "aws_sns_topic_subscription" "email_feedback" {
  topic_arn            = aws_sns_topic.email_feedback.arn
  protocol             = "sqs"
  endpoint             = aws_sqs_queue.email_feedback.arn
  raw_message_delivery = true
}
resource "aws_ses_event_destination" "feedback" {
  name                   = "delivery-feedback"
  configuration_set_name = aws_ses_configuration_set.main.name
  enabled                = true
  matching_types         = ["send", "delivery", "bounce", "complaint", "reject"]
  sns_destination {
    topic_arn = aws_sns_topic.email_feedback.arn
  }
}

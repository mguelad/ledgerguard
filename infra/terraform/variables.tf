variable "environment" {
  type = string
  validation {
    condition     = contains(["dev", "staging", "production"], var.environment)
    error_message = "Use a separate AWS account for each supported environment."
  }
}
variable "aws_account_id" {
  type = string
}
variable "domain_name" {
  type = string
}
variable "hosted_zone_id" {
  type = string
}
variable "image_digest_uri" {
  type = string
  validation {
    condition     = can(regex("@sha256:[a-f0-9]{64}$", var.image_digest_uri))
    error_message = "Deploy an immutable image digest, never a tag."
  }
}
variable "github_repository" {
  type = string
}
variable "github_oidc_provider_arn" {
  type = string
}
variable "application_secret_arn" {
  type        = string
  description = "Secrets Manager JSON containing DJANGO_SECRET_KEY, DATABASE_URL, STRIPE_DEVELOPER_TEST_KEY, STRIPE_DEVELOPER_LIVE_KEY, STRIPE_TEST_CLIENT_ID, STRIPE_LIVE_CLIENT_ID, and COGNITO_CLIENT_SECRET. Populate before enabling services."
}
variable "migrator_database_secret_arn" {
  type = string
}
variable "maintenance_database_secret_arn" {
  type = string
}
variable "desired_api_tasks" {
  type    = number
  default = 0
}
variable "desired_worker_tasks" {
  type    = number
  default = 0
}
variable "db_instance_class" {
  type    = string
  default = "db.t4g.small"
}
variable "db_storage_gib" {
  type    = number
  default = 30
}
variable "enable_multi_az" {
  type    = bool
  default = true
}
variable "alerts_email_domain" {
  type = string
}
variable "operations_topic_arn" {
  type = string
}
variable "vpc_cidr" {
  type    = string
  default = "10.42.0.0/16"
}
variable "deletion_protection" {
  type    = bool
  default = true
}
variable "ga_release" {
  type        = bool
  default     = false
  description = "Set only after the external GA evidence gates have passed."
}

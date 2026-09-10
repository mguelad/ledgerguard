output "application_url" {
  value = "https://${var.domain_name}"
}
output "database_endpoint" {
  value = aws_db_instance.main.address
}
output "database_master_secret_arn" {
  value     = aws_db_instance.main.master_user_secret[0].secret_arn
  sensitive = true
}
output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}
output "cluster_name" {
  value = aws_ecs_cluster.main.name
}
output "private_subnets" {
  value = aws_subnet.private[*].id
}
output "task_security_group" {
  value = aws_security_group.tasks.id
}
output "kms_key_arn" {
  value = aws_kms_key.data.arn
}
output "migrator_task_definition" {
  value = aws_ecs_task_definition.operations["migrator"].arn
}
output "cognito_client_id" {
  value = aws_cognito_user_pool_client.web.id
}
output "cognito_client_secret" {
  value     = aws_cognito_user_pool_client.web.client_secret
  sensitive = true
}
output "queue_urls" {
  value = {
    for key, queue in aws_sqs_queue.work : key => queue.id
  }
}

# Deliberately excludes master credentials and Cognito client secrets. Export
# only this output, never the full Terraform state/output collection, for checks.
output "acceptance_config" {
  value = {
    account_id             = var.aws_account_id
    environment            = var.environment
    region                 = "eu-west-1"
    database_identifier    = aws_db_instance.main.identifier
    cluster_name           = aws_ecs_cluster.main.name
    kms_key_arn            = aws_kms_key.data.arn
    cognito_user_pool_id   = aws_cognito_user_pool.main.id
    report_bucket          = aws_s3_bucket.reports.id
    deletion_ledger_bucket = aws_s3_bucket.audit.id
    email_domain           = var.alerts_email_domain
  }
}

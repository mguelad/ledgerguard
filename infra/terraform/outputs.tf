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

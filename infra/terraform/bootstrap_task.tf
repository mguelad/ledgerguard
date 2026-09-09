resource "aws_iam_role" "bootstrap" {
  name               = "${local.name}-bootstrap"
  assume_role_policy = local.ecs_trust
}
resource "aws_iam_role_policy" "bootstrap" {
  role = aws_iam_role.bootstrap.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret", "secretsmanager:PutSecretValue"], Resource = [var.application_secret_arn, var.migrator_database_secret_arn, var.maintenance_database_secret_arn] },
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_db_instance.main.master_user_secret[0].secret_arn },
    { Effect = "Allow", Action = ["kms:Decrypt"], Resource = aws_kms_key.data.arn },
    { Effect = "Allow", Action = ["cognito-idp:DescribeUserPoolClient"], Resource = aws_cognito_user_pool.main.arn }
  ] })
}
resource "aws_ecs_task_definition" "bootstrap" {
  family                   = "${local.name}-bootstrap"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.bootstrap.arn
  container_definitions = jsonencode([{
    name    = "bootstrap", image = var.image_digest_uri, essential = true,
    user    = "10001:10001", readonlyRootFilesystem = true,
    command = ["python", "scripts/provision_database.py"],
    environment = [
      { name = "DATABASE_MASTER_SECRET_ARN", value = aws_db_instance.main.master_user_secret[0].secret_arn },
      { name = "DATABASE_HOST", value = aws_db_instance.main.address },
      { name = "APPLICATION_SECRET_ARN", value = var.application_secret_arn },
      { name = "MIGRATOR_SECRET_ARN", value = var.migrator_database_secret_arn },
      { name = "MAINTENANCE_SECRET_ARN", value = var.maintenance_database_secret_arn },
      { name = "COGNITO_POOL_ID", value = aws_cognito_user_pool.main.id },
      { name = "COGNITO_CLIENT_ID", value = aws_cognito_user_pool_client.web.id }
    ],
    logConfiguration = local.logs,
    mountPoints      = [{ sourceVolume = "tmp", containerPath = "/tmp", readOnly = false }]
  }])
  volume { name = "tmp" }
}

resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "IMMUTABLE"
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.data.arn
  }
  image_scanning_configuration {
    scan_on_push = true
  }
}
resource "aws_cloudwatch_log_group" "app" {
  depends_on        = [aws_kms_key_policy.data]
  name              = "/ecs/${local.name}"
  retention_in_days = 30
  kms_key_id        = aws_kms_key.data.arn
}
resource "aws_cloudwatch_log_group" "security" {
  depends_on        = [aws_kms_key_policy.data]
  name              = "/ledgerguard/${local.name}/security"
  retention_in_days = 90
  kms_key_id        = aws_kms_key.data.arn
}
resource "aws_ecs_cluster" "main" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}
resource "aws_iam_role" "execution" {
  name               = "${local.name}-execution"
  assume_role_policy = local.ecs_trust
}
locals {
  ecs_trust = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect = "Allow",
      Principal = {
        Service = "ecs-tasks.amazonaws.com"
      },
      Action = "sts:AssumeRole",
      Condition = { StringEquals = { "aws:SourceAccount" = var.aws_account_id } }
      }
    ]
    }
  )
  common_environment = [
    { name = "DELETION_LEDGER_BUCKET", value = aws_s3_bucket.audit.id },
    {
      name  = "LEDGERGUARD_ENV",
      value = var.environment
    },
    {
      name  = "PUBLIC_URL",
      value = "https://${var.domain_name}"
    },
    {
      name  = "ALLOWED_HOSTS",
      value = var.domain_name
    },
    {
      name  = "AWS_REGION",
      value = "eu-west-1"
    },
    {
      name  = "DB_SSLMODE",
      value = "verify-full"
    },
    {
      name  = "KMS_KEY_ID",
      value = aws_kms_key.data.arn
    },
    {
      name  = "REPORT_BUCKET",
      value = aws_s3_bucket.reports.id
    },
    {
      name = "QUEUE_URLS",
      value = jsonencode({
        for key, queue in aws_sqs_queue.work : key => queue.id
        }
      )
    },
    {
      name  = "SES_FEEDBACK_QUEUE_URL",
      value = aws_sqs_queue.email_feedback.id
    },
    {
      name  = "SES_FROM_EMAIL",
      value = "alerts@${var.alerts_email_domain}"
    },
    {
      name  = "SES_CONFIGURATION_SET",
      value = aws_ses_configuration_set.main.name
    },
    {
      name  = "COGNITO_ISSUER",
      value = "https://cognito-idp.eu-west-1.amazonaws.com/${aws_cognito_user_pool.main.id}"
    },
    {
      name  = "COGNITO_DOMAIN",
      value = "https://${aws_cognito_user_pool_domain.main.domain}.auth.eu-west-1.amazoncognito.com"
    },
    {
      name  = "COGNITO_CLIENT_ID",
      value = aws_cognito_user_pool_client.web.id
    },
    {
      name  = "COGNITO_MFA_ENFORCED",
      value = "1"
    }
  ]
  common_secrets = [for key in ["DJANGO_SECRET_KEY", "DATABASE_URL", "STRIPE_DEVELOPER_TEST_KEY", "STRIPE_DEVELOPER_LIVE_KEY", "STRIPE_TEST_CLIENT_ID", "STRIPE_LIVE_CLIENT_ID", "COGNITO_CLIENT_SECRET"] : {
    name      = key,
    valueFrom = "${var.application_secret_arn}:${key}::"
    }
  ]
  logs = {
    logDriver = "awslogs",
    options = {
      awslogs-group         = aws_cloudwatch_log_group.app.name,
      awslogs-region        = "eu-west-1",
      awslogs-stream-prefix = "app"
    }
  }
}
resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}
resource "aws_iam_role_policy" "execution_secrets" {
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect   = "Allow",
        Action   = ["secretsmanager:GetSecretValue"],
        Resource = [var.application_secret_arn, var.migrator_database_secret_arn, var.maintenance_database_secret_arn]
      },
      {
        Effect   = "Allow",
        Action   = ["kms:Decrypt"],
        Resource = [aws_kms_key.data.arn]
      }
    ]
    }
  )
}
resource "aws_iam_role" "api" {
  name               = "${local.name}-api"
  assume_role_policy = local.ecs_trust
}
resource "aws_iam_role" "worker" {
  name               = "${local.name}-worker"
  assume_role_policy = local.ecs_trust
}
resource "aws_iam_role" "migrator" {
  name               = "${local.name}-migrator"
  assume_role_policy = local.ecs_trust
}
resource "aws_iam_role" "maintenance" {
  name               = "${local.name}-maintenance"
  assume_role_policy = local.ecs_trust
}
resource "aws_iam_role_policy" "api" {
  role = aws_iam_role.api.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect   = "Allow",
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"],
        Resource = [aws_kms_key.data.arn]
      },
      {
        Effect   = "Allow",
        Action   = ["s3:GetObject"],
        Resource = ["${aws_s3_bucket.reports.arn}/reports/*"]
      }
    ]
    }
  )
}
resource "aws_iam_role_policy" "worker" {
  role = aws_iam_role.worker.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect   = "Allow",
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"],
        Resource = [aws_kms_key.data.arn]
      },
      {
        Effect   = "Allow",
        Action   = ["sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"],
        Resource = concat([for q in aws_sqs_queue.work : q.arn], [aws_sqs_queue.email_feedback.arn])
      },
      {
        Effect   = "Allow",
        Action   = ["s3:PutObject", "s3:GetObject"],
        Resource = ["${aws_s3_bucket.reports.arn}/reports/*"]
      },
      {
        Effect   = "Allow",
        Action   = ["ses:SendEmail"],
        Resource = [aws_ses_domain_identity.mail.arn, "arn:aws:ses:eu-west-1:${var.aws_account_id}:configuration-set/${aws_ses_configuration_set.main.name}"]
      }
    ]
    }
  )
}
resource "aws_iam_role_policy" "maintenance" {
  role = aws_iam_role.maintenance.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      { Effect = "Allow", Action = ["s3:PutObject"], Resource = ["${aws_s3_bucket.audit.arn}/erasures/*"] },
      {
        Effect   = "Allow",
        Action   = ["s3:ListBucket", "s3:ListBucketVersions"],
        Resource = [aws_s3_bucket.reports.arn]
      },
      {
        Effect   = "Allow",
        Action   = ["s3:DeleteObject", "s3:DeleteObjectVersion"],
        Resource = ["${aws_s3_bucket.reports.arn}/*"]
      },
      {
        Effect   = "Allow",
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"],
        Resource = [aws_kms_key.data.arn]
      }
    ]
    }
  )
}
resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.api.arn
  container_definitions = jsonencode([{
    name                   = "app",
    image                  = var.image_digest_uri,
    essential              = true,
    user                   = "10001:10001",
    readonlyRootFilesystem = true,
    command                = ["gunicorn", "apps.control_plane.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "60", "--worker-tmp-dir", "/tmp"],
    portMappings = [{
      containerPort = 8000,
      protocol      = "tcp"
      }
    ],
    environment = concat(local.common_environment, [{
      name  = "LEDGERGUARD_DATABASE_ROLE",
      value = "application"
    }]),
    secrets          = local.common_secrets,
    logConfiguration = local.logs,
    linuxParameters = {
      initProcessEnabled = true
    },
    mountPoints = [{
      sourceVolume  = "tmp",
      containerPath = "/tmp",
      readOnly      = false
      }
    ]
    }
  ])
  volume {
    name = "tmp"
  }
}
resource "aws_ecs_task_definition" "worker" {
  for_each                 = local.queue_classes
  family                   = "${local.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "2048"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.worker.arn
  container_definitions = jsonencode([{
    name                   = "worker",
    image                  = var.image_digest_uri,
    essential              = true,
    user                   = "10001:10001",
    readonlyRootFilesystem = true,
    stopTimeout            = 120,
    command                = ["python", "manage.py", "worker"],
    environment = concat(local.common_environment, [
      {
        name  = "LEDGERGUARD_DATABASE_ROLE",
        value = "application"
      },
      {
        name  = "WORKER_QUEUE_CLASS",
        value = each.key
      }
    ]),
    secrets          = local.common_secrets,
    logConfiguration = local.logs,
    linuxParameters = {
      initProcessEnabled = true
    },
    mountPoints = [{
      sourceVolume  = "tmp",
      containerPath = "/tmp",
      readOnly      = false
      }
    ]
    }
  ])
  volume {
    name = "tmp"
  }
}
resource "aws_ecs_task_definition" "operations" {
  for_each                 = toset(["migrator", "scheduler", "maintenance"])
  family                   = "${local.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = each.key == "migrator" ? aws_iam_role.migrator.arn : each.key == "maintenance" ? aws_iam_role.maintenance.arn : aws_iam_role.worker.arn
  container_definitions = jsonencode([{
    name                   = "operation",
    image                  = var.image_digest_uri,
    essential              = true,
    user                   = "10001:10001",
    readonlyRootFilesystem = true,
    command                = each.key == "migrator" ? ["python", "manage.py", "migrate", "--noinput"] : each.key == "maintenance" ? ["python", "manage.py", "maintain"] : ["python", "manage.py", "schedule"],
    environment = concat(local.common_environment, [{
      name  = "LEDGERGUARD_DATABASE_ROLE",
      value = each.key == "scheduler" ? "application" : each.key
    }]),
    secrets = concat([for secret in local.common_secrets : secret if secret.name != "DATABASE_URL"], [{
      name      = "DATABASE_URL",
      valueFrom = each.key == "migrator" ? var.migrator_database_secret_arn : each.key == "maintenance" ? var.maintenance_database_secret_arn : "${var.application_secret_arn}:DATABASE_URL::"
      }
    ]),
    logConfiguration = local.logs,
    mountPoints = [{
      sourceVolume  = "tmp",
      containerPath = "/tmp",
      readOnly      = false
      }
    ]
    }
  ])
  volume {
    name = "tmp"
  }
}
resource "aws_ecs_service" "api" {
  name                   = "${local.name}-api"
  cluster                = aws_ecs_cluster.main.id
  task_definition        = aws_ecs_task_definition.api.arn
  desired_count          = var.desired_api_tasks
  launch_type            = "FARGATE"
  enable_execute_command = false
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "app"
    container_port   = 8000
  }
  depends_on = [aws_lb_listener.https]
  lifecycle {
    precondition {
      condition     = !var.ga_release || var.desired_api_tasks >= 2
      error_message = "GA requires at least two API tasks."
    }
  }
}
resource "aws_ecs_service" "worker" {
  for_each               = local.queue_classes
  name                   = "${local.name}-${each.key}"
  cluster                = aws_ecs_cluster.main.id
  task_definition        = aws_ecs_task_definition.worker[each.key].arn
  desired_count          = var.desired_worker_tasks
  launch_type            = "FARGATE"
  enable_execute_command = false
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }
}
resource "aws_iam_role" "scheduler" {
  name = "${local.name}-schedule"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect = "Allow",
      Principal = {
        Service = "scheduler.amazonaws.com"
      },
      Action = "sts:AssumeRole",
      Condition = { StringEquals = { "aws:SourceAccount" = var.aws_account_id } }
      }
    ]
    }
  )
}
resource "aws_iam_role_policy" "scheduler" {
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{ Effect = "Allow", Action = ["sqs:SendMessage"], Resource = aws_sqs_queue.dead["ingestion"].arn }, { Effect = "Allow", Action = ["kms:GenerateDataKey", "kms:Decrypt"], Resource = aws_kms_key.data.arn }, {
      Effect   = "Allow",
      Action   = ["ecs:RunTask"],
      Resource = [for name in ["scheduler", "maintenance"] : "arn:aws:ecs:eu-west-1:${var.aws_account_id}:task-definition/${local.name}-${name}:*"]
      }, {
      Effect   = "Allow",
      Action   = ["iam:PassRole"],
      Resource = [aws_iam_role.execution.arn, aws_iam_role.worker.arn, aws_iam_role.maintenance.arn],
      Condition = {
        StringEquals = {
          "iam:PassedToService" = "ecs-tasks.amazonaws.com"
        }
      }
      }
    ]
    }
  )
}
resource "aws_scheduler_schedule" "operations" {
  for_each = {
    scheduler   = "rate(5 minutes)",
    maintenance = "rate(1 hour)"
  }
  name                = "${local.name}-${each.key}"
  state               = var.desired_worker_tasks > 0 ? "ENABLED" : "DISABLED"
  schedule_expression = each.value
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = aws_ecs_cluster.main.arn
    role_arn = aws_iam_role.scheduler.arn
    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.operations[each.key].arn
      launch_type         = "FARGATE"
      task_count          = 1
      network_configuration {
        subnets          = aws_subnet.private[*].id
        security_groups  = [aws_security_group.tasks.id]
        assign_public_ip = false
      }
    }
    retry_policy {
      maximum_event_age_in_seconds = 300
      maximum_retry_attempts       = 2
    }
    dead_letter_config {
      arn = aws_sqs_queue.dead["ingestion"].arn
    }
  }
}

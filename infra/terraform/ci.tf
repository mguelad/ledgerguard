resource "aws_iam_role" "github_deploy" {
  name = "${local.name}-github-deploy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect = "Allow",
      Principal = {
        Federated = var.github_oidc_provider_arn
      },
      Action = "sts:AssumeRoleWithWebIdentity",
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
          "token.actions.githubusercontent.com:sub" = "repo:${var.github_repository}:environment:${var.environment}"
        }
      }
      }
    ]
    }
  )
  max_session_duration = 3600
}
resource "aws_iam_role_policy" "github_deploy" {
  role = aws_iam_role.github_deploy.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      { Effect = "Allow", Action = ["scheduler:GetSchedule", "scheduler:UpdateSchedule"], Resource = [for name in ["scheduler", "maintenance"] : "arn:aws:scheduler:eu-west-1:${var.aws_account_id}:schedule/default/${local.name}-${name}"] },
      { Effect = "Allow", Action = ["iam:PassRole"], Resource = aws_iam_role.scheduler.arn, Condition = { StringEquals = { "iam:PassedToService" = "scheduler.amazonaws.com" } } },
      {
        Effect   = "Allow",
        Action   = ["ecr:GetAuthorizationToken"],
        Resource = "*"
      },
      {
        Effect   = "Allow",
        Action   = ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
        Resource = aws_ecr_repository.app.arn
      },
      {
        Effect   = "Allow",
        Action   = ["ecs:DescribeServices", "ecs:UpdateService"],
        Resource = concat([aws_ecs_service.api.id], [for service in aws_ecs_service.worker : service.id])
      },
      {
        Effect   = "Allow",
        Action   = ["ecs:DescribeTaskDefinition", "ecs:RegisterTaskDefinition", "ecs:DescribeTasks"],
        Resource = "*"
      },
      {
        Effect   = "Allow",
        Action   = ["ecs:RunTask"],
        Resource = ["arn:aws:ecs:eu-west-1:${var.aws_account_id}:task-definition/${local.name}-migrator:*"]
      },
      {
        Effect   = "Allow",
        Action   = ["iam:PassRole"],
        Resource = [aws_iam_role.api.arn, aws_iam_role.worker.arn, aws_iam_role.execution.arn, aws_iam_role.migrator.arn, aws_iam_role.maintenance.arn],
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

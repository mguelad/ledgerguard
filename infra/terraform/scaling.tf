resource "aws_appautoscaling_target" "api" {
  suspended_state {
    dynamic_scaling_in_suspended  = var.desired_api_tasks == 0
    dynamic_scaling_out_suspended = var.desired_api_tasks == 0
    scheduled_scaling_suspended   = var.desired_api_tasks == 0
  }
  min_capacity       = var.desired_api_tasks
  max_capacity       = max(var.desired_api_tasks, 8)
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.api.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}
resource "aws_appautoscaling_policy" "api_cpu" {
  name               = "${local.name}-api-cpu"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.api.resource_id
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension
  service_namespace  = aws_appautoscaling_target.api.service_namespace
  target_tracking_scaling_policy_configuration {
    target_value       = 60
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
    predefined_metric_specification { predefined_metric_type = "ECSServiceAverageCPUUtilization" }
  }
}
resource "aws_appautoscaling_target" "worker" {
  suspended_state {
    dynamic_scaling_in_suspended  = var.desired_worker_tasks == 0
    dynamic_scaling_out_suspended = var.desired_worker_tasks == 0
    scheduled_scaling_suspended   = var.desired_worker_tasks == 0
  }
  for_each           = local.queue_classes
  min_capacity       = var.desired_worker_tasks
  max_capacity       = max(var.desired_worker_tasks, 4)
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.worker[each.key].name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}
resource "aws_appautoscaling_policy" "worker_age" {
  for_each           = local.queue_classes
  name               = "${local.name}-${each.key}-age"
  policy_type        = "StepScaling"
  resource_id        = aws_appautoscaling_target.worker[each.key].resource_id
  scalable_dimension = aws_appautoscaling_target.worker[each.key].scalable_dimension
  service_namespace  = aws_appautoscaling_target.worker[each.key].service_namespace
  step_scaling_policy_configuration {
    adjustment_type         = "ChangeInCapacity"
    cooldown                = 120
    metric_aggregation_type = "Maximum"
    step_adjustment {
      metric_interval_lower_bound = 0
      scaling_adjustment          = 1
    }
  }
}
resource "aws_appautoscaling_policy" "worker_idle" {
  for_each           = local.queue_classes
  name               = "${local.name}-${each.key}-idle"
  policy_type        = "StepScaling"
  resource_id        = aws_appautoscaling_target.worker[each.key].resource_id
  scalable_dimension = aws_appautoscaling_target.worker[each.key].scalable_dimension
  service_namespace  = aws_appautoscaling_target.worker[each.key].service_namespace
  step_scaling_policy_configuration {
    adjustment_type         = "ChangeInCapacity"
    cooldown                = 600
    metric_aggregation_type = "Maximum"
    step_adjustment {
      metric_interval_upper_bound = 0
      scaling_adjustment          = -1
    }
  }
}
resource "aws_cloudwatch_metric_alarm" "worker_idle" {
  for_each            = local.queue_classes
  alarm_name          = "${local.name}-${each.key}-idle"
  comparison_operator = "LessThanOrEqualToThreshold"
  threshold           = 0
  evaluation_periods  = 10
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_appautoscaling_policy.worker_idle[each.key].arn]
  metric_query {
    id          = "total"
    expression  = "visible+inflight+delayed"
    return_data = true
  }
  dynamic "metric_query" {
    for_each = { visible = "ApproximateNumberOfMessagesVisible", inflight = "ApproximateNumberOfMessagesNotVisible", delayed = "ApproximateNumberOfMessagesDelayed" }
    content {
      id = metric_query.key
      metric {
        namespace   = "AWS/SQS"
        metric_name = metric_query.value
        period      = 60
        stat        = "Maximum"
        dimensions  = { QueueName = aws_sqs_queue.work[each.key].name }
      }
    }
  }
}
resource "aws_cloudwatch_metric_alarm" "application" {
  for_each            = { CoverageAgeSeconds = 900, WorkFailed = 10, WebhookDurationMs = 500, DetectionDelaySeconds = 300 }
  alarm_name          = "${local.name}-${each.key}"
  namespace           = "LedgerGuard"
  metric_name         = each.key
  dimensions          = { Service = "LedgerGuard" }
  comparison_operator = "GreaterThanThreshold"
  threshold           = each.value
  period              = 300
  evaluation_periods  = 2
  statistic           = contains(["WorkFailed", "CoverageAgeSeconds"], each.key) ? (each.key == "WorkFailed" ? "Sum" : "Maximum") : null
  extended_statistic  = contains(["WebhookDurationMs", "DetectionDelaySeconds"], each.key) ? "p95" : null
  treat_missing_data  = "missing"
  alarm_actions       = [var.operations_topic_arn]
}

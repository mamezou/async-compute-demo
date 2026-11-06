# 性能計測：ワーカーがログに出したメトリクス（EMF）と、ALB / ECS の標準メトリクスを 1 画面に並べる。
locals {
  ns = "AsyncComputeDemo"
}

resource "aws_cloudwatch_dashboard" "this" {
  dashboard_name = var.name
  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric", x = 0, y = 0, width = 12, height = 6
        properties = {
          title  = "API 応答時間（p50 / p99）"
          region = var.region
          stat   = "p50"
          metrics = [
            ["AWS/ApplicationELB", "TargetResponseTime", "LoadBalancer", aws_lb.api.arn_suffix, { stat = "p50" }],
            ["...", { stat = "p99" }],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 0, width = 12, height = 6
        properties = {
          title  = "API の CPU とタスク数"
          region = var.region
          metrics = [
            ["AWS/ECS", "CPUUtilization", "ClusterName", aws_ecs_cluster.this.name, "ServiceName", aws_ecs_service.api.name],
            ["ECS/ContainerInsights", "RunningTaskCount", "ClusterName", aws_ecs_cluster.this.name, "ServiceName", aws_ecs_service.api.name, { yAxis = "right" }],
          ]
        }
      },
      {
        type = "metric", x = 0, y = 6, width = 12, height = 6
        properties = {
          title  = "ワーカー 1 つの処理時間とピークメモリ"
          region = var.region
          metrics = [
            [local.ns, "ChunkDuration", "Phase", "chunk", { stat = "p90" }],
            [local.ns, "PeakMemory", "Phase", "chunk", { stat = "Maximum", yAxis = "right" }],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 6, width = 12, height = 6
        properties = {
          title  = "ジョブ投入から結果までの時間"
          region = var.region
          metrics = [
            [local.ns, "JobTurnaround", "Phase", "reduce", { stat = "p50" }],
            ["...", { stat = "Maximum" }],
          ]
        }
      },
    ]
  })
}

# ワーカーのメモリがジョブ定義の上限の 80% を超えたら知らせる（サイジングの見直し合図）
resource "aws_cloudwatch_metric_alarm" "worker_memory" {
  alarm_name          = "${var.name}-worker-memory-high"
  namespace           = local.ns
  metric_name         = "PeakMemory"
  dimensions          = { Phase = "chunk" }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = var.worker_memory_mib * 0.8
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
}

resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name          = "${var.name}-api-5xx"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  dimensions          = { LoadBalancer = aws_lb.api.arn_suffix }
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
}

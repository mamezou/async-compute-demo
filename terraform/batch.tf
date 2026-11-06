# 計算層：AWS Batch on Fargate。ジョブがないときは 0 台でコストがかからない。
# Spot を優先し、確保できなければオンデマンドを使う。

resource "aws_batch_compute_environment" "spot" {
  compute_environment_name_prefix = "${var.name}-spot-"
  type                            = "MANAGED"

  compute_resources {
    type               = "FARGATE_SPOT"
    max_vcpus          = var.batch_max_vcpus
    subnets            = data.aws_subnets.default.ids
    security_group_ids = [aws_security_group.worker.id]
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_batch_compute_environment" "ondemand" {
  compute_environment_name_prefix = "${var.name}-od-"
  type                            = "MANAGED"

  compute_resources {
    type               = "FARGATE"
    max_vcpus          = var.batch_max_vcpus
    subnets            = data.aws_subnets.default.ids
    security_group_ids = [aws_security_group.worker.id]
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_batch_job_queue" "this" {
  name     = "${var.name}-queue"
  state    = "ENABLED"
  priority = 1

  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.spot.arn
  }
  compute_environment_order {
    order               = 2
    compute_environment = aws_batch_compute_environment.ondemand.arn
  }
}

resource "aws_batch_job_definition" "worker" {
  name                  = "${var.name}-worker"
  type                  = "container"
  platform_capabilities = ["FARGATE"]

  container_properties = jsonencode({
    image   = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
    command = ["python", "-m", "app.worker", "--help"] # 投入時に上書きする
    resourceRequirements = [
      { type = "VCPU", value = tostring(var.worker_vcpu) },
      { type = "MEMORY", value = tostring(var.worker_memory_mib) },
    ]
    environment = [
      { name = "BUCKET", value = aws_s3_bucket.jobs.bucket },
    ]
    executionRoleArn = aws_iam_role.execution.arn
    jobRoleArn       = aws_iam_role.worker.arn
    # デフォルト VPC のパブリックサブネットなので、ECR と S3 に届くよう公開 IP を付ける
    networkConfiguration         = { assignPublicIp = "ENABLED" }
    fargatePlatformConfiguration = { platformVersion = "LATEST" }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.worker.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "worker"
      }
    }
  })

  # Spot の中断で落ちたときだけやり直す。コードのエラーはやり直さない。
  retry_strategy {
    attempts = 3
    evaluate_on_exit {
      on_status_reason = "Your Spot Task was interrupted*" # Fargate Spot の中断理由
      action           = "RETRY"
    }
    evaluate_on_exit {
      on_reason = "*"
      action    = "EXIT"
    }
  }

  timeout {
    attempt_duration_seconds = 1800
  }
}

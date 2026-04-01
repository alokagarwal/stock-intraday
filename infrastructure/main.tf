# infrastructure/main.tf — Intraday Momentum Bot
#
# All free-tier-forever AWS services. No ECS, no ECR, no Docker.
# Lambda zips uploaded to S3 (bypasses 50MB direct upload limit).
#
# Resources:
#   S3              — Lambda zip storage + Terraform state
#   DynamoDB        — momentum_watchlist, momentum_trades
#   Secrets Manager — Alpaca credentials (never in TF state)
#   Lambda          — intraday_monitor, eod_seller
#   EventBridge     — schedules (free)
#   SNS             — email alerts (free 1000/mo)
#   CloudWatch      — log groups (14-day retention)
#   IAM             — minimal least-privilege roles

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

provider "aws" { region = var.aws_region }

data "aws_caller_identity" "current" {}

locals {
  account_id  = data.aws_caller_identity.current.account_id
  bucket_name = "momentum-bot-${local.account_id}-${var.aws_region}"
  prefix      = "momentum-bot"
  tags        = { Project = "intraday-momentum-bot", ManagedBy = "terraform" }
}

# ── S3 — bucket created by bootstrap.sh before terraform init ──
data "aws_s3_bucket" "main" {
  bucket = local.bucket_name
}

# ── DynamoDB ──────────────────────────────────────────────────
resource "aws_dynamodb_table" "watchlist" {
  name         = "momentum_watchlist"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "ticker"

  attribute {
    name = "ticker"
    type = "S"
  }

  tags = local.tags
}

resource "aws_dynamodb_table" "trades" {
  name         = "momentum_trades"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "trade_id"
  range_key    = "timestamp"

  attribute {
    name = "trade_id"
    type = "S"
  }

  attribute {
    name = "timestamp"
    type = "S"
  }

  tags = local.tags
}

# ── Secrets Manager ───────────────────────────────────────────
resource "aws_secretsmanager_secret" "alpaca" {
  name                    = "${local.prefix}/alpaca"
  description             = "Alpaca API credentials for momentum bot"
  recovery_window_in_days = 0
  tags                    = local.tags
}

resource "aws_secretsmanager_secret_version" "alpaca_placeholder" {
  secret_id = aws_secretsmanager_secret.alpaca.id
  secret_string = jsonencode({
    ALPACA_API_KEY    = "PLACEHOLDER"
    ALPACA_SECRET_KEY = "PLACEHOLDER"
    ALPACA_BASE_URL   = "https://paper-api.alpaca.markets"
  })
  lifecycle {
    ignore_changes = [secret_string]
  }
}

# ── SNS ───────────────────────────────────────────────────────
resource "aws_sns_topic" "alerts" {
  name = "${local.prefix}-alerts"
  tags = local.tags
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ── CloudWatch log groups ─────────────────────────────────────
resource "aws_cloudwatch_log_group" "monitor" {
  name              = "/aws/lambda/${local.prefix}-monitor"
  retention_in_days = 14
  tags              = local.tags
}

resource "aws_cloudwatch_log_group" "eod" {
  name              = "/aws/lambda/${local.prefix}-eod-seller"
  retention_in_days = 14
  tags              = local.tags
}

# ── IAM — Lambda execution role ───────────────────────────────
resource "aws_iam_role" "lambda" {
  name = "${local.prefix}-lambda-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "lambda.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })
  tags = local.tags
}

resource "aws_iam_role_policy" "lambda" {
  name = "${local.prefix}-lambda-policy"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DDB"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Scan",
          "dynamodb:Query"
        ]
        Resource = [
          aws_dynamodb_table.watchlist.arn,
          aws_dynamodb_table.trades.arn
        ]
      },
      {
        Sid      = "SNS"
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = [aws_sns_topic.alerts.arn]
      },
      {
        Sid      = "Secrets"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [aws_secretsmanager_secret.alpaca.arn]
      },
      {
        Sid    = "Logs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = ["arn:aws:logs:*:*:*"]
      }
    ]
  })
}

# ── IAM — GitHub Actions deploy user ─────────────────────────
resource "aws_iam_user" "gha" {
  name = "${local.prefix}-github-actions"
  tags = local.tags
}

resource "aws_iam_user_policy" "gha" {
  name = "${local.prefix}-github-policy"
  user = aws_iam_user.gha.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DDB"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Scan",
          "dynamodb:Query"
        ]
        Resource = [
          aws_dynamodb_table.watchlist.arn,
          aws_dynamodb_table.trades.arn
        ]
      },
      {
        Sid      = "SNS"
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = [aws_sns_topic.alerts.arn]
      },
      {
        Sid      = "Secrets"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [aws_secretsmanager_secret.alpaca.arn]
      }
    ]
  })
}

resource "aws_iam_access_key" "gha" {
  user = aws_iam_user.gha.name
}

# ── Lambda functions ──────────────────────────────────────────
resource "aws_lambda_function" "monitor" {
  function_name    = "${local.prefix}-monitor"
  role             = aws_iam_role.lambda.arn
  s3_bucket        = data.aws_s3_bucket.main.id
  s3_key           = "lambdas/lambda_monitor.zip"
  source_code_hash = var.monitor_zip_hash
  handler          = "intraday_monitor.handler"
  runtime          = "python3.11"
  timeout          = 180
  memory_size      = 256

  environment {
    variables = {
      WATCHLIST_TABLE          = aws_dynamodb_table.watchlist.name
      TRADES_TABLE             = aws_dynamodb_table.trades.name
      SNS_TOPIC_ARN            = aws_sns_topic.alerts.arn
      SECRETS_ARN              = aws_secretsmanager_secret.alpaca.arn
      POSITION_SIZE_USD        = var.position_size_usd
      MAX_POSITIONS            = var.max_positions
      STOP_LOSS_PCT            = var.stop_loss_pct
      PROFIT_TARGET_PCT        = var.profit_target_pct
      MAX_SCALE_FACTOR         = var.max_scale_factor
      BUY_SIGNAL_SCORE         = var.buy_signal_score
      MAX_DAILY_LOSS_PCT       = var.max_daily_loss_pct
      VIX_CAUTION_LEVEL        = var.vix_caution_level
      VIX_HALT_LEVEL           = var.vix_halt_level
      NO_NEW_BUYS_BEFORE_CLOSE = var.no_new_buys_before_close
      REENTRY_PULLBACK_PCT     = var.reentry_pullback_pct
    }
  }

  depends_on = [aws_cloudwatch_log_group.monitor]
  tags       = local.tags
}

resource "aws_lambda_function" "eod_seller" {
  function_name    = "${local.prefix}-eod-seller"
  role             = aws_iam_role.lambda.arn
  s3_bucket        = data.aws_s3_bucket.main.id
  s3_key           = "lambdas/lambda_eod.zip"
  source_code_hash = var.eod_zip_hash
  handler          = "eod_seller.handler"
  runtime          = "python3.11"
  timeout          = 60
  memory_size      = 128

  environment {
    variables = {
      WATCHLIST_TABLE    = aws_dynamodb_table.watchlist.name
      TRADES_TABLE       = aws_dynamodb_table.trades.name
      SNS_TOPIC_ARN      = aws_sns_topic.alerts.arn
      SECRETS_ARN        = aws_secretsmanager_secret.alpaca.arn
      EOD_WINDOW_MINUTES = var.eod_window_minutes
    }
  }

  depends_on = [aws_cloudwatch_log_group.eod]
  tags       = local.tags
}

# ── EventBridge — intraday monitor every 2 min ────────────────
# 9:25 AM – 4:01 PM ET = 13:25 – 20:01 UTC (covers EST + EDT)
# Lambda still calls is_market_open() — this prevents ~250 wasted cold starts/day
resource "aws_cloudwatch_event_rule" "monitor" {
  name                = "${local.prefix}-monitor"
  description         = "Fire every 2 min Mon-Fri 9:25AM-4:05PM ET"
  schedule_expression = "cron(25/2 13-21 ? * MON-FRI *)"
  tags                = local.tags
}

resource "aws_cloudwatch_event_target" "monitor" {
  rule      = aws_cloudwatch_event_rule.monitor.name
  target_id = "monitor-lambda"
  arn       = aws_lambda_function.monitor.arn
}

resource "aws_lambda_permission" "monitor" {
  statement_id  = "AllowEB-Monitor"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.monitor.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.monitor.arn
}

# ── EventBridge — EOD seller every 5 min (clock-checked) ──────
# rate(5 minutes) + internal clock check is DST-safe.
# A fixed cron like cron(55 20 ...) is wrong half the year.
resource "aws_cloudwatch_event_rule" "eod" {
  name                = "${local.prefix}-eod-seller"
  description         = "Every 5 min — eod_seller checks Alpaca clock internally"
  schedule_expression = "rate(5 minutes)"
  tags                = local.tags
}

resource "aws_cloudwatch_event_target" "eod" {
  rule      = aws_cloudwatch_event_rule.eod.name
  target_id = "eod-lambda"
  arn       = aws_lambda_function.eod_seller.arn
}

resource "aws_lambda_permission" "eod" {
  statement_id  = "AllowEB-EOD"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.eod_seller.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.eod.arn
}

# ── Outputs ───────────────────────────────────────────────────
output "github_access_key_id" {
  value = aws_iam_access_key.gha.id
}

output "github_secret_access_key" {
  value     = aws_iam_access_key.gha.secret
  sensitive = true
}

output "sns_topic_arn" {
  value = aws_sns_topic.alerts.arn
}

output "secrets_arn" {
  value = aws_secretsmanager_secret.alpaca.arn
}

output "lambda_bucket" {
  value = data.aws_s3_bucket.main.id
}

output "watchlist_table" {
  value = aws_dynamodb_table.watchlist.name
}

output "trades_table" {
  value = aws_dynamodb_table.trades.name
}

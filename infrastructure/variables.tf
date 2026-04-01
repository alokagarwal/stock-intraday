# infrastructure/variables.tf

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "alert_email" {
  type        = string
  default     = ""
  description = "Email address for SNS trade alerts"
}

# ── Trade sizing ──────────────────────────────────────────────
variable "position_size_usd" {
  type    = string
  default = "1000"
}
variable "max_positions" {
  type    = string
  default = "8"
}
variable "max_scale_factor" {
  type    = string
  default = "2.0"
  description = "Max multiple of position_size_usd per position (scale-ins)"
}

# ── Risk ──────────────────────────────────────────────────────
variable "stop_loss_pct" {
  type    = string
  default = "0.02"
  description = "Trailing stop as decimal (0.02 = 2%)"
}
variable "profit_target_pct" {
  type    = string
  default = "0.05"
  description = "First profit target that enables scale-in (5%)"
}
variable "max_daily_loss_pct" {
  type    = string
  default = "0.03"
  description = "Daily loss limit as fraction of equity (0.03 = 3%)"
}
variable "vix_caution_level" {
  type    = string
  default = "25.0"
  description = "VIX above this halves position size"
}
variable "vix_halt_level" {
  type    = string
  default = "35.0"
  description = "VIX above this stops all new buys"
}
variable "no_new_buys_before_close" {
  type    = string
  default = "20"
  description = "Minutes before close to stop opening new positions"
}

# ── Signal ────────────────────────────────────────────────────
variable "buy_signal_score" {
  type    = string
  default = "65.0"
  description = "Minimum composite signal score (0-100) to trigger a buy"
}
variable "reentry_pullback_pct" {
  type    = string
  default = "0.03"
  description = "Required pullback from HOD before re-entry allowed (3%)"
}

# ── EOD ───────────────────────────────────────────────────────
variable "eod_window_minutes" {
  type    = string
  default = "10"
  description = "Minutes before close to start EOD sell"
}

# ── Injected by deploy.sh ─────────────────────────────────────
variable "monitor_zip_hash" {
  type    = string
  default = ""
  description = "base64(sha256(lambda_monitor.zip)) — forces Lambda update"
}
variable "eod_zip_hash" {
  type    = string
  default = ""
  description = "base64(sha256(lambda_eod.zip)) — forces Lambda update"
}

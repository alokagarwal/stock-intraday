# config.py — Intraday momentum bot
#
# All non-sensitive config lives here as env vars.
# Alpaca credentials come from AWS Secrets Manager on Lambda cold start,
# and from env vars when running locally (fallback).
#
# Secret JSON stored in Secrets Manager:
#   { "ALPACA_API_KEY": "PK...",
#     "ALPACA_SECRET_KEY": "...",
#     "ALPACA_BASE_URL": "https://paper-api.alpaca.markets" }

import json, os, logging
log = logging.getLogger(__name__)

# ── AWS / infra ───────────────────────────────────────────────
AWS_REGION    = os.environ.get("AWS_REGION",    "us-east-1")
SECRETS_ARN   = os.environ.get("SECRETS_ARN",   "")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")

# ── DynamoDB tables ───────────────────────────────────────────
WATCHLIST_TABLE = os.environ.get("WATCHLIST_TABLE", "momentum_watchlist")
TRADES_TABLE    = os.environ.get("TRADES_TABLE",    "momentum_trades")

# ── Scanner settings ──────────────────────────────────────────
MIN_DOLLAR_VOLUME = float(os.environ.get("MIN_DOLLAR_VOLUME", "5000000"))  # $5M avg daily dv
MIN_PRICE         = float(os.environ.get("MIN_PRICE",         "2.00"))
MAX_PRICE         = float(os.environ.get("MAX_PRICE",         "500.00"))
MIN_GAP_PCT       = float(os.environ.get("MIN_GAP_PCT",       "0.03"))     # 3% min pre-market gap
MAX_GAP_PCT       = float(os.environ.get("MAX_GAP_PCT",       "0.40"))     # 40% max (PND guard)

# ── Trade sizing ──────────────────────────────────────────────
POSITION_SIZE_USD = float(os.environ.get("POSITION_SIZE_USD", "1000"))
MAX_POSITIONS     = int(os.environ.get("MAX_POSITIONS",        "8"))
MAX_SCALE_FACTOR  = float(os.environ.get("MAX_SCALE_FACTOR",  "2.0"))  # max 2× base per position

# ── Risk parameters ───────────────────────────────────────────
STOP_LOSS_PCT          = float(os.environ.get("STOP_LOSS_PCT",          "0.02"))  # 2% trailing
PROFIT_TARGET_PCT      = float(os.environ.get("PROFIT_TARGET_PCT",      "0.05"))  # 5% — enables scale-in
PROFIT_TARGET2_PCT     = float(os.environ.get("PROFIT_TARGET2_PCT",     "0.10"))  # 10% — tier 2
MAX_DAILY_LOSS_PCT     = float(os.environ.get("MAX_DAILY_LOSS_PCT",     "0.03"))  # 3% — halt trading
VIX_CAUTION_LEVEL      = float(os.environ.get("VIX_CAUTION_LEVEL",     "25.0"))  # halve size
VIX_HALT_LEVEL         = float(os.environ.get("VIX_HALT_LEVEL",        "35.0"))  # stop all buys
NO_NEW_BUYS_BEFORE_CLOSE = int(os.environ.get("NO_NEW_BUYS_BEFORE_CLOSE", "20")) # min before close
EOD_WINDOW_MINUTES     = int(os.environ.get("EOD_WINDOW_MINUTES",       "10"))

# ── Signal thresholds ─────────────────────────────────────────
BUY_SIGNAL_SCORE    = float(os.environ.get("BUY_SIGNAL_SCORE",    "65.0"))  # 0-100
REENTRY_PULLBACK_PCT= float(os.environ.get("REENTRY_PULLBACK_PCT","0.03"))  # 3% pullback from HOD

# ── Secrets cache ─────────────────────────────────────────────
_secrets: dict = {}

def _load_secrets() -> dict:
    global _secrets
    if _secrets:
        return _secrets
    if SECRETS_ARN:
        try:
            import boto3
            c = boto3.client("secretsmanager", region_name=AWS_REGION)
            _secrets = json.loads(c.get_secret_value(SecretId=SECRETS_ARN)["SecretString"])
            log.info("Secrets loaded from Secrets Manager")
            return _secrets
        except Exception as e:
            log.error(f"Secrets Manager failed: {e}")
    _secrets = {
        "ALPACA_API_KEY":    os.environ.get("ALPACA_API_KEY",    ""),
        "ALPACA_SECRET_KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
        "ALPACA_BASE_URL":   os.environ.get("ALPACA_BASE_URL",   "https://paper-api.alpaca.markets"),
    }
    log.info("Secrets loaded from env (fallback)")
    return _secrets

_s = _load_secrets()
ALPACA_API_KEY    = _s.get("ALPACA_API_KEY",    "")
ALPACA_SECRET_KEY = _s.get("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL   = _s.get("ALPACA_BASE_URL",   "https://paper-api.alpaca.markets")
ALPACA_PAPER      = "paper" in ALPACA_BASE_URL

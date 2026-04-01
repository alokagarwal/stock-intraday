# Intraday Momentum Bot

Automated intraday momentum trading bot built on AWS free-tier services and Alpaca.
No Docker, no ECS. Runs at approximately **$0/month**.

## How it works

| Time | What happens |
|------|-------------|
| 8:30 AM ET | GitHub Actions runs `pre_market_scanner.py` — scans for gapping momentum stocks, writes candidates to DynamoDB |
| 9:25 AM – 3:55 PM ET | Lambda runs every 2 min — scores candidates, buys on strong signals, scales into winners, re-enters after pullbacks |
| ~3:50 PM ET | Lambda closes all positions, emails EOD summary |

## Architecture

```
pre_market_scanner.py       (GitHub Actions, daily 8:30 AM ET)
        |
        v
DynamoDB — momentum_watchlist
        |
        v
intraday_monitor.py         (Lambda, every 2 min during market hours)
  ├── signal_engine.py      VWAP + RSI + volume + HOD + gap scoring
  ├── risk_guard.py         daily loss / VIX / sector / time checks
  └── trader.py             Alpaca buy/stop/sell wrapper
        |
        v
DynamoDB — momentum_trades
        |
        v
eod_seller.py               (Lambda, ~3:50 PM ET)
        |
        v
SNS email — EOD summary
```

## Stock selection

The scanner filters the full Alpaca US equity universe to stocks that:
- Price: $2 – $500
- Average dollar volume ≥ $5M (real liquidity, not micro-caps)
- Pre-market gap: **3% – 40%** vs previous close
  - Below 3% = not enough catalyst
  - Above 40% = PND/manipulation guard (excluded)

Each candidate is scored 0–100 in real time using:
- **VWAP relationship** — price above VWAP and by how much
- **RSI** — momentum in the 50–70 sweet spot (not overextended)
- **Volume surge** — today vs 20-day average
- **High-of-day proximity** — price near HOD = strength
- **Gap quality** — 3–15% gap scores highest

## Re-entries

After a stop-out or scale-exit, the bot can re-enter the same stock
**unlimited times** as long as:
1. Price pulled back ≥ 3% from the high of day after exit
2. Price has since recovered above the exit price
3. Price is still above VWAP

## Risk management

| Control | Default | Env var |
|---------|---------|---------|
| Trailing stop | 2% | `STOP_LOSS_PCT` |
| Max positions | 8 | `MAX_POSITIONS` |
| Daily loss halt | 3% of equity | `MAX_DAILY_LOSS_PCT` |
| VIX caution | >25 → halve size | `VIX_CAUTION_LEVEL` |
| VIX halt | >35 → no new buys | `VIX_HALT_LEVEL` |
| Sector limit | Max 2 per sector | hardcoded |
| No new buys | 20 min before close | `NO_NEW_BUYS_BEFORE_CLOSE` |

## Setup (15 minutes)

### 1. Alpaca account
Sign up at [alpaca.markets](https://alpaca.markets) → Paper Trading → API Keys.

### 2. AWS credentials
```bash
aws configure   # enter access key, secret, region (us-east-1 recommended)
```

### 3. Clone and deploy
```bash
git clone https://github.com/YOU/momentum-bot
cd momentum-bot

export ALPACA_API_KEY="PK..."
export ALPACA_SECRET_KEY="..."

./deploy.sh --email you@example.com
```

### 4. Add GitHub Secrets
`deploy.sh` prints exactly what to add, and sets them automatically
if the `gh` CLI is installed.

### 5. Test
```bash
# Trigger monitor Lambda manually
aws lambda invoke --function-name momentum-bot-monitor /tmp/out.json
cat /tmp/out.json

# Run scanner immediately
pip install -r requirements-scanner.txt
python scanner_task/pre_market_scanner.py
```

### 6. Go live (after paper trading for at least 4 weeks)
```bash
export ALPACA_API_KEY="AK..."   # live key (different from paper)
export ALPACA_SECRET_KEY="..."
./deploy.sh --email you@example.com --live
```

## File structure

```
momentum-bot/
├── config.py                   All settings via env vars
├── trader.py                   Alpaca buy/stop/sell wrapper
├── watchlist_db.py             DynamoDB read/write
├── signal_engine.py            VWAP/RSI/volume/HOD/gap scoring
├── risk_guard.py               Pre-trade risk gate
├── requirements-scanner.txt    GitHub Actions scanner deps
├── deploy.sh                   One-command deploy
│
├── scanner_task/
│   └── pre_market_scanner.py   Daily 8:30 AM scanner
│
├── lambdas/
│   ├── intraday_monitor.py     Every-2-min Lambda
│   └── eod_seller.py           EOD close Lambda
│
├── infrastructure/
│   ├── main.tf                 DynamoDB + Lambda + EventBridge + SNS + IAM
│   ├── variables.tf
│   ├── backend.tf              S3 state backend
│   └── bootstrap.sh            Create S3 bucket before terraform init
│
└── .github/workflows/
    ├── deploy.yml              Manual deploy trigger
    ├── daily_scan.yml          8:30 AM ET Mon-Fri scanner
    └── redeploy_lambdas.yml    Lambda-only redeploy
```

## CloudWatch log markers (grep these)

```
[SCAN_CANDIDATE]       [SCAN_REJECTED]        [SCAN_VIX_ABORT]
[BUY_TRIGGERED]        [BUY_SKIPPED]          [BUY_CONFIRMED]
[SIGNAL_SCORED]        [SIGNAL_SKIPPED]
[REENTRY_TRIGGERED]    [REENTRY_SKIPPED]      [REENTRY_CHECK]
[SCALE_IN]             [SCALE_SKIPPED]
[RISK_OK]              [RISK_DAILY_LOSS]      [RISK_VIX_HALT]
[REPAIR_STOP_PLACED]   [REPAIR_EMERGENCY_SELL]
[POSITION_SUMMARY]     [HOD_UPDATED]
[EOD_SOLD]             [EOD_SUMMARY_SENT]
```

## Warnings

- **Paper trade first.** Minimum 4 weeks before using real money.
- **Slippage is real.** Mid-cap stocks have tighter spreads than micro-caps but fills can still differ from quoted prices at market open.
- **Stop-losses are not guaranteed.** In fast-moving markets a 2% stop can gap to 5%+ if there's no liquidity.
- **Not financial advice.** Research/educational purposes only.

# lambdas/eod_seller.py — EOD position close-out + daily summary
#
# IMPORTANT — account isolation:
#   This bot shares an Alpaca account with another bot.
#   We NEVER use account.equity - account.last_equity for P&L because
#   that would include the other bot's trades.
#
#   All P&L figures in the summary are computed exclusively from
#   THIS bot's DynamoDB trade records (momentum_trades table).
#
#   "Starting capital deployed" = sum of BUY notionals from today's trades.
#   "P&L" = sum of pnl fields from today's SELL records.
#   "Balance" = Alpaca account.portfolio_value (shown for reference only,
#               labelled clearly as whole-account, not this-bot-only).
#
# Flow:
#   1. Clock guard — skip if not near close
#   2. Snapshot whole-account balance (reference only)
#   3. Market-sell all positions opened by this bot
#   4. Log sells to DynamoDB
#   5. Mark watchlist entries exited (re-entry logic for tomorrow)
#   6. Fetch today's BUY + SELL records from our trade log
#   7. Build + email EOD summary derived purely from our trade log
#
# Log markers:
#   [EOD_NOT_YET]       [EOD_NO_POSITIONS]   [EOD_SELLING]
#   [EOD_SOLD]          [EOD_ERROR]          [EOD_SUMMARY_SENT]

import logging, sys
sys.path.insert(0, "/var/task")

import boto3
from datetime import datetime, timezone

import config
from trader import AlpacaTrader
from watchlist_db import WatchlistDB

log = logging.getLogger()
log.setLevel(logging.INFO)
UTC = timezone.utc
SEP = "=" * 54
sep = "-" * 54


# ── Clock ──────────────────────────────────────────────────────

def _minutes_to_close(trader: AlpacaTrader) -> float:
    try:
        clock = trader.trading.get_clock()
        if not clock.is_open:
            return -999.0
        now   = datetime.now(UTC)
        close = clock.next_close.replace(tzinfo=UTC)
        return (close - now).total_seconds() / 60
    except Exception as e:
        log.warning(f"Clock check failed: {e}")
        return 999.0


# ── Account balance (reference only, whole-account) ────────────

def _get_account_balance(trader: AlpacaTrader) -> tuple:
    try:
        acct = trader.trading.get_account()
        return float(acct.portfolio_value), float(acct.cash)
    except Exception as e:
        log.warning(f"Account fetch failed: {e}")
        return 0.0, 0.0


# ── Our trade records ──────────────────────────────────────────

def _get_today_buys(db: WatchlistDB) -> list:
    """BUY records written by THIS bot today."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    try:
        from boto3.dynamodb.conditions import Attr
        resp = db.trades.scan(
            FilterExpression=(
                Attr("timestamp").begins_with(today) &
                Attr("action").eq("BUY")
            )
        )
        return sorted(resp.get("Items", []), key=lambda x: x.get("ticker", ""))
    except Exception as e:
        log.warning(f"Today's buys fetch failed: {e}")
        return []


def _get_today_sells(db: WatchlistDB) -> list:
    """All SELL records written by THIS bot today (stop-outs + EOD)."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    try:
        from boto3.dynamodb.conditions import Attr
        resp = db.trades.scan(
            FilterExpression=(
                Attr("timestamp").begins_with(today) &
                Attr("action").begins_with("SELL")
            )
        )
        return resp.get("Items", [])
    except Exception as e:
        log.warning(f"Today's sells fetch failed: {e}")
        return []


# ── Email builder ──────────────────────────────────────────────
#
# P&L is derived 100% from our DynamoDB records.
# The Alpaca account balance is shown as a separate "whole account"
# reference line so there's no confusion with other bots.

def _build_email(
    date_str,
    acct_portfolio,      # whole-account value from Alpaca (reference only)
    acct_cash,           # whole-account cash from Alpaca (reference only)
    today_buys,          # BUY records from our DB
    eod_closed,          # results from sell_all_eod() — the sells we just placed
    eod_errors,
):
    # ── Compute everything from our trade log only ────────────
    # Capital deployed = sum of BUY notionals this bot placed today
    capital_deployed = sum(float(r.get("notional", 0)) for r in today_buys)

    # P&L = sum of pnl from the EOD closes we just executed
    total_pnl   = sum(r.get("pnl", 0.0)     for r in eod_closed)
    total_cost  = sum(r.get("cost", 0.0)     for r in eod_closed)
    winners     = [r for r in eod_closed if r.get("pnl", 0.0) > 0]
    losers      = [r for r in eod_closed if r.get("pnl", 0.0) <= 0]
    reentries   = sum(1 for r in today_buys if int(r.get("reentry_num", 0)) > 0)
    scale_ins   = sum(1 for r in today_buys if int(r.get("scale_num", 1)) > 1)

    outcome  = "PROFITABLE" if total_pnl > 0 else ("FLAT" if total_pnl == 0 else "LOSS DAY")
    sign     = "+" if total_pnl >= 0 else ""
    pct_ret  = (total_pnl / total_cost * 100) if total_cost else 0.0

    lines = [
        SEP,
        "  MOMENTUM BOT — END-OF-DAY SUMMARY",
        f"  {date_str}",
        SEP,
        "",
        f"  Outcome      : {outcome}",
        f"  Trades closed: {len(eod_closed)}"
        + (f"  ({len(eod_errors)} error(s))" if eod_errors else ""),
        f"  Winners      : {len(winners)}    Losers: {len(losers)}",
        f"  Re-entries   : {reentries}    Scale-ins: {scale_ins}",
        "",
        # ── This-bot-only section ──────────────────────────────
        sep,
        "  THIS BOT — CAPITAL & P&L",
        "  (derived from this bot's trade log only,",
        "   unaffected by any other bots on the account)",
        sep,
        f"  Capital deployed today : ${capital_deployed:>11,.2f}",
        f"  Net gain / loss        : {sign}${abs(total_pnl):>10,.2f}",
        f"  Return on capital      : {pct_ret:>+.2f}%",
        "",
        # ── Whole-account reference ────────────────────────────
        sep,
        "  ALPACA ACCOUNT BALANCE (whole account, all bots)",
        sep,
        f"  Portfolio value  : ${acct_portfolio:>11,.2f}",
        f"  Cash             : ${acct_cash:>11,.2f}",
        "",
        # ── Buys ──────────────────────────────────────────────
        sep,
        "  STOCKS BOUGHT TODAY (this bot)",
        sep,
    ]

    if today_buys:
        lines.append(
            f"  {'TICKER':<8}  {'STRATEGY':<12}  {'RE#':>3}  "
            f"{'QTY':>6}  {'ENTRY':>9}  {'COST':>10}  {'STOP':>9}"
        )
        lines.append("  " + "-" * 64)
        for b in today_buys:
            lines.append(
                f"  {str(b.get('ticker','?')):<8}  "
                f"{str(b.get('strategy','?'))[:12]:<12}  "
                f"{int(b.get('reentry_num', 0)):>3}  "
                f"{int(float(b.get('qty', 0))):>6,}  "
                f"${float(b.get('entry_price', 0)):>8.3f}  "
                f"${float(b.get('notional', 0)):>9,.2f}  "
                f"${float(b.get('stop_price', 0)):>8.3f}"
            )
    else:
        lines.append("  No buys today.")
    lines.append("")

    # ── EOD Sells ─────────────────────────────────────────────
    lines += [sep, "  STOCKS SOLD AT EOD (this bot)", sep]
    if eod_closed:
        lines.append(
            f"  {'':2}{'TICKER':<8}  {'QTY':>6}  "
            f"{'BOUGHT':>9}  {'SOLD':>9}  {'P&L $':>9}  {'P&L %':>7}  TYPE"
        )
        lines.append("  " + "-" * 72)
        for r in sorted(eod_closed, key=lambda x: x.get("pnl_pct", 0), reverse=True):
            pnl = r.get("pnl", 0.0)
            renum = r.get("reentry_num", 0)
            lines.append(
                f"  {'^ ' if pnl >= 0 else 'v '}"
                f"{r.get('ticker', '?'):<7} "
                f"{int(r.get('qty', 0)):>6,}  "
                f"${r.get('entry', 0.0):>8.3f}  "
                f"${r.get('last_price', 0.0):>8.3f}  "
                f"{'+' if pnl >= 0 else ''}{pnl:>8.2f}  "
                f"{r.get('pnl_pct', 0.0):>+6.1f}%  "
                f"{'re#' + str(renum) if renum else 'initial'}"
            )
    else:
        lines.append("  No EOD closes today.")
    lines.append("")

    if eod_errors:
        lines += [sep, "  ERRORS", sep]
        for e in eod_errors:
            lines.append(f"  {e.get('ticker','?')}  —  {e.get('status','?')}")
        lines.append("")

    lines.append(SEP)

    subject = (
        f"EOD {date_str} | {sign}${abs(total_pnl):.2f} | "
        f"{len(winners)}W/{len(losers)}L | "
        f"Deployed ${capital_deployed:,.0f}"
    )
    return subject, "\n".join(lines)


# ── Handler ────────────────────────────────────────────────────

def handler(event, context):
    trader = AlpacaTrader()
    db     = WatchlistDB()
    sns    = boto3.client("sns", region_name=config.AWS_REGION)

    mins_left = _minutes_to_close(trader)
    log.info(f"Minutes to close: {mins_left:.1f}")

    if mins_left > config.EOD_WINDOW_MINUTES:
        log.info(f"[EOD_NOT_YET] {mins_left:.1f} min remaining")
        return {"status": "not_eod", "minutes_to_close": round(mins_left, 1)}

    if mins_left < -60:
        log.info("Market closed >1 hr — skip")
        return {"status": "market_closed"}

    # Whole-account balance — reference only
    acct_portfolio, acct_cash = _get_account_balance(trader)
    log.info(f"Whole-account balance: portfolio=${acct_portfolio:.2f}  cash=${acct_cash:.2f}")

    # Only close positions that are in our watchlist — avoids touching
    # positions opened by any other bot on the same account
    our_watchlist_tickers = {
        item["ticker"] for item in db.get_all_candidates()
    }
    all_positions = trader.get_positions()
    our_positions = [p for p in all_positions if p.symbol in our_watchlist_tickers]

    if not our_positions:
        log.info("[EOD_NO_POSITIONS] no positions belonging to this bot")
        return {"status": "no_positions"}

    log.info(
        f"[EOD_SELLING] {len(our_positions)} of {len(all_positions)} "
        f"account positions belong to this bot  ({mins_left:.1f} min to close)"
    )
    for pos in our_positions:
        log.info(
            f"  {pos.symbol}: qty={pos.qty}  "
            f"entry=${float(pos.avg_entry_price):.3f}  "
            f"now=${float(pos.current_price):.3f}  "
            f"P&L={float(pos.unrealized_plpc)*100:+.1f}%"
        )

    # Sell only our positions, not the whole account
    results = []
    try:
        trader.trading.cancel_orders()
    except Exception as e:
        log.warning(f"Cancel orders error: {e}")

    for pos in our_positions:
        ticker = pos.symbol
        try:
            trader.trading.close_position(ticker)
            r = {
                "ticker":     ticker,
                "qty":        float(pos.qty),
                "entry":      float(pos.avg_entry_price),
                "last_price": float(pos.current_price),
                "pnl":        float(pos.unrealized_pl),
                "pnl_pct":    float(pos.unrealized_plpc) * 100,
                "status":     "closed",
            }
            results.append(r)
            log.info(f"[EOD_SOLD] {ticker}  pnl=${r['pnl']:+.2f}  ({r['pnl_pct']:+.1f}%)")
        except Exception as e:
            log.error(f"[EOD_ERROR] {ticker}: {e}")
            results.append({"ticker": ticker, "status": f"error: {e}"})

    closed = [r for r in results if r.get("status") == "closed"]
    errors = [r for r in results if r.get("status") != "closed"]

    # cost basis per closed position (for email)
    for r in closed:
        r["cost"] = r["qty"] * r["entry"]

    # Log to DynamoDB
    db.log_eod_sell(results)

    # Reset watchlist so re-entry logic works next session
    for r in closed:
        try:
            db.mark_exited(r["ticker"], exit_price=r["last_price"])
        except Exception as e:
            log.warning(f"mark_exited failed {r['ticker']}: {e}")

    # Whole-account balance after sells (reference only)
    acct_portfolio_end, acct_cash_end = _get_account_balance(trader)
    log.info(f"Whole-account balance after sells: portfolio=${acct_portfolio_end:.2f}")

    # Build email from our trade log
    date_str   = datetime.now(UTC).strftime("%Y-%m-%d")
    buys_today = _get_today_buys(db)

    subject, body = _build_email(
        date_str       = date_str,
        acct_portfolio = acct_portfolio_end,
        acct_cash      = acct_cash_end,
        today_buys     = buys_today,
        eod_closed     = closed,
        eod_errors     = errors,
    )

    log.info(f"[EOD_SUMMARY]\n{body}")

    if config.SNS_TOPIC_ARN:
        try:
            sns.publish(
                TopicArn = config.SNS_TOPIC_ARN,
                Subject  = subject,
                Message  = body,
            )
            log.info("[EOD_SUMMARY_SENT]")
        except Exception as e:
            log.warning(f"SNS failed: {e}")

    total_pnl = sum(r.get("pnl", 0) for r in closed)
    return {
        "status":          "sold",
        "closed":          len(closed),
        "errors":          len(errors),
        "this_bot_pnl":    round(total_pnl, 2),
        "acct_portfolio":  round(acct_portfolio_end, 2),
    }

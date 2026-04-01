# lambdas/intraday_monitor.py — Main intraday Lambda
#
# EventBridge: every 2 minutes Mon-Fri 9:25 AM – 3:55 PM ET
# cron(25/2 13-21 ? * MON-FRI *)
#
# IMPORTANT — account isolation:
#   This bot shares an Alpaca account with another bot.
#   - Hourly P&L is computed from THIS bot's open positions only
#     (filtered by our watchlist tickers — not all account positions).
#   - The risk guard's daily-loss check also uses our positions only.
#   - We never read account.equity - account.last_equity for P&L.
#
# Per-invocation flow:
#   1. Market-open guard
#   2. Repair pass  — fix unprotected positions (our tickers only)
#   3. HOD update   — track high-of-day for all our open positions
#   4. Scale-in     — add to winners when signal stays strong
#   5. New buys     — score watchlist, buy if score >= BUY_SIGNAL_SCORE
#   6. Re-entries   — re-buy after pullback + recovery (unlimited)
#   7. Hourly email — snapshot of this bot's positions and P&L
#
# Log markers:
#   [MARKET_CLOSED]           [REPAIR_STOP_PLACED]    [REPAIR_EMERGENCY_SELL]
#   [HOD_UPDATED]             [SCALE_IN]              [SCALE_SKIPPED]
#   [BUY_TRIGGERED]           [BUY_SKIPPED]           [BUY_CONFIRMED]
#   [REENTRY_TRIGGERED]       [REENTRY_SKIPPED]       [RISK_BLOCK]
#   [POSITION_SUMMARY]

import logging, sys
sys.path.insert(0, "/var/task")

import boto3
from datetime import datetime, timezone

import config
from trader import AlpacaTrader
from watchlist_db import WatchlistDB
from signal_engine import SignalEngine
from risk_guard import RiskGuard

log = logging.getLogger()
log.setLevel(logging.INFO)
UTC = timezone.utc
SEP = "=" * 54
sep = "-" * 42


# ── Helpers ───────────────────────────────────────────────────

def _alert(sns, subject: str, body: str):
    if not config.SNS_TOPIC_ARN:
        return
    try:
        sns.publish(
            TopicArn = config.SNS_TOPIC_ARN,
            Subject  = subject[:100],
            Message  = body,
        )
    except Exception as e:
        log.warning(f"SNS failed: {e}")


def _fetch_vix(trader: AlpacaTrader) -> float:
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestTradeRequest
        data = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
        req  = StockLatestTradeRequest(symbol_or_symbols="VIXY")
        vixy = float(data.get_stock_latest_trade(req)["VIXY"].price)
        return round(vixy * 4.5, 1)
    except Exception:
        return 0.0


def _our_positions(trader: AlpacaTrader, our_tickers: set) -> list:
    """
    Return only positions that belong to this bot.
    Filters all Alpaca account positions by the tickers in our watchlist,
    so we never accidentally touch positions opened by another bot.
    """
    return [p for p in trader.get_positions() if p.symbol in our_tickers]


# ── Repair pass ───────────────────────────────────────────────

def _repair_pass(trader: AlpacaTrader, our_tickers: set, sns) -> list:
    """Run repair only on positions that belong to this bot."""
    all_repairs = trader.repair_unprotected_positions(config.STOP_LOSS_PCT)
    # Filter to our tickers only — don't log noise about the other bot's positions
    repairs = [r for r in all_repairs if r.get("ticker") in our_tickers]
    for r in repairs:
        action = r.get("action", "?")
        ticker = r.get("ticker", "?")
        if action == "stop_placed":
            log.info(f"[REPAIR_STOP_PLACED] {ticker}  stop={r.get('label','?')}")
        elif action == "emergency_sell":
            log.warning(
                f"[REPAIR_EMERGENCY_SELL] {ticker}  "
                f"pnl={r.get('pnl_pct', 0):+.1f}%"
            )
            _alert(sns,
                f"EMERGENCY SELL {ticker} — unprotected below stop",
                f"{ticker} had no stop.\n"
                f"Price ${r['current']:.3f}  entry ${r['entry']:.3f}  "
                f"P&L {r['pnl_pct']:+.1f}%\nSold at market.",
            )
        elif action == "stop_failed":
            log.error(f"[REPAIR_STOP_FAILED] {ticker}: {r.get('error')}")
    return repairs


# ── HOD tracking ──────────────────────────────────────────────

def _update_hod(our_positions: list, db: WatchlistDB):
    for pos in our_positions:
        ticker  = pos.symbol
        current = float(pos.current_price)
        item    = db.get_candidate(ticker)
        if not item:
            continue
        hod = float(item.get("high_of_day") or 0)
        if current > hod:
            db.update_high_of_day(ticker, current)
            log.info(
                f"[HOD_UPDATED] {ticker}  "
                f"new=${current:.3f}  prev=${hod:.3f}"
            )


# ── Scale-in ──────────────────────────────────────────────────

def _maybe_scale_in(trader, db, engine, pos, sns, risk) -> bool:
    ticker   = pos.symbol
    entry    = float(pos.avg_entry_price)
    current  = float(pos.current_price)
    qty      = float(pos.qty)
    notional = qty * current
    pnl_pct  = float(pos.unrealized_plpc)

    if pnl_pct < config.PROFIT_TARGET_PCT:
        log.info(
            f"[SCALE_SKIPPED] {ticker}  "
            f"pnl={pnl_pct*100:+.1f}% < {config.PROFIT_TARGET_PCT*100:.0f}%"
        )
        return False

    max_notional = config.POSITION_SIZE_USD * config.MAX_SCALE_FACTOR
    if notional >= max_notional * 0.9:
        log.info(f"[SCALE_SKIPPED] {ticker}  at max size ${notional:.0f}")
        return False

    item = db.get_candidate(ticker)
    if not item:
        return False

    sig = engine.score(ticker, float(item.get("prev_close") or entry), current)
    if sig.get("skip") or sig["score"] < config.BUY_SIGNAL_SCORE:
        log.info(f"[SCALE_SKIPPED] {ticker}  score={sig.get('score',0):.1f}")
        return False

    scale_usd = min(config.POSITION_SIZE_USD * 0.5, max_notional - notional)
    if scale_usd < 50:
        return False

    allowed, reason, adj_size = risk.check(
        ticker            = ticker,
        sector            = item.get("sector", ""),
        open_positions    = trader.get_positions(),
        position_size_usd = scale_usd,
    )
    if not allowed:
        log.info(f"[SCALE_SKIPPED] {ticker}  risk={reason}")
        return False

    try:
        order = trader.buy(
            ticker       = ticker,
            notional_usd = adj_size,
            stop_loss_pct= config.STOP_LOSS_PCT,
        )
        db.log_buy(ticker=ticker, order=order, signal_score=sig["score"],
                   strategy="scale_in", scale_num=2)
        log.info(
            f"[SCALE_IN] {ticker}  added=${adj_size:.0f}  "
            f"pnl={pnl_pct*100:+.1f}%  score={sig['score']:.1f}"
        )
        _alert(sns,
            f"SCALE IN {ticker} ({pnl_pct*100:+.0f}%) score={sig['score']:.0f}",
            f"Added ${adj_size:.0f} to {ticker}\n"
            f"Entry: ${entry:.3f}  Now: ${current:.3f}  "
            f"P&L: {pnl_pct*100:+.1f}%\n"
            f"Score: {sig['score']:.1f}  RSI: {sig.get('rsi',0):.1f}  "
            f"Vol surge: {sig.get('vol_surge',0):.1f}x",
        )
        return True
    except Exception as e:
        log.error(f"Scale-in error {ticker}: {e}")
        return False


# ── New buy ───────────────────────────────────────────────────

def _check_and_buy(trader, db, engine, item, risk, sns) -> str:
    ticker     = item["ticker"]
    prev_close = float(item.get("prev_close") or 0)
    ref_price  = float(item.get("ref_price") or 0)

    sig = engine.score(ticker, prev_close, ref_price)
    if sig.get("skip"):
        log.info(f"[BUY_SKIPPED] {ticker}  reason={sig.get('reason','?')}")
        return "skipped"

    score   = sig["score"]
    current = sig.get("current", 0)

    if score < config.BUY_SIGNAL_SCORE:
        log.info(
            f"[BUY_SKIPPED] {ticker}  score={score:.1f}  "
            f"vwap={sig.get('vwap_score',0):.0f}  "
            f"rsi={sig.get('rsi_score',0):.0f}  "
            f"vol={sig.get('volume_score',0):.0f}  "
            f"hod={sig.get('hod_score',0):.0f}  "
            f"gap={sig.get('gap_score',0):.0f}"
        )
        return "skipped"

    allowed, reason, adj_size = risk.check(
        ticker            = ticker,
        sector            = item.get("sector", ""),
        open_positions    = trader.get_positions(),
        position_size_usd = config.POSITION_SIZE_USD,
    )
    if not allowed:
        log.info(f"[RISK_BLOCK] {ticker}  {reason}")
        return f"risk_block:{reason}"

    log.info(
        f"[BUY_TRIGGERED] {ticker}  score={score:.1f}  cur=${current:.3f}  "
        f"vwap=${sig.get('vwap',0):.3f}  rsi={sig.get('rsi',0):.1f}  "
        f"surge={sig.get('vol_surge',0):.1f}x  gap={sig.get('gap_pct',0):+.1f}%"
    )
    try:
        order   = trader.buy(ticker=ticker, notional_usd=adj_size,
                             stop_loss_pct=config.STOP_LOSS_PCT)
        db.mark_triggered(ticker, entry_price=order.get("est_entry", current))
        db.log_buy(ticker=ticker, order=order, signal_score=score,
                   strategy=item.get("strategy", "momentum"), reentry_num=0)
        stop_ok = bool(order.get("stop_order_id"))
        log.info(
            f"[BUY_CONFIRMED] {ticker}  qty={order.get('qty')}  "
            f"entry=${order.get('est_entry',0):.3f}  "
            f"stop=${order.get('stop_price',0):.3f}  stop_set={stop_ok}"
        )
        _alert(sns,
            f"BUY {ticker}  score={score:.0f}  gap={sig.get('gap_pct',0):+.0f}%",
            f"Bought {ticker}\n"
            f"Score: {score:.1f}/100\n"
            f"Entry: ${order.get('est_entry',0):.3f}  "
            f"Stop: ${order.get('stop_price',0):.3f} "
            f"({'SET' if stop_ok else 'MISSING!'})\n"
            f"VWAP: ${sig.get('vwap',0):.3f}  RSI: {sig.get('rsi',0):.1f}  "
            f"Vol surge: {sig.get('vol_surge',0):.1f}x\n"
            f"Gap: {sig.get('gap_pct',0):+.1f}%  Sector: {item.get('sector','?')}",
        )
        return "bought"
    except Exception as e:
        log.error(f"[BUY_ERROR] {ticker}: {e}")
        return f"error:{e}"


# ── Re-entry ──────────────────────────────────────────────────

def _check_reentry(trader, db, engine, item, risk, sns) -> str:
    ticker      = item["ticker"]
    hod         = float(item.get("high_of_day") or 0)
    last_exit   = float(item.get("last_exit_price") or 0)
    prev_close  = float(item.get("prev_close") or 0)
    reentry_num = int(item.get("reentry_count") or 0) + 1

    if last_exit <= 0:
        return "skipped"

    current = trader.get_current_price(ticker)
    if current <= 0:
        return "skipped"

    if not engine.is_reentry_valid(ticker, hod, last_exit, current):
        log.info(
            f"[REENTRY_SKIPPED] {ticker}  "
            f"conditions not met  re#{reentry_num}"
        )
        return "skipped"

    sig = engine.score(ticker, prev_close, current)
    if sig.get("skip") or sig["score"] < config.BUY_SIGNAL_SCORE:
        log.info(
            f"[REENTRY_SKIPPED] {ticker}  "
            f"score={sig.get('score',0):.1f}  re#{reentry_num}"
        )
        return "skipped"

    allowed, reason, adj_size = risk.check(
        ticker            = ticker,
        sector            = item.get("sector", ""),
        open_positions    = trader.get_positions(),
        position_size_usd = config.POSITION_SIZE_USD,
    )
    if not allowed:
        log.info(f"[REENTRY_RISK_BLOCK] {ticker}  {reason}")
        return f"risk_block:{reason}"

    log.info(
        f"[REENTRY_TRIGGERED] {ticker}  re#{reentry_num}  "
        f"score={sig['score']:.1f}  cur=${current:.3f}  "
        f"hod=${hod:.3f}  exit=${last_exit:.3f}"
    )
    try:
        order = trader.buy(ticker=ticker, notional_usd=adj_size,
                           stop_loss_pct=config.STOP_LOSS_PCT)
        db.mark_triggered(ticker, entry_price=order.get("est_entry", current))
        db.log_buy(ticker=ticker, order=order, signal_score=sig["score"],
                   strategy="reentry", reentry_num=reentry_num)
        _alert(sns,
            f"RE-ENTRY #{reentry_num} {ticker}  score={sig['score']:.0f}",
            f"Re-entered {ticker} (re-entry #{reentry_num})\n"
            f"HOD: ${hod:.3f}  Last exit: ${last_exit:.3f}  Now: ${current:.3f}\n"
            f"Score: {sig['score']:.1f}  RSI: {sig.get('rsi',0):.1f}  "
            f"Vol: {sig.get('vol_surge',0):.1f}x",
        )
        return "reentry"
    except Exception as e:
        log.error(f"[REENTRY_ERROR] {ticker}: {e}")
        return f"error:{e}"


# ── Hourly snapshot — this bot only ──────────────────────────

def _hourly_summary(trader: AlpacaTrader, our_positions: list, db: WatchlistDB, sns):
    """
    Sends a position snapshot email once per hour.
    Reports only positions belonging to this bot.
    P&L is from Alpaca's unrealized_pl on those positions — accurate
    because it's position-level, not account-level.
    """
    if datetime.now(UTC).minute > 4:
        return   # only in first 5 min of each hour

    ts = datetime.now(UTC).strftime("%H:%M UTC")

    if not our_positions:
        _alert(sns, f"Hourly {ts}: no open positions (this bot)", "No positions open.")
        log.info("[POSITION_SUMMARY] No positions (this bot)")
        return

    # Fetch stop order status for our tickers
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        orders = trader.trading.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.OPEN)
        )
        stops = {
            str(o.symbol) for o in orders
            if str(o.side) in ("OrderSide.SELL", "sell")
            and str(o.symbol) in {p.symbol for p in our_positions}
        }
    except Exception:
        stops = set()

    # P&L from position-level data (accurate for this bot; unaffected by other bots)
    total_pnl  = sum(float(p.unrealized_pl)  for p in our_positions)
    total_cost = sum(float(p.cost_basis)     for p in our_positions)

    # Also pull today's realised P&L from our trade log
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    realised_pnl = 0.0
    try:
        from boto3.dynamodb.conditions import Attr
        resp = db.trades.scan(
            FilterExpression=(
                Attr("timestamp").begins_with(today) &
                Attr("action").begins_with("SELL")
            )
        )
        realised_pnl = sum(
            float(r.get("pnl", 0)) for r in resp.get("Items", [])
        )
    except Exception:
        pass

    lines = [
        SEP,
        f"  MOMENTUM BOT — HOURLY SNAPSHOT  {ts}",
        "  (this bot's positions only)",
        SEP,
        f"  Open positions   : {len(our_positions)}",
        f"  Unrealised P&L   : ${total_pnl:+.2f}",
        f"  Realised P&L today: ${realised_pnl:+.2f}  "
        f"(from our trade log)",
        f"  Total P&L today  : ${total_pnl + realised_pnl:+.2f}",
        "",
        f"  {'':2}{'TICKER':<8} {'QTY':>6}  "
        f"{'ENTRY':>9}  {'NOW':>9}  {'UNREAL P&L':>11}  STOP",
        "  " + sep,
    ]

    for pos in sorted(our_positions,
                      key=lambda p: float(p.unrealized_plpc), reverse=True):
        t   = pos.symbol
        pnl = float(pos.unrealized_pl)
        pct = float(pos.unrealized_plpc) * 100
        lines.append(
            f"  {'^ ' if pnl >= 0 else 'v '}{t:<7} "
            f"{int(float(pos.qty)):>6,}  "
            f"${float(pos.avg_entry_price):>8.3f}  "
            f"${float(pos.current_price):>8.3f}  "
            f"{'+' if pnl >= 0 else ''}{pnl:>8.2f} "
            f"({pct:+.1f}%)  "
            f"{'OK' if t in stops else 'MISSING!'}"
        )

    lines += [
        "",
        f"  Stops active: {', '.join(sorted(stops)) or 'none'}",
        SEP,
    ]
    body = "\n".join(lines)
    log.info(f"[POSITION_SUMMARY]\n{body}")

    all_ok = all(p.symbol in stops for p in our_positions)
    _alert(sns,
        f"Hourly {ts} | {len(our_positions)} pos | "
        f"Unreal ${total_pnl:+.2f} | Real ${realised_pnl:+.2f} | "
        f"{'OK' if all_ok else 'STOPS MISSING!'}",
        body,
    )


# ── Handler ───────────────────────────────────────────────────

def handler(event, context):
    trader = AlpacaTrader()
    db     = WatchlistDB()
    engine = SignalEngine()
    sns    = boto3.client("sns", region_name=config.AWS_REGION)

    if not trader.is_market_open():
        log.info("[MARKET_CLOSED] skipping")
        return {"status": "market_closed"}

    # Tickers we own — used throughout to filter account positions
    our_tickers = {item["ticker"] for item in db.get_all_candidates()}

    # 1. Repair — only our positions
    repairs = _repair_pass(trader, our_tickers, sns)

    # 2. VIX + risk guard
    vix  = _fetch_vix(trader)
    risk = RiskGuard(trader, db, vix=vix)

    # 3. Get our open positions (not all account positions)
    our_pos     = _our_positions(trader, our_tickers)
    open_tickers = {p.symbol for p in our_pos}

    # 4. HOD update
    _update_hod(our_pos, db)

    # 5. Scale-in existing winners
    scale_ins = []
    for pos in our_pos:
        if _maybe_scale_in(trader, db, engine, pos, sns, risk):
            scale_ins.append(pos.symbol)

    # Refresh after scale-ins
    our_pos      = _our_positions(trader, our_tickers)
    open_tickers = {p.symbol for p in our_pos}
    capacity     = config.MAX_POSITIONS - len(our_pos)

    # 6. New buys + re-entries
    all_items = db.get_all_candidates()
    new_cands = sorted(
        [i for i in all_items
         if not i.get("triggered") and i["ticker"] not in open_tickers],
        key=lambda x: float(x.get("score", 0)), reverse=True,
    )
    reentry_cands = sorted(
        [i for i in all_items
         if i.get("triggered")
         and i["ticker"] not in open_tickers
         and float(i.get("last_exit_price") or 0) > 0],
        key=lambda x: float(x.get("score", 0)), reverse=True,
    )

    bought, reentered, errors = [], [], []

    for item in new_cands:
        if capacity <= 0:
            break
        result = _check_and_buy(trader, db, engine, item, risk, sns)
        if result == "bought":
            bought.append(item["ticker"])
            capacity -= 1
        elif result.startswith("error"):
            errors.append({"ticker": item["ticker"], "error": result})

    for item in reentry_cands:
        if capacity <= 0:
            break
        result = _check_reentry(trader, db, engine, item, risk, sns)
        if result == "reentry":
            reentered.append(item["ticker"])
            capacity -= 1
        elif result.startswith("error"):
            errors.append({"ticker": item["ticker"], "error": result})

    # 7. Hourly snapshot — our positions only
    our_pos = _our_positions(trader, our_tickers)   # refresh one final time
    _hourly_summary(trader, our_pos, db, sns)

    return {
        "status":    "ok",
        "bought":    bought,
        "reentered": reentered,
        "scaled_in": scale_ins,
        "errors":    errors,
        "repairs":   len(repairs),
        "vix_est":   vix,
    }

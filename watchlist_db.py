# watchlist_db.py — DynamoDB read/write
#
# Tables
# ──────
# momentum_watchlist  PK: ticker
#   score, signal_label, ref_price, prev_close, premarket_gap_pct,
#   avg_dollar_volume, sector, strategy, added_at,
#   triggered, triggered_at, high_of_day,
#   reentry_count, last_exit_price, last_exit_at
#
# momentum_trades     PK: trade_id  SK: timestamp
#   action: BUY | SELL_STOP | SELL_TARGET | SELL_EOD | SELL_RISK
#   ticker, qty, entry_price, exit_price, stop_price,
#   notional, pnl, pnl_pct, signal_score, strategy,
#   reentry_num, scale_num, order_id, stop_order_id

import logging, uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr
import config

log = logging.getLogger(__name__)
UTC = timezone.utc


def _d(v):
    return Decimal(str(round(float(v), 6))) if v is not None else None


class WatchlistDB:
    def __init__(self):
        ddb = boto3.resource("dynamodb", region_name=config.AWS_REGION)
        self.watchlist = ddb.Table(config.WATCHLIST_TABLE)
        self.trades    = ddb.Table(config.TRADES_TABLE)

    # ── Watchlist writes ──────────────────────────────────────

    def upsert_candidate(
        self, ticker, score, signal_label, ref_price, prev_close,
        premarket_gap_pct, avg_dollar_volume=0, sector="", strategy="momentum",
    ):
        self.watchlist.put_item(Item={
            "ticker":             ticker.upper(),
            "score":              _d(score),
            "signal_label":       signal_label,
            "ref_price":          _d(ref_price),
            "prev_close":         _d(prev_close),
            "premarket_gap_pct":  _d(premarket_gap_pct),
            "avg_dollar_volume":  _d(avg_dollar_volume),
            "sector":             sector[:64],
            "strategy":           strategy,
            "added_at":           datetime.now(UTC).isoformat(),
            "triggered":          False,
            "triggered_at":       None,
            "high_of_day":        _d(ref_price),
            "reentry_count":      0,
            "last_exit_price":    None,
            "last_exit_at":       None,
        })
        log.info(
            f"[WATCHLIST_UPSERT] {ticker}  score={score:.1f}  "
            f"gap={premarket_gap_pct*100:+.1f}%  sector={sector}"
        )

    def mark_triggered(self, ticker: str, entry_price: float):
        self.watchlist.update_item(
            Key={"ticker": ticker},
            UpdateExpression="SET triggered=:t, triggered_at=:ts, ref_price=:ep",
            ExpressionAttributeValues={
                ":t":  True,
                ":ts": datetime.now(UTC).isoformat(),
                ":ep": _d(entry_price),
            },
        )

    def update_high_of_day(self, ticker: str, new_high: float):
        self.watchlist.update_item(
            Key={"ticker": ticker},
            UpdateExpression="SET high_of_day=:h",
            ExpressionAttributeValues={":h": _d(new_high)},
        )

    def mark_exited(self, ticker: str, exit_price: float):
        """Reset triggered so re-entry logic can fire on next pullback/recovery."""
        self.watchlist.update_item(
            Key={"ticker": ticker},
            UpdateExpression=(
                "SET triggered=:f, last_exit_price=:ep, "
                "last_exit_at=:ts, reentry_count=reentry_count+:one"
            ),
            ExpressionAttributeValues={
                ":f":   False,
                ":ep":  _d(exit_price),
                ":ts":  datetime.now(UTC).isoformat(),
                ":one": 1,
            },
        )

    # ── Watchlist reads ───────────────────────────────────────

    def get_active_candidates(self) -> list:
        return self.watchlist.scan(
            FilterExpression=Attr("triggered").eq(False)
        ).get("Items", [])

    def get_all_candidates(self) -> list:
        return self.watchlist.scan().get("Items", [])

    def get_candidate(self, ticker: str) -> dict | None:
        resp = self.watchlist.get_item(Key={"ticker": ticker})
        return resp.get("Item")

    def clean_stale(self, max_age_hours: int = 20) -> int:
        cutoff  = (datetime.now(UTC) - timedelta(hours=max_age_hours)).isoformat()
        removed = 0
        for item in self.get_all_candidates():
            if item.get("added_at", "") < cutoff:
                self.watchlist.delete_item(Key={"ticker": item["ticker"]})
                removed += 1
                log.info(f"[WATCHLIST_STALE_REMOVED] {item['ticker']}")
        return removed

    # ── Trade log writes ──────────────────────────────────────

    def log_buy(self, ticker, order, signal_score=0,
                strategy="", reentry_num=0, scale_num=1):
        self.trades.put_item(Item={
            "trade_id":      str(uuid.uuid4()),
            "timestamp":     datetime.now(UTC).isoformat(),
            "ticker":        ticker,
            "action":        "BUY",
            "order_id":      order.get("order_id", ""),
            "stop_order_id": order.get("stop_order_id", ""),
            "qty":           _d(order.get("qty")),
            "entry_price":   _d(order.get("est_entry")),
            "stop_price":    _d(order.get("stop_price")),
            "notional":      _d(order.get("notional")),
            "signal_score":  _d(signal_score),
            "strategy":      strategy,
            "reentry_num":   reentry_num,
            "scale_num":     scale_num,
        })

    def log_sell(self, ticker, action, qty, entry, exit_price,
                 pnl, pnl_pct, order_id="", reentry_num=0):
        self.trades.put_item(Item={
            "trade_id":    str(uuid.uuid4()),
            "timestamp":   datetime.now(UTC).isoformat(),
            "ticker":      ticker,
            "action":      action,
            "qty":         _d(qty),
            "entry_price": _d(entry),
            "exit_price":  _d(exit_price),
            "pnl":         _d(pnl),
            "pnl_pct":     _d(pnl_pct),
            "order_id":    order_id,
            "reentry_num": reentry_num,
        })

    def log_eod_sell(self, results: list):
        for r in results:
            if r.get("status") != "closed":
                continue
            self.log_sell(
                ticker     = r["ticker"],
                action     = "SELL_EOD",
                qty        = float(r.get("qty", 0)),
                entry      = float(r.get("entry", 0)),
                exit_price = float(r.get("last_price", 0)),
                pnl        = float(r.get("pnl", 0)),
                pnl_pct    = float(r.get("pnl_pct", 0)),
            )

    # ── Trade log reads ───────────────────────────────────────

    def get_today_trades(self) -> list:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        resp  = self.trades.scan(
            FilterExpression=Attr("timestamp").begins_with(today)
        )
        return resp.get("Items", [])

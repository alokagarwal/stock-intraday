# risk_guard.py — Pre-trade risk gate
#
# IMPORTANT — account isolation:
#   This bot shares an Alpaca account with another bot.
#   The daily loss check uses THIS BOT's realised P&L from DynamoDB,
#   NOT account.equity - account.last_equity (which would include
#   the other bot's trades and give wrong signals).
#
# Checks (in order):
#   1. Daily loss limit  — this bot's realised P&L from DB today
#   2. Max positions     — our open positions only
#   3. VIX guard         — halt or halve size
#   4. Time guard        — no new buys < NO_NEW_BUYS_BEFORE_CLOSE min to close
#   5. Sector limit      — max 2 open positions per sector (our positions)
#
# Log markers:
#   [RISK_OK]            [RISK_DAILY_LOSS]    [RISK_MAX_POSITIONS]
#   [RISK_VIX_HALT]      [RISK_VIX_CAUTION]  [RISK_TOO_CLOSE]
#   [RISK_SECTOR_LIMIT]

import logging
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr
import config

log = logging.getLogger(__name__)
UTC = timezone.utc


class RiskGuard:
    def __init__(self, trader, db, vix: float = 0.0):
        self.trader   = trader
        self.db       = db
        self.vix      = vix
        self._account = None

    def _get_account(self):
        if self._account is None:
            self._account = self.trader.trading.get_account()
        return self._account

    def _this_bot_daily_pnl(self) -> float:
        """
        Compute today's realised P&L purely from this bot's DynamoDB sell records.
        Returns a dollar amount (negative = loss).
        Uses dollar P&L, not % — easier to compare against a fixed limit.
        """
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        try:
            resp = self.db.trades.scan(
                FilterExpression=(
                    Attr("timestamp").begins_with(today) &
                    Attr("action").begins_with("SELL")
                )
            )
            return sum(float(r.get("pnl", 0)) for r in resp.get("Items", []))
        except Exception as e:
            log.warning(f"Daily P&L check failed: {e}")
            return 0.0

    def _this_bot_capital_deployed(self) -> float:
        """Sum of BUY notionals this bot placed today — used as the loss denominator."""
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        try:
            resp = self.db.trades.scan(
                FilterExpression=(
                    Attr("timestamp").begins_with(today) &
                    Attr("action").eq("BUY")
                )
            )
            return sum(float(r.get("notional", 0)) for r in resp.get("Items", []))
        except Exception as e:
            log.warning(f"Capital deployed check failed: {e}")
            return 0.0

    def _minutes_to_close(self) -> float:
        try:
            clock = self.trader.trading.get_clock()
            if not clock.is_open:
                return -999.0
            now   = datetime.now(UTC)
            close = clock.next_close.replace(tzinfo=UTC)
            return (close - now).total_seconds() / 60
        except Exception:
            return 999.0

    def _sector_count(self, sector: str, open_positions: list) -> int:
        if not sector:
            return 0
        wl_map = {
            item["ticker"]: item.get("sector", "")
            for item in self.db.get_all_candidates()
        }
        open_tickers = {p.symbol for p in open_positions}
        return sum(
            1 for t in open_tickers
            if wl_map.get(t, "").lower() == sector.lower()
        )

    def check(
        self,
        ticker: str,
        sector: str,
        open_positions: list,
        position_size_usd: float,
    ) -> tuple:
        """
        Returns (allowed: bool, reason: str, adjusted_size_usd: float).
        open_positions should be THIS BOT's positions only (not all account positions).
        """
        size = position_size_usd

        # 1. Daily loss limit — our trades only
        daily_pnl     = self._this_bot_daily_pnl()
        capital_today = self._this_bot_capital_deployed()

        # Express as fraction of capital deployed today (fallback to flat $500 min denominator)
        denominator = max(capital_today, 500.0)
        daily_loss_frac = daily_pnl / denominator   # negative = loss

        if daily_loss_frac <= -config.MAX_DAILY_LOSS_PCT:
            log.warning(
                f"[RISK_DAILY_LOSS] {ticker}  "
                f"this_bot_pnl=${daily_pnl:.2f}  "
                f"capital_deployed=${capital_today:.0f}  "
                f"loss={daily_loss_frac*100:.2f}% >= limit {config.MAX_DAILY_LOSS_PCT*100:.1f}%"
            )
            return False, f"daily_loss_limit ({daily_loss_frac*100:.2f}%)", size

        # 2. Max positions (count of our positions)
        if len(open_positions) >= config.MAX_POSITIONS:
            log.info(
                f"[RISK_MAX_POSITIONS] {ticker}  "
                f"our_count={len(open_positions)}"
            )
            return False, "max_positions", size

        # 3. VIX guard
        if self.vix > 0:
            if self.vix >= config.VIX_HALT_LEVEL:
                log.warning(f"[RISK_VIX_HALT] {ticker}  vix={self.vix:.1f}")
                return False, f"vix_halt ({self.vix:.1f})", size
            if self.vix >= config.VIX_CAUTION_LEVEL:
                size = round(size * 0.5, 2)
                log.info(
                    f"[RISK_VIX_CAUTION] {ticker}  "
                    f"vix={self.vix:.1f}  size halved to ${size:.0f}"
                )

        # 4. Time guard
        mins_left = self._minutes_to_close()
        if 0 < mins_left < config.NO_NEW_BUYS_BEFORE_CLOSE:
            log.info(
                f"[RISK_TOO_CLOSE] {ticker}  "
                f"{mins_left:.0f} min < {config.NO_NEW_BUYS_BEFORE_CLOSE}"
            )
            return False, f"too_close ({mins_left:.0f}min)", size

        # 5. Sector concentration
        if sector:
            count = self._sector_count(sector, open_positions)
            if count >= 2:
                log.info(
                    f"[RISK_SECTOR_LIMIT] {ticker}  "
                    f"sector={sector}  count={count}"
                )
                return False, f"sector_limit ({sector})", size

        log.info(
            f"[RISK_OK] {ticker}  "
            f"bot_pnl=${daily_pnl:.2f}  vix={self.vix:.1f}  "
            f"size=${size:.0f}  our_positions={len(open_positions)}"
        )
        return True, "ok", size

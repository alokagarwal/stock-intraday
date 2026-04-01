# trader.py — Alpaca buy/sell wrapper
#
# Wraps alpaca-py with:
#   - fill-price confirmation (critical for illiquid stocks)
#   - trailing stop placement with fixed-stop fallback
#   - unprotected position repair on every monitor invocation
#   - clean EOD market sell
#
# Log markers:
#   [BUY_SUBMITTED]        — market buy order sent
#   [FILL_CONFIRMED]       — fill price received
#   [TRAILING_STOP_SET]    — trailing stop placed OK
#   [TRAILING_STOP_MISS]   — all stop attempts failed (position unprotected)
#   [REPAIR_STOP_PLACED]   — repair pass placed a new stop
#   [REPAIR_EMERGENCY_SELL]— position already through stop, sold at market
#   [EOD_SELL]             — EOD close submitted

import logging, time
from datetime import date, timedelta

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, StopOrderRequest,
    TrailingStopOrderRequest, GetOrdersRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame

import config

log = logging.getLogger(__name__)


class AlpacaTrader:
    def __init__(self):
        self.trading = TradingClient(
            api_key   = config.ALPACA_API_KEY,
            secret_key= config.ALPACA_SECRET_KEY,
            paper     = config.ALPACA_PAPER,
        )
        self.data = StockHistoricalDataClient(
            api_key   = config.ALPACA_API_KEY,
            secret_key= config.ALPACA_SECRET_KEY,
        )

    # ── Market data ───────────────────────────────────────────

    def is_market_open(self) -> bool:
        try:
            return self.trading.get_clock().is_open
        except Exception as e:
            log.warning(f"Clock check failed: {e}")
            return False

    def get_current_price(self, ticker: str) -> float:
        try:
            req  = StockLatestTradeRequest(symbol_or_symbols=ticker)
            data = self.data.get_stock_latest_trade(req)
            return float(data[ticker].price)
        except Exception as e:
            log.warning(f"Price fetch failed {ticker}: {e}")
            return 0.0

    def get_prev_close(self, ticker: str) -> float:
        try:
            req  = StockBarsRequest(
                symbol_or_symbols=ticker,
                timeframe=TimeFrame.Day,
                start=(date.today() - timedelta(days=7)).isoformat(),
                end=(date.today() - timedelta(days=1)).isoformat(),
                limit=1,
            )
            bars = self.data.get_stock_bars(req)
            if bars and ticker in bars.data:
                return float(bars.data[ticker][-1].close)
        except Exception as e:
            log.warning(f"Prev close failed {ticker}: {e}")
        return 0.0

    def get_positions(self) -> list:
        return self.trading.get_all_positions()

    def position_exists(self, ticker: str) -> bool:
        try:
            self.trading.get_open_position(ticker)
            return True
        except Exception:
            return False

    # ── Order helpers ─────────────────────────────────────────

    def _cancel_open_sell_orders(self, ticker: str):
        """Cancel stale sell/stop orders before placing a new one."""
        try:
            req    = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[ticker])
            orders = self.trading.get_orders(filter=req)
            for o in orders:
                if str(o.side) in ("OrderSide.SELL", "sell"):
                    self.trading.cancel_order_by_id(str(o.id))
                    log.info(f"Cancelled stale sell order {o.id} for {ticker}")
        except Exception as e:
            log.warning(f"Could not cancel sell orders for {ticker}: {e}")

    def _wait_for_fill(self, order_id: str, ticker: str, timeout_s: int = 12) -> float:
        """Poll for fill price. Falls back to 0.0 if not filled within timeout."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                o = self.trading.get_order_by_id(order_id)
                if str(o.status) in ("OrderStatus.FILLED", "filled", "partially_filled"):
                    fill = float(o.filled_avg_price or 0)
                    if fill > 0:
                        log.info(f"[FILL_CONFIRMED] {ticker} @ ${fill:.4f}")
                        return fill
            except Exception:
                pass
            time.sleep(1)
        log.warning(f"Fill not confirmed within {timeout_s}s for {order_id}")
        return 0.0

    # ── Buy ───────────────────────────────────────────────────

    def buy(self, ticker: str, notional_usd: float,
            stop_loss_pct: float = None) -> dict:
        """
        1. Cancel stale sell orders.
        2. Submit market buy.
        3. Wait for actual fill price.
        4. Place trailing stop using fill price (not snapshot).

        Returns order dict with keys:
          order_id, stop_order_id, ticker, qty, est_entry,
          stop_price, status, notional
        """
        if stop_loss_pct is None:
            stop_loss_pct = config.STOP_LOSS_PCT

        snapshot = self.get_current_price(ticker)
        if snapshot <= 0:
            raise ValueError(f"Cannot fetch price for {ticker}")

        qty = max(1, int(notional_usd / snapshot))
        log.info(f"[BUY_SUBMITTED] {ticker} qty={qty} @~${snapshot:.3f}")

        self._cancel_open_sell_orders(ticker)
        time.sleep(0.3)

        buy_req = MarketOrderRequest(
            symbol         = ticker,
            qty            = qty,
            side           = OrderSide.BUY,
            time_in_force  = TimeInForce.DAY,
        )
        order       = self.trading.submit_order(buy_req)
        fill_price  = self._wait_for_fill(str(order.id), ticker, timeout_s=12)
        entry_price = fill_price if fill_price > 0 else snapshot
        stop_price  = round(entry_price * (1 - stop_loss_pct), 2)
        trail_pct   = round(stop_loss_pct * 100, 2)

        log.info(
            f"  entry=${entry_price:.4f}  stop=${stop_price:.2f} "
            f"({stop_loss_pct*100:.1f}% below)"
        )

        stop_order_id = None
        for attempt in range(4):
            try:
                time.sleep(0.5 * (attempt + 1))
                if attempt == 0:
                    stop_req = TrailingStopOrderRequest(
                        symbol        = ticker,
                        qty           = qty,
                        side          = OrderSide.SELL,
                        time_in_force = TimeInForce.GTC,
                        trail_percent = trail_pct,
                    )
                    label = f"trailing {trail_pct}%"
                else:
                    live = self.get_current_price(ticker)
                    if live > 0 and stop_price >= live:
                        stop_price = round(live * (1 - stop_loss_pct), 2)
                    stop_req = StopOrderRequest(
                        symbol        = ticker,
                        qty           = qty,
                        side          = OrderSide.SELL,
                        time_in_force = TimeInForce.GTC,
                        stop_price    = stop_price,
                    )
                    label = f"fixed ${stop_price:.2f}"

                stop_order = self.trading.submit_order(stop_req)
                stop_order_id = str(stop_order.id)
                log.info(f"[TRAILING_STOP_SET] {ticker} {label} id={stop_order_id}")
                break
            except Exception as e:
                log.warning(f"Stop attempt {attempt+1} failed: {e}")

        if not stop_order_id:
            log.error(f"[TRAILING_STOP_MISS] {ticker} — all stop attempts failed")

        return {
            "order_id":      str(order.id),
            "stop_order_id": stop_order_id,
            "ticker":        ticker,
            "qty":           qty,
            "est_entry":     entry_price,
            "stop_price":    stop_price,
            "status":        str(order.status),
            "notional":      round(qty * entry_price, 2),
        }

    # ── Repair ────────────────────────────────────────────────

    def repair_unprotected_positions(self, stop_loss_pct: float = None) -> list:
        """
        Called at the top of every monitor invocation.
        For each open position with no GTC stop order:
          (a) Price still above stop level → place new stop.
          (b) Price has blown through stop level → emergency market sell.
        """
        if stop_loss_pct is None:
            stop_loss_pct = config.STOP_LOSS_PCT

        repairs   = []
        positions = self.get_positions()
        if not positions:
            return repairs

        try:
            req        = GetOrdersRequest(status=QueryOrderStatus.OPEN)
            all_orders = self.trading.get_orders(filter=req)
            protected  = {
                str(o.symbol)
                for o in all_orders
                if str(o.side) in ("OrderSide.SELL", "sell")
            }
        except Exception as e:
            log.warning(f"Could not fetch open orders for repair: {e}")
            protected = set()

        for pos in positions:
            ticker      = pos.symbol
            entry       = float(pos.avg_entry_price)
            qty         = int(float(pos.qty))
            current     = float(pos.current_price)
            stop_level  = round(entry * (1 - stop_loss_pct), 2)
            pnl_pct     = float(pos.unrealized_plpc) * 100

            if ticker in protected:
                log.info(f"[REPAIR_STOP_OK] {ticker} stop confirmed active")
                continue

            log.warning(
                f"[REPAIR_NEEDED] {ticker} no stop  "
                f"entry=${entry:.3f} cur=${current:.3f} "
                f"stop_level=${stop_level:.3f} pnl={pnl_pct:+.1f}%"
            )

            if current <= stop_level:
                log.warning(f"[REPAIR_EMERGENCY_SELL] {ticker} below stop — selling at market")
                try:
                    self.trading.close_position(ticker)
                    repairs.append({
                        "ticker": ticker, "action": "emergency_sell",
                        "entry": entry, "current": current, "pnl_pct": round(pnl_pct, 2),
                    })
                except Exception as e:
                    log.error(f"Emergency sell failed {ticker}: {e}")
                    repairs.append({"ticker": ticker, "action": "sell_failed", "error": str(e)})
                continue

            trail_pct = round(stop_loss_pct * 100, 2)
            for attempt in range(2):
                try:
                    time.sleep(0.3)
                    if attempt == 0:
                        stop_req = TrailingStopOrderRequest(
                            symbol=ticker, qty=qty, side=OrderSide.SELL,
                            time_in_force=TimeInForce.GTC, trail_percent=trail_pct,
                        )
                        label = f"trailing {trail_pct}%"
                    else:
                        stop_req = StopOrderRequest(
                            symbol=ticker, qty=qty, side=OrderSide.SELL,
                            time_in_force=TimeInForce.GTC, stop_price=stop_level,
                        )
                        label = f"fixed ${stop_level:.2f}"

                    stop_order = self.trading.submit_order(stop_req)
                    repairs.append({
                        "ticker": ticker, "action": "stop_placed",
                        "label": label, "order_id": str(stop_order.id),
                    })
                    log.info(f"[REPAIR_STOP_PLACED] {ticker} {label}")
                    break
                except Exception as e:
                    log.warning(f"Repair stop attempt {attempt+1} failed {ticker}: {e}")
                    if attempt == 1:
                        repairs.append({"ticker": ticker, "action": "stop_failed", "error": str(e)})

        return repairs

    # ── EOD sell ──────────────────────────────────────────────

    def sell_all_eod(self) -> list:
        """Cancel all orders then market-sell every open position."""
        results = []
        try:
            self.trading.cancel_orders()
        except Exception as e:
            log.warning(f"Cancel orders error: {e}")

        for pos in self.get_positions():
            ticker = pos.symbol
            try:
                self.trading.close_position(ticker)
                results.append({
                    "ticker":     ticker,
                    "qty":        float(pos.qty),
                    "entry":      float(pos.avg_entry_price),
                    "last_price": float(pos.current_price),
                    "pnl":        float(pos.unrealized_pl),
                    "pnl_pct":    float(pos.unrealized_plpc) * 100,
                    "status":     "closed",
                })
                log.info(
                    f"[EOD_SELL] {ticker}  "
                    f"pnl=${float(pos.unrealized_pl):+.2f} "
                    f"({float(pos.unrealized_plpc)*100:+.1f}%)"
                )
            except Exception as e:
                log.error(f"EOD sell failed {ticker}: {e}")
                results.append({"ticker": ticker, "status": f"error: {e}"})

        return results

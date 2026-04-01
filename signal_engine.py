# signal_engine.py — Composite signal scoring (0-100)
#
# Five components, each capped at 20 points:
#   vwap_score   — is price above VWAP and by how much?
#   rsi_score    — is momentum in the 50-70 sweet spot?
#   volume_score — is today's volume a surge vs the 20-day average?
#   hod_score    — is price near the high of day (strength)?
#   gap_score    — is the gap in the 3-15% quality zone?
#
# Re-entry validity is also computed here:
#   pullback from HOD confirmed + price recovered + above VWAP
#
# Log markers:
#   [SIGNAL_SCORED]  — score computed
#   [SIGNAL_SKIPPED] — could not fetch bars, skip ticker
#   [REENTRY_CHECK]  — re-entry condition evaluation

import logging
from datetime import date, timedelta

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestBarRequest
from alpaca.data.timeframe import TimeFrame

import config

log = logging.getLogger(__name__)


class SignalEngine:
    def __init__(self):
        self.data = StockHistoricalDataClient(
            api_key    = config.ALPACA_API_KEY,
            secret_key = config.ALPACA_SECRET_KEY,
        )

    # ── Public API ────────────────────────────────────────────

    def score(self, ticker: str, prev_close: float, ref_price: float) -> dict:
        """
        Returns dict with 'score' (0-100) and component breakdown.
        Returns {'score': 0, 'skip': True, 'reason': ...} on data failure.
        """
        try:
            bars_1m = self._get_intraday_bars(ticker)
            if not bars_1m or len(bars_1m) < 5:
                log.info(f"[SIGNAL_SKIPPED] {ticker} insufficient intraday bars ({len(bars_1m) if bars_1m else 0})")
                return {"score": 0, "skip": True, "reason": "no_intraday_bars"}

            daily_bars = self._get_daily_bars(ticker)
            current    = float(bars_1m[-1].close)
            vwap       = self._vwap(bars_1m)
            rsi        = self._rsi(bars_1m)
            vol_surge  = self._volume_surge(bars_1m, daily_bars)
            hod        = max(float(b.high) for b in bars_1m)
            gap_pct    = (ref_price - prev_close) / prev_close if prev_close > 0 else 0

            components = {
                "vwap_score":   self._score_vwap(current, vwap),
                "rsi_score":    self._score_rsi(rsi),
                "volume_score": self._score_volume(vol_surge),
                "hod_score":    self._score_hod(current, hod),
                "gap_score":    self._score_gap(gap_pct),
            }
            total = sum(components.values())

            result = {
                "score":     round(total, 1),
                "skip":      False,
                "ticker":    ticker,
                "current":   round(current, 4),
                "vwap":      round(vwap, 4),
                "rsi":       round(rsi, 1),
                "vol_surge": round(vol_surge, 2),
                "hod":       round(hod, 4),
                "gap_pct":   round(gap_pct * 100, 2),
                **components,
            }
            log.info(
                f"[SIGNAL_SCORED] {ticker}  score={total:.1f}  "
                f"vwap={components['vwap_score']:.0f}  rsi={components['rsi_score']:.0f}  "
                f"vol={components['volume_score']:.0f}  hod={components['hod_score']:.0f}  "
                f"gap={components['gap_score']:.0f}  "
                f"cur=${current:.3f}  VWAP=${vwap:.3f}  RSI={rsi:.1f}  surge={vol_surge:.1f}x"
            )
            return result

        except Exception as e:
            log.warning(f"[SIGNAL_SKIPPED] {ticker} exception: {e}")
            return {"score": 0, "skip": True, "reason": str(e)}

    def is_reentry_valid(
        self, ticker: str,
        high_of_day: float,
        last_exit_price: float,
        current_price: float,
    ) -> bool:
        """
        Re-entry valid when:
          (a) price pulled back >= REENTRY_PULLBACK_PCT from HOD after exit
          (b) price has since recovered above last_exit_price
          (c) price is still above VWAP (trend intact)
        """
        if high_of_day <= 0 or last_exit_price <= 0:
            return False

        pullback = (high_of_day - last_exit_price) / high_of_day
        recovered = current_price >= last_exit_price * 1.005

        above_vwap = True
        try:
            bars = self._get_intraday_bars(ticker)
            if bars:
                above_vwap = current_price > self._vwap(bars)
        except Exception:
            pass

        valid = (
            pullback >= config.REENTRY_PULLBACK_PCT
            and recovered
            and above_vwap
        )
        log.info(
            f"[REENTRY_CHECK] {ticker}  hod=${high_of_day:.3f}  "
            f"exit=${last_exit_price:.3f}  cur=${current_price:.3f}  "
            f"pullback={pullback*100:.1f}%  recovered={recovered}  "
            f"above_vwap={above_vwap}  valid={valid}"
        )
        return valid

    # ── Data fetchers ─────────────────────────────────────────

    def _get_intraday_bars(self, ticker: str) -> list:
        req  = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Minute,
            start=date.today().isoformat(),
            limit=390,
        )
        bars = self.data.get_stock_bars(req)
        return list(bars.data.get(ticker, []))

    def _get_daily_bars(self, ticker: str, lookback: int = 22) -> list:
        req  = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Day,
            start=(date.today() - timedelta(days=lookback)).isoformat(),
            end=(date.today() - timedelta(days=1)).isoformat(),
        )
        bars = self.data.get_stock_bars(req)
        return list(bars.data.get(ticker, []))

    # ── Indicators ────────────────────────────────────────────

    def _vwap(self, bars: list) -> float:
        total_vol = sum(float(b.volume) for b in bars)
        if total_vol <= 0:
            return float(bars[-1].close)
        total_vp = sum(
            float(b.volume) * (float(b.high) + float(b.low) + float(b.close)) / 3
            for b in bars
        )
        return total_vp / total_vol

    def _rsi(self, bars: list, period: int = 14) -> float:
        closes  = [float(b.close) for b in bars]
        if len(closes) <= period:
            return 50.0
        changes = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains   = [max(c, 0)   for c in changes]
        losses  = [abs(min(c, 0)) for c in changes]
        avg_g   = sum(gains[-period:])  / period
        avg_l   = sum(losses[-period:]) / period
        if avg_l == 0:
            return 100.0
        return 100 - (100 / (1 + avg_g / avg_l))

    def _volume_surge(self, bars_1m: list, daily_bars: list) -> float:
        today_vol = sum(float(b.volume) for b in bars_1m)
        if not daily_bars:
            return 1.0
        avg_daily = sum(float(b.volume) for b in daily_bars) / len(daily_bars)
        return today_vol / avg_daily if avg_daily > 0 else 1.0

    # ── Scoring components (0-20 each) ────────────────────────

    def _score_vwap(self, price: float, vwap: float) -> float:
        if vwap <= 0:
            return 10.0
        pct = (price - vwap) / vwap
        if pct < 0:
            return 0.0
        return round(min(pct / 0.03, 1.0) * 20, 1)

    def _score_rsi(self, rsi: float) -> float:
        if rsi < 45:   return 0.0
        if rsi > 80:   return 5.0
        if 55 <= rsi <= 70: return 20.0
        if rsi < 55:   return round((rsi - 45) / 10 * 20, 1)
        return round((80 - rsi) / 10 * 20, 1)

    def _score_volume(self, surge: float) -> float:
        return round(min(surge / 5.0, 1.0) * 20, 1)

    def _score_hod(self, price: float, hod: float) -> float:
        if hod <= 0:   return 10.0
        pct_below = (hod - price) / hod
        if pct_below <= 0.005: return 20.0
        if pct_below >= 0.05:  return 0.0
        return round((1 - pct_below / 0.05) * 20, 1)

    def _score_gap(self, gap_pct: float) -> float:
        if gap_pct < 0.03:  return 0.0
        if gap_pct <= 0.10: return 20.0
        if gap_pct <= 0.20: return 15.0
        if gap_pct <= 0.40: return 5.0
        return 0.0

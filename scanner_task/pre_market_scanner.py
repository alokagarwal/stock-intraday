# scanner_task/pre_market_scanner.py
#
# Runs 8:30–9:20 AM ET daily via GitHub Actions cron.
# Scans the Alpaca US equity universe for intraday momentum candidates.
#
# Filters applied (all must pass):
#   1. Price: $2 – $500
#   2. Average dollar volume >= $5M (liquidity)
#   3. Pre-market gap: 3% – 40% vs previous close
#      < 3%  = not enough catalyst
#      > 40% = PND/manipulation guard
#
# Writes qualified candidates to DynamoDB momentum_watchlist.
# The Lambda monitor scores and buys them once market opens.
#
# Log markers:
#   [SCAN_START]     [SCAN_VIX_ABORT]   [SCAN_CANDIDATE]
#   [SCAN_REJECTED]  [SCAN_COMPLETE]

import logging, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import date, timedelta

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetAssetsRequest
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import (
    StockLatestBarRequest, StockBarsRequest, StockLatestTradeRequest,
)
from alpaca.data.timeframe import TimeFrame

import config
from watchlist_db import WatchlistDB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(__name__)

_SECTOR_MAP = {
    "NVDA":"tech","AMD":"tech","INTC":"tech","AAPL":"tech","MSFT":"tech",
    "GOOGL":"tech","META":"tech","TSM":"tech","QCOM":"tech","MU":"tech",
    "MRNA":"biotech","BNTX":"biotech","BIIB":"biotech","REGN":"biotech",
    "VRTX":"biotech","GILD":"biotech","AMGN":"biotech",
    "XOM":"energy","CVX":"energy","SLB":"energy","OXY":"energy",
    "JPM":"financials","BAC":"financials","GS":"financials","MS":"financials",
    "AMZN":"consumer","WMT":"consumer","TGT":"consumer","HD":"consumer",
}
def _sector(t): return _SECTOR_MAP.get(t.upper(), "other")


def _vix_proxy(data):
    try:
        req = StockLatestTradeRequest(symbol_or_symbols="VIXY")
        return float(data.get_stock_latest_trade(req)["VIXY"].price) * 4.5
    except Exception as e:
        log.warning(f"VIX proxy failed: {e}")
        return 0.0

def _prev_close(data, ticker):
    try:
        req  = StockBarsRequest(
            symbol_or_symbols=ticker, timeframe=TimeFrame.Day,
            start=(date.today()-timedelta(days=7)).isoformat(),
            end=(date.today()-timedelta(days=1)).isoformat(), limit=1,
        )
        bars = list(data.get_stock_bars(req).data.get(ticker, []))
        return float(bars[-1].close) if bars else 0.0
    except Exception:
        return 0.0

def _premarket_price(data, ticker):
    try:
        req = StockLatestBarRequest(symbol_or_symbols=ticker)
        return float(data.get_stock_latest_bar(req)[ticker].close)
    except Exception:
        return 0.0

def _avg_dollar_volume(data, ticker):
    try:
        req  = StockBarsRequest(
            symbol_or_symbols=ticker, timeframe=TimeFrame.Day,
            start=(date.today()-timedelta(days=30)).isoformat(),
            end=(date.today()-timedelta(days=1)).isoformat(), limit=20,
        )
        bars = list(data.get_stock_bars(req).data.get(ticker, []))
        if not bars: return 0.0
        return sum(float(b.close)*float(b.volume) for b in bars) / len(bars)
    except Exception:
        return 0.0


def run_scan():
    log.info("[SCAN_START] Pre-market momentum scanner")
    trading = TradingClient(api_key=config.ALPACA_API_KEY,
                            secret_key=config.ALPACA_SECRET_KEY,
                            paper=config.ALPACA_PAPER)
    data    = StockHistoricalDataClient(api_key=config.ALPACA_API_KEY,
                                        secret_key=config.ALPACA_SECRET_KEY)
    db      = WatchlistDB()

    vix = _vix_proxy(data)
    log.info(f"VIX proxy: {vix:.1f}")
    if vix >= config.VIX_HALT_LEVEL:
        log.warning(f"[SCAN_VIX_ABORT] VIX {vix:.1f} >= halt {config.VIX_HALT_LEVEL}")
        return {"status": "vix_abort", "vix": vix, "candidates": 0}

    removed = db.clean_stale(max_age_hours=20)
    if removed:
        log.info(f"Removed {removed} stale entries")

    assets   = trading.get_all_assets(
        GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE)
    )
    universe = [a for a in assets if a.tradable and a.fractionable]
    log.info(f"Universe: {len(universe)} equities")

    candidates = []
    rej = {"price":0,"volume":0,"gap_small":0,"gap_large":0,"no_data":0}

    for asset in universe:
        ticker = asset.symbol
        pm = _premarket_price(data, ticker)
        if pm <= 0: rej["no_data"] += 1; continue
        if not (config.MIN_PRICE <= pm <= config.MAX_PRICE): rej["price"] += 1; continue
        adv = _avg_dollar_volume(data, ticker)
        if adv < config.MIN_DOLLAR_VOLUME: rej["volume"] += 1; continue
        pc = _prev_close(data, ticker)
        if pc <= 0: rej["no_data"] += 1; continue
        gap = (pm - pc) / pc
        if gap < config.MIN_GAP_PCT: rej["gap_small"] += 1; continue
        if gap > config.MAX_GAP_PCT:
            rej["gap_large"] += 1
            log.info(f"[SCAN_REJECTED] {ticker} gap={gap*100:.1f}% PND guard"); continue

        score = 50.0 + min(gap*100, 20.0) + min(adv/config.MIN_DOLLAR_VOLUME*5, 20.0)
        sec   = _sector(ticker)
        db.upsert_candidate(
            ticker=ticker, score=round(score,1), signal_label="momentum_gap",
            ref_price=pm, prev_close=pc, premarket_gap_pct=gap,
            avg_dollar_volume=adv, sector=sec, strategy="intraday_momentum",
        )
        candidates.append(ticker)
        log.info(
            f"[SCAN_CANDIDATE] {ticker}  gap={gap*100:+.1f}%  "
            f"pm=${pm:.2f}  adv=${adv/1e6:.1f}M  sector={sec}  score={score:.0f}"
        )

    log.info(
        f"[SCAN_COMPLETE] {len(candidates)} candidates.  "
        f"Rejected: {rej}"
    )
    return {"status": "ok", "candidates": len(candidates), "vix": vix}


if __name__ == "__main__":
    print(run_scan())

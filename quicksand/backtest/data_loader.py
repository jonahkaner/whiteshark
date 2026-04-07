"""Historical data fetching and caching for backtesting."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import ccxt
import pandas as pd

from quicksand.utils.logging import get_logger

log = get_logger("data_loader")

CACHE_DIR = Path("data/cache")


@dataclass
class HistoricalData:
    """Container for all data needed to backtest funding arb on a single pair."""

    symbol: str
    exchange: str
    funding_rates: pd.DataFrame  # columns: timestamp, rate, annualized
    spot_prices: pd.DataFrame  # columns: timestamp, open, high, low, close, volume
    perp_prices: pd.DataFrame  # columns: timestamp, open, high, low, close, volume
    start_date: str
    end_date: str


def _cache_path(exchange: str, symbol: str, data_type: str) -> Path:
    safe_symbol = symbol.replace("/", "_").replace(":", "_")
    return CACHE_DIR / f"{exchange}_{safe_symbol}_{data_type}.parquet"


def _load_cache(path: Path) -> pd.DataFrame | None:
    if path.exists():
        log.info("cache_hit", path=str(path))
        return pd.read_parquet(path)
    return None


def _save_cache(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    log.info("cache_saved", path=str(path), rows=len(df))


def fetch_funding_rate_history(
    exchange_id: str,
    symbol: str,
    since: str | None = None,
    limit_days: int = 365,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Fetch historical funding rates for a perpetual contract.

    Args:
        exchange_id: Exchange name (e.g. 'binance')
        symbol: Perp symbol (e.g. 'BTC/USDT:USDT')
        since: Start date string (e.g. '2025-01-01')
        limit_days: How many days of history to fetch
        use_cache: Whether to use local cache
    """
    cache = _cache_path(exchange_id, symbol, "funding")
    if use_cache:
        cached = _load_cache(cache)
        if cached is not None:
            return cached

    exchange_class = getattr(ccxt, exchange_id, None)
    if exchange_class is None:
        raise ValueError(f"Unknown exchange: {exchange_id}")

    exchange = exchange_class({"enableRateLimit": True})
    exchange.load_markets()

    if since:
        since_ms = exchange.parse8601(since + "T00:00:00Z")
    else:
        since_ms = exchange.milliseconds() - (limit_days * 24 * 60 * 60 * 1000)

    all_rates = []
    current_since = since_ms
    end_ms = exchange.milliseconds()

    log.info("fetching_funding_rates", exchange=exchange_id, symbol=symbol)

    while current_since < end_ms:
        try:
            rates = exchange.fetch_funding_rate_history(
                symbol, since=current_since, limit=1000
            )
        except Exception as e:
            log.warning("funding_fetch_error", error=str(e))
            break

        if not rates:
            break

        all_rates.extend(rates)
        current_since = rates[-1]["timestamp"] + 1

        # Rate limit
        time.sleep(exchange.rateLimit / 1000)

    if not all_rates:
        log.warning("no_funding_data", exchange=exchange_id, symbol=symbol)
        return pd.DataFrame(columns=["timestamp", "datetime", "rate", "annualized"])

    df = pd.DataFrame(all_rates)
    df = df[["timestamp", "datetime", "fundingRate"]].rename(
        columns={"fundingRate": "rate"}
    )
    df["annualized"] = df["rate"] * 3 * 365  # 3 funding periods per day
    df = df.sort_values("timestamp").reset_index(drop=True)

    if use_cache:
        _save_cache(df, cache)

    log.info("funding_rates_fetched", rows=len(df), span_days=limit_days)
    return df


def fetch_ohlcv_history(
    exchange_id: str,
    symbol: str,
    timeframe: str = "1h",
    since: str | None = None,
    limit_days: int = 365,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Fetch historical OHLCV data.

    Args:
        exchange_id: Exchange name
        symbol: Trading pair (spot or perp)
        timeframe: Candle timeframe ('1h', '4h', '1d')
        since: Start date string
        limit_days: Days of history
        use_cache: Whether to use local cache
    """
    cache = _cache_path(exchange_id, symbol, f"ohlcv_{timeframe}")
    if use_cache:
        cached = _load_cache(cache)
        if cached is not None:
            return cached

    exchange_class = getattr(ccxt, exchange_id, None)
    if exchange_class is None:
        raise ValueError(f"Unknown exchange: {exchange_id}")

    exchange = exchange_class({"enableRateLimit": True})
    exchange.load_markets()

    if since:
        since_ms = exchange.parse8601(since + "T00:00:00Z")
    else:
        since_ms = exchange.milliseconds() - (limit_days * 24 * 60 * 60 * 1000)

    all_candles = []
    current_since = since_ms
    end_ms = exchange.milliseconds()

    log.info("fetching_ohlcv", exchange=exchange_id, symbol=symbol, timeframe=timeframe)

    while current_since < end_ms:
        try:
            candles = exchange.fetch_ohlcv(
                symbol, timeframe, since=current_since, limit=1000
            )
        except Exception as e:
            log.warning("ohlcv_fetch_error", error=str(e))
            break

        if not candles:
            break

        all_candles.extend(candles)
        current_since = candles[-1][0] + 1

        time.sleep(exchange.rateLimit / 1000)

    if not all_candles:
        log.warning("no_ohlcv_data", exchange=exchange_id, symbol=symbol)
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")

    if use_cache:
        _save_cache(df, cache)

    log.info("ohlcv_fetched", rows=len(df), symbol=symbol)
    return df


def load_pair_data(
    exchange_id: str,
    pair: str,
    since: str | None = None,
    limit_days: int = 365,
    timeframe: str = "1h",
    use_cache: bool = True,
) -> HistoricalData:
    """Load all historical data needed for funding arb backtest on a single pair.

    Args:
        exchange_id: Exchange (e.g. 'binance')
        pair: Base pair (e.g. 'BTC/USDT')
        since: Start date
        limit_days: Days of history
        timeframe: OHLCV timeframe
        use_cache: Use local cache
    """
    perp_symbol = f"{pair}:USDT"

    funding = fetch_funding_rate_history(
        exchange_id, perp_symbol, since=since, limit_days=limit_days, use_cache=use_cache
    )
    spot_prices = fetch_ohlcv_history(
        exchange_id, pair, timeframe=timeframe, since=since,
        limit_days=limit_days, use_cache=use_cache,
    )
    perp_prices = fetch_ohlcv_history(
        exchange_id, perp_symbol, timeframe=timeframe, since=since,
        limit_days=limit_days, use_cache=use_cache,
    )

    start = since or "auto"
    end = "now"

    return HistoricalData(
        symbol=pair,
        exchange=exchange_id,
        funding_rates=funding,
        spot_prices=spot_prices,
        perp_prices=perp_prices,
        start_date=start,
        end_date=end,
    )

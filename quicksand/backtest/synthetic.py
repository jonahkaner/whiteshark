"""Synthetic data generator for testing backtests without exchange access.

Generates realistic-looking funding rate and price data based on
statistical properties of actual crypto markets.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from quicksand.backtest.data_loader import HistoricalData


def generate_synthetic_data(
    symbol: str = "BTC/USDT",
    days: int = 365,
    base_price: float = 60000.0,
    volatility: float = 0.02,  # Daily volatility (2%)
    mean_funding_rate: float = 0.0003,  # Mean 8h funding rate (~33% annualized)
    funding_vol: float = 0.0005,  # Funding rate volatility
    seed: int | None = 42,
) -> HistoricalData:
    """Generate synthetic historical data for backtesting.

    Generates:
    - Price series with geometric Brownian motion
    - Funding rates with mean-reverting process (Ornstein-Uhlenbeck)
    - Perp prices that track spot with small basis

    Args:
        symbol: Trading pair name
        days: Number of days to generate
        base_price: Starting price
        volatility: Daily price volatility
        mean_funding_rate: Average 8h funding rate
        funding_vol: Funding rate volatility
        seed: Random seed for reproducibility
    """
    if seed is not None:
        np.random.seed(seed)

    # Time parameters
    hours = days * 24
    funding_periods = days * 3  # 3 funding periods per day (every 8h)

    # Generate hourly price series (GBM)
    hourly_returns = np.random.normal(0, volatility / np.sqrt(24), hours)
    # Add a slight upward drift (typical of crypto bull markets)
    hourly_returns += 0.0001  # ~2.4% monthly drift
    prices = base_price * np.cumprod(1 + hourly_returns)

    # Generate timestamps (hourly, starting from ~1 year ago)
    start_ms = 1680000000000  # Arbitrary start
    hourly_timestamps = np.array([start_ms + i * 3600 * 1000 for i in range(hours)])

    # Spot prices DataFrame
    spot_df = pd.DataFrame({
        "timestamp": hourly_timestamps,
        "open": prices,
        "high": prices * (1 + np.abs(np.random.normal(0, 0.002, hours))),
        "low": prices * (1 - np.abs(np.random.normal(0, 0.002, hours))),
        "close": prices,
        "volume": np.random.lognormal(20, 1, hours),
        "datetime": pd.to_datetime(hourly_timestamps, unit="ms"),
    })

    # Perp prices: spot + small random basis
    basis = np.random.normal(0, base_price * 0.0001, hours)
    perp_prices = prices + basis

    perp_df = pd.DataFrame({
        "timestamp": hourly_timestamps,
        "open": perp_prices,
        "high": perp_prices * (1 + np.abs(np.random.normal(0, 0.002, hours))),
        "low": perp_prices * (1 - np.abs(np.random.normal(0, 0.002, hours))),
        "close": perp_prices,
        "volume": np.random.lognormal(20, 1, hours),
        "datetime": pd.to_datetime(hourly_timestamps, unit="ms"),
    })

    # Generate funding rates (Ornstein-Uhlenbeck mean-reverting process)
    funding_timestamps = np.array([start_ms + i * 8 * 3600 * 1000 for i in range(funding_periods)])
    rates = np.zeros(funding_periods)
    rates[0] = mean_funding_rate

    theta = 0.1  # Mean reversion speed
    for i in range(1, funding_periods):
        # Mean-reverting with occasional spikes
        rates[i] = (
            rates[i - 1]
            + theta * (mean_funding_rate - rates[i - 1])
            + np.random.normal(0, funding_vol)
        )
        # Occasional regime changes (simulates market euphoria/panic)
        if np.random.random() < 0.02:  # 2% chance per period
            rates[i] += np.random.choice([-1, 1]) * np.random.exponential(0.001)

    funding_df = pd.DataFrame({
        "timestamp": funding_timestamps,
        "datetime": pd.to_datetime(funding_timestamps, unit="ms"),
        "rate": rates,
        "annualized": rates * 3 * 365,
    })

    return HistoricalData(
        symbol=symbol,
        exchange="synthetic",
        funding_rates=funding_df,
        spot_prices=spot_df,
        perp_prices=perp_df,
        start_date="synthetic",
        end_date="synthetic",
    )

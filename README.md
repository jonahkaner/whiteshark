# Quicksand

Multi-strategy crypto trading bot. Currently implements:

1. **Funding Rate Arbitrage** — Delta-neutral strategy collecting funding payments from perpetual futures. Buy spot + short perp (or vice versa) when funding rates are attractive.

## Quick Start

```bash
# Install
pip install -e .

# Configure
cp config.example.yaml config.yaml
# Edit config.yaml with your exchange API keys

# Dry run (connect + fetch balances)
quicksand --config config.yaml --dry-run

# Paper trading
quicksand --config config.yaml --paper

# Live trading (use with caution)
quicksand --config config.yaml
```

## Architecture

```
quicksand/
├── main.py              # Entry point
├── config.py            # YAML config with pydantic validation
├── core/
│   ├── engine.py        # Async event loop orchestrator
│   ├── portfolio.py     # Position tracking and P&L
│   ├── order_manager.py # Order lifecycle (paper + live)
│   └── risk_manager.py  # Hard limits + circuit breaker
├── connectors/
│   └── exchange.py      # ccxt-based unified connector
└── strategies/
    ├── base.py          # Strategy interface
    └── funding_arb.py   # Funding rate arbitrage
```

## Risk Controls

- **Max position size**: 20% of equity per position
- **Max leverage**: 3x
- **Daily loss limit**: 2% — triggers circuit breaker
- **Max drawdown**: 10% — triggers circuit breaker
- Paper trading mode for testing before deploying real capital

## Supported Exchanges

Any exchange supported by [ccxt](https://github.com/ccxt/ccxt) (100+). Tested with:
- Binance
- Bybit
- OKX

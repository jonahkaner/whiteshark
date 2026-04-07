"""Entry point for the Quicksand trading bot."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from quicksand.config import load_config
from quicksand.core.engine import Engine
from quicksand.utils.logging import setup_logging, get_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="quicksand",
        description="Quicksand: Multi-strategy crypto trading bot",
    )
    parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        help="Path to config file (default: config.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Connect to exchanges, fetch balances, then exit",
    )
    parser.add_argument(
        "--paper",
        action="store_true",
        help="Force paper trading mode regardless of config",
    )
    return parser.parse_args()


async def dry_run(config) -> None:
    """Connect, fetch balances, print status, exit."""
    log = get_logger("dry_run")
    engine = Engine(config)

    log.info("connecting_exchanges")
    await engine._connect_exchanges()
    await engine._init_portfolio()

    log.info("dry_run_complete", **engine.portfolio.summary())

    for connector in engine.exchanges.values():
        await connector.close()


async def run(config) -> None:
    """Run the trading engine."""
    engine = Engine(config)
    try:
        await engine.start()
    except KeyboardInterrupt:
        pass
    finally:
        await engine.stop()


def main() -> None:
    args = parse_args()

    # Load config
    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print(f"Config file not found: {args.config}")
        print("Copy config.example.yaml to config.yaml and fill in your values.")
        sys.exit(1)

    # Force paper mode if requested
    if args.paper:
        config.mode = "paper"

    # Setup logging
    setup_logging(
        level=config.logging.level,
        log_file=config.logging.file,
    )

    log = get_logger("main")
    log.info("quicksand_starting", mode=config.mode, config=args.config)

    if args.dry_run:
        asyncio.run(dry_run(config))
    else:
        asyncio.run(run(config))


if __name__ == "__main__":
    main()

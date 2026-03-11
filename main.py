#!/usr/bin/env python3
"""Entry point for the Automated AP Agent.

Usage:
    python main.py              # Run all phases once
    python main.py --phase 1    # Run only intake phase
    python main.py --phase 2    # Run only approval check phase
    python main.py --phase 3    # Run only confirmation check phase
"""

import argparse
import logging
import sys

import yaml

from ap_agent.agent import APAgent


def load_config(config_path: str = "config.yaml") -> dict:
    """Load configuration from YAML file."""
    with open(config_path) as f:
        return yaml.safe_load(f)


def main():
    parser = argparse.ArgumentParser(description="Automated AP Agent")
    parser.add_argument(
        "--config", default="config.yaml", help="Path to config file"
    )
    parser.add_argument(
        "--phase", type=int, choices=[1, 2, 3],
        help="Run a specific phase only (1=intake, 2=approvals, 3=confirmations)"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging"
    )
    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config = load_config(args.config)
    agent = APAgent(config)

    if args.phase == 1:
        agent.phase_intake()
    elif args.phase == 2:
        agent.phase_check_approvals()
    elif args.phase == 3:
        agent.phase_check_confirmations()
    else:
        agent.run()


if __name__ == "__main__":
    main()

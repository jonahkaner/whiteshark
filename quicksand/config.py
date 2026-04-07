"""Configuration loader with validation."""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator


class ExchangeConfig(BaseModel):
    api_key: str = ""
    secret: str = ""
    sandbox: bool = True


class RiskConfig(BaseModel):
    max_position_pct: float = 0.20
    max_leverage: float = 3.0
    daily_loss_limit_pct: float = 0.02
    max_drawdown_pct: float = 0.10
    max_open_positions: int = 10


class FundingArbConfig(BaseModel):
    enabled: bool = True
    min_annualized_rate: float = 0.15
    min_volume_multiple: float = 10.0
    max_basis_drift_pct: float = 0.02
    check_interval_seconds: int = 300
    kelly_sizing: bool = True  # Use Kelly criterion for dynamic position sizing
    kelly_fraction: float = 0.5  # Half-Kelly (conservative)
    max_position_pct: float = 0.35  # Max per-position cap even with Kelly
    pairs: list[str] = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "ARB/USDT", "DOGE/USDT"]


class MarketMakerConfig(BaseModel):
    enabled: bool = False
    spread_multiplier: float = 1.5
    max_inventory_pct: float = 0.05
    levels: int = 3
    pairs: list[str] = []


class StrategiesConfig(BaseModel):
    funding_arb: FundingArbConfig = FundingArbConfig()
    market_maker: MarketMakerConfig = MarketMakerConfig()


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "logs/quicksand.log"


class TelegramConfig(BaseModel):
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""


class AlertsConfig(BaseModel):
    telegram: TelegramConfig = TelegramConfig()


class Config(BaseModel):
    mode: str = "paper"
    exchanges: dict[str, ExchangeConfig] = {}
    risk: RiskConfig = RiskConfig()
    strategies: StrategiesConfig = StrategiesConfig()
    logging: LoggingConfig = LoggingConfig()
    alerts: AlertsConfig = AlertsConfig()

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        if v not in ("paper", "live"):
            raise ValueError(f"mode must be 'paper' or 'live', got '{v}'")
        return v

    @property
    def is_paper(self) -> bool:
        return self.mode == "paper"


_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _resolve_env_vars(value: str) -> str:
    """Replace ${VAR_NAME} with environment variable values."""

    def replacer(match: re.Match) -> str:
        var_name = match.group(1)
        return os.environ.get(var_name, "")

    return _ENV_VAR_PATTERN.sub(replacer, value)


def _resolve_env_recursive(obj: object) -> object:
    """Walk a parsed YAML structure and resolve env vars in all strings."""
    if isinstance(obj, str):
        return _resolve_env_vars(obj)
    if isinstance(obj, dict):
        return {k: _resolve_env_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_recursive(item) for item in obj]
    return obj


def load_config(path: str | Path = "config.yaml") -> Config:
    """Load and validate configuration from a YAML file."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path) as f:
        raw = yaml.safe_load(f)

    resolved = _resolve_env_recursive(raw or {})
    return Config.model_validate(resolved)

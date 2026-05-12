from __future__ import annotations

from typing import List
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # LLM
    anthropic_api_key: str
    anthropic_model: str = "claude-sonnet-4-6"

    # Binance
    binance_api_key: str
    binance_api_secret: str
    binance_testnet: bool = True

    # Data
    fred_api_key: str = ""

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Universe & risk
    symbols: List[str] = Field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])
    quote_asset: str = "USDT"
    portfolio_notional: float = 10_000.0
    max_risk_pct: float = 0.02
    default_leverage: int = 3

    # Cadence (seconds)
    fetch_1m_seconds: int = 60
    fetch_5m_seconds: int = 300
    fetch_15m_seconds: int = 900
    sentiment_seconds: int = 600
    macro_seconds: int = 3600
    decision_seconds: int = 300

    # UI / logging
    streamlit_port: int = 8501
    log_level: str = "INFO"

    @field_validator("symbols", mode="before")
    @classmethod
    def _split_symbols(cls, v):
        if isinstance(v, str):
            return [s.strip().upper() for s in v.split(",") if s.strip()]
        return v


settings = Settings()

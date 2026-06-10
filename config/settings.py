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

    # LLM — per-agent model routing (token economy).
    # Quant + Sentiment are structured-extraction tasks → Haiku 4.5 ($1/$5 per
    # MTok). Master Trader is the capital-decision verifier → Opus-tier
    # quality ($5/$25). `anthropic_model` is the legacy/global fallback.
    anthropic_api_key: str
    anthropic_model: str = "claude-sonnet-4-6"
    anthropic_model_quant: str = "claude-haiku-4-5"
    anthropic_model_sentiment: str = "claude-haiku-4-5"
    anthropic_model_master: str = "claude-opus-4-8"

    # AI verification guardrails.
    # Master Trader LONG/SHORT below this confidence is downgraded to WAIT —
    # the AI layer is a verifier, not a signal generator; low-conviction
    # overrides are exactly the "trade to look productive" failure mode.
    ai_min_confidence: int = 65
    # Sentiment output is macro-wide (not symbol-specific); cache it in Redis
    # for this long so one cycle = at most one Sentiment LLM call.
    sentiment_cache_seconds: int = 1800

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

    # LLM cost budget — daily USD ceiling. Telegram fires once when crossed.
    # 0 disables the check entirely. Soft alarm only; the bot keeps running
    # until the user toggles markets off manually.
    llm_daily_budget_usd: float = 5.0

    # Portfolio-level risk gates (execution/portfolio_guard.py).
    # daily_loss_limit_pct: realized+theoretical PnL today below
    #   -(portfolio_notional × pct) blocks new entries until UTC midnight.
    #   0 disables. 3% ≈ 1.5–2 full-risk losses on the 2%/trade model.
    daily_loss_limit_pct: float = 0.03
    # Max simultaneous open exposures (unresolved signals + live trades).
    max_open_per_market: int = 3
    max_open_total: int = 8
    # Block same-direction re-entry after a stop-loss for a term-scaled window.
    sl_cooldown_enabled: bool = True

    @field_validator("symbols", mode="before")
    @classmethod
    def _split_symbols(cls, v):
        if isinstance(v, str):
            return [s.strip().upper() for s in v.split(",") if s.strip()]
        return v


settings = Settings()

"""Shared pytest fixtures.

Loads `.env` early via dotenv so settings.* doesn't blow up on missing
required vars (ANTHROPIC_API_KEY etc.) when running tests in isolation.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Make repo root importable regardless of where pytest is invoked from.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Provide dummy env vars so `config.settings` validates in test contexts.
# Production env files override these; tests only need the keys to exist.
_DEFAULTS = {
    "ANTHROPIC_API_KEY": "test-key",
    "BINANCE_API_KEY": "test",
    "BINANCE_API_SECRET": "test",
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
    "FRED_API_KEY": "",
    "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
    "REDIS_URL": "redis://localhost:6379/15",
}
for k, v in _DEFAULTS.items():
    os.environ.setdefault(k, v)

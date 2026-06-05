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

# Force dummy env vars so tests never hit live APIs or the production DB when
# they run inside the deployed container.
_TEST_ENV = {
    "ANTHROPIC_API_KEY": "test-key",
    "BINANCE_API_KEY": "test",
    "BINANCE_API_SECRET": "test",
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
    "FRED_API_KEY": "",
    "DATABASE_URL": "sqlite+aiosqlite:////tmp/traderay-pytest.db",
    "REDIS_URL": "redis://localhost:6379/15",
}
for k, v in _TEST_ENV.items():
    os.environ[k] = v

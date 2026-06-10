"""TradeRay — clean, bilingual (TR/EN) dashboard.

Design goals (final v1.0):
  - Sidebar: language picker, master switch, per-market controls (enabled,
    use_ai, term, execution_mode for crypto only, dynamic_screener).
  - PnL Matrix: per-market split (Crypto Bot / Crypto Signals / BIST / US),
    plus a Total card. Daily / Weekly / Monthly windows.
  - Tabs: Signals / Trades / Latest Decisions / API Costs.
  - Help: modal popup ("How to use") accessible from sidebar.

Defense-in-depth:
  - SQLA_DISABLE_POOL=true uses NullPool — every Streamlit rerun gets a fresh
    asyncpg connection, no cross-event-loop binding issues.
  - All db / redis ops wrapped in `_run_async` so cached calls share semantics.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import redis.asyncio as redis_asyncio
import streamlit as st
from sqlalchemy import select

from config import settings
from models import (
    AsyncSessionLocal,
    AuditCategory,
    AuditMode,
    AuditOutcome,
    DecisionAudit,
    ExecutionMode,
    LLMCostLog,
    MarketConfig,
    MarketType,
    Signal,
    SignalAction,
    Term,
    Trade,
    TradeStatus,
)


# ============================================================================
# Page setup
# ============================================================================

st.set_page_config(
    page_title="TradeRay — Global Financial Terminal",
    layout="wide",
    page_icon="📈",
    initial_sidebar_state="expanded",
)
# NOTE: removed the 30s meta-refresh — it nuked scroll position, modals, and
# tab state every half-minute. The page is now manually refreshed via the
# "🔄 Yenile" sidebar button (calls `st.rerun()`). Streamlit's natural rerun
# on any widget interaction is plenty for everyday use.


# ============================================================================
# i18n — single source of truth for all UI strings
# ============================================================================

_STRINGS: dict[str, dict[str, str]] = {
    # --- Header ---
    "app_title":           {"tr": "📈 TradeRay — Küresel Finans Terminali",
                            "en": "📈 TradeRay — Global Financial Terminal"},
    "as_of":               {"tr": "Güncelleme",   "en": "As of"},
    "auto_refresh":        {"tr": "manuel yenileme (kenar çubuğundan)",
                            "en": "manual refresh (sidebar button)"},
    "refresh_now":         {"tr": "🔄 Yenile",
                            "en": "🔄 Refresh"},
    # --- Sidebar ---
    "language":            {"tr": "🌐 Dil",       "en": "🌐 Language"},
    "system":              {"tr": "Sistem",       "en": "System"},
    "system_running":      {"tr": "🟢 ÇALIŞIYOR", "en": "🟢 RUNNING"},
    "system_paused":       {"tr": "🔴 DURDU",     "en": "🔴 PAUSED"},
    "master_switch":       {"tr": "Botu aç (master switch)",
                            "en": "Enable bot (master switch)"},
    "master_help":         {"tr": "Kapalıyken hiçbir döngü çalışmaz — LLM çağrısı yok, emir yok, jeton harcaması yok.",
                            "en": "When OFF, no cycles fire — no LLM calls, no orders, no token spend."},
    "market_controls":     {"tr": "⚙️ Piyasa Kontrolleri",
                            "en": "⚙️ Market Controls"},
    "enabled":             {"tr": "Aktif",        "en": "Enabled"},
    "use_ai":              {"tr": "AI Kullan",    "en": "Use AI"},
    "use_ai_help":         {"tr": "Açıkken kural motoru bir setup bulduğunda Master Trader LLM doğrular. Kapalıyken sadece kural motoru çalışır (jeton harcamaz).",
                            "en": "When ON, Master Trader LLM verifies any setup the rule engine finds. When OFF, only the rule engine runs (zero tokens)."},
    "term":                {"tr": "Zaman Dilimi", "en": "Term"},
    "execution":           {"tr": "Yürütme",      "en": "Execution"},
    "exec_locked":         {"tr": "🔒 Bu piyasa SADECE-SİNYAL (execution/engine.py'de sabit).",
                            "en": "🔒 This market is SIGNAL-ONLY (hard-locked in execution/engine.py)."},
    "dynamic_screener":    {"tr": "Dinamik tarayıcı",
                            "en": "Dynamic screener"},
    "screener_help":       {"tr": "Açıkken orkestratör her döngüde sembol listesini fetcher.get_dynamic_symbols() ile günceller.",
                            "en": "When ON, the orchestrator refreshes the symbol list each cycle via fetcher.get_dynamic_symbols()."},
    "symbols":             {"tr": "Semboller (virgülle)",
                            "en": "Symbols (comma-separated)"},
    "symbols_disabled":    {"tr": "Dinamik tarayıcı açıkken devre dışı.",
                            "en": "Disabled while dynamic screener is ON."},
    "last_run":            {"tr": "Son döngü",    "en": "Last run"},
    "apply":               {"tr": "Uygula",       "en": "Apply"},
    "updated":             {"tr": "güncellendi",  "en": "updated"},
    # --- Help dialog ---
    "help_button":         {"tr": "❓ Nasıl Kullanılır?", "en": "❓ How to use"},
    "help_title":          {"tr": "TradeRay — Hızlı Kullanım Kılavuzu",
                            "en": "TradeRay — Quick start guide"},
    # --- KPI strip ---
    "active_markets":      {"tr": "Aktif Piyasalar",     "en": "Active Markets"},
    "signals_30d":         {"tr": "Sinyaller (30g)",     "en": "Signals (30d)"},
    "non_wait":            {"tr": "İşlem Sinyali",       "en": "Actionable"},
    "resolved":            {"tr": "Çözüldü",             "en": "Resolved"},
    "open_trades":         {"tr": "Açık İşlem",          "en": "Open Trades"},
    "today_cost":          {"tr": "Bugünkü API Maliyeti","en": "Today's API Cost"},
    "calls":               {"tr": "çağrı",               "en": "calls"},
    # --- PnL Matrix ---
    "pnl_title":           {"tr": "💰 PnL Matrisi",      "en": "💰 PnL Matrix"},
    "pnl_caption":         {"tr": "Gerçekleşen PnL = Borsada kapanan Crypto AUTO_BOT trade'leri. "
                                  "Teorik PnL = Sinyallerin TP/SL'ye değme durumuna göre simülasyon. "
                                  "Genel toplam tüm borsaları birleştirir.",
                            "en": "Realized PnL = closed Crypto AUTO_BOT trades. "
                                  "Theoretical PnL = signals scored against TP/SL touches. "
                                  "Total combines all markets."},
    "crypto_bot":          {"tr": "🪙 Crypto Bot (Gerçek)", "en": "🪙 Crypto Bot (Realized)"},
    "crypto_signals":      {"tr": "📡 Crypto Sinyalleri",   "en": "📡 Crypto Signals"},
    "bist_signals":        {"tr": "🇹🇷 BIST Sinyalleri",    "en": "🇹🇷 BIST Signals"},
    "us_signals":          {"tr": "🇺🇸 ABD Sinyalleri",     "en": "🇺🇸 US Signals"},
    "total_pnl":           {"tr": "🎯 GENEL TOPLAM",        "en": "🎯 TOTAL"},
    "daily":               {"tr": "Günlük",   "en": "Daily"},
    "weekly":              {"tr": "Haftalık", "en": "Weekly"},
    "monthly":             {"tr": "Aylık",    "en": "Monthly"},
    "trades":              {"tr": "trade",    "en": "trades"},
    "win_rate":            {"tr": "kazanma oranı", "en": "win rate"},
    "open_n":              {"tr": "açık",     "en": "open"},
    "resolved_pnl":        {"tr": "kesinleşen", "en": "resolved"},
    "floating_mtm":        {"tr": "mtm",      "en": "MTM"},
    # --- Tabs ---
    "tab_perf":            {"tr": "📈 Performans",       "en": "📈 Performance"},
    "tab_perf_title":      {"tr": "### 📈 Sinyal Performansı — ne oldu, ne bitti (son 30 gün)",
                            "en": "### 📈 Signal Performance — what happened (last 30 days)"},
    "perf_open_title":     {"tr": "#### ⏳ Açık sinyaller — sonuç bekleniyor",
                            "en": "#### ⏳ Open signals — awaiting resolution"},
    "perf_closed_title":   {"tr": "#### ✅ Sonuçlanan sinyaller",
                            "en": "#### ✅ Resolved signals"},
    "perf_breakdown":      {"tr": "#### 🧮 Piyasa / vade kırılımı",
                            "en": "#### 🧮 Market / term breakdown"},
    "perf_no_signals":     {"tr": "Henüz LONG/SHORT sinyal yok. Sistem setup bulunca burada görünecek.",
                            "en": "No LONG/SHORT signals yet. They will appear here once the engine finds a setup."},
    "perf_no_open":        {"tr": "Şu an açık sinyal yok.", "en": "No open signals right now."},
    "perf_no_closed":      {"tr": "Henüz TP/SL'ye ulaşan sinyal yok — pozisyonlar hâlâ açık.",
                            "en": "No signal has hit TP/SL yet — positions still open."},
    "perf_help":           {"tr": "Bu sayfa SİNYAL bazlıdır: sen işlem açmasan da sistem her sinyali "
                                  "kendi planıyla (giriş/TP/SL) takip eder ve fiyat TP ya da SL'ye "
                                  "değdiğinde sonucu buraya yazar. 'Anlık K/Z' henüz kapanmamış "
                                  "sinyalin şu anki fiyata göre teorik kâr/zararıdır.",
                            "en": "This page is SIGNAL-based: even if you don't trade, the system tracks "
                                  "every signal against its own plan (entry/TP/SL) and records the outcome "
                                  "when price touches TP or SL. 'Floating PnL' is the theoretical PnL of a "
                                  "still-open signal at the current price."},
    "tab_signals":         {"tr": "📡 Sinyaller",        "en": "📡 Signals"},
    "tab_trades":          {"tr": "🪙 Trade'ler",         "en": "🪙 Trades"},
    "tab_decisions":       {"tr": "🧠 Son Kararlar",      "en": "🧠 Latest Decisions"},
    "tab_costs":           {"tr": "💰 API Maliyetleri",   "en": "💰 API Costs"},
    "tab_signals_title":   {"tr": "### 📡 Aktif Sinyaller (son 30 gün)",
                            "en": "### 📡 Active Signals (last 30 days)"},
    "filter_market":       {"tr": "Piyasa",   "en": "Market"},
    "filter_action":       {"tr": "Aksiyon",  "en": "Action"},
    "filter_resolution":   {"tr": "Çözüm",    "en": "Resolution"},
    "no_signals":          {"tr": "Bu filtreye uygun sinyal yok.",
                            "en": "No signals match this filter."},
    "tab_trades_title":    {"tr": "### 🪙 Gerçekleşmiş Crypto Trade'leri (son 90 gün)",
                            "en": "### 🪙 Executed Crypto Trades (last 90 days)"},
    "no_trades":           {"tr": "Henüz trade yok — Crypto AUTO_BOT moduna alınmalı.",
                            "en": "No trades yet — Crypto must be in AUTO_BOT mode."},
    "tab_decisions_title": {"tr": "### 🧠 Sembol Bazlı Son Kararlar",
                            "en": "### 🧠 Latest Decisions per Symbol"},
    "no_decisions":        {"tr": "Redis'te cache'lenmiş karar yok — ilk döngüyü bekle.",
                            "en": "No decisions cached in Redis yet — wait for the first cycle."},
    "tab_costs_title":     {"tr": "### 💰 LLM API Maliyetleri (bugün, UTC)",
                            "en": "### 💰 LLM API Costs (today, UTC)"},
    "no_costs":            {"tr": "Bugün hiç LLM çağrısı kaydedilmedi.",
                            "en": "No LLM calls logged yet today."},
    # --- Audit / Decision Trace ---
    "tab_audit":           {"tr": "🔍 Karar İzleyici",   "en": "🔍 Decision Trace"},
    "tab_audit_title":     {"tr": "### 🔍 Karar İzleyici — Sistemin neden sinyal attığı / atmadığı",
                            "en": "### 🔍 Decision Trace — why the system signaled (or didn't)"},
    "audit_caption":       {"tr": "Her sembol döngüsü için bir satır. `logic_trace` JSON'u indikatörler, kural motoru kararı, AI analizi (varsa) ve doğrulama adımlarının HEPSİNİ kapsar.",
                            "en": "One row per symbol cycle. The `logic_trace` JSON captures indicators, rule-engine verdict, AI analysis (if any), and validation steps in FULL."},
    "filter_outcome":      {"tr": "Sonuç",           "en": "Outcome"},
    "filter_mode":         {"tr": "Mod",             "en": "Mode"},
    "no_audits":           {"tr": "Bu filtreye uygun kayıt yok.",
                            "en": "No audit rows match this filter."},
    "audit_select":        {"tr": "Detay için bir satır seç",
                            "en": "Select a row to inspect"},
    "audit_view":          {"tr": "🔎 Detayı Aç / Logları Kopyala",
                            "en": "🔎 Open detail / Copy logs"},
    "audit_modal_title":   {"tr": "Karar Detayı — Tam Düşünce Zinciri",
                            "en": "Decision Detail — Full Reasoning Chain"},
    "modal_time":          {"tr": "Zaman",  "en": "Time"},
    "modal_category":      {"tr": "Kategori", "en": "Category"},
    "modal_mode":          {"tr": "Mod", "en": "Mode"},
    "modal_outcome":       {"tr": "Sonuç", "en": "Outcome"},
    "modal_reason":        {"tr": "Özet",  "en": "Reason"},
    "modal_indicators":    {"tr": "📊 İndikatörler", "en": "📊 Indicators"},
    "modal_rule_engine":   {"tr": "⚙️ Kural Motoru", "en": "⚙️ Rule Engine"},
    "modal_ai_analysis":   {"tr": "🤖 AI Analizi (ham)", "en": "🤖 AI Analysis (raw)"},
    "modal_no_ai":         {"tr": "_Bu döngüde AI çalışmadı (Rule-Only mod veya WAIT)._",
                            "en": "_AI did not run for this cycle (Rule-Only mode or WAIT)._"},
    "modal_validation":    {"tr": "🛡 Doğrulama (TP/SL/Risk)", "en": "🛡 Validation (TP/SL/Risk)"},
    "modal_execution":     {"tr": "🚀 Yürütme", "en": "🚀 Execution"},
    "modal_full_json":     {"tr": "📋 Tam JSON (kopyalanabilir)", "en": "📋 Full JSON (copyable)"},
    # Narrative section
    "narrative_title":     {"tr": "📖 Karar Hikayesi", "en": "📖 Decision Story"},
    "section_news":        {"tr": "📰 Sistem hangi haberleri okudu",
                            "en": "📰 What news the system read"},
    "section_macro":       {"tr": "🌍 Makro tablo",  "en": "🌍 Macro picture"},
    "section_microstructure": {"tr": "🪙 Mikro yapı (funding / OI / USDTRY)",
                                "en": "🪙 Microstructure (funding / OI / USDTRY)"},
    "section_ai_verdict":  {"tr": "🤖 AI'nın nihai kararı",
                            "en": "🤖 AI's final verdict"},
    "section_outcome":     {"tr": "🚀 Sonuç",  "en": "🚀 Outcome"},
    "show_raw_json":       {"tr": "🔧 Geliştirici görünümü (ham JSON)",
                            "en": "🔧 Developer view (raw JSON)"},
    "no_news":             {"tr": "_(Bu döngüde haber okunmadı — AI kapalıydı.)_",
                            "en": "_(No news read this cycle — AI was off.)_"},
    "no_macro":            {"tr": "_(Makro veriye dokunulmadı.)_",
                            "en": "_(Macro context not pulled.)_"},
    "no_micro":            {"tr": "_(Mikro yapı verisi yok.)_",
                            "en": "_(No microstructure data.)_"},
    # --- Decision card labels ---
    "justification":       {"tr": "Gerekçe",  "en": "Justification"},
    "chart_obs":           {"tr": "Grafik gözlemleri", "en": "Chart observations"},
    "rule_refs":           {"tr": "Kural referansları","en": "Rulebook references"},
    "conflict":            {"tr": "Çatışma uyarıları", "en": "Conflict flags"},
    "live_price":          {"tr": "Anlık Fiyat", "en": "Live Price"},
    "entry":               {"tr": "Giriş",       "en": "Entry"},
    "tp":                  {"tr": "Kâr Al",      "en": "Take-Profit"},
    "sl":                  {"tr": "Zarar Durdur","en": "Stop-Loss"},
    "rr":                  {"tr": "R:R",         "en": "R:R"},
    "risk":                {"tr": "Risk",        "en": "Risk"},
    "lev":                 {"tr": "Kaldıraç",    "en": "Leverage"},
    "source_rule":         {"tr": "⚙️ Kural motoru", "en": "⚙️ Rule engine"},
    "source_ai":           {"tr": "🤖 AI doğruladı", "en": "🤖 AI verified"},
    "view_full_json":      {"tr": "📋 Karar JSON", "en": "📋 Full decision JSON"},
    "view_quant":          {"tr": "📊 Quant raporu", "en": "📊 Quant report"},
    "view_sentiment":      {"tr": "📰 Duyarlılık raporu", "en": "📰 Sentiment report"},
}


def _lang() -> str:
    return st.session_state.get("lang", "tr")


def t(key: str) -> str:
    return _STRINGS.get(key, {}).get(_lang(), key)


# ============================================================================
# Async helpers
# ============================================================================

def _run_async(coro):
    return asyncio.run(coro)


# -- DB --------------------------------------------------------------------

async def _aload_market_configs() -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(select(MarketConfig))).scalars().all()
    return [
        {
            "market": r.market.value,
            "enabled": r.enabled,
            "use_ai": r.use_ai,
            "term": r.term.value,
            "execution_mode": r.execution_mode.value,
            "symbols_csv": r.symbols_csv,
            "last_run_at": r.last_run_at,
        }
        for r in rows
    ]


async def _asave_market_config(
    *, market: MarketType, enabled: bool, use_ai: bool, term: Term,
    execution_mode: ExecutionMode, symbols_csv: str,
) -> None:
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(MarketConfig).where(MarketConfig.market == market)
            )
        ).scalar_one_or_none()
        if row is None:
            row = MarketConfig(market=market)
            session.add(row)
        row.enabled = enabled
        row.use_ai = use_ai
        row.term = term
        row.execution_mode = execution_mode
        row.symbols_csv = symbols_csv
        await session.commit()


async def _aload_signals(*, days_back: int = 30) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(Signal).where(Signal.created_at >= cutoff).order_by(
                    Signal.created_at.desc()
                )
            )
        ).scalars().all()
    return [
        {
            "id": r.id,
            "created_at": r.created_at,
            "market": r.market.value,
            "term": r.term.value,
            "symbol": r.symbol,
            "action": r.action.value,
            "confidence": r.confidence,
            "entry": r.entry_price,
            "tp": r.take_profit,
            "sl": r.stop_loss,
            "risk_usd": r.risk_usd,
            "rr": r.reward_risk_ratio,
            "leverage": r.leverage,
            "quant_score": r.quant_score,
            "sentiment_score": r.sentiment_score,
            "macro_regime": r.macro_regime,
            "justification": r.justification,
            "raw_payload": r.raw_payload or {},
        }
        for r in rows
    ]


async def _aload_trades(*, days_back: int = 90) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(Trade).where(Trade.created_at >= cutoff).order_by(
                    Trade.created_at.desc()
                )
            )
        ).scalars().all()
    return [
        {
            "id": r.id,
            "created_at": r.created_at,
            "closed_at": r.closed_at,
            "symbol": r.symbol,
            "side": r.side,
            "entry": r.entry_price,
            "tp": r.take_profit,
            "sl": r.stop_loss,
            "qty": r.quantity_base,
            "leverage": r.leverage,
            "status": r.status.value,
            "realized_pnl_usd": r.realized_pnl_usd,
            "client_order_id": r.client_order_id,
        }
        for r in rows
    ]


async def _aload_audit_logs(
    *, days_back: int = 7, limit: int = 500
) -> list[dict[str, Any]]:
    """Most recent audit rows (newest first), capped at `limit`."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(DecisionAudit)
                .where(DecisionAudit.created_at >= cutoff)
                .order_by(DecisionAudit.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()
    return [
        {
            "id": r.id,
            "created_at": r.created_at,
            "category": r.category.value,
            "market": r.market.value,
            "symbol": r.symbol,
            "mode": r.mode.value,
            "outcome": r.outcome.value,
            "reason": r.reason,
            "logic_trace": r.logic_trace or {},
        }
        for r in rows
    ]


async def _aload_cost_logs(*, days_back: int = 7) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(LLMCostLog).where(LLMCostLog.created_at >= cutoff).order_by(
                    LLMCostLog.created_at.desc()
                )
            )
        ).scalars().all()
    return [
        {
            "id": r.id,
            "created_at": r.created_at,
            "market": r.market.value if r.market else None,
            "symbol": r.symbol,
            "agent_label": r.agent_label,
            "model": r.model,
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "estimated_cost_usd": r.estimated_cost_usd,
        }
        for r in rows
    ]


# -- Redis -----------------------------------------------------------------

async def _aload_redis_state() -> dict[str, Any]:
    r = redis_asyncio.from_url(settings.redis_url, decode_responses=True)
    out: dict[str, Any] = {"prices": {}, "decisions": {}, "screener_flags": {}}
    try:
        cfgs = await _aload_market_configs()
        for cfg in cfgs:
            for sym in [s.strip() for s in cfg["symbols_csv"].split(",") if s.strip()]:
                p = await r.get(f"price:{sym}")
                out["prices"][sym] = float(p) if p else None
                d = await r.get(f"decision:{sym}:latest")
                if d:
                    try:
                        out["decisions"][sym] = json.loads(d)
                    except json.JSONDecodeError:
                        pass
            flag = await r.get(f"config:{cfg['market']}:dynamic_screener")
            out["screener_flags"][cfg["market"]] = (flag == "1")

        # Also collect prices/decisions for any symbol that has a cached
        # decision but isn't in symbols_csv (screener-picked symbols)
        async for key in r.scan_iter("decision:*:latest"):
            sym = key.split(":")[1]
            if sym not in out["decisions"]:
                d = await r.get(key)
                if d:
                    try:
                        out["decisions"][sym] = json.loads(d)
                    except json.JSONDecodeError:
                        pass
            if sym not in out["prices"]:
                p = await r.get(f"price:{sym}")
                out["prices"][sym] = float(p) if p else None
    finally:
        await r.aclose()
    return out


async def _aset_screener_flag(market: str, value: bool) -> None:
    r = redis_asyncio.from_url(settings.redis_url, decode_responses=True)
    try:
        await r.set(f"config:{market}:dynamic_screener", "1" if value else "0")
    finally:
        await r.aclose()


async def _aread_system_enabled() -> bool:
    r = redis_asyncio.from_url(settings.redis_url, decode_responses=True)
    try:
        return (await r.get("config:system_enabled")) == "1"
    finally:
        await r.aclose()


async def _awrite_system_enabled(value: bool) -> None:
    r = redis_asyncio.from_url(settings.redis_url, decode_responses=True)
    try:
        await r.set("config:system_enabled", "1" if value else "0")
    finally:
        await r.aclose()


# -- Streamlit cache wrappers ----------------------------------------------

@st.cache_data(ttl=10)
def load_market_configs() -> list[dict[str, Any]]:
    return _run_async(_aload_market_configs())


@st.cache_data(ttl=10)
def load_signals(days_back: int = 30) -> list[dict[str, Any]]:
    return _run_async(_aload_signals(days_back=days_back))


@st.cache_data(ttl=10)
def load_trades(days_back: int = 90) -> list[dict[str, Any]]:
    return _run_async(_aload_trades(days_back=days_back))


@st.cache_data(ttl=10)
def load_redis_state() -> dict[str, Any]:
    return _run_async(_aload_redis_state())


@st.cache_data(ttl=10)
def load_cost_logs(days_back: int = 7) -> list[dict[str, Any]]:
    return _run_async(_aload_cost_logs(days_back=days_back))


@st.cache_data(ttl=10)
def load_audit_logs(days_back: int = 7, limit: int = 500) -> list[dict[str, Any]]:
    return _run_async(_aload_audit_logs(days_back=days_back, limit=limit))


@st.cache_data(ttl=5)
def load_system_enabled() -> bool:
    return _run_async(_aread_system_enabled())


# ============================================================================
# Resolution + PnL math
# ============================================================================

def _signal_resolution(signal: dict[str, Any]) -> dict[str, Any] | None:
    return (signal.get("raw_payload") or {}).get("resolution") or None


def _resolution_status(signal: dict[str, Any]) -> str:
    if signal["action"] == SignalAction.WAIT.value:
        return "—"
    res = _signal_resolution(signal)
    if not res:
        return "OPEN"
    return res.get("status", "OPEN")


def _bucket_window(now: datetime) -> dict[str, datetime]:
    today = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return {
        "daily":   today,
        "weekly":  today - timedelta(days=today.weekday()),
        "monthly": datetime(now.year, now.month, 1, tzinfo=timezone.utc),
    }


def compute_realized_crypto_pnl(trades: list[dict]) -> dict[str, dict[str, Any]]:
    """Realized PnL from CLOSED Trade rows (Crypto AUTO_BOT only)."""
    now = datetime.now(timezone.utc)
    windows = _bucket_window(now)
    closed = [
        t for t in trades
        if t["status"] == TradeStatus.CLOSED.value
        and t["realized_pnl_usd"] is not None
        and t["closed_at"] is not None
    ]
    out = {}
    for label, start in windows.items():
        slice_ = [t for t in closed if t["closed_at"] >= start]
        wins = [t for t in slice_ if (t["realized_pnl_usd"] or 0) > 0]
        losses = [t for t in slice_ if (t["realized_pnl_usd"] or 0) <= 0]
        total = sum(t["realized_pnl_usd"] or 0 for t in slice_)
        out[label] = {
            "n_trades": len(slice_),
            "n_wins": len(wins),
            "n_losses": len(losses),
            "win_rate": (len(wins) / len(slice_)) if slice_ else 0.0,
            "pnl_usd": total,
        }
    return out


def compute_theoretical_pnl_by_market(
    signals: list[dict], current_prices: dict[str, float | None],
    market_filter: set[str],
) -> dict[str, dict[str, Any]]:
    """Theoretical PnL aggregated across only the markets in `market_filter`.

    Resolution-first: prefer the tracker's `resolution` block. Fall back to
    a snapshot heuristic vs current price (floating MTM).
    """
    now = datetime.now(timezone.utc)
    windows = _bucket_window(now)
    out = {}
    for label, start in windows.items():
        slice_ = [
            s for s in signals
            if s["created_at"] >= start
            and s["market"] in market_filter
            and s["action"] in (SignalAction.LONG.value, SignalAction.SHORT.value)
            and s["entry"] is not None and s["sl"] is not None and s["tp"] is not None
            and s["risk_usd"] is not None and s["risk_usd"] > 0
        ]
        wins = losses = open_n = 0
        resolved_pnl = 0.0
        mtm = 0.0
        for s in slice_:
            res = _signal_resolution(s)
            if res:
                pnl = res.get("theoretical_pnl_usd") or 0.0
                resolved_pnl += pnl
                if res.get("outcome") == "TP":
                    wins += 1
                else:
                    losses += 1
                continue

            entry, sl, tp, risk = s["entry"], s["sl"], s["tp"], s["risk_usd"]
            current = current_prices.get(s["symbol"])
            risk_per_unit = abs(entry - sl)
            if risk_per_unit <= 0:
                continue
            size_base = risk / risk_per_unit
            is_long = s["action"] == SignalAction.LONG.value
            open_n += 1
            if current is None:
                continue
            mtm += (current - entry) * size_base if is_long else (entry - current) * size_base

        n_resolved = wins + losses
        out[label] = {
            "n_signals": len(slice_),
            "n_wins": wins,
            "n_losses": losses,
            "n_open": open_n,
            "win_rate": (wins / n_resolved) if n_resolved else 0.0,
            "pnl_resolved": resolved_pnl,
            "pnl_mtm": mtm,
            "pnl_total": resolved_pnl + mtm,
        }
    return out


# ============================================================================
# UI rendering
# ============================================================================

def _term_index(value: str) -> int:
    order = [Term.SCALP.value, Term.SHORT_TERM.value, Term.MID_TERM.value]
    return order.index(value) if value in order else 1


@st.dialog("TradeRay — Quick Start / Hızlı Başlangıç", width="large")
def show_help_dialog() -> None:
    """Bilingual modal explaining how to operate the dashboard."""
    if _lang() == "tr":
        st.markdown(
            """
            ### 🚀 Hızlı Başlangıç

            **1. Sistem'i Aç** — sidebar'da en üstteki master switch'i aç. Bu, scheduler'ı tetikler.

            **2. Bir piyasa seç** — CRYPTO / BIST / SP500 / NASDAQ. Açtığın piyasa için:
              - **Aktif**: piyasa döngüleri çalışsın
              - **AI Kullan**: Kapalı → sadece kural motoru (jeton harcamaz). Açık → Quant + Sentiment + Master Trader LLM doğrulaması.
              - **Dinamik tarayıcı**: sürekli açık tut. Piyasanın en yüksek hacim/volatilite sembollerini her döngüde otomatik tarar.
              - **Yürütme** (sadece Crypto): SADECE-SİNYAL veya OTO-BOT (testnet'e gerçek emir gönderir).

            **3. Uygula'ya bas** — değişiklikler bir sonraki döngüde geçerli olur.

            ### 💰 Jeton Tasarrufu

            - **Default**: tüm piyasalar kapalı, AI kapalı. Sıfır jeton harcaması.
            - **Rule-only mode**: AI kapalı → motor TA-Lib ile karar verir, LLM çağrısı yok.
            - **AI mode**: yalnızca kural motoru bir setup bulunca LLM çağrılır.

            ### 🛡 TP/SL Kuralı

            LONG ve SHORT sinyaller MUTLAKA TP ve SL içermeli. Yoksa engine direkt reddeder, DB'ye yazılmaz.
            """
        )
    else:
        st.markdown(
            """
            ### 🚀 Quick Start

            **1. Enable the System** — flip the master switch at the top of the sidebar.

            **2. Configure a market** — CRYPTO / BIST / SP500 / NASDAQ:
              - **Enabled**: cycles fire for this market
              - **Use AI**: OFF → pure rule engine (zero tokens). ON → Quant + Sentiment + Master Trader LLM verification.
              - **Dynamic screener**: keep ON. Picks the top USDT perps by 24h volume / top equity movers automatically.
              - **Execution** (Crypto only): SIGNAL-ONLY or AUTO-BOT (places real orders on testnet).

            **3. Apply** — changes take effect on the next cycle tick.

            ### 💰 Token Savings

            - **Default**: every market OFF, AI OFF. Zero token spend.
            - **Rule-only mode**: AI OFF → engine decides via TA-Lib, no LLM calls.
            - **AI mode**: LLM is called ONLY when the rule engine finds a setup.

            ### 🛡 TP/SL Rule

            LONG and SHORT signals MUST carry TP and SL. Otherwise the engine rejects outright — no DB row, no order.
            """
        )


def render_sidebar(
    configs: list[dict[str, Any]], screener_flags: dict[str, bool]
) -> None:
    # Language picker
    lang_choice = st.sidebar.selectbox(
        t("language"),
        options=["tr", "en"],
        format_func=lambda x: {"tr": "🇹🇷 Türkçe", "en": "🇬🇧 English"}[x],
        index=0 if _lang() == "tr" else 1,
        key="lang_picker",
    )
    if lang_choice != _lang():
        st.session_state["lang"] = lang_choice
        st.rerun()

    # Manual refresh button — replaces the removed 30s meta-refresh.
    # Streamlit cache is keyed by function args, so this rerun pulls fresh
    # DB / Redis data without server-side wholesale invalidation.
    if st.sidebar.button(t("refresh_now"), use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    # Help button → modal
    if st.sidebar.button(t("help_button"), use_container_width=True):
        show_help_dialog()

    st.sidebar.markdown("---")

    # Master switch
    sys_on = load_system_enabled()
    st.sidebar.markdown(
        f"### {t('system')}: **{t('system_running') if sys_on else t('system_paused')}**"
    )
    new_state = st.sidebar.toggle(
        t("master_switch"), value=sys_on,
        key="master_switch_toggle", help=t("master_help"),
    )
    if new_state != sys_on:
        _run_async(_awrite_system_enabled(new_state))
        st.cache_data.clear()
        st.rerun()

    st.sidebar.markdown("---")
    st.sidebar.markdown(f"### {t('market_controls')}")
    st.sidebar.caption(
        f"`{settings.anthropic_model}` · Binance "
        f"**{'Testnet' if settings.binance_testnet else 'LIVE'}**"
    )

    cfg_by_market = {c["market"]: c for c in configs}

    for market_value in (m.value for m in MarketType):
        cfg = cfg_by_market.get(market_value)
        if cfg is None:
            continue

        with st.sidebar.expander(
            f"🌐 {market_value}", expanded=(market_value == "CRYPTO")
        ):
            enabled = st.toggle(
                t("enabled"), value=cfg["enabled"], key=f"enabled:{market_value}",
            )
            use_ai = st.toggle(
                t("use_ai"), value=cfg["use_ai"],
                key=f"use_ai:{market_value}", help=t("use_ai_help"),
            )
            term_value = st.radio(
                t("term"),
                options=[Term.SCALP.value, Term.SHORT_TERM.value, Term.MID_TERM.value],
                index=_term_index(cfg["term"]),
                key=f"term:{market_value}",
            )

            if market_value == MarketType.CRYPTO.value:
                exec_mode = st.radio(
                    t("execution"),
                    options=[ExecutionMode.SIGNAL_ONLY.value, ExecutionMode.AUTO_BOT.value],
                    index=(0 if cfg["execution_mode"] == ExecutionMode.SIGNAL_ONLY.value else 1),
                    horizontal=True,
                    key=f"exec:{market_value}",
                )
            else:
                st.info(t("exec_locked"))
                exec_mode = ExecutionMode.SIGNAL_ONLY.value

            screener_on = st.toggle(
                t("dynamic_screener"),
                value=screener_flags.get(market_value, True),
                key=f"screener:{market_value}", help=t("screener_help"),
            )

            symbols_csv = st.text_input(
                t("symbols"),
                value=cfg["symbols_csv"],
                key=f"symbols:{market_value}",
                disabled=screener_on,
                help=t("symbols_disabled") if screener_on else "",
            )

            if cfg["last_run_at"]:
                st.caption(
                    f"{t('last_run')}: {cfg['last_run_at']:%Y-%m-%d %H:%M UTC}"
                )

            if st.button(t("apply"), key=f"apply:{market_value}", type="primary"):
                _run_async(
                    _asave_market_config(
                        market=MarketType(market_value),
                        enabled=enabled, use_ai=use_ai,
                        term=Term(term_value),
                        execution_mode=ExecutionMode(exec_mode),
                        symbols_csv=symbols_csv,
                    )
                )
                _run_async(_aset_screener_flag(market_value, screener_on))
                st.cache_data.clear()
                st.success(f"{market_value} {t('updated')}.")
                st.rerun()


def _pnl_card(title: str, stats: dict[str, Any], is_realized: bool) -> None:
    st.markdown(f"**{title}**")
    if is_realized:
        st.metric(
            "USD",
            f"${stats['pnl_usd']:,.2f}",
            delta=(
                f"{stats['n_wins']}W / {stats['n_losses']}L "
                f"({stats['win_rate']:.0%})"
            ),
        )
        st.caption(f"{stats['n_trades']} {t('trades')}")
    else:
        st.metric(
            "USD",
            f"${stats['pnl_total']:,.2f}",
            delta=(
                f"{stats['n_wins']}W / {stats['n_losses']}L · "
                f"{stats['n_open']} {t('open_n')}"
            ),
        )
        st.caption(
            f"{t('resolved_pnl')} ${stats['pnl_resolved']:,.2f} · "
            f"{t('floating_mtm')} ${stats['pnl_mtm']:,.2f}"
        )


def render_pnl_matrix(
    trades: list[dict], signals: list[dict],
    prices: dict[str, float | None],
) -> None:
    st.markdown(f"### {t('pnl_title')}")
    st.caption(t("pnl_caption"))

    # Daily window for the per-market cards (the hot view)
    realized = compute_realized_crypto_pnl(trades)["daily"]
    crypto_sig = compute_theoretical_pnl_by_market(
        signals, prices, {MarketType.CRYPTO.value}
    )["daily"]
    bist_sig = compute_theoretical_pnl_by_market(
        signals, prices, {MarketType.BIST.value}
    )["daily"]
    us_sig = compute_theoretical_pnl_by_market(
        signals, prices, {MarketType.SP500.value, MarketType.NASDAQ.value}
    )["daily"]

    total = (
        realized["pnl_usd"]
        + crypto_sig["pnl_total"]
        + bist_sig["pnl_total"]
        + us_sig["pnl_total"]
    )

    # 5 cards side by side: 4 markets + grand total
    cols = st.columns(5)
    with cols[0]:
        _pnl_card(t("crypto_bot"), realized, is_realized=True)
    with cols[1]:
        _pnl_card(t("crypto_signals"), crypto_sig, is_realized=False)
    with cols[2]:
        _pnl_card(t("bist_signals"), bist_sig, is_realized=False)
    with cols[3]:
        _pnl_card(t("us_signals"), us_sig, is_realized=False)
    with cols[4]:
        st.markdown(f"**{t('total_pnl')}**")
        st.metric("USD", f"${total:,.2f}")
        st.caption(t("daily"))

    # Compact weekly/monthly totals below
    realized_w = compute_realized_crypto_pnl(trades)
    crypto_w = compute_theoretical_pnl_by_market(
        signals, prices, {MarketType.CRYPTO.value}
    )
    bist_w = compute_theoretical_pnl_by_market(
        signals, prices, {MarketType.BIST.value}
    )
    us_w = compute_theoretical_pnl_by_market(
        signals, prices, {MarketType.SP500.value, MarketType.NASDAQ.value}
    )

    with st.expander(f"📊 {t('weekly')} / {t('monthly')}"):
        rows = []
        for window, label in (("daily", t("daily")), ("weekly", t("weekly")), ("monthly", t("monthly"))):
            total_w = (
                realized_w[window]["pnl_usd"]
                + crypto_w[window]["pnl_total"]
                + bist_w[window]["pnl_total"]
                + us_w[window]["pnl_total"]
            )
            rows.append({
                "Period": label,
                "Crypto Bot": f"${realized_w[window]['pnl_usd']:,.2f}",
                "Crypto Sig": f"${crypto_w[window]['pnl_total']:,.2f}",
                "BIST":       f"${bist_w[window]['pnl_total']:,.2f}",
                "US":         f"${us_w[window]['pnl_total']:,.2f}",
                "TOTAL":      f"${total_w:,.2f}",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def render_performance_tab(
    signals: list[dict], prices: dict[str, float | None],
) -> None:
    """Plain-language signal scoreboard: open positions, resolved outcomes,
    and a market×term breakdown. Built for monitoring without trading."""
    st.markdown(t("tab_perf_title"))
    st.caption(t("perf_help"))

    actionable = [
        s for s in signals
        if s["action"] in ("LONG", "SHORT")
        and s["entry"] is not None and s["tp"] is not None and s["sl"] is not None
    ]
    if not actionable:
        st.info(t("perf_no_signals"))
        return

    now = datetime.now(timezone.utc)
    open_rows, closed_rows = [], []
    total_resolved_pnl = 0.0
    total_mtm = 0.0
    wins = losses = 0

    for s in actionable:
        entry, tp, sl = s["entry"], s["tp"], s["sl"]
        risk = s["risk_usd"] or 0.0
        risk_per_unit = abs(entry - sl)
        size = (risk / risk_per_unit) if risk_per_unit > 0 else 0.0
        is_long = s["action"] == "LONG"
        res = _signal_resolution(s)

        if res:
            pnl = res.get("theoretical_pnl_usd") or 0.0
            total_resolved_pnl += pnl
            outcome = res.get("outcome")
            if outcome == "TP":
                wins += 1
            else:
                losses += 1
            resolved_at = res.get("resolved_at")
            try:
                dur_h = (
                    datetime.fromisoformat(str(resolved_at)) - s["created_at"]
                ).total_seconds() / 3600.0
            except (TypeError, ValueError):
                dur_h = None
            r_mult = (pnl / risk) if risk > 0 else None
            closed_rows.append({
                "Tarih": s["created_at"].strftime("%m-%d %H:%M"),
                "Piyasa": s["market"],
                "Sembol": s["symbol"],
                "Yön": "⬆ LONG" if is_long else "⬇ SHORT",
                "Sonuç": "✅ TP (kâr)" if outcome == "TP" else "🛑 SL (zarar)",
                "Giriş": round(entry, 4),
                "Çıkış": round(res.get("exit_price") or 0, 4),
                "K/Z (USD)": round(pnl, 2),
                "R": round(r_mult, 2) if r_mult is not None else None,
                "Süre (saat)": round(dur_h, 1) if dur_h is not None else None,
            })
        else:
            current = prices.get(s["symbol"])
            mtm = None
            if current is not None and size > 0:
                mtm = (current - entry) * size if is_long else (entry - current) * size
                total_mtm += mtm
            tp_dist = abs(tp - (current or entry)) / (current or entry) * 100
            sl_dist = abs((current or entry) - sl) / (current or entry) * 100
            age_h = (now - s["created_at"]).total_seconds() / 3600.0
            open_rows.append({
                "Tarih": s["created_at"].strftime("%m-%d %H:%M"),
                "Piyasa": s["market"],
                "Sembol": s["symbol"],
                "Yön": "⬆ LONG" if is_long else "⬇ SHORT",
                "Giriş": round(entry, 4),
                "Şu an": round(current, 4) if current is not None else None,
                "Anlık K/Z (USD)": round(mtm, 2) if mtm is not None else None,
                "TP'ye uzaklık %": round(tp_dist, 2),
                "SL'ye uzaklık %": round(sl_dist, 2),
                "Yaş (saat)": round(age_h, 1),
            })

    # Headline scoreboard
    n_resolved = wins + losses
    win_rate = (wins / n_resolved) if n_resolved else 0.0
    k = st.columns(6)
    k[0].metric("Toplam sinyal" if _lang() == "tr" else "Total signals", len(actionable))
    k[1].metric("Açık" if _lang() == "tr" else "Open", len(open_rows))
    k[2].metric("✅ TP", wins)
    k[3].metric("🛑 SL", losses)
    k[4].metric(
        "Kazanma oranı" if _lang() == "tr" else "Win rate",
        f"{win_rate:.0%}" if n_resolved else "—",
    )
    k[5].metric(
        "Toplam K/Z (USD)" if _lang() == "tr" else "Total PnL (USD)",
        f"${total_resolved_pnl + total_mtm:,.2f}",
        delta=(
            f"kapanan ${total_resolved_pnl:,.2f} + açık ${total_mtm:,.2f}"
            if _lang() == "tr"
            else f"closed ${total_resolved_pnl:,.2f} + floating ${total_mtm:,.2f}"
        ),
        delta_color="off",
    )

    st.markdown(t("perf_open_title"))
    if open_rows:
        st.dataframe(pd.DataFrame(open_rows), use_container_width=True, hide_index=True)
    else:
        st.info(t("perf_no_open"))

    st.markdown(t("perf_closed_title"))
    if closed_rows:
        st.dataframe(pd.DataFrame(closed_rows), use_container_width=True, hide_index=True)
    else:
        st.info(t("perf_no_closed"))

    # Market × term breakdown
    st.markdown(t("perf_breakdown"))
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for s in actionable:
        key = (s["market"], s["term"])
        g = groups.setdefault(key, {"n": 0, "tp": 0, "sl": 0, "pnl": 0.0})
        g["n"] += 1
        res = _signal_resolution(s)
        if res:
            g["pnl"] += res.get("theoretical_pnl_usd") or 0.0
            if res.get("outcome") == "TP":
                g["tp"] += 1
            else:
                g["sl"] += 1
    rows = []
    for (mkt, term), g in sorted(groups.items()):
        n_res = g["tp"] + g["sl"]
        rows.append({
            "Piyasa": mkt,
            "Vade": term,
            "Sinyal": g["n"],
            "Açık": g["n"] - n_res,
            "✅ TP": g["tp"],
            "🛑 SL": g["sl"],
            "Kazanma %": f"{g['tp'] / n_res:.0%}" if n_res else "—",
            "Kapanan K/Z (USD)": round(g["pnl"], 2),
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def render_signals_tab(signals: list[dict]) -> None:
    st.markdown(t("tab_signals_title"))
    fcols = st.columns([1, 1, 1, 2])
    with fcols[0]:
        market_filter = st.selectbox(
            t("filter_market"),
            options=["ALL"] + [m.value for m in MarketType], index=0,
        )
    with fcols[1]:
        action_filter = st.selectbox(
            t("filter_action"),
            options=["ALL", "LONG", "SHORT", "WAIT"], index=0,
        )
    with fcols[2]:
        resolution_filter = st.selectbox(
            t("filter_resolution"),
            options=["ALL", "OPEN", "RESOLVED_TP", "RESOLVED_SL"], index=0,
        )

    rows = signals
    if market_filter != "ALL":
        rows = [s for s in rows if s["market"] == market_filter]
    if action_filter != "ALL":
        rows = [s for s in rows if s["action"] == action_filter]
    if resolution_filter != "ALL":
        rows = [s for s in rows if _resolution_status(s) == resolution_filter]

    if not rows:
        st.info(t("no_signals"))
        return

    enriched = []
    for s in rows:
        res = _signal_resolution(s)
        enriched.append({
            **s,
            "resolution": _resolution_status(s),
            "theoretical_pnl_usd": (res or {}).get("theoretical_pnl_usd"),
            "exit_price": (res or {}).get("exit_price"),
        })
    df = pd.DataFrame(enriched)
    df["created_at"] = pd.to_datetime(df["created_at"]).dt.strftime("%Y-%m-%d %H:%M")
    cols = [
        "created_at", "market", "term", "symbol", "action", "resolution",
        "confidence", "entry", "tp", "sl", "exit_price", "rr",
        "theoretical_pnl_usd", "risk_usd",
    ]
    cols = [c for c in cols if c in df.columns]
    st.dataframe(df[cols], use_container_width=True, hide_index=True)


def render_trades_tab(trades: list[dict]) -> None:
    st.markdown(t("tab_trades_title"))
    if not trades:
        st.info(t("no_trades"))
        return
    df = pd.DataFrame(trades)
    df["created_at"] = pd.to_datetime(df["created_at"]).dt.strftime("%Y-%m-%d %H:%M")
    if "closed_at" in df.columns:
        df["closed_at"] = pd.to_datetime(df["closed_at"]).dt.strftime("%Y-%m-%d %H:%M")
    cols = ["created_at", "closed_at", "symbol", "side", "entry", "tp", "sl",
            "qty", "leverage", "status", "realized_pnl_usd", "client_order_id"]
    cols = [c for c in cols if c in df.columns]
    st.dataframe(df[cols], use_container_width=True, hide_index=True)


def render_decisions_tab(
    decisions: dict[str, dict], prices: dict[str, float | None]
) -> None:
    st.markdown(t("tab_decisions_title"))
    if not decisions:
        st.info(t("no_decisions"))
        return

    items = sorted(
        decisions.items(),
        key=lambda kv: kv[1].get("produced_at", ""),
        reverse=True,
    )

    for symbol, bundle in items:
        d = bundle.get("decision", {})
        action = d.get("decision", "—")
        confidence = d.get("confidence_level", d.get("confidence", 0)) or 0
        market = bundle.get("market", "?")
        term = bundle.get("term", "?")
        produced = bundle.get("produced_at", "")
        price = prices.get(symbol)
        pending = bundle.get("pending_order")
        source = d.get("source") or bundle.get("decision", {}).get("source") or "?"

        color = {
            "LONG": "🟢", "SHORT": "🔴", "WAIT": "🟡", "CANCEL_PENDING": "🟣",
        }.get(action, "⚪")
        source_badge = t("source_ai") if source == "ai_verified" else t("source_rule")
        header = (
            f"{color} {symbol} · {market} · {term} · {action} "
            f"({int(confidence)}%) · {source_badge} · {produced[:16]}"
        )
        if pending:
            header += " · 📌"

        with st.expander(
            header, expanded=(action not in ("WAIT", "CANCEL_PENDING"))
        ):
            top = st.columns([2, 1])
            with top[0]:
                st.markdown(
                    f"**{t('justification')}:** "
                    f"{d.get('justification') or d.get('rationale') or '—'}"
                )
                if d.get("chart_observations"):
                    st.markdown(f"**{t('chart_obs')}:**")
                    for obs in d["chart_observations"]:
                        st.markdown(f"- {obs}")
                if d.get("rulebook_references"):
                    st.markdown(
                        f"**{t('rule_refs')}:** " + ", ".join(d["rulebook_references"])
                    )
                if d.get("conflict_flags"):
                    st.warning(f"{t('conflict')}: " + ", ".join(d["conflict_flags"]))

            with top[1]:
                st.metric(t("live_price"), f"{price:,.2f}" if price else "—")
                if action in ("LONG", "SHORT"):
                    st.metric(t("entry"), f"{d.get('entry_price') or d.get('entry'):,.2f}")
                    st.metric(t("tp"), f"{d.get('take_profit'):,.2f}")
                    st.metric(t("sl"), f"{d.get('stop_loss'):,.2f}")
                    st.caption(
                        f"{t('rr')} {d.get('reward_risk_ratio', 0):.2f} · "
                        f"{t('risk')} ${d.get('risk_usd', 0):,.2f} · "
                        f"{t('lev')} {d.get('leverage', 1)}x"
                    )

            sub = st.columns(3)
            with sub[0]:
                if st.button(t("view_full_json"), key=f"json:{symbol}"):
                    st.code(json.dumps(d, indent=2, default=str), language="json")
            with sub[1]:
                if st.button(t("view_quant"), key=f"quant:{symbol}"):
                    st.json(bundle.get("quant") or {"info": "rule-only cycle (no AI)"})
            with sub[2]:
                if st.button(t("view_sentiment"), key=f"sent:{symbol}"):
                    st.json(bundle.get("sentiment") or {"info": "rule-only cycle (no AI)"})


def _humanize_indicators(inds: dict) -> list[str]:
    """RSI/MACD/EMA/ATR numerical values → bilingual readable bullets."""
    if not inds:
        return []
    rsi = inds.get("rsi")
    macd_h = inds.get("macd_hist")
    above_ema = inds.get("above_ema_slow")
    atr_pct = inds.get("atr_pct")
    last = inds.get("last_close")
    primary = inds.get("primary_interval", "?")

    out: list[str] = []
    if rsi is not None:
        if _lang() == "tr":
            zone = (
                "aşırı satım" if rsi < 30 else
                "satım bölgesi" if rsi < 40 else
                "nötr" if rsi < 60 else
                "alım bölgesi" if rsi < 70 else
                "aşırı alım"
            )
            out.append(f"**RSI**: `{rsi:.1f}` ({zone})")
        else:
            zone = (
                "extreme oversold" if rsi < 30 else
                "oversold zone" if rsi < 40 else
                "neutral" if rsi < 60 else
                "overbought zone" if rsi < 70 else
                "extreme overbought"
            )
            out.append(f"**RSI**: `{rsi:.1f}` ({zone})")
    if macd_h is not None:
        if _lang() == "tr":
            direction = "pozitif" if macd_h > 0 else "negatif" if macd_h < 0 else "nötr"
            out.append(f"**MACD histogram**: `{macd_h:+.4f}` ({direction})")
        else:
            direction = "positive" if macd_h > 0 else "negative" if macd_h < 0 else "flat"
            out.append(f"**MACD histogram**: `{macd_h:+.4f}` ({direction})")
    if above_ema is not None and last is not None:
        if _lang() == "tr":
            stance = "EMA yavaşın ÜZERİNDE (yükseliş filtresi)" if above_ema else "EMA yavaşın ALTINDA (düşüş filtresi)"
            out.append(f"**Fiyat / EMA**: `{last:,.4f}` — {stance}")
        else:
            stance = "ABOVE slow EMA (uptrend filter)" if above_ema else "BELOW slow EMA (downtrend filter)"
            out.append(f"**Price / EMA**: `{last:,.4f}` — {stance}")
    if atr_pct is not None:
        if _lang() == "tr":
            regime = (
                "düşük" if atr_pct < 0.005 else
                "normal" if atr_pct < 0.015 else
                "yüksek" if atr_pct < 0.030 else
                "ekstrem"
            )
            out.append(f"**Volatilite (ATR/fiyat)**: `{atr_pct:.2%}` ({regime})")
        else:
            regime = (
                "low" if atr_pct < 0.005 else
                "normal" if atr_pct < 0.015 else
                "elevated" if atr_pct < 0.030 else
                "extreme"
            )
            out.append(f"**Volatility (ATR/price)**: `{atr_pct:.2%}` ({regime})")
    out.append(
        f"_({_lang() and 'birincil grafik' if _lang()=='tr' else 'primary chart'}: `{primary}`)_"
    )
    return out


def _outcome_emoji(outcome: str) -> str:
    return {
        "EXECUTED": "✅",
        "SIGNAL_SENT": "📡",
        "WAITED": "⏸",
        "REJECTED": "❌",
        "ERROR": "💥",
    }.get(outcome, "•")


def _decision_headline(row: dict) -> str:
    """Bilingual one-liner: 'BTCUSDT için CRYPTO Bot WAITED — neden'."""
    emo = _outcome_emoji(row["outcome"])
    if _lang() == "tr":
        outcome_word = {
            "EXECUTED": "EMİR GÖNDERİLDİ",
            "SIGNAL_SENT": "SİNYAL ÜRETİLDİ",
            "WAITED": "BEKLEDİ",
            "REJECTED": "REDDEDİLDİ",
            "ERROR": "HATA",
        }.get(row["outcome"], row["outcome"])
        mode_word = "AI ile" if row["mode"] == "AI_ENABLED" else "Sadece Kural Motoru"
        return f"{emo} **{row['symbol']}** · {row['market']} · {mode_word} → **{outcome_word}**"
    outcome_word = {
        "EXECUTED": "ORDER PLACED",
        "SIGNAL_SENT": "SIGNAL LOGGED",
        "WAITED": "WAITED",
        "REJECTED": "REJECTED",
        "ERROR": "ERROR",
    }.get(row["outcome"], row["outcome"])
    mode_word = "AI verified" if row["mode"] == "AI_ENABLED" else "Rule engine only"
    return f"{emo} **{row['symbol']}** · {row['market']} · {mode_word} → **{outcome_word}**"


def _humanize_macro(macro: dict) -> list[str]:
    """FRED-style macro snapshot → bilingual readable bullets."""
    if not macro or not macro.get("available", True):
        return []
    out: list[str] = []
    vix = macro.get("vix")
    t10y2y = macro.get("yield_curve_10y2y")
    dxy = macro.get("dxy")
    dff = macro.get("fed_funds_rate")
    dgs10 = macro.get("us_10y_treasury")

    if vix is not None:
        if _lang() == "tr":
            tone = "düşük (sakin)" if vix < 15 else "normal" if vix < 20 else "yüksek (gergin)" if vix < 30 else "kriz seviyesi"
            out.append(f"**VIX**: `{vix:.1f}` ({tone})")
        else:
            tone = "low (calm)" if vix < 15 else "normal" if vix < 20 else "elevated (anxious)" if vix < 30 else "crisis"
            out.append(f"**VIX**: `{vix:.1f}` ({tone})")
    if t10y2y is not None:
        if _lang() == "tr":
            tone = "TERS (resesyon sinyali)" if t10y2y < 0 else "düz" if t10y2y < 0.3 else "dik (sağlıklı)"
            out.append(f"**10Y-2Y verim eğrisi**: `{t10y2y:+.2f}` ({tone})")
        else:
            tone = "INVERTED (recession signal)" if t10y2y < 0 else "flat" if t10y2y < 0.3 else "steep (healthy)"
            out.append(f"**10Y-2Y yield curve**: `{t10y2y:+.2f}` ({tone})")
    if dxy is not None:
        out.append(f"**DXY (USD endeksi)**: `{dxy:.2f}`" if _lang() == "tr"
                   else f"**DXY (USD index)**: `{dxy:.2f}`")
    if dff is not None:
        out.append(f"**Fed Funds**: `{dff:.2f}%`")
    if dgs10 is not None:
        out.append(f"**US 10Y**: `{dgs10:.2f}%`")
    return out


@st.dialog("🔍 Decision Trace Detail", width="large")
def show_audit_detail(row: dict) -> None:
    """Narrative-first modal:
       1) Headline + reason
       2) Indicator readout in plain language
       3) News read this cycle (AI mode)
       4) Macro picture
       5) Microstructure
       6) AI verdict (Master Trader's justification sentence)
       7) Outcome
       8) Full raw JSON at the bottom (for devs / copy-paste).
    """
    trace = row["logic_trace"] or {}

    # ── Header ───────────────────────────────────────────────────────
    st.markdown(f"## {t('narrative_title')}")
    st.markdown(_decision_headline(row))
    st.caption(
        f"{row['created_at']:%Y-%m-%d %H:%M:%S} UTC · "
        f"{t('modal_category')}: `{row['category']}` · "
        f"{t('modal_mode')}: `{row['mode']}` · "
        f"{t('term').lower()}: `{trace.get('indicators', {}).get('primary_interval', '?')}`"
    )
    st.info(f"**{t('modal_reason')}:** {row['reason'] or '—'}")

    # ── Rule engine verdict (one sentence) ───────────────────────────
    rule = trace.get("rule_engine") or {}
    if rule:
        st.markdown(f"### {t('modal_rule_engine')}")
        decision = rule.get("decision", "?")
        confidence = rule.get("confidence", 0)
        emoji = {"LONG": "🟢", "SHORT": "🔴", "WAIT": "🟡"}.get(decision, "⚪")
        if _lang() == "tr":
            st.markdown(
                f"{emoji} **Karar:** `{decision}` · **Güven:** `{confidence}/100`"
            )
        else:
            st.markdown(
                f"{emoji} **Decision:** `{decision}` · **Confidence:** `{confidence}/100`"
            )
        if rule.get("justification"):
            st.write(rule["justification"])
        if decision in ("LONG", "SHORT"):
            c1, c2, c3, c4 = st.columns(4)
            c1.metric(t("entry"), f"{rule.get('entry'):,.4f}" if rule.get("entry") else "—")
            c2.metric(t("tp"),    f"{rule.get('tp'):,.4f}" if rule.get("tp") else "—")
            c3.metric(t("sl"),    f"{rule.get('sl'):,.4f}" if rule.get("sl") else "—")
            c4.metric(t("rr"),    f"{rule.get('rr'):.2f}" if rule.get("rr") else "—")

    # ── Indicators in plain language ─────────────────────────────────
    st.markdown(f"### {t('modal_indicators')}")
    inds = trace.get("indicators") or {}
    bullets = _humanize_indicators(inds)
    if bullets:
        for b in bullets:
            st.markdown(f"- {b}")
    else:
        st.caption("—")

    # ── News (AI mode) ───────────────────────────────────────────────
    ai = trace.get("ai_analysis")
    st.markdown(f"### {t('section_news')}")
    if not ai:
        st.markdown(t("no_news"))
    else:
        sent = ai.get("sentiment_report") or {}
        catalysts = sent.get("news_catalysts") or []
        if catalysts:
            for c in catalysts:
                impact = c.get("impact_tier", "?")
                direction = c.get("direction", "?")
                regime = "🔥" if c.get("is_regime_shifting") else "•"
                arrow = {"bullish": "🟢", "bearish": "🔴"}.get(direction, "⚪")
                st.markdown(
                    f"- {regime} {arrow} `[{impact}]` "
                    f"**{c.get('headline', '—')}**"
                )
        else:
            st.markdown(t("no_news"))

    # ── Macro picture ────────────────────────────────────────────────
    st.markdown(f"### {t('section_macro')}")
    macro_data: dict = {}
    if ai:
        sent = ai.get("sentiment_report") or {}
        # Sentiment'in macro_regime / fear_greed_label oradan gelir
        fg = sent.get("fear_greed_label")
        regime = sent.get("macro_regime")
        if fg or regime:
            if _lang() == "tr":
                tr_regime = {
                    "risk_on": "RİSK-ON (büyüme dostu)",
                    "risk_off": "RİSK-OFF (savunmacı)",
                    "neutral": "nötr",
                }.get(regime, regime)
                tr_fg = {
                    "extreme_fear": "aşırı korku",
                    "fear": "korku",
                    "neutral": "nötr",
                    "greed": "açgözlülük",
                    "extreme_greed": "aşırı açgözlülük",
                }.get(fg, fg)
                st.markdown(
                    f"**Genel rejim**: `{tr_regime}` · **Korku/Açgözlülük**: `{tr_fg}`"
                )
            else:
                st.markdown(
                    f"**Regime**: `{regime}` · **Fear/Greed**: `{fg}`"
                )
        # Macro drivers from the sentiment_report
        drivers = sent.get("macro_drivers") or []
        if drivers:
            for d in drivers:
                arrow = "🟢" if d.get("direction") == "risk_on" else "🔴"
                st.markdown(
                    f"- {arrow} **{d.get('factor', '?')}**: "
                    f"`{d.get('value')}` ({d.get('direction')})"
                )
    if not ai:
        st.markdown(t("no_macro"))

    # ── Microstructure ───────────────────────────────────────────────
    st.markdown(f"### {t('section_microstructure')}")
    if ai and ai.get("microstructure"):
        micro = ai["microstructure"]
        funding = micro.get("funding_rate")
        oi = micro.get("open_interest")
        usdtry = micro.get("usdtry")
        if funding:
            st.markdown(
                f"- **Funding rate**: `{funding.get('funding_rate', 0):+.4%}` "
                f"(yıllık ≈ `{funding.get('annualized_pct', 0):+.1f}%`)"
            )
        if oi:
            st.markdown(
                f"- **Açık pozisyon (OI)**: `{oi.get('open_interest_base', 0):,.2f}` "
                f"(USD karşılığı `${oi.get('open_interest_usd', 0):,.0f}`)"
            )
        if usdtry:
            pct = usdtry.get("pct_change_1d", 0) * 100
            st.markdown(
                f"- **USDTRY**: `{usdtry.get('rate', 0):.4f}` "
                f"(günlük değişim `{pct:+.2f}%`)"
            )
        if not (funding or oi or usdtry):
            st.markdown(t("no_micro"))
    else:
        st.markdown(t("no_micro"))

    # ── AI verdict ───────────────────────────────────────────────────
    st.markdown(f"### {t('section_ai_verdict')}")
    if not ai:
        st.markdown(t("modal_no_ai"))
    else:
        master = ai.get("master_decision") or {}
        if master:
            d = master.get("decision", "?")
            conf = master.get("confidence_level", 0)
            emoji = {"LONG": "🟢", "SHORT": "🔴", "WAIT": "🟡", "CANCEL_PENDING": "🟣"}.get(d, "⚪")
            st.markdown(f"{emoji} **{d}** · {conf}/100")
            if master.get("justification"):
                # Bu "AI'nın nihai karar cümlesi" — özellikle kırpılmaz
                st.write(master["justification"])
            if master.get("chart_observations"):
                st.caption(
                    "🖼 " + " · ".join(master["chart_observations"][:5])
                )
            if master.get("conflict_flags"):
                st.warning("⚠️ " + ", ".join(master["conflict_flags"]))
        else:
            st.caption("Master Trader yanıtı yok / no Master Trader response")

    # ── Final outcome ────────────────────────────────────────────────
    st.markdown(f"### {t('section_outcome')}")
    exec_info = trace.get("execution") or {}
    val = trace.get("validation") or {}
    if val.get("decision") in ("LONG", "SHORT") and val.get("has_complete_plan"):
        if _lang() == "tr":
            st.markdown(
                f"Plan: **{val['decision']}** giriş `{val.get('entry'):,.4f}`, "
                f"TP `{val.get('take_profit'):,.4f}`, SL `{val.get('stop_loss'):,.4f}`, "
                f"R:R `{val.get('reward_risk_ratio'):.2f}`, Risk `${val.get('risk_usd', 0):,.2f}`"
            )
        else:
            st.markdown(
                f"Plan: **{val['decision']}** entry `{val.get('entry'):,.4f}`, "
                f"TP `{val.get('take_profit'):,.4f}`, SL `{val.get('stop_loss'):,.4f}`, "
                f"R:R `{val.get('reward_risk_ratio'):.2f}`, risk `${val.get('risk_usd', 0):,.2f}`"
            )
    if _lang() == "tr":
        st.markdown(
            f"Yürütme modu: `{exec_info.get('mode_applied')}` (istenen "
            f"`{exec_info.get('mode_requested')}`) — "
            f"emir gönderildi mi: **{'Evet' if exec_info.get('executed') else 'Hayır'}**"
        )
    else:
        st.markdown(
            f"Execution mode: `{exec_info.get('mode_applied')}` (requested "
            f"`{exec_info.get('mode_requested')}`) — "
            f"order placed: **{'Yes' if exec_info.get('executed') else 'No'}**"
        )
    if exec_info.get("route_reason"):
        st.caption(f"_engine.route reason: `{exec_info['route_reason']}`_")

    # ── Raw JSON at the very bottom ──────────────────────────────────
    st.divider()
    with st.expander(t("show_raw_json"), expanded=False):
        full_blob = {
            "id": row["id"],
            "created_at": row["created_at"].isoformat(),
            "category": row["category"],
            "market": row["market"],
            "symbol": row["symbol"],
            "mode": row["mode"],
            "outcome": row["outcome"],
            "reason": row["reason"],
            "logic_trace": trace,
        }
        st.code(json.dumps(full_blob, indent=2, default=str), language="json")


def render_audit_tab(audit_logs: list[dict]) -> None:
    """Decision Trace tab — table + per-row detail modal."""
    st.markdown(t("tab_audit_title"))
    st.caption(t("audit_caption"))

    # Filter row
    fcols = st.columns([1, 1, 1, 1])
    with fcols[0]:
        market_filter = st.selectbox(
            t("filter_market"),
            options=["ALL"] + [m.value for m in MarketType], index=0,
            key="audit_market_filter",
        )
    with fcols[1]:
        outcome_filter = st.selectbox(
            t("filter_outcome"),
            options=["ALL"] + [o.value for o in AuditOutcome], index=0,
            key="audit_outcome_filter",
        )
    with fcols[2]:
        mode_filter = st.selectbox(
            t("filter_mode"),
            options=["ALL"] + [m.value for m in AuditMode], index=0,
            key="audit_mode_filter",
        )
    with fcols[3]:
        category_filter = st.selectbox(
            t("filter_market") + " (cat)",
            options=["ALL"] + [c.value for c in AuditCategory], index=0,
            key="audit_category_filter",
            label_visibility="visible",
        )

    rows = audit_logs
    if market_filter != "ALL":
        rows = [r for r in rows if r["market"] == market_filter]
    if outcome_filter != "ALL":
        rows = [r for r in rows if r["outcome"] == outcome_filter]
    if mode_filter != "ALL":
        rows = [r for r in rows if r["mode"] == mode_filter]
    if category_filter != "ALL":
        rows = [r for r in rows if r["category"] == category_filter]

    if not rows:
        st.info(t("no_audits"))
        return

    # Compact table
    df_rows = [
        {
            "id": r["id"],
            "Time (UTC)": r["created_at"].strftime("%Y-%m-%d %H:%M:%S"),
            "Category": r["category"],
            "Market": r["market"],
            "Symbol": r["symbol"],
            "Mode": r["mode"],
            "Outcome": r["outcome"],
            "Reason": (r["reason"] or "")[:80],
        }
        for r in rows
    ]
    st.dataframe(
        pd.DataFrame(df_rows),
        use_container_width=True,
        hide_index=True,
    )

    # Detail picker — select ID from the filtered set, view modal
    st.markdown("---")
    by_id = {r["id"]: r for r in rows}
    id_options = list(by_id.keys())
    label_of = lambda rid: (
        f"#{rid} · {by_id[rid]['created_at']:%H:%M:%S} · "
        f"{by_id[rid]['market']}/{by_id[rid]['symbol']} · "
        f"{by_id[rid]['outcome']}"
    )
    selected_id = st.selectbox(
        t("audit_select"),
        options=id_options,
        format_func=label_of,
        key="audit_detail_picker",
    )
    if st.button(t("audit_view"), key="audit_detail_open", type="primary"):
        show_audit_detail(by_id[selected_id])


def render_costs_tab(cost_logs: list[dict]) -> None:
    st.markdown(t("tab_costs_title"))
    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    today_rows = [r for r in cost_logs if r["created_at"] >= start]

    total_cost = sum(r["estimated_cost_usd"] or 0 for r in today_rows)
    total_in = sum(r["input_tokens"] or 0 for r in today_rows)
    total_out = sum(r["output_tokens"] or 0 for r in today_rows)

    cols = st.columns(4)
    cols[0].metric("Calls", len(today_rows))
    cols[1].metric("USD", f"${total_cost:,.4f}")
    cols[2].metric("In tokens", f"{total_in:,}")
    cols[3].metric("Out tokens", f"{total_out:,}")

    if not today_rows:
        st.info(t("no_costs"))
        return

    df = pd.DataFrame(today_rows)
    df["created_at"] = pd.to_datetime(df["created_at"]).dt.strftime("%H:%M:%S")
    df = df.rename(columns={
        "created_at": "Time (UTC)",
        "market": "Market", "symbol": "Symbol",
        "agent_label": "Agent", "model": "Model",
        "input_tokens": "In", "output_tokens": "Out",
        "estimated_cost_usd": "Cost (USD)",
    })
    cols_show = ["Time (UTC)", "Market", "Symbol", "Agent", "Model",
                 "In", "Out", "Cost (USD)"]
    cols_show = [c for c in cols_show if c in df.columns]
    st.dataframe(df[cols_show], use_container_width=True, hide_index=True)


# ============================================================================
# Main
# ============================================================================

def main() -> None:
    if "lang" not in st.session_state:
        st.session_state["lang"] = "tr"

    configs = load_market_configs()
    redis_state = load_redis_state()
    signals = load_signals(days_back=30)
    trades = load_trades(days_back=90)
    cost_logs = load_cost_logs(days_back=7)
    audit_logs = load_audit_logs(days_back=7, limit=500)

    st.title(t("app_title"))
    st.caption(
        f"{t('as_of')}: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')} · "
        f"{t('auto_refresh')}"
    )

    render_sidebar(configs, redis_state.get("screener_flags", {}))

    # KPI strip — compact
    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    today_cost = sum(
        (r["estimated_cost_usd"] or 0)
        for r in cost_logs
        if r["created_at"] >= start
    )
    n_today_calls = sum(1 for r in cost_logs if r["created_at"] >= start)

    n_active_signals = sum(1 for s in signals if s["action"] != "WAIT")
    n_resolved = sum(1 for s in signals if _signal_resolution(s) is not None)
    n_open_trades = sum(1 for t in trades if t["status"] in ("PENDING", "OPEN"))

    kpi = st.columns(6)
    kpi[0].metric(t("active_markets"), sum(1 for c in configs if c["enabled"]))
    kpi[1].metric(t("signals_30d"), len(signals))
    kpi[2].metric(t("non_wait"), n_active_signals)
    kpi[3].metric(t("resolved"), n_resolved)
    kpi[4].metric(t("open_trades"), n_open_trades)
    kpi[5].metric(
        t("today_cost"),
        f"${today_cost:,.2f}",
        delta=f"{n_today_calls} {t('calls')}",
        delta_color="off",
    )

    st.divider()
    render_pnl_matrix(trades, signals, redis_state["prices"])

    st.divider()
    t0, t1, t2, t3, t4, t5 = st.tabs([
        t("tab_perf"),
        t("tab_signals"), t("tab_trades"),
        t("tab_decisions"), t("tab_costs"),
        t("tab_audit"),
    ])
    with t0:
        render_performance_tab(signals, redis_state["prices"])
    with t1:
        render_signals_tab(signals)
    with t2:
        render_trades_tab(trades)
    with t3:
        render_decisions_tab(redis_state["decisions"], redis_state["prices"])
    with t4:
        render_costs_tab(cost_logs)
    with t5:
        render_audit_tab(audit_logs)


main()

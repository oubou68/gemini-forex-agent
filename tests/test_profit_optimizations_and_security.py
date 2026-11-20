import base64
import hashlib
import hmac
import json
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from agent.ai.memory import AgentMemory
from agent.analysis.indicators import TechnicalIndicatorEngine
from agent.analysis.stock_indicators import StockTechnicalIndicatorEngine
from agent.analysis.stock_session import StockSessionAnalyzer
from agent.core.models import (
    AccountSummary,
    Candle,
    GeminiTradeDecision,
    OrderRequest,
    Position,
    PositionDirection,
    TradeAction,
)
from agent.core.orchestrator import TradingAgentOrchestrator
from agent.core.stock_orchestrator import StockTradingAgentOrchestrator
from agent.risk.risk_manager import RiskManager
from agent.security.auth import (
    auth_manager,
    b64url_encode,
    create_session_token,
    verify_google_id_token,
    verify_session_token,
)
from config.settings import settings
from web.app import app, agent_manager


def _make_trending_candles(start: float, step: float, count: int = 80, day: str = "2026-11-20") -> list[Candle]:
    base = datetime(2026, 11, 20, 14, 30, tzinfo=timezone.utc)
    if day != "2026-11-20":
        y, m, d = map(int, day.split("-"))
        base = datetime(y, m, d, 14, 30, tzinfo=timezone.utc)
    candles = []
    price = start
    for i in range(count):
        t = (base + timedelta(minutes=i * 5)).isoformat().replace("+00:00", "Z")
        o = price
        c = price + step
        h = max(o, c) + abs(step) * 0.4
        l = min(o, c) - abs(step) * 0.2
        candles.append(Candle(time=t, open=o, high=h, low=l, close=c, volume=10000 + i * 250))
        price = c
    return candles


# ==============================================================================
# 1. PROFITABILITY & TRADING ENGINE OPTIMIZATION TESTS
# ==============================================================================

def test_jpy_position_sizing_and_risk_clamping():
    rm = RiskManager(risk_per_trade_pct=1.0)
    # USD_JPY: Entry 154.50, SL 154.20 (30 pips = 0.30 JPY)
    # 1% of 10,000 = 100 EUR; risk_per_unit = 0.30 / 154.50 = 0.0019417 -> ~51,500 units (NOT 333!)
    units_jpy = rm.calculate_position_size(
        equity=10000.0,
        entry_price=154.50,
        stop_loss_price=154.20,
        instrument="USD_JPY",
        risk_pct=1.0
    )
    assert 45000 <= units_jpy <= 55000

    # Hallucinated LLM risk_pct=50.0 must be clamped safely
    units_clamped = rm.calculate_position_size(
        equity=10000.0,
        entry_price=1.0850,
        stop_loss_price=1.0835,
        instrument="EUR_USD",
        risk_pct=50.0
    )
    assert units_clamped <= 100000


def test_dynamic_risk_scaling_confidence_drawdown_and_streak():
    rm = RiskManager(risk_per_trade_pct=1.0, max_daily_drawdown_pct=3.0, dynamic_risk_scaling=True)

    # High confidence, zero DD, no streak -> scaled up
    r_high = rm.compute_dynamic_risk_pct(base_risk_pct=1.0, confidence=90.0, daily_drawdown_pct=0.0, consecutive_losses=0)
    assert r_high > 1.0

    # Moderate drawdown (>= 50% of limit) -> scaled down
    r_dd = rm.compute_dynamic_risk_pct(base_risk_pct=1.0, confidence=80.0, daily_drawdown_pct=2.0, consecutive_losses=0)
    assert r_dd < 0.7

    # Losing streak (3 losses) -> strongly reduced
    r_streak = rm.compute_dynamic_risk_pct(base_risk_pct=1.0, confidence=80.0, daily_drawdown_pct=0.5, consecutive_losses=3)
    assert r_streak <= 0.55


def test_take_profit_direction_validation_and_forex_correlation_guard():
    rm = RiskManager(max_open_positions=5, max_correlated_exposure=2)
    account = AccountSummary(account_id="T", balance=10000.0, equity=10000.0, daily_drawdown_pct=0.0)

    # BUY with TP below entry must be REJECTED
    bad_tp_decision = GeminiTradeDecision(
        action=TradeAction.BUY,
        instrument="EUR_USD",
        confidence=85.0,
        entry_price=1.0850,
        stop_loss=1.0830,
        take_profit_1=1.0800
    )
    ok, reason = rm.validate_trade_decision(bad_tp_decision, account, spread_pips=1.0, open_positions=[])
    assert ok is False
    assert "Take-Profit" in reason

    # 2 existing USD-short positions (EUR_USD BUY + GBP_USD BUY) -> 3rd USD-short (AUD_USD BUY) blocked
    pos1 = Position(id="1", instrument="EUR_USD", direction=PositionDirection.BUY, units=10000, entry_price=1.08, current_price=1.08, open_time="2026-11-20T10:00:00Z")
    pos2 = Position(id="2", instrument="GBP_USD", direction=PositionDirection.BUY, units=10000, entry_price=1.27, current_price=1.27, open_time="2026-11-20T10:00:00Z")
    aud_buy = GeminiTradeDecision(
        action=TradeAction.BUY,
        instrument="AUD_USD",
        confidence=85.0,
        entry_price=0.6600,
        stop_loss=0.6580,
        take_profit_1=0.6650
    )
    ok_corr, reason_corr = rm.validate_trade_decision(aud_buy, account, spread_pips=1.0, open_positions=[pos1, pos2])
    assert ok_corr is False
    assert "Korrelationslimit" in reason_corr

    # Offsetting USD-long (USD_JPY BUY) is ALLOWED
    usdjpy_buy = GeminiTradeDecision(
        action=TradeAction.BUY,
        instrument="USD_JPY",
        confidence=85.0,
        entry_price=154.50,
        stop_loss=154.00,
        take_profit_1=155.50
    )
    ok_offset, _ = rm.validate_trade_decision(usdjpy_buy, account, spread_pips=1.2, open_positions=[pos1, pos2])
    assert ok_offset is True


def test_multistage_breakeven_and_atr_trailing_stop():
    rm = RiskManager(breakeven_trigger_r=1.0, trailing_stop_enabled=True)

    # Stock BUY: Entry 200.00, Initial SL 198.00 (1R = $2.00)
    pos = Position(
        id="stk1",
        instrument="AAPL",
        direction=PositionDirection.BUY,
        units=50,
        entry_price=200.00,
        current_price=200.00,
        stop_loss=198.00,
        initial_stop_loss=198.00,
        take_profit=208.00,
        open_time="2026-11-20T14:30:00Z"
    )

    # Stage 1: +1.0R ($202.00) -> Move SL to Breakeven + $0.02 = $200.02
    sl1 = rm.evaluate_breakeven_adjustment(pos, current_price=202.10, atr=1.20)
    assert sl1 == 200.02
    pos.stop_loss = sl1

    # Stage 2: +1.6R ($203.20) -> Using initial_stop_loss (1R=$2), locks >= +0.5R ($201.00) or ATR trail ($202.00)
    sl2 = rm.evaluate_breakeven_adjustment(pos, current_price=203.20, atr=1.20)
    assert sl2 is not None
    assert sl2 >= 201.00
    pos.stop_loss = sl2

    # Stage 3: +2.5R ($205.00) -> Locks >= +1.0R ($202.00) / ATR trail ($204.04)
    sl3 = rm.evaluate_breakeven_adjustment(pos, current_price=205.00, atr=1.20)
    assert sl3 is not None
    assert sl3 > sl2

    # Disabled trailing stop returns None
    rm_off = RiskManager(trailing_stop_enabled=False)
    assert rm_off.evaluate_breakeven_adjustment(pos, current_price=210.00, atr=1.20) is None


def test_wilder_indicators_adx_htf_and_session_anchored_vwap():
    forex_candles = _make_trending_candles(1.0800, 0.0003, count=80)
    ind = TechnicalIndicatorEngine.calculate_indicators(forex_candles)
    assert ind.adx_14 is not None and ind.adx_14 > 25.0
    assert ind.htf_trend_bias == "BULLISH"
    assert ind.trend_bias == "BULLISH"

    # Stock candles across 2 days: yesterday low price ($100), today high price ($200)
    yesterday = _make_trending_candles(100.0, 0.1, count=30, day="2026-11-19")
    today = _make_trending_candles(200.0, 0.5, count=30, day="2026-11-20")
    stock_ind = StockTechnicalIndicatorEngine.calculate_indicators(yesterday + today, orb_bars=3)
    # Anchored VWAP and ORB must reflect TODAY (~$200+), NOT dragged down to $150 by yesterday!
    assert stock_ind.vwap is not None and stock_ind.vwap > 195.0
    assert stock_ind.orb_low is not None and stock_ind.orb_low >= 199.0
    assert stock_ind.adx_14 is not None and stock_ind.adx_14 > 20.0
    assert stock_ind.htf_trend_bias == "BULLISH"


def test_stock_session_dst_winter_and_summer():
    # Winter EST (Nov 20, 2026 = UTC-5): 14:30 UTC is 09:30 EST (Opening Drive)
    nov_open = datetime(2026, 11, 20, 14, 35, tzinfo=timezone.utc)
    assert StockSessionAnalyzer.get_current_session(nov_open) == "US_OPENING_DRIVE (RTH OPEN)"
    assert StockSessionAnalyzer.is_regular_trading_hours(nov_open) is True

    # Winter EST: 20:30 UTC is 15:30 EST (Power Hour, still RTH!)
    nov_power = datetime(2026, 11, 20, 20, 30, tzinfo=timezone.utc)
    assert StockSessionAnalyzer.get_current_session(nov_power) == "US_POWER_HOUR"
    assert StockSessionAnalyzer.is_regular_trading_hours(nov_power) is True

    # Summer EDT (July 15, 2026 = UTC-4): 13:35 UTC is 09:35 EDT (Opening Drive)
    jul_open = datetime(2026, 7, 15, 13, 35, tzinfo=timezone.utc)
    assert StockSessionAnalyzer.get_current_session(jul_open) == "US_OPENING_DRIVE (RTH OPEN)"


def test_memory_namespace_isolation_drawdown_and_loss_cooldown():
    with tempfile.TemporaryDirectory() as tmp:
        mem = AgentMemory(storage_path=str(Path(tmp) / "mem.json"))
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        p1 = Position(id="t1", instrument="EUR_USD", direction=PositionDirection.BUY, units=10000, entry_price=1.08, current_price=1.09, realized_pnl=200.0, open_time=now_iso, close_time=now_iso, close_reason="TP_HIT")
        p2 = Position(id="t2", instrument="EUR_USD", direction=PositionDirection.BUY, units=10000, entry_price=1.09, current_price=1.085, realized_pnl=-150.0, open_time=now_iso, close_time=now_iso, close_reason="SL_HIT")
        p3 = Position(id="t3", instrument="GBP_USD", direction=PositionDirection.SELL, units=10000, entry_price=1.27, current_price=1.275, realized_pnl=-100.0, open_time=now_iso, close_time=now_iso, close_reason="SL_HIT")

        mem.record_closed_trade(p1)
        mem.record_closed_trade(p2)
        mem.record_closed_trade(p3)

        stats = mem.get_performance_stats(reference_capital=10000.0)
        assert stats.max_drawdown_pct > 2.0
        assert mem.get_consecutive_losses() == 2
        assert mem.get_instrument_loss_cooldown_remaining("EUR_USD", cooldown_seconds=300) > 0
        assert mem.get_instrument_loss_cooldown_remaining("USD_JPY", cooldown_seconds=300) == 0.0

    # Verify Forex & Stock orchestrators have distinct memory files
    forex_orch = TradingAgentOrchestrator()
    stock_orch = StockTradingAgentOrchestrator()
    assert forex_orch.memory.storage_path != stock_orch.memory.storage_path


@pytest.mark.asyncio
async def test_auto_liquidate_on_drawdown_enforcement():
    orch = TradingAgentOrchestrator()
    await orch.set_mode("simulator")
    await orch.initialize()
    orch.risk_manager.update_risk_parameters(auto_liquidate_on_drawdown=True, max_daily_drawdown_pct=2.0)

    await orch.execute_manual_trade("EUR_USD", "BUY", risk_pct=1.0)
    assert len(await orch.broker.get_open_positions()) == 1

    # Simulate equity drawdown >= 2%
    orch.broker.balance = 9700.0  # 3% loss vs 10,000 initial_equity
    await orch._manage_open_positions()
    assert len(await orch.broker.get_open_positions()) == 0


@pytest.mark.asyncio
async def test_single_position_close_preserves_other_positions():
    await agent_manager.forex_agent.set_mode("simulator")
    await agent_manager.forex_agent.emergency_close_all()

    p1 = await agent_manager.forex_agent.execute_manual_trade("EUR_USD", "BUY", risk_pct=1.0)
    p2 = await agent_manager.forex_agent.execute_manual_trade("GBP_USD", "SELL", risk_pct=1.0)
    assert len(await agent_manager.forex_agent.broker.get_open_positions()) == 2

    client = TestClient(app)
    auth_manager.rate_limiter.clear_all()
    client.post("/api/auth/login", json={"username": "admin", "password": "AuraQuant_2026!TradeSafe"})

    res = client.post("/api/control/close-position", json={"bot_type": "forex", "position_id": p1.id})
    assert res.status_code == 200
    assert res.json()["status"] == "closed"

    remaining = await agent_manager.forex_agent.broker.get_open_positions()
    assert len(remaining) == 1
    assert remaining[0].id == p2.id
    await agent_manager.forex_agent.emergency_close_all()


# ==============================================================================
# 2. SECURITY AUDIT VERIFICATION TESTS
# ==============================================================================

def test_jwt_strict_header_and_expiration_validation():
    secret = "test_super_secret_key_for_hmac_256_validation"

    # Forged alg=none token must be rejected
    header_none = b64url_encode(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload_ok = b64url_encode(json.dumps({"sub": "admin", "exp": int(time.time()) + 3600}).encode())
    sig = b64url_encode(hmac.new(secret.encode(), f"{header_none}.{payload_ok}".encode(), hashlib.sha256).digest())
    assert verify_session_token(f"{header_none}.{payload_ok}.{sig}", secret) is None

    # Token missing exp must be rejected
    header_ok = b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload_no_exp = b64url_encode(json.dumps({"sub": "admin"}).encode())
    sig_no_exp = b64url_encode(hmac.new(secret.encode(), f"{header_ok}.{payload_no_exp}".encode(), hashlib.sha256).digest())
    assert verify_session_token(f"{header_ok}.{payload_no_exp}.{sig_no_exp}", secret) is None

    # Valid token passes
    tok = create_session_token({"sub": "admin"}, secret, expire_hours=1)
    decoded = verify_session_token(tok, secret)
    assert decoded is not None and "jti" in decoded


def test_logout_revokes_bearer_token_server_side():
    auth_manager.rate_limiter.clear_all()
    c = TestClient(app)
    login_res = c.post("/api/auth/login", json={"username": "admin", "password": "AuraQuant_2026!TradeSafe"})
    assert login_res.status_code == 200
    token = login_res.json()["token"]

    # Works before logout
    c_bearer = TestClient(app)
    assert c_bearer.get("/api/status", headers={"Authorization": f"Bearer {token}"}).status_code == 200

    # Logout with Bearer token
    assert c_bearer.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"}).status_code == 200

    # Same Bearer token MUST now be rejected (401)
    assert c_bearer.get("/api/status", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_login_brute_force_rate_limiting():
    auth_manager.rate_limiter.clear_all()
    c = TestClient(app)
    headers = {"X-Forwarded-For": "198.51.100.42"}

    for _ in range(settings.AUTH_RATE_LIMIT_MAX_ATTEMPTS):
        res = c.post("/api/auth/login", json={"username": "admin", "password": "wrong"}, headers=headers)
        assert res.status_code == 401

    # Next attempt must be blocked with 429 Too Many Requests
    blocked = c.post("/api/auth/login", json={"username": "admin", "password": "AuraQuant_2026!TradeSafe"}, headers=headers)
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers

    auth_manager.rate_limiter.clear_all()


def test_security_headers_cors_and_disabled_docs():
    c = TestClient(app)
    res = c.get("/api/auth/config", headers={"Origin": "https://evil-attacker.example"})
    assert res.status_code == 200
    assert res.headers.get("X-Content-Type-Options") == "nosniff"
    assert res.headers.get("X-Frame-Options") == "DENY"
    assert "Content-Security-Policy" in res.headers
    assert res.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    # Evil origin must NOT be reflected
    assert res.headers.get("Access-Control-Allow-Origin") != "https://evil-attacker.example"

    # Public OpenAPI docs disabled by default
    assert c.get("/docs").status_code == 404
    assert c.get("/openapi.json").status_code == 404


def test_api_input_validation_and_injection_blocking():
    auth_manager.rate_limiter.clear_all()
    c = TestClient(app)
    c.post("/api/auth/login", json={"username": "admin", "password": "AuraQuant_2026!TradeSafe"})

    # Path traversal / injection in instrument
    assert c.get("/api/candles?bot_type=forex&instrument=../../accounts/123").status_code == 400
    assert c.get("/api/candles?bot_type=stock&instrument=AAPL;DROP").status_code == 400

    # Unbounded count DoS
    assert c.get("/api/candles?bot_type=forex&count=99999").status_code == 422

    # Invalid manual order params
    assert c.post("/api/control/manual-order", json={"bot_type": "forex", "direction": "HACK", "risk_pct": 1.0}).status_code == 400
    assert c.post("/api/control/manual-order", json={"bot_type": "forex", "direction": "BUY", "risk_pct": 500.0}).status_code == 400

    # Invalid mode & risk bounds
    assert c.post("/api/control/mode", json={"bot_type": "forex", "mode": "evil_mode"}).status_code == 400
    assert c.post("/api/config/risk", json={"risk_per_trade_pct": 99.0}).status_code == 400
    assert c.post("/api/config/risk", json={"max_daily_drawdown_pct": -5.0}).status_code == 400


def test_websocket_blocks_forbidden_origin():
    auth_manager.rate_limiter.clear_all()
    c = TestClient(app)
    token = c.post("/api/auth/login", json={"username": "admin", "password": "AuraQuant_2026!TradeSafe"}).json()["token"]

    with pytest.raises(Exception):
        with c.websocket_connect(f"/ws?token={token}", headers={"Origin": "https://evil.example"}) as ws:
            ws.receive_json()


@patch("requests.get")
def test_google_token_issuer_and_expiration_checks(mock_get):
    # Bad issuer rejected
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "iss": "https://evil-issuer.example",
        "email": "oubou68@gmail.com",
        "email_verified": "true",
        "exp": str(int(time.time()) + 3600)
    }
    mock_get.return_value = mock_resp
    with pytest.raises(PermissionError):
        verify_google_id_token("token", allowed_email="oubou68@gmail.com")

    # Expired token rejected
    mock_resp.json.return_value = {
        "iss": "https://accounts.google.com",
        "email": "oubou68@gmail.com",
        "email_verified": "true",
        "exp": str(int(time.time()) - 100)
    }
    with pytest.raises(ValueError):
        verify_google_id_token("token", allowed_email="oubou68@gmail.com")

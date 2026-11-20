from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from agent.ai.crypto_analyst import GeminiCryptoAnalyst
from agent.analysis.crypto_indicators import CryptoTechnicalIndicatorEngine
from agent.analysis.crypto_session import CryptoSessionAnalyzer
from agent.broker.alpaca_crypto_client import AlpacaCryptoClient, normalize_crypto_symbol
from agent.broker.crypto_simulator import CryptoSimulatorBroker
from agent.core.crypto_orchestrator import CryptoTradingAgentOrchestrator
from agent.core.models import (
    IndicatorValues,
    MarketPrice,
    MarketStructure,
    OrderRequest,
    PositionDirection,
    TradeAction,
)
from agent.security.auth import auth_manager
from web.app import app, agent_manager


TOP_20_EXPECTED = [
    "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "DOGE/USD",
    "LINK/USD", "AVAX/USD", "DOT/USD", "UNI/USD", "AAVE/USD",
    "LTC/USD", "BCH/USD", "SHIB/USD", "PEPE/USD", "GRT/USD",
    "XTZ/USD", "CRV/USD", "SUSHI/USD", "MKR/USD", "BAT/USD",
]


@pytest.mark.asyncio
async def test_top_20_crypto_universe_and_simulator():
    orch = CryptoTradingAgentOrchestrator()
    await orch.set_mode("simulator")
    await orch.initialize()

    catalog = orch.get_universe_catalog()
    assert catalog["total_symbols_count"] == 20
    assert set(catalog["symbols"]) == set(TOP_20_EXPECTED)
    assert len(catalog["tier_1_core"]) == 4
    assert len(catalog["tier_2_alt_defi"]) == 11
    assert len(catalog["tier_3_high_beta"]) == 5

    # Verify pricing & candles across large-cap and micro-cap (SHIB/PEPE)
    for sym in ("BTC/USD", "SOL/USD", "SHIB/USD", "PEPE/USD"):
        p = await orch.broker.get_current_price(sym)
        assert p.mid > 0
        candles = await orch.broker.get_candles(sym, count=25)
        assert len(candles) == 25
        ind = CryptoTechnicalIndicatorEngine.calculate_indicators(candles, btc_return_pct=0.2, btc_regime="RISK_ON")
        assert ind.vwap is not None and ind.vwap > 0
        assert ind.adx_14 is not None

    assert CryptoSessionAnalyzer.is_market_open() is True


@pytest.mark.asyncio
async def test_crypto_strategy_btc_regime_gating_and_rs_alpha():
    analyst = GeminiCryptoAnalyst(api_key="")  # Deterministic Quant strategy mode
    price = MarketPrice(
        instrument="SOL/USD",
        time="2026-11-20T18:00:00Z",
        bid=73.15,
        ask=73.25,
        spread_pips=0.14,
        mid=73.20,
    )
    ind_strong = IndicatorValues(
        ema_9=73.10,
        ema_21=72.40,
        rsi_14=58.0,
        atr_14=1.10,
        vwap=72.10,
        orb_high=72.90,
        orb_low=70.80,
        rvol=1.65,
        adx_14=28.0,
        htf_trend_bias="BULLISH",
        trend_bias="BULLISH",
        rs_vs_btc=1.45,
        btc_regime="RISK_ON",
    )
    struct = MarketStructure(active_session="CRYPTO_US_OVERLAP")

    # 1. RISK_ON + Strong RS vs BTC -> Spot BUY with asymmetric RRR >= 1.8
    dec_on = await analyst.analyze_market(
        symbol="SOL/USD",
        price=price,
        indicators=ind_strong,
        structure=struct,
        session_name="CRYPTO_US_OVERLAP",
        open_positions_count=0,
        daily_drawdown_pct=0.0,
        tier="TIER_1",
        btc_regime="RISK_ON",
    )
    assert dec_on.action == TradeAction.BUY
    assert dec_on.risk_reward_ratio >= 1.8
    assert dec_on.stop_loss < dec_on.entry_price < dec_on.take_profit_1

    # 2. RISK_OFF -> Speculative Tier-3 altcoin MUST be blocked (HOLD in USD Cash)
    dec_off = await analyst.analyze_market(
        symbol="DOGE/USD",
        price=MarketPrice(instrument="DOGE/USD", time="2026-11-20T18:00:00Z", bid=0.1418, ask=0.1422, spread_pips=0.22, mid=0.1420),
        indicators=ind_strong.model_copy(update={"btc_regime": "RISK_OFF", "rsi_14": 52.0}),
        structure=struct,
        session_name="CRYPTO_US_OVERLAP",
        open_positions_count=0,
        daily_drawdown_pct=0.0,
        tier="TIER_3",
        btc_regime="RISK_OFF",
    )
    assert dec_off.action == TradeAction.HOLD


def test_crypto_volatility_targeted_sizing_tier_caps_and_exposure_limit():
    orch = CryptoTradingAgentOrchestrator()
    orch.btc_regime = "RISK_ON"
    equity = 50000.0

    # Tier 1 (BTC/USD) capped at <= 20% of equity ($10,000)
    qty_btc = orch.calculate_crypto_quantity(
        symbol="BTC/USD",
        equity=equity,
        entry_price=64500.0,
        stop_loss_price=63500.0,  # $1,000 stop -> raw risk $625 = 0.625 BTC ($40k) -> capped at $10k
        risk_pct=1.25,
        current_deployed_notional=0.0,
    )
    assert qty_btc > 0
    assert qty_btc * 64500.0 <= equity * 0.20 + 1.0

    # Tier 3 (PEPE/USD) strictly capped at <= 6% of equity ($3,000)
    qty_pepe = orch.calculate_crypto_quantity(
        symbol="PEPE/USD",
        equity=equity,
        entry_price=0.00000820,
        stop_loss_price=0.00000800,
        risk_pct=1.25,
        current_deployed_notional=0.0,
    )
    assert qty_pepe > 0
    assert qty_pepe * 0.00000820 <= equity * 0.06 + 1.0

    # Portfolio exposure cap (65% = $32,500 already deployed -> 0.0 new allocation!)
    qty_blocked = orch.calculate_crypto_quantity(
        symbol="ETH/USD",
        equity=equity,
        entry_price=1800.0,
        stop_loss_price=1740.0,
        risk_pct=1.25,
        current_deployed_notional=32500.0,
    )
    assert qty_blocked == 0.0


@pytest.mark.asyncio
async def test_crypto_partial_tp1_scaleout_and_chandelier_trailing():
    orch = CryptoTradingAgentOrchestrator()
    await orch.set_mode("simulator")
    await orch.initialize()
    orch.btc_regime = "RISK_ON"

    # Place a synthetic SOL/USD spot position: Entry $70, SL $66 (1R = $4), TP $95
    orch.broker.current_prices["SOL/USD"] = 70.0
    pos = await orch.broker.place_order(
        OrderRequest(
            instrument="SOL/USD",
            direction=PositionDirection.BUY,
            units=10.0,
            stop_loss=66.0,
            take_profit=95.0,
        )
    )
    entry = pos.entry_price
    initial_risk = entry - 66.0

    # Move price to +2.2R -> Triggers 50% Partial TP1 scale-out AND locks SL >= +1.0R
    orch.broker.current_prices["SOL/USD"] = round(entry + 2.2 * initial_risk, 4)
    orch._symbol_atr_cache["SOL/USD"] = 1.5
    await orch._manage_open_positions()

    open_after = await orch.broker.get_open_positions()
    assert len(open_after) == 1
    assert open_after[0].partial_tp_taken is True
    assert open_after[0].units == pytest.approx(5.0, abs=0.05)
    assert open_after[0].stop_loss >= entry + initial_risk - 0.1


@pytest.mark.asyncio
@patch("requests.delete")
@patch("requests.post")
@patch("requests.get")
async def test_alpaca_crypto_client_v1beta3_and_gtc_orders(mock_get, mock_post, mock_delete):
    assert normalize_crypto_symbol("btcusd") == "BTC/USD"
    assert normalize_crypto_symbol("ETH_USD") == "ETH/USD"

    client = AlpacaCryptoClient(api_key="PKTEST", secret_key="SKTEST", environment="paper")

    # 1. Mock v1beta3 crypto bars
    bars_resp = MagicMock()
    bars_resp.status_code = 200
    bars_resp.json.return_value = {
        "bars": {
            "BTC/USD": [
                {"t": "2026-11-20T18:00:00Z", "o": 64400.0, "h": 64600.0, "l": 64350.0, "c": 64550.0, "v": 120.5}
            ]
        }
    }
    mock_get.return_value = bars_resp
    candles = await client.get_candles("BTC/USD", granularity="M5", count=10)
    assert len(candles) == 1
    assert "/v1beta3/crypto/us/bars" in mock_get.call_args.args[0]

    # 2. Mock v2 crypto spot order (simple + gtc + fractional qty)
    order_resp = MagicMock()
    order_resp.status_code = 200
    order_resp.json.return_value = {"id": "ord_btc_1", "filled_avg_price": "64550.00"}
    mock_post.return_value = order_resp

    pos = await client.place_order(
        OrderRequest(
            instrument="BTC/USD",
            direction=PositionDirection.BUY,
            units=0.05,
            stop_loss=63000.0,
            take_profit=68000.0,
        )
    )
    sent_payload = mock_post.call_args.kwargs["json"]
    assert sent_payload["symbol"] == "BTC/USD"
    assert sent_payload["time_in_force"] == "gtc"
    assert sent_payload["qty"] == "0.05"
    assert "order_class" not in sent_payload  # Spot simple order only!
    assert pos.units == 0.05

    # 3. Close position uses /v2/positions/BTCUSD (no slash)
    quote_resp = MagicMock()
    quote_resp.status_code = 200
    quote_resp.json.return_value = {"quotes": {"BTC/USD": {"bp": 65000.0, "ap": 65050.0, "t": "2026-11-20T18:05:00Z"}}}
    mock_get.return_value = quote_resp
    del_resp = MagicMock()
    del_resp.status_code = 200
    mock_delete.return_value = del_resp

    closed = await client.close_position(pos.id, reason="TEST_CLOSE")
    assert mock_delete.call_args.args[0].endswith("/positions/BTCUSD")
    assert closed.realized_pnl > 0


@pytest.mark.asyncio
async def test_crypto_rest_api_and_websocket():
    await agent_manager.crypto_agent.set_mode("simulator")
    await agent_manager.crypto_agent.emergency_close_all()

    auth_manager.rate_limiter.clear_all()
    c = TestClient(app)
    login = c.post("/api/auth/login", json={"username": "admin", "password": "AuraQuant_2026!TradeSafe"})
    token = login.json()["token"]

    # Universe & Screener endpoints
    univ = c.get("/api/crypto/universe")
    assert univ.status_code == 200
    assert univ.json()["total_symbols_count"] == 20
    assert "BTC/USD" in univ.json()["symbols"]

    scr = c.get("/api/crypto/screener")
    assert scr.status_code == 200
    assert len(scr.json()) > 0

    # Status & Candles
    st = c.get("/api/status?bot_type=crypto")
    assert st.status_code == 200
    assert st.json()["account"]["currency"] == "USD"

    cdl = c.get("/api/candles?bot_type=crypto&instrument=ETH/USD&count=30")
    assert cdl.status_code == 200
    assert len(cdl.json()) == 30

    # Manual BUY + Manual SELL (closes spot position)
    buy_res = c.post("/api/control/manual-order", json={
        "bot_type": "crypto",
        "instrument": "SOL/USD",
        "direction": "BUY",
        "risk_pct": 1.25,
    })
    assert buy_res.status_code == 200
    assert buy_res.json()["position"]["instrument"] == "SOL/USD"

    sell_res = c.post("/api/control/manual-order", json={
        "bot_type": "crypto",
        "instrument": "SOL/USD",
        "direction": "SELL",
        "risk_pct": 1.25,
    })
    assert sell_res.status_code == 200

    # WebSocket includes crypto in INITIAL_STATE and supports REQUEST_CANDLES
    with c.websocket_connect(f"/ws?token={token}") as ws:
        init = ws.receive_json()
        assert "crypto" in init
        ws.receive_json()  # forex candles
        ws.receive_json()  # stock candles

        ws.send_json({"action": "REQUEST_CANDLES", "bot_type": "crypto", "instrument": "AVAX/USD"})
        resp = ws.receive_json()
        assert resp["type"] == "CANDLES"
        assert resp["bot_type"] == "crypto"
        assert resp["instrument"] == "AVAX/USD"
        assert len(resp["data"]) > 0


@pytest.mark.asyncio
@patch("requests.get")
async def test_alpaca_crypto_batch_caching_and_429_fallback(mock_get):
    from agent.broker.alpaca_rate_limiter import shared_alpaca_rate_limiter
    shared_alpaca_rate_limiter.reset()

    client = AlpacaCryptoClient(api_key="PKTEST", secret_key="SKTEST", environment="paper")

    # 1. Batch quotes for all 20 cryptos -> exactly 1 HTTP request!
    quotes_resp = MagicMock()
    quotes_resp.status_code = 200
    quotes_resp.json.return_value = {
        "quotes": {sym: {"bp": 100.0, "ap": 100.1, "t": "2026-11-20T22:00:00Z"} for sym in TOP_20_EXPECTED}
    }
    mock_get.return_value = quotes_resp
    prices = await client.get_multi_prices(TOP_20_EXPECTED)
    assert len(prices) == 20
    assert mock_get.call_count == 1

    # Subsequent single quote call hits TTL cache -> still 1 HTTP request total!
    btc_p = await client.get_current_price("BTC/USD")
    assert btc_p.mid > 0
    assert mock_get.call_count == 1

    # 2. Batch bars for all 20 cryptos -> 2 HTTP requests (chunks of 10), populates cache!
    bars_resp = MagicMock()
    bars_resp.status_code = 200
    bars_resp.json.return_value = {
        "bars": {
            sym: [
                {"t": f"2026-11-20T21:{i:02d}:00Z", "o": 100.0, "h": 101.0, "l": 99.5, "c": 100.5, "v": 50}
                for i in range(25)
            ]
            for sym in TOP_20_EXPECTED
        }
    }
    mock_get.return_value = bars_resp
    bars_map = await client.get_multi_candles(TOP_20_EXPECTED, granularity="M5", count=25)
    assert len(bars_map) == 20
    assert mock_get.call_count == 3  # 1 quote batch + 2 bar batches

    # 3. Simulate HTTP 429 after cache expires -> activates backoff and falls back cleanly to stale cache!
    client._cache._store["bars:BTC/USD:M5"] = (0.0, bars_map["BTC/USD"])
    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_429.headers = {"Retry-After": "15"}
    resp_429.text = '{"message": "too many requests."}'
    mock_get.return_value = resp_429

    stale_candles = await client.get_candles("BTC/USD", granularity="M5", count=25)
    assert len(stale_candles) == 25
    backed_off, remaining = shared_alpaca_rate_limiter.is_backed_off()
    assert backed_off is True and remaining > 0
    shared_alpaca_rate_limiter.reset()

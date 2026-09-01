import pytest
from fastapi.testclient import TestClient
from web.app import app, agent_manager


client = TestClient(app)


@pytest.mark.asyncio
async def test_rest_status_endpoints():
    await agent_manager.forex_agent.set_mode("simulator")
    await agent_manager.stock_agent.set_mode("simulator")
    agent_manager.forex_agent.is_running = False
    agent_manager.stock_agent.is_running = False

    # All telemetry
    res = client.get("/api/status")
    assert res.status_code == 200
    data = res.json()
    assert "forex" in data
    assert "stock" in data
    assert data["forex"]["account"]["currency"] == "EUR"
    assert data["stock"]["account"]["currency"] == "USD"

    # Forex only
    res_forex = client.get("/api/status?bot_type=forex")
    assert res_forex.status_code == 200
    assert res_forex.json()["account"]["currency"] == "EUR"

    # Stock only
    res_stock = client.get("/api/status?bot_type=stock")
    assert res_stock.status_code == 200
    assert res_stock.json()["account"]["currency"] == "USD"


@pytest.mark.asyncio
async def test_rest_candles_endpoints():
    await agent_manager.forex_agent.set_mode("simulator")
    await agent_manager.stock_agent.set_mode("simulator")

    # Forex candles
    res_forex = client.get("/api/candles?bot_type=forex&instrument=EUR_USD&count=50")
    assert res_forex.status_code == 200
    candles_forex = res_forex.json()
    assert len(candles_forex) == 50
    assert "open" in candles_forex[0]

    # Stock candles (NASDAQ)
    res_stock = client.get("/api/candles?bot_type=stock&instrument=NVDA&count=40")
    assert res_stock.status_code == 200
    candles_stock = res_stock.json()
    assert len(candles_stock) == 40
    assert "volume" in candles_stock[0]


@pytest.mark.asyncio
async def test_rest_stock_universe_and_screener():
    await agent_manager.stock_agent.set_mode("simulator")

    # Universe catalog
    res_univ = client.get("/api/stock/universe")
    assert res_univ.status_code == 200
    univ = res_univ.json()
    assert "nasdaq_100" in univ
    assert "dow_jones_30" in univ
    assert "index_etfs" in univ
    assert "AAPL" in univ["dow_jones_30"]
    assert "MSFT" in univ["nasdaq_100"]

    # Screener radar
    res_screen = client.get("/api/stock/screener")
    assert res_screen.status_code == 200
    candidates = res_screen.json()
    assert len(candidates) > 0
    assert "score" in candidates[0]
    assert "rvol" in candidates[0]


@pytest.mark.asyncio
async def test_rest_control_endpoints():
    await agent_manager.forex_agent.set_mode("simulator")
    await agent_manager.stock_agent.set_mode("simulator")

    # Stop and Start
    res_stop = client.post("/api/control/stop", json={"bot_type": "all"})
    assert res_stop.status_code == 200
    assert res_stop.json()["status"] == "stopped"

    res_start = client.post("/api/control/start", json={"bot_type": "all"})
    assert res_start.status_code == 200
    assert res_start.json()["status"] == "started"

    # Switch Instrument via REST
    res_inst = client.post("/api/control/instrument", json={"bot_type": "stock", "instrument": "TSLA"})
    assert res_inst.status_code == 200
    assert res_inst.json()["current_symbol"] == "TSLA"

    # Manual Order via REST (Stock)
    res_order = client.post("/api/control/manual-order", json={
        "bot_type": "stock",
        "instrument": "AAPL",
        "direction": "BUY",
        "risk_pct": 1.0
    })
    assert res_order.status_code == 200
    assert res_order.json()["status"] == "executed"
    assert res_order.json()["position"]["instrument"] == "AAPL"

    # Emergency Close via REST
    res_close = client.post("/api/control/emergency-close", json={"bot_type": "stock"})
    assert res_close.status_code == 200
    assert res_close.json()["status"] == "success"


@pytest.mark.asyncio
async def test_websocket_lifecycle_and_messages():
    await agent_manager.forex_agent.set_mode("simulator")
    await agent_manager.stock_agent.set_mode("simulator")

    with client.websocket_connect("/ws") as websocket:
        # 1. Receive INITIAL_STATE
        init_data = websocket.receive_json()
        assert init_data["type"] == "INITIAL_STATE"
        assert "forex" in init_data
        assert "stock" in init_data

        # 2. Receive Initial Forex Candles
        forex_candles_msg = websocket.receive_json()
        assert forex_candles_msg["type"] == "CANDLES"
        assert forex_candles_msg["bot_type"] == "forex"
        assert len(forex_candles_msg["data"]) > 0

        # 3. Receive Initial Stock Candles
        stock_candles_msg = websocket.receive_json()
        assert stock_candles_msg["type"] == "CANDLES"
        assert stock_candles_msg["bot_type"] == "stock"
        assert len(stock_candles_msg["data"]) > 0

        # 4. Test non-blocking SWITCH_INSTRUMENT
        websocket.send_json({
            "action": "SWITCH_INSTRUMENT",
            "bot_type": "stock",
            "instrument": "NVDA"
        })
        switch_res = websocket.receive_json()
        assert switch_res["type"] == "CANDLES"
        assert switch_res["bot_type"] == "stock"
        assert switch_res["instrument"] == "NVDA"
        assert len(switch_res["data"]) > 0

        # 5. Test REQUEST_CANDLES
        websocket.send_json({
            "action": "REQUEST_CANDLES",
            "bot_type": "forex",
            "instrument": "GBP_USD"
        })
        req_res = websocket.receive_json()
        assert req_res["type"] == "CANDLES"
        assert req_res["bot_type"] == "forex"
        assert req_res["instrument"] == "GBP_USD"


@pytest.mark.asyncio
async def test_rest_trade_history_endpoint():
    await agent_manager.forex_agent.set_mode("simulator")
    await agent_manager.stock_agent.set_mode("simulator")

    # Place a trade and close it
    pos = await agent_manager.stock_agent.execute_manual_trade("AAPL", "BUY", 1.0)
    await agent_manager.stock_agent.broker.close_position(pos.id, reason="MANUAL_TEST")

    res = client.get("/api/trades/history")
    assert res.status_code == 200
    data = res.json()
    assert "forex" in data
    assert "stock" in data
    assert len(data["stock"]) > 0
    assert data["stock"][0]["instrument"] == "AAPL"

    # Specific bot query
    res_stock = client.get("/api/trades/history?bot_type=stock")
    assert res_stock.status_code == 200
    assert res_stock.json()["bot_type"] == "stock"
    assert len(res_stock.json()["trades"]) > 0

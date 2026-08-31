import pytest
from agent.core.orchestrator import TradingAgentOrchestrator
from agent.core.stock_orchestrator import StockTradingAgentOrchestrator
from agent.core.models import PositionDirection


@pytest.mark.asyncio
async def test_e2e_forex_orchestrator_cycle():
    forex_orch = TradingAgentOrchestrator()
    await forex_orch.set_mode("simulator")
    await forex_orch.initialize()

    # 1. Execute Scan Cycle
    await forex_orch.run_scan_cycle("EUR_USD")
    assert forex_orch.last_price is not None
    assert forex_orch.last_indicators is not None
    assert forex_orch.last_structure is not None
    assert forex_orch.last_decision is not None

    # 2. Manual Trade & Position Management
    pos = await forex_orch.execute_manual_trade("EUR_USD", "BUY", risk_pct=1.0)
    assert pos.instrument == "EUR_USD"
    assert pos.direction == PositionDirection.BUY
    assert pos.units > 0

    open_positions = await forex_orch.broker.get_open_positions()
    assert len(open_positions) >= 1

    # 3. Telemetry Integrity
    telemetry = await forex_orch.get_telemetry()
    assert telemetry.current_instrument == "EUR_USD"
    assert telemetry.account.currency == "EUR"
    assert len(telemetry.open_positions) >= 1

    # 4. Emergency Close
    closed = await forex_orch.emergency_close_all()
    assert closed >= 1
    assert len(await forex_orch.broker.get_open_positions()) == 0


@pytest.mark.asyncio
async def test_e2e_stock_orchestrator_multi_index_cycle():
    stock_orch = StockTradingAgentOrchestrator()
    await stock_orch.set_mode("simulator")
    await stock_orch.initialize()

    # 1. Multi-Symbol Screen across NASDAQ and Dow Jones
    screened = await stock_orch.screen_market_universe(limit=6)
    assert len(screened) > 0
    top_ticker = screened[0].symbol

    # 2. Switch to Top Screener Ticker & Scan
    await stock_orch.set_symbol(top_ticker)
    assert stock_orch.current_symbol == top_ticker
    assert stock_orch.last_price is not None
    assert stock_orch.last_indicators is not None

    # 3. Manual Quick Trade on Screened Ticker
    pos = await stock_orch.execute_manual_trade(top_ticker, "BUY", risk_pct=1.0)
    assert pos.instrument == top_ticker
    assert pos.units > 0

    # 4. Telemetry includes screener radar
    tel = await stock_orch.get_telemetry()
    assert tel.current_instrument == top_ticker
    assert tel.account.currency == "USD"
    assert tel.screener_candidates is not None
    assert len(tel.screener_candidates) > 0

    # 5. Clean Liquidation
    closed = await stock_orch.emergency_close_all()
    assert closed >= 1
    assert len(await stock_orch.broker.get_open_positions()) == 0

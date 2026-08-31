import pytest
from agent.core.stock_orchestrator import StockTradingAgentOrchestrator
from agent.core.models import PositionDirection


@pytest.mark.asyncio
async def test_stock_orchestrator_lifecycle():
    orch = StockTradingAgentOrchestrator()
    await orch.set_mode("simulator")
    await orch.initialize()

    # 1. Check Initial State
    telemetry = await orch.get_telemetry()
    assert telemetry.current_instrument == "AAPL"
    assert telemetry.account.currency == "USD"
    assert telemetry.account.equity >= 10000.0

    # 2. Run Scan Cycle
    await orch.run_scan_cycle("AAPL")
    assert orch.last_price is not None
    assert orch.last_indicators is not None
    assert orch.last_decision is not None

    # 3. Calculate Shares Size
    shares = orch.calculate_shares_size(equity=100000.0, entry_price=225.0, stop_loss_price=220.0, risk_pct=1.0)
    # Risk = 1000 USD, SL distance = 5 USD -> 200 shares
    assert shares == 200

    # 4. Manual Order Execution
    pos = await orch.execute_manual_trade("AAPL", "BUY", risk_pct=1.0)
    assert pos.instrument == "AAPL"
    assert pos.direction == PositionDirection.BUY
    assert pos.units > 0

    # 5. Emergency Close
    closed_count = await orch.emergency_close_all()
    assert closed_count >= 1
    assert len(await orch.broker.get_open_positions()) == 0

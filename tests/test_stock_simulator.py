import pytest
from agent.broker.stock_simulator import StockSimulatorBroker
from agent.core.models import OrderRequest, PositionDirection


@pytest.mark.asyncio
async def test_stock_simulator_lifecycle():
    sim = StockSimulatorBroker(initial_balance=100000.0, currency="USD")
    await sim.initialize()

    # 1. Price check
    price = await sim.get_current_price("AAPL")
    assert price.bid < price.ask
    assert price.spread_pips > 0
    assert price.mid > 100.0

    # 2. Candles check
    candles = await sim.get_candles("AAPL", count=50)
    assert len(candles) == 50
    assert candles[0].volume > 0

    # 3. Place order (Shares)
    order = OrderRequest(
        instrument="AAPL",
        direction=PositionDirection.BUY,
        units=25,
        stop_loss=220.0,
        take_profit=235.0
    )
    pos = await sim.place_order(order)
    assert pos.status.value == "OPEN"
    assert pos.units == 25
    assert len(await sim.get_open_positions()) == 1

    # 4. Account Summary
    summary = await sim.get_account_summary()
    assert summary.balance == 100000.0
    assert summary.currency == "USD"
    assert summary.open_positions_count == 1

    # 5. Update Stop Loss
    updated = await sim.update_stop_loss(pos.id, 222.0)
    assert updated.stop_loss == 222.0

    # 6. Close position
    closed = await sim.close_position(pos.id, reason="MANUAL_TEST")
    assert closed.status.value == "CLOSED"
    assert len(await sim.get_open_positions()) == 0
    assert len(await sim.get_closed_positions()) == 1

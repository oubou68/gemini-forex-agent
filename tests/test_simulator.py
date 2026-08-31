import pytest
from agent.broker.simulator import SimulatorBroker
from agent.core.models import OrderRequest, PositionDirection


@pytest.mark.asyncio
async def test_simulator_broker_lifecycle():
    sim = SimulatorBroker(initial_balance=10000.0)
    await sim.initialize()

    # 1. Price check
    price = await sim.get_current_price("EUR_USD")
    assert price.bid < price.ask
    assert price.spread_pips > 0

    # 2. Candles check
    candles = await sim.get_candles("EUR_USD", count=50)
    assert len(candles) == 50

    # 3. Place order
    order = OrderRequest(
        instrument="EUR_USD",
        direction=PositionDirection.BUY,
        units=10000,
        stop_loss=1.0800,
        take_profit=1.0900
    )
    pos = await sim.place_order(order)
    assert pos.status.value == "OPEN"
    assert len(await sim.get_open_positions()) == 1

    # 4. Account Summary
    summary = await sim.get_account_summary()
    assert summary.balance == 10000.0
    assert summary.open_positions_count == 1

    # 5. Close position
    closed = await sim.close_position(pos.id, reason="MANUAL_TEST")
    assert closed.status.value == "CLOSED"
    assert len(await sim.get_open_positions()) == 0
    assert len(await sim.get_closed_positions()) == 1

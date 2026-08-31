import pytest
from agent.core.multi_agent_manager import MultiAgentManager


@pytest.mark.asyncio
async def test_multi_agent_manager_lifecycle():
    manager = MultiAgentManager()
    await manager.forex_agent.set_mode("simulator")
    await manager.stock_agent.set_mode("simulator")
    await manager.initialize()

    # 1. Check all telemetry
    all_tel = await manager.get_all_telemetry()
    assert "forex" in all_tel
    assert "stock" in all_tel
    assert all_tel["forex"]["account"]["currency"] == "EUR"
    assert all_tel["stock"]["account"]["currency"] == "USD"

    # 2. Start / Stop individual bot
    await manager.start_bot("stock")
    assert manager.stock_agent.is_running is True

    await manager.stop_bot("stock")
    assert manager.stock_agent.is_running is False

    # 3. Emergency close
    closed = await manager.emergency_close_all()
    assert "forex" in closed
    assert "stock" in closed

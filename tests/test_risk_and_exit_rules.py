import pytest
from fastapi.testclient import TestClient
from web.app import app, agent_manager
from agent.risk.risk_manager import RiskManager
from agent.core.models import (
    GeminiTradeDecision, TradeAction, Position, PositionDirection, PositionStatus, AccountSummary
)

client = TestClient(app)


def test_risk_manager_configurable_parameters():
    rm = RiskManager(
        risk_per_trade_pct=1.5,
        max_open_positions=8,
        max_daily_drawdown_pct=4.0,
        default_atr_multiplier_sl=2.0,
        default_atr_multiplier_tp=3.0,
        min_risk_reward_ratio=2.0,
        allow_ai_close_signals=False,
        auto_liquidate_on_drawdown=True
    )
    assert rm.risk_per_trade_pct == 1.5
    assert rm.max_open_positions == 8
    assert rm.max_daily_drawdown_pct == 4.0
    assert rm.allow_ai_close_signals is False
    assert rm.auto_liquidate_on_drawdown is True

    # Update dynamically
    rm.update_risk_parameters(risk_per_trade_pct=0.8, allow_ai_close_signals=True)
    assert rm.risk_per_trade_pct == 0.8
    assert rm.allow_ai_close_signals is True


@pytest.mark.asyncio
async def test_rest_risk_config_and_clear_history():
    await agent_manager.forex_agent.set_mode("simulator")
    await agent_manager.stock_agent.set_mode("simulator")

    # 1. Update risk configuration via REST
    res = client.post("/api/config/risk", json={
        "risk_per_trade_pct": 0.5,
        "max_daily_drawdown_pct": 5.0,
        "max_open_positions": 6,
        "stock_max_open_positions": 15,
        "default_atr_multiplier_sl": 2.0,
        "default_atr_multiplier_tp": 3.5,
        "min_risk_reward_ratio": 1.8,
        "allow_ai_close_signals": False,
        "auto_liquidate_on_drawdown": False
    })
    assert res.status_code == 200
    assert res.json()["status"] == "risk_config_updated"

    assert agent_manager.forex_agent.risk_manager.risk_per_trade_pct == 0.5
    assert agent_manager.forex_agent.risk_manager.max_daily_drawdown_pct == 5.0
    assert agent_manager.forex_agent.risk_manager.allow_ai_close_signals is False

    assert agent_manager.stock_agent.risk_manager.risk_per_trade_pct == 0.5
    assert agent_manager.stock_agent.risk_manager.max_open_positions == 15
    assert agent_manager.stock_agent.risk_manager.allow_ai_close_signals is False

    # 2. Test Clear History endpoint
    pos = await agent_manager.stock_agent.execute_manual_trade("TSLA", "BUY", 1.0)
    await agent_manager.stock_agent.broker.close_position(pos.id, reason="MANUAL_TEST")
    
    # Trigger telemetry to sync closed trade into memory
    await agent_manager.stock_agent.get_telemetry()
    
    # Verify trade was recorded
    assert len(agent_manager.stock_agent.memory.trade_logs) > 0

    # Clear history
    clear_res = client.post("/api/trades/clear", json={"bot_type": "all"})
    assert clear_res.status_code == 200
    assert clear_res.json()["status"] == "history_cleared"
    assert len(agent_manager.stock_agent.memory.trade_logs) == 0
    assert len(agent_manager.forex_agent.memory.trade_logs) == 0

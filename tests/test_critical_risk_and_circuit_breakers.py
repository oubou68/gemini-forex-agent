import pytest
from agent.risk.risk_manager import RiskManager
from agent.broker.simulator import SimulatorBroker
from agent.broker.stock_simulator import StockSimulatorBroker
from agent.core.models import (
    GeminiTradeDecision, TradeAction, AccountSummary,
    Position, PositionDirection, OrderRequest
)


def test_circuit_breaker_exact_and_breached_thresholds():
    rm = RiskManager(max_daily_drawdown_pct=3.0)
    decision = GeminiTradeDecision(
        action=TradeAction.BUY,
        instrument="EUR_USD",
        confidence=80.0,
        risk_reward_ratio=2.0,
        suggested_risk_pct=1.0,
        entry_price=1.0850,
        stop_loss=1.0820,
        take_profit_1=1.0910
    )

    # 1. Below limit (2.9%) -> VALID
    acc_safe = AccountSummary(account_id="TEST", balance=10000.0, daily_drawdown_pct=2.9)
    valid_safe, reason_safe = rm.validate_trade_decision(decision, acc_safe, spread_pips=1.0, open_positions=[])
    assert valid_safe is True
    assert reason_safe == "OK"

    # 2. Exactly at limit (3.0%) -> REJECTED (CIRCUIT_BREAKER)
    acc_limit = AccountSummary(account_id="TEST", balance=10000.0, daily_drawdown_pct=3.0)
    valid_limit, reason_limit = rm.validate_trade_decision(decision, acc_limit, spread_pips=1.0, open_positions=[])
    assert valid_limit is False
    assert "CIRCUIT BREAKER" in reason_limit

    # 3. Far above limit (5.5%) -> REJECTED (CIRCUIT_BREAKER)
    acc_breached = AccountSummary(account_id="TEST", balance=10000.0, daily_drawdown_pct=5.5)
    valid_breached, reason_breached = rm.validate_trade_decision(decision, acc_breached, spread_pips=1.0, open_positions=[])
    assert valid_breached is False
    assert "CIRCUIT BREAKER" in reason_breached


def test_max_open_positions_guard():
    rm = RiskManager(max_open_positions=2)
    decision = GeminiTradeDecision(
        action=TradeAction.BUY,
        instrument="USD_JPY",
        confidence=75.0,
        risk_reward_ratio=2.0,
        suggested_risk_pct=1.0,
        entry_price=150.50,
        stop_loss=150.00,
        take_profit_1=151.50
    )
    account = AccountSummary(account_id="TEST", balance=10000.0, daily_drawdown_pct=0.5)

    # 1 position open -> Allowed
    pos1 = Position(id="p1", instrument="EUR_USD", direction=PositionDirection.BUY, units=10000, entry_price=1.0800, current_price=1.0805, open_time="2026-08-31T00:00:00Z")
    valid_1, _ = rm.validate_trade_decision(decision, account, spread_pips=1.0, open_positions=[pos1])
    assert valid_1 is True

    # 2 positions open (Limit reached) -> REJECTED
    pos2 = Position(id="p2", instrument="GBP_USD", direction=PositionDirection.BUY, units=10000, entry_price=1.2800, current_price=1.2810, open_time="2026-08-31T00:00:00Z")
    valid_2, reason_2 = rm.validate_trade_decision(decision, account, spread_pips=1.0, open_positions=[pos1, pos2])
    assert valid_2 is False
    assert "Positionslimit erreicht" in reason_2


def test_spread_guards_forex_and_stocks():
    rm_forex = RiskManager(max_spread_pips=2.5)
    decision = GeminiTradeDecision(
        action=TradeAction.BUY,
        instrument="EUR_USD",
        confidence=80.0,
        risk_reward_ratio=2.0,
        suggested_risk_pct=1.0,
        entry_price=1.0850,
        stop_loss=1.0820,
        take_profit_1=1.0910
    )
    account = AccountSummary(account_id="TEST", balance=10000.0, daily_drawdown_pct=0.0)

    # Normal spread -> OK
    valid_ok, _ = rm_forex.validate_trade_decision(decision, account, spread_pips=1.5, open_positions=[])
    assert valid_ok is True

    # Excessive spread -> REJECTED
    valid_spike, reason_spike = rm_forex.validate_trade_decision(decision, account, spread_pips=3.2, open_positions=[])
    assert valid_spike is False
    assert "Spread zu weit" in reason_spike


def test_breakeven_trailing_logic():
    rm = RiskManager(breakeven_trigger_r=1.0)

    # BUY Position: Entry = 1.0850, SL = 1.0830 (Risk = 20 pips). 1R target = 1.0870
    pos_buy = Position(
        id="buy_1",
        instrument="EUR_USD",
        direction=PositionDirection.BUY,
        units=10000,
        entry_price=1.0850,
        current_price=1.0850,
        stop_loss=1.0830,
        take_profit=1.0950,
        open_time="2026-08-31T00:00:00Z"
    )

    # Current price = 1.0860 (< 1R profit) -> No adjustment
    new_sl_no = rm.evaluate_breakeven_adjustment(pos_buy, current_price=1.0860)
    assert new_sl_no is None

    # Current price = 1.0875 (>= 1R profit) -> Move SL to entry + buffer
    new_sl_yes = rm.evaluate_breakeven_adjustment(pos_buy, current_price=1.0875)
    assert new_sl_yes is not None
    assert new_sl_yes >= 1.0850


@pytest.mark.asyncio
async def test_emergency_liquidation_all_brokers():
    # Forex Simulator multi-position close
    sim_forex = SimulatorBroker(initial_balance=10000.0)
    await sim_forex.initialize()
    await sim_forex.place_order(OrderRequest(instrument="EUR_USD", direction=PositionDirection.BUY, units=10000, stop_loss=1.0800))
    await sim_forex.place_order(OrderRequest(instrument="GBP_USD", direction=PositionDirection.SELL, units=10000, stop_loss=1.3000))
    assert len(await sim_forex.get_open_positions()) == 2

    # Liquidate all
    open_pos = await sim_forex.get_open_positions()
    for p in open_pos:
        await sim_forex.close_position(p.id, reason="EMERGENCY_CLOSE")
    assert len(await sim_forex.get_open_positions()) == 0

    # Stock Simulator multi-position close
    sim_stock = StockSimulatorBroker(initial_balance=100000.0)
    await sim_stock.initialize()
    await sim_stock.place_order(OrderRequest(instrument="AAPL", direction=PositionDirection.BUY, units=50, stop_loss=210.0))
    await sim_stock.place_order(OrderRequest(instrument="NVDA", direction=PositionDirection.BUY, units=40, stop_loss=120.0))
    assert len(await sim_stock.get_open_positions()) == 2

    open_stock_pos = await sim_stock.get_open_positions()
    for p in open_stock_pos:
        await sim_stock.close_position(p.id, reason="EMERGENCY_CLOSE")
    assert len(await sim_stock.get_open_positions()) == 0

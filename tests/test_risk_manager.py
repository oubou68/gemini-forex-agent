import pytest
from agent.risk.risk_manager import RiskManager
from agent.core.models import (
    GeminiTradeDecision, TradeAction, AccountSummary,
    Position, PositionDirection, PositionStatus
)


def test_position_sizing():
    rm = RiskManager(risk_per_trade_pct=1.0)
    equity = 10000.0
    entry = 1.08500
    sl = 1.08350  # 15 pips risk
    instrument = "EUR_USD"

    units = rm.calculate_position_size(equity, entry, sl, instrument)
    # Risk = 100 EUR. Diff = 0.0015 -> 100 / 0.0015 = 66,666 units
    assert 60000 <= units <= 70000


def test_risk_manager_validation_spread_reject():
    rm = RiskManager(max_spread_pips=2.5)
    account = AccountSummary(account_id="TEST", balance=10000.0, equity=10000.0)
    decision = GeminiTradeDecision(
        action=TradeAction.BUY,
        instrument="EUR_USD",
        confidence=80.0,
        entry_price=1.0850,
        stop_loss=1.0830,
        take_profit_1=1.0890
    )

    # Wide spread -> reject
    is_valid, reason = rm.validate_trade_decision(decision, account, spread_pips=3.2, open_positions=[])
    assert not is_valid
    assert "Spread zu weit" in reason


def test_risk_manager_circuit_breaker():
    rm = RiskManager(max_daily_drawdown_pct=3.0)
    account = AccountSummary(account_id="TEST", balance=10000.0, equity=9600.0, daily_drawdown_pct=4.0)
    decision = GeminiTradeDecision(
        action=TradeAction.BUY,
        instrument="EUR_USD",
        confidence=80.0,
        entry_price=1.0850,
        stop_loss=1.0830,
        take_profit_1=1.0890
    )

    is_valid, reason = rm.validate_trade_decision(decision, account, spread_pips=1.2, open_positions=[])
    assert not is_valid
    assert "CIRCUIT BREAKER" in reason


def test_breakeven_adjustment():
    rm = RiskManager(breakeven_trigger_r=1.0)
    pos = Position(
        id="P1",
        instrument="EUR_USD",
        direction=PositionDirection.BUY,
        units=10000,
        entry_price=1.0850,
        current_price=1.0870,  # 20 pips in profit
        stop_loss=1.0830,      # 20 pips initial risk
        open_time="2026-08-29T12:00:00Z"
    )

    # Profit (0.0020) >= 1R (0.0020) -> SL should adjust to entry + buffer
    new_sl = rm.evaluate_breakeven_adjustment(pos, current_price=1.0870)
    assert new_sl is not None
    assert new_sl >= 1.0850

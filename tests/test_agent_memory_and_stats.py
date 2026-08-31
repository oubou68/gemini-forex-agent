import tempfile
from pathlib import Path
from agent.ai.memory import AgentMemory
from agent.core.models import Position, PositionDirection, PositionStatus


def test_agent_memory_lifecycle():
    with tempfile.TemporaryDirectory() as tmp_dir:
        storage = Path(tmp_dir) / "test_memory.json"
        mem = AgentMemory(storage_path=str(storage))

        # Initial state should be empty
        stats_init = mem.get_performance_stats()
        assert stats_init.total_trades == 0
        assert stats_init.win_rate_pct == 0.0

        # Record 3 Winning Trades
        pos_w1 = Position(id="w1", instrument="EUR_USD", direction=PositionDirection.BUY, units=10000, entry_price=1.0800, current_price=1.0850, realized_pnl=50.0, open_time="2026-08-31T10:00:00Z", close_time="2026-08-31T10:30:00Z", close_reason="TAKE_PROFIT")
        pos_w2 = Position(id="w2", instrument="AAPL", direction=PositionDirection.BUY, units=100, entry_price=220.0, current_price=225.0, realized_pnl=500.0, open_time="2026-08-31T11:00:00Z", close_time="2026-08-31T11:45:00Z", close_reason="TAKE_PROFIT")
        pos_w3 = Position(id="w3", instrument="NVDA", direction=PositionDirection.BUY, units=50, entry_price=120.0, current_price=125.0, realized_pnl=250.0, open_time="2026-08-31T12:00:00Z", close_time="2026-08-31T12:20:00Z", close_reason="TAKE_PROFIT")

        # Record 1 Losing Trade
        pos_l1 = Position(id="l1", instrument="GBP_USD", direction=PositionDirection.SELL, units=10000, entry_price=1.2800, current_price=1.2830, realized_pnl=-30.0, open_time="2026-08-31T13:00:00Z", close_time="2026-08-31T13:15:00Z", close_reason="STOP_LOSS")

        mem.record_closed_trade(pos_w1, thesis="Bullish FVG retest")
        mem.record_closed_trade(pos_w2, thesis="ORB Breakout above VWAP")
        mem.record_closed_trade(pos_w3, thesis="RVOL Momentum surge")
        mem.record_closed_trade(pos_l1, thesis="False breakdown")

        stats = mem.get_performance_stats()
        assert stats.total_trades == 4
        assert stats.winning_trades == 3
        assert stats.losing_trades == 1
        assert stats.win_rate_pct == 75.0
        assert stats.total_pnl == (50.0 + 500.0 + 250.0 - 30.0) # 770.0
        assert stats.profit_factor > 1.0

        # Check Context String Formatting
        ctx = mem.get_recent_history_context(limit=3)
        assert "AAPL" in ctx
        assert "NVDA" in ctx
        assert "GBP_USD" in ctx

        # Check Persistence Reload
        mem_reloaded = AgentMemory(storage_path=str(storage))
        assert len(mem_reloaded.trade_logs) == 4
        assert mem_reloaded.get_performance_stats().win_rate_pct == 75.0

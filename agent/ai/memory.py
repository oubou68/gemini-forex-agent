import json
import logging
import os
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

from agent.core.models import Position, TradePerformanceStats, PositionStatus, PositionDirection

logger = logging.getLogger(__name__)


class AgentMemory:
    """
    Speichert historische Trade-Entscheidungen, PnL und Metriken
    und stellt Kontext für Gemini zur kontinuierlichen Selbstreflektion bereit.
    """

    def __init__(self, storage_path: Optional[str] = None):
        if storage_path:
            self.storage_path = Path(storage_path)
        elif os.environ.get("PYTEST_CURRENT_TEST"):
            self.storage_path = Path(__file__).resolve().parent.parent.parent / "data" / "test_trade_memory.json"
        else:
            self.storage_path = Path(__file__).resolve().parent.parent.parent / "data" / "trade_memory.json"
        
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.trade_logs: List[Dict[str, Any]] = []
        self._load_memory()

    def _load_memory(self):
        if self.storage_path.exists():
            try:
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    self.trade_logs = json.load(f)
            except Exception as e:
                logger.warning(f"Konnte Memory-Datei nicht laden: {e}")
                self.trade_logs = []

    def _save_memory(self):
        try:
            with open(self.storage_path, "w", encoding="utf-8") as f:
                json.dump(self.trade_logs, f, indent=2)
        except Exception as e:
            logger.error(f"Fehler beim Speichern der Memory-Datei: {e}")

    def clear_memory(self):
        """Leert den Trade-Verlauf und speichert die leere Historie ab."""
        self.trade_logs = []
        self._save_memory()
        logger.info("AgentMemory: Trade-Historie erfolgreich zurückgesetzt.")

    def record_closed_trade(self, position: Position, thesis: str = ""):
        dir_val = position.direction.value if hasattr(position.direction, "value") else str(position.direction)
        close_t = position.close_time or (datetime.now(timezone.utc).isoformat() + "Z")
        
        trade_entry = {
            "id": position.id,
            "instrument": position.instrument,
            "direction": dir_val,
            "units": position.units,
            "entry_price": position.entry_price,
            "exit_price": position.current_price,
            "realized_pnl": position.realized_pnl,
            "open_time": position.open_time,
            "close_time": close_t,
            "close_reason": position.close_reason or "CLOSED",
            "thesis": thesis
        }
        self.trade_logs.append(trade_entry)
        self._save_memory()

    def get_performance_stats(self) -> TradePerformanceStats:
        if not self.trade_logs:
            return TradePerformanceStats()

        total = len(self.trade_logs)
        wins = [t for t in self.trade_logs if t.get("realized_pnl", 0) > 0]
        losses = [t for t in self.trade_logs if t.get("realized_pnl", 0) <= 0]
        
        winning_count = len(wins)
        losing_count = len(losses)
        win_rate = (winning_count / total * 100) if total > 0 else 0.0

        gross_profit = sum(t.get("realized_pnl", 0) for t in wins)
        gross_loss = abs(sum(t.get("realized_pnl", 0) for t in losses))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 1.0)
        total_pnl = sum(t.get("realized_pnl", 0) for t in self.trade_logs)

        avg_win = (gross_profit / winning_count) if winning_count > 0 else 0.0
        avg_loss = (gross_loss / losing_count) if losing_count > 0 else 0.0

        return TradePerformanceStats(
            total_trades=total,
            winning_trades=winning_count,
            losing_trades=losing_count,
            win_rate_pct=round(win_rate, 1),
            profit_factor=round(profit_factor, 2),
            total_pnl=round(total_pnl, 2),
            avg_win=round(avg_win, 2),
            avg_loss=round(avg_loss, 2)
        )

    def get_recent_history_context(self, limit: int = 5) -> str:
        recent = self.trade_logs[-limit:]
        if not recent:
            return "Keine vorherigen Trades in der Historie vorhanden."

        lines = []
        for t in recent:
            pnl_val = t.get("realized_pnl", 0)
            pnl_str = f"+{pnl_val}" if pnl_val >= 0 else f"{pnl_val}"
            lines.append(f"[{t['instrument']} {t['direction']}]: PnL={pnl_str} ({t.get('close_reason', 'CLOSED')})")
        
        return " | ".join(lines)

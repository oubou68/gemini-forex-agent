import json
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime

from agent.core.models import Position, TradePerformanceStats, PositionStatus

logger = logging.getLogger(__name__)


class AgentMemory:
    """
    Speichert historische Trade-Entscheidungen, PnL und Metriken
    und stellt Kontext für Gemini zur kontinuierlichen Selbstreflektion bereit.
    """

    def __init__(self, storage_path: Optional[str] = None):
        if storage_path:
            self.storage_path = Path(storage_path)
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

    def record_closed_trade(self, position: Position, thesis: str = ""):
        trade_entry = {
            "id": position.id,
            "instrument": position.instrument,
            "direction": position.direction.value,
            "units": position.units,
            "entry_price": position.entry_price,
            "exit_price": position.current_price,
            "realized_pnl": position.realized_pnl,
            "open_time": position.open_time,
            "close_time": position.close_time or datetime.utcnow().isoformat(),
            "close_reason": position.close_reason,
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
            pnl_str = f"+{t['realized_pnl']} EUR" if t['realized_pnl'] >= 0 else f"{t['realized_pnl']} EUR"
            lines.append(f"[{t['instrument']} {t['direction']}]: PnL={pnl_str} ({t.get('close_reason', 'CLOSED')})")
        return " | ".join(lines)

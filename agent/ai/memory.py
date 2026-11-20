import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

from agent.core.models import Position, TradePerformanceStats

logger = logging.getLogger(__name__)


class AgentMemory:
    """
    Speichert historische Trade-Entscheidungen, PnL und Metriken getrennt je Bot-Namespace
    und stellt Kontext für Gemini zur kontinuierlichen Selbstreflektion & Cooldown-Steuerung bereit.
    """

    def __init__(self, storage_path: Optional[str] = None, namespace: Optional[str] = None):
        data_dir = Path(__file__).resolve().parent.parent.parent / "data"
        is_test_env = bool(os.environ.get("PYTEST_CURRENT_TEST")) or ("pytest" in sys.modules)
        if storage_path:
            self.storage_path = Path(storage_path)
        elif is_test_env:
            prefix = f"test_{namespace}_" if namespace else "test_"
            self.storage_path = data_dir / f"{prefix}trade_memory.json"
        else:
            prefix = f"{namespace}_" if namespace else ""
            self.storage_path = data_dir / f"{prefix}trade_memory.json"

        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.trade_logs: List[Dict[str, Any]] = []
        self._load_memory()

    def _load_memory(self):
        if self.storage_path.exists():
            try:
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    self.trade_logs = loaded if isinstance(loaded, list) else []
            except Exception as e:
                logger.warning(f"Konnte Memory-Datei nicht laden: {e}")
                self.trade_logs = []

    def _save_memory(self):
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=str(self.storage_path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self.trade_logs, f, indent=2)
                os.replace(tmp_path, self.storage_path)
            except Exception:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise
        except Exception as e:
            logger.error(f"Fehler beim Speichern der Memory-Datei: {e}")

    def clear_memory(self):
        """Leert den Trade-Verlauf und speichert die leere Historie ab."""
        self.trade_logs = []
        self._save_memory()
        logger.info("AgentMemory: Trade-Historie erfolgreich zurückgesetzt.")

    def record_closed_trade(self, position: Position, thesis: str = ""):
        # Verhindere Duplikate bei mehrfachem Sync
        if any(t.get("id") == position.id for t in self.trade_logs):
            return

        dir_val = position.direction.value if hasattr(position.direction, "value") else str(position.direction)
        close_t = position.close_time or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

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

    def get_performance_stats(self, reference_capital: float = 10000.0) -> TradePerformanceStats:
        if not self.trade_logs:
            return TradePerformanceStats()

        total = len(self.trade_logs)
        wins = [t for t in self.trade_logs if t.get("realized_pnl", 0) > 0]
        losses = [t for t in self.trade_logs if t.get("realized_pnl", 0) <= 0]

        winning_count = len(wins)
        losing_count = len(losses)
        win_rate = (winning_count / total * 100.0) if total > 0 else 0.0

        gross_profit = sum(t.get("realized_pnl", 0.0) for t in wins)
        gross_loss = abs(sum(t.get("realized_pnl", 0.0) for t in losses))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 1.0)
        total_pnl = sum(t.get("realized_pnl", 0.0) for t in self.trade_logs)

        avg_win = (gross_profit / winning_count) if winning_count > 0 else 0.0
        avg_loss = (gross_loss / losing_count) if losing_count > 0 else 0.0

        # Peak-to-trough Max Drawdown % auf kumulierter Equity-Kurve
        equity = reference_capital
        peak = equity
        max_dd_pct = 0.0
        for t in self.trade_logs:
            equity += float(t.get("realized_pnl", 0.0))
            if equity > peak:
                peak = equity
            if peak > 0:
                dd = (peak - equity) / peak * 100.0
                if dd > max_dd_pct:
                    max_dd_pct = dd

        expectancy = ((win_rate / 100.0) * avg_win) - ((1.0 - win_rate / 100.0) * avg_loss)

        return TradePerformanceStats(
            total_trades=total,
            winning_trades=winning_count,
            losing_trades=losing_count,
            win_rate_pct=round(win_rate, 1),
            profit_factor=round(profit_factor, 2),
            total_pnl=round(total_pnl, 2),
            max_drawdown_pct=round(max_dd_pct, 2),
            avg_win=round(avg_win, 2),
            avg_loss=round(avg_loss, 2),
            expectancy=round(expectancy, 2)
        )

    def get_consecutive_losses(self) -> int:
        """Zählt aktuelle ununterbrochene Verlust-Trades am Ende der Historie."""
        streak = 0
        for t in reversed(self.trade_logs):
            if float(t.get("realized_pnl", 0.0)) < 0:
                streak += 1
            else:
                break
        return streak

    def get_instrument_loss_cooldown_remaining(self, instrument: str, cooldown_seconds: int = 300) -> float:
        """
        Prüft, ob das Instrument kürzlich mit Verlust ausgestoppt wurde.
        Gibt die verbleibende Cooldown-Zeit in Sekunden zurück (0.0 wenn kein Cooldown aktiv).
        """
        if cooldown_seconds <= 0 or not self.trade_logs:
            return 0.0

        target = instrument.replace("/", "_").upper()
        now = datetime.now(timezone.utc)
        for t in reversed(self.trade_logs[-20:]):
            t_inst = str(t.get("instrument", "")).replace("/", "_").upper()
            if t_inst != target:
                continue
            pnl = float(t.get("realized_pnl", 0.0))
            reason = str(t.get("close_reason", "")).upper()
            if pnl < 0 and ("SL" in reason or "STOP" in reason):
                close_str = str(t.get("close_time", ""))
                try:
                    close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
                    if close_dt.tzinfo is None:
                        close_dt = close_dt.replace(tzinfo=timezone.utc)
                    elapsed = (now - close_dt).total_seconds()
                    if 0 <= elapsed < cooldown_seconds:
                        return round(cooldown_seconds - elapsed, 1)
                except Exception:
                    pass
            break
        return 0.0

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

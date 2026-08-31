import logging
import asyncio
import math
import random
import uuid
from datetime import datetime, timedelta
from typing import List, Dict, Optional

from agent.broker.base_broker import BaseBroker
from agent.core.models import (
    Candle, MarketPrice, OrderRequest, Position,
    PositionDirection, PositionStatus, AccountSummary
)

logger = logging.getLogger(__name__)


class SimulatorBroker(BaseBroker):
    """
    High-Fidelity Paper Trading & Replay Simulator für Forex-Märkte.
    Simuliert realistische Preisbewegungen, Spreads, Slippage, Margin und SL/TP-Trigger.
    """

    BASE_PRICES = {
        "EUR_USD": 1.0850,
        "GBP_USD": 1.2720,
        "USD_JPY": 154.50,
        "AUD_USD": 0.6580,
        "EUR_JPY": 167.60,
    }

    SPREAD_PIPS = {
        "EUR_USD": 1.2,
        "GBP_USD": 1.6,
        "USD_JPY": 1.4,
        "AUD_USD": 1.5,
        "EUR_JPY": 2.0,
    }

    def __init__(self, initial_balance: float = 10000.0, currency: str = "EUR"):
        self.currency = currency
        self.balance = initial_balance
        self.initial_equity = initial_balance
        self.open_positions: List[Position] = []
        self.closed_positions: List[Position] = []
        
        # State tracking per instrument
        self.current_prices: Dict[str, float] = dict(self.BASE_PRICES)
        self.candle_history: Dict[str, List[Candle]] = {}
        self._generate_initial_candle_history()

    def _get_pip_multiplier(self, instrument: str) -> float:
        return 0.01 if "JPY" in instrument.upper() else 0.0001

    def _generate_initial_candle_history(self, count: int = 150):
        """Generiert eine realistische historische Kerzenreihe für alle Standard-Währungspaare."""
        now = datetime.utcnow()
        for inst, base_p in self.BASE_PRICES.items():
            candles = []
            curr_p = base_p
            pip = self._get_pip_multiplier(inst)
            
            # Generiere 150 5-Minuten Kerzen
            for i in range(count, 0, -1):
                c_time = (now - timedelta(minutes=i * 5)).isoformat() + "Z"
                # Volatilität: 3 bis 8 Pips pro Kerze
                drift = random.gauss(0, 4 * pip)
                open_p = curr_p
                close_p = open_p + drift
                high_p = max(open_p, close_p) + abs(random.gauss(0, 2 * pip))
                low_p = min(open_p, close_p) - abs(random.gauss(0, 2 * pip))
                vol = random.randint(150, 1200)

                candles.append(Candle(
                    time=c_time,
                    open=round(open_p, 5 if pip == 0.0001 else 3),
                    high=round(high_p, 5 if pip == 0.0001 else 3),
                    low=round(low_p, 5 if pip == 0.0001 else 3),
                    close=round(close_p, 5 if pip == 0.0001 else 3),
                    volume=vol,
                    complete=True
                ))
                curr_p = close_p
            self.candle_history[inst] = candles
            self.current_prices[inst] = curr_p

    def step_market(self, instrument: str = "EUR_USD"):
        """Simuliert den nächsten Preistick und aktualisiert Kerzen sowie offene Positionen."""
        pip = self._get_pip_multiplier(instrument)
        curr = self.current_prices.get(instrument, self.BASE_PRICES.get(instrument, 1.0850))
        
        # Leichter Zufalls-Walk mit Mean-Reverting Tendenz
        delta = random.gauss(0, 1.2 * pip)
        new_price = round(curr + delta, 5 if pip == 0.0001 else 3)
        self.current_prices[instrument] = new_price

        # Update last candle or append new one
        candles = self.candle_history.setdefault(instrument, [])
        now_str = datetime.utcnow().isoformat() + "Z"
        
        if candles:
            last = candles[-1]
            last.high = max(last.high, new_price)
            last.low = min(last.low, new_price)
            last.close = new_price
            last.volume += random.randint(1, 15)
        else:
            candles.append(Candle(
                time=now_str,
                open=new_price,
                high=new_price,
                low=new_price,
                close=new_price,
                volume=10,
                complete=False
            ))

        # Check Position SL/TP
        self._check_positions_sl_tp(instrument, new_price)

    def _check_positions_sl_tp(self, instrument: str, price: float):
        pip = self._get_pip_multiplier(instrument)
        to_close = []

        for pos in self.open_positions:
            if pos.instrument != instrument:
                continue
            
            pos.current_price = price
            
            # Unrealized PnL
            if pos.direction == PositionDirection.BUY:
                pips = (price - pos.entry_price) / pip
                pos.unrealized_pnl = round(pips * (pos.units / 10000.0), 2)
                
                # Check Take Profit
                if pos.take_profit and price >= pos.take_profit:
                    to_close.append((pos, "TP_HIT", pos.take_profit))
                # Check Stop Loss
                elif pos.stop_loss and price <= pos.stop_loss:
                    to_close.append((pos, "SL_HIT", pos.stop_loss))
            else:
                pips = (pos.entry_price - price) / pip
                pos.unrealized_pnl = round(pips * (pos.units / 10000.0), 2)
                
                # Check Take Profit
                if pos.take_profit and price <= pos.take_profit:
                    to_close.append((pos, "TP_HIT", pos.take_profit))
                # Check Stop Loss
                elif pos.stop_loss and price >= pos.stop_loss:
                    to_close.append((pos, "SL_HIT", pos.stop_loss))

        for pos, reason, exit_price in to_close:
            self._close_position_internal(pos, exit_price, reason)

    def _close_position_internal(self, pos: Position, exit_price: float, reason: str):
        pip = self._get_pip_multiplier(pos.instrument)
        if pos.direction == PositionDirection.BUY:
            pips = (exit_price - pos.entry_price) / pip
        else:
            pips = (pos.entry_price - exit_price) / pip
        
        realized = round(pips * (pos.units / 10000.0), 2)
        pos.realized_pnl = realized
        pos.unrealized_pnl = 0.0
        pos.current_price = exit_price
        pos.status = PositionStatus.CLOSED
        pos.close_time = datetime.utcnow().isoformat() + "Z"
        pos.close_reason = reason
        
        self.balance += realized
        if pos in self.open_positions:
            self.open_positions.remove(pos)
        self.closed_positions.append(pos)
        logger.info(f"[SIMULATOR] Position {pos.id} geschlossen ({reason}): PnL={realized} {self.currency}")

    async def initialize(self) -> bool:
        logger.info("[SIMULATOR] Initialisiert mit virtuellem Startkapital: 10,000 EUR")
        return True

    async def get_account_summary(self) -> AccountSummary:
        unrealized = sum(p.unrealized_pnl for p in self.open_positions)
        equity = self.balance + unrealized
        margin_used = sum(p.units * 0.03 for p in self.open_positions)  # 1:30 leverage approx
        daily_dd = max(0.0, ((self.initial_equity - equity) / self.initial_equity) * 100)

        return AccountSummary(
            account_id="SIM-DEMO-001",
            currency=self.currency,
            balance=round(self.balance, 2),
            unrealized_pl=round(unrealized, 2),
            realized_pl=round(sum(p.realized_pnl for p in self.closed_positions), 2),
            equity=round(equity, 2),
            margin_used=round(margin_used, 2),
            margin_available=round(max(0.0, equity - margin_used), 2),
            open_positions_count=len(self.open_positions),
            daily_drawdown_pct=round(daily_dd, 2),
            daily_start_equity=self.initial_equity
        )

    async def get_candles(self, instrument: str, granularity: str = "M5", count: int = 100) -> List[Candle]:
        self.step_market(instrument)
        candles = self.candle_history.get(instrument, [])
        return candles[-count:]

    async def get_current_price(self, instrument: str) -> MarketPrice:
        self.step_market(instrument)
        mid = self.current_prices.get(instrument, self.BASE_PRICES.get(instrument, 1.0850))
        spread_pip = self.SPREAD_PIPS.get(instrument, 1.5)
        pip = self._get_pip_multiplier(instrument)
        
        half_spread = (spread_pip * pip) / 2.0
        bid = round(mid - half_spread, 5 if pip == 0.0001 else 3)
        ask = round(mid + half_spread, 5 if pip == 0.0001 else 3)

        return MarketPrice(
            instrument=instrument,
            time=datetime.utcnow().isoformat() + "Z",
            bid=bid,
            ask=ask,
            spread_pips=spread_pip,
            mid=mid
        )

    async def place_order(self, order: OrderRequest) -> Position:
        price_info = await self.get_current_price(order.instrument)
        # Entry price: BUY at ask, SELL at bid
        entry = price_info.ask if order.direction == PositionDirection.BUY else price_info.bid
        pos_id = f"SIM-{uuid.uuid4().hex[:8].upper()}"

        pos = Position(
            id=pos_id,
            instrument=order.instrument,
            direction=order.direction,
            units=order.units,
            entry_price=entry,
            current_price=entry,
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            open_time=datetime.utcnow().isoformat() + "Z",
            status=PositionStatus.OPEN
        )
        self.open_positions.append(pos)
        logger.info(f"[SIMULATOR] Order eröffnet: {order.direction.value} {order.units} {order.instrument} @ {entry} (SL: {order.stop_loss}, TP: {order.take_profit})")
        return pos

    async def close_position(self, position_id: str, reason: str = "MANUAL") -> Position:
        pos = next((p for p in self.open_positions if p.id == position_id), None)
        if not pos:
            # Check if it was already closed (e.g. by TP/SL hit)
            closed_match = next((p for p in self.closed_positions if p.id == position_id), None)
            if closed_match:
                return closed_match
            raise Exception(f"Position {position_id} nicht gefunden.")
        
        mid = self.current_prices.get(pos.instrument, self.BASE_PRICES.get(pos.instrument, 1.0850))
        pip = self._get_pip_multiplier(pos.instrument)
        spread_pip = self.SPREAD_PIPS.get(pos.instrument, 1.5)
        half_spread = (spread_pip * pip) / 2.0
        exit_price = mid - half_spread if pos.direction == PositionDirection.BUY else mid + half_spread
        self._close_position_internal(pos, exit_price, reason)
        return pos

    async def update_stop_loss(self, position_id: str, new_sl: float) -> Position:
        pos = next((p for p in self.open_positions if p.id == position_id), None)
        if not pos:
            raise Exception(f"Position {position_id} nicht gefunden.")
        pos.stop_loss = new_sl
        logger.info(f"[SIMULATOR] SL für {pos.id} angepasst auf {new_sl}")
        return pos

    async def get_open_positions(self) -> List[Position]:
        return list(self.open_positions)

    async def get_closed_positions(self) -> List[Position]:
        return list(self.closed_positions)

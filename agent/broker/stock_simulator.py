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


class StockSimulatorBroker(BaseBroker):
    """
    High-Fidelity Paper Trading Simulator für US-Aktien & ETFs (Alpaca Modus).
    Unterstützt das gesamte NASDAQ-100 Universum, den Dow Jones Industrial Average (DJIA 30)
    sowie beliebige US-Equities & Index-ETFs mit realistischer M5-Dynamik.
    """

    BASE_STOCK_PRICES: Dict[str, float] = {
        # Magnificent 7 & Mega-Caps
        "AAPL": 227.50,
        "MSFT": 448.20,
        "NVDA": 128.80,
        "AMZN": 182.40,
        "GOOGL": 165.30,
        "GOOG": 166.50,
        "META": 512.60,
        "TSLA": 214.20,
        "AVGO": 158.30,

        # Major Index ETFs
        "SPY": 564.80,
        "QQQ": 486.20,
        "DIA": 412.50,
        "IWM": 218.40,
        "SMH": 242.10,
        "XLK": 224.50,
        "XLF": 44.80,
        "XLE": 89.60,

        # Dow Jones 30 Blue Chips
        "UNH": 582.40,
        "GS": 498.20,
        "HD": 372.50,
        "CAT": 354.10,
        "MCD": 286.40,
        "CRM": 262.30,
        "V": 274.80,
        "AMGN": 326.50,
        "BA": 162.80,
        "IBM": 194.20,
        "JNJ": 164.50,
        "DIS": 96.40,
        "JPM": 218.60,
        "WMT": 74.20,
        "PG": 168.30,
        "TRV": 228.40,
        "NKE": 82.50,
        "MRK": 118.40,
        "AXP": 252.10,
        "CVX": 146.50,
        "HON": 204.80,
        "CSCO": 51.20,
        "KO": 69.40,
        "MMM": 128.60,
        "DOW": 52.80,
        "VZ": 41.50,
        "INTC": 20.80,

        # NASDAQ-100 Top Growth & Tech
        "AMD": 156.40,
        "COST": 884.20,
        "PEP": 174.50,
        "ADBE": 532.10,
        "NFLX": 684.50,
        "QCOM": 168.20,
        "AMAT": 198.40,
        "TXN": 204.10,
        "INTU": 652.80,
        "ISRG": 458.20,
        "BKNG": 3940.00,
        "SBUX": 94.60,
        "VRTX": 482.10,
        "GILD": 78.40,
        "MDLZ": 71.20,
        "ADP": 272.50,
        "REGN": 1042.00,
        "PANW": 348.60,
        "KLAC": 724.30,
        "SNPS": 562.40,
        "CDNS": 288.50,
        "ASML": 842.10,
        "CRWD": 278.40,
        "ABNB": 118.50,
        "WBD": 7.80,
        "DASH": 128.40,
        "MCHP": 78.20,
        "DXCM": 74.50,
        "KDP": 36.40,
        "PAYX": 128.90,
        "ORLY": 1142.00,
        "CTAS": 784.50,
        "NXPI": 242.30,
        "MAR": 248.60,
        "CSX": 34.20,
        "MNST": 50.40,
        "PCAR": 98.60,
        "FTNT": 72.80,
        "CPRT": 54.20,
        "ROST": 148.50,
        "KHC": 34.80,
        "FAST": 68.20,
        "ODFL": 198.50,
        "BIIB": 204.60,
        "EXC": 39.40,
        "VRSK": 268.20,
        "IDXX": 482.40,
        "LULU": 264.80,
        "EA": 146.20,
        "GEHC": 86.40,
        "ILMN": 128.40,
        "ALGN": 242.60,
        "ZS": 192.40,
        "ANSS": 328.50,
        "TEAM": 178.60,
        "DDOG": 114.20,
        "ON": 72.40,
        "MRVL": 74.80,
        "MRNA": 82.50,
        "DLTR": 68.40,
        "SIRI": 3.80,
        "CEG": 188.50,
        "ARM": 134.20,
        "SMCI": 448.60,
        "PLTR": 31.50,
        "PYPL": 71.40,
        "SHOP": 74.80,
        "MELI": 1980.00,
        "COIN": 218.40,
        "MDB": 284.20,
        "TTD": 98.40,
        "TTWO": 164.20,
        "WDAY": 254.80,
        "MU": 104.50,
        "FANG": 188.40,
        "ADI": 214.50,
        "ADSK": 258.20,
    }

    def __init__(self, initial_balance: float = 100000.0, currency: str = "USD"):
        self.currency = currency
        self.balance = initial_balance
        self.initial_equity = initial_balance
        self.daily_start_equity = initial_balance
        self.open_positions: List[Position] = []
        self.closed_positions: List[Position] = []
        
        # State tracking per stock
        self.current_prices: Dict[str, float] = dict(self.BASE_STOCK_PRICES)
        self.candle_history: Dict[str, List[Candle]] = {}
        self._generate_initial_candle_history()

    def _ensure_symbol_initialized(self, symbol: str, count: int = 150):
        """Initialisiert eine neue Aktie on-demand mit konsistenten Startdaten."""
        symbol = symbol.replace("/", "").replace("_", "").upper()
        if symbol in self.candle_history and symbol in self.current_prices:
            return

        base_p = self.BASE_STOCK_PRICES.get(symbol)
        if not base_p:
            # Deterministischer Pseudo-Preis basierend auf Ticker-Hash
            hash_val = sum(ord(c) for c in symbol)
            base_p = round(50.0 + (hash_val % 450), 2)
            self.BASE_STOCK_PRICES[symbol] = base_p

        now = datetime.utcnow()
        candles = []
        curr_p = base_p
        vol_factor = base_p * 0.002

        for i in range(count, 0, -1):
            c_time = (now - timedelta(minutes=i * 5)).isoformat() + "Z"
            drift = random.gauss(0, vol_factor)
            open_p = curr_p
            close_p = max(1.0, open_p + drift)
            high_p = max(open_p, close_p) + abs(random.gauss(0, vol_factor * 0.6))
            low_p = min(open_p, close_p) - abs(random.gauss(0, vol_factor * 0.6))
            low_p = max(0.5, low_p)
            vol = random.randint(15000, 350000)

            candles.append(Candle(
                time=c_time,
                open=round(open_p, 2),
                high=round(high_p, 2),
                low=round(low_p, 2),
                close=round(close_p, 2),
                volume=vol,
                complete=True
            ))
            curr_p = close_p

        self.candle_history[symbol] = candles
        self.current_prices[symbol] = round(curr_p, 2)

    def _generate_initial_candle_history(self, count: int = 150):
        """Generiert eine realistische historische M5-Kerzenreihe für Standard-Aktien."""
        for symbol in list(self.BASE_STOCK_PRICES.keys())[:25]:
            self._ensure_symbol_initialized(symbol, count)

    def step_market(self, instrument: str = "AAPL"):
        """Simuliert den nächsten Aktien-Tick und prüft SL/TP-Trigger."""
        symbol = instrument.replace("/", "").replace("_", "").upper()
        self._ensure_symbol_initialized(symbol)

        curr = self.current_prices.get(symbol, 200.0)
        vol_factor = curr * 0.0015
        
        # Random Walk mit Momentum
        delta = random.gauss(0, vol_factor)
        new_price = max(1.0, round(curr + delta, 2))
        self.current_prices[symbol] = new_price

        # Update Kerzenverlauf
        candles = self.candle_history.setdefault(symbol, [])
        now_str = datetime.utcnow().isoformat() + "Z"
        
        if candles:
            last_c = candles[-1]
            last_c.close = new_price
            last_c.high = max(last_c.high, new_price)
            last_c.low = min(last_c.low, new_price)
            last_c.volume += random.randint(500, 4500)
            
            # Alle 5 Minuten neue Kerze
            try:
                last_time = datetime.fromisoformat(last_c.time.replace("Z", ""))
                if (datetime.utcnow() - last_time).total_seconds() > 300:
                    candles.append(Candle(
                        time=now_str,
                        open=new_price,
                        high=new_price,
                        low=new_price,
                        close=new_price,
                        volume=random.randint(10000, 50000),
                        complete=True
                    ))
                    if len(candles) > 300:
                        candles.pop(0)
            except Exception:
                pass
        else:
            self._ensure_symbol_initialized(symbol)

        # Offene Positionen evaluieren
        self._evaluate_positions()

    def _evaluate_positions(self):
        """Überprüft Stop-Loss und Take-Profit für alle offenen Positionen."""
        to_close = []
        for pos in self.open_positions:
            curr_p = self.current_prices.get(pos.instrument, pos.entry_price)
            pos.current_price = curr_p

            if pos.direction == PositionDirection.BUY:
                pos.unrealized_pnl = round((curr_p - pos.entry_price) * pos.units, 2)
                if pos.stop_loss and curr_p <= pos.stop_loss:
                    to_close.append((pos, "STOP_LOSS"))
                elif pos.take_profit and curr_p >= pos.take_profit:
                    to_close.append((pos, "TAKE_PROFIT"))
            else:
                pos.unrealized_pnl = round((pos.entry_price - curr_p) * pos.units, 2)
                if pos.stop_loss and curr_p >= pos.stop_loss:
                    to_close.append((pos, "STOP_LOSS"))
                elif pos.take_profit and curr_p <= pos.take_profit:
                    to_close.append((pos, "TAKE_PROFIT"))

        for pos, reason in to_close:
            self.close_position_sync(pos.id, reason)

    async def get_open_positions(self) -> List[Position]:
        return list(self.open_positions)

    async def get_closed_positions(self) -> List[Position]:
        return list(self.closed_positions)

    async def update_stop_loss(self, position_id: str, new_stop_loss: float) -> Position:
        for pos in self.open_positions:
            if pos.id == position_id:
                pos.stop_loss = round(new_stop_loss, 2)
                logger.info(f"[STOCK_SIMULATOR] SL für {pos.instrument} auf ${new_stop_loss:.2f} angepasst.")
                return pos
        raise ValueError(f"Position {position_id} nicht gefunden.")

    async def close_position(self, position_id: str, reason: str = "MANUAL") -> Position:
        return self.close_position_sync(position_id, reason)

    def close_position_sync(self, position_id: str, reason: str = "MANUAL") -> Position:
        pos = next((p for p in self.open_positions if p.id == position_id), None)
        if not pos:
            raise ValueError(f"Position {position_id} nicht gefunden.")

        self.open_positions.remove(pos)
        pos.status = PositionStatus.CLOSED
        pos.close_time = datetime.utcnow().isoformat() + "Z"
        pos.close_reason = reason
        pos.realized_pnl = pos.unrealized_pnl
        pos.unrealized_pnl = 0.0
        self.balance += pos.realized_pnl
        self.balance = round(self.balance, 2)
        self.closed_positions.append(pos)
        logger.info(f"[STOCK_SIMULATOR] Position {pos.id} ({pos.instrument}) geschlossen ({reason}). Realisierter PnL: ${pos.realized_pnl:,.2f}")
        return pos

    async def initialize(self) -> bool:
        logger.info(f"Stock Simulator Broker initialisiert. Startkapital: ${self.balance:,.2f} {self.currency}")
        return True

    async def get_account_summary(self) -> AccountSummary:
        unrealized = sum(p.unrealized_pnl for p in self.open_positions)
        realized = sum(p.realized_pnl for p in self.closed_positions)
        equity = round(self.balance + unrealized, 2)
        margin_used = round(sum(p.current_price * p.units for p in self.open_positions), 2)
        margin_avail = round(max(0.0, equity * 2 - margin_used), 2)  # 2x Intraday Buying Power

        daily_dd_pct = 0.0
        if self.daily_start_equity > 0:
            dd = (self.daily_start_equity - equity) / self.daily_start_equity * 100.0
            daily_dd_pct = max(0.0, round(dd, 2))

        return AccountSummary(
            account_id="SIM_ALPACA_STOCKS_101",
            currency=self.currency,
            balance=self.balance,
            unrealized_pl=round(unrealized, 2),
            realized_pl=round(realized, 2),
            equity=equity,
            margin_used=margin_used,
            margin_available=margin_avail,
            open_positions_count=len(self.open_positions),
            daily_drawdown_pct=daily_dd_pct,
            daily_start_equity=self.daily_start_equity
        )

    async def get_candles(self, instrument: str, granularity: str = "M5", count: int = 100) -> List[Candle]:
        symbol = instrument.replace("/", "").replace("_", "").upper()
        self._ensure_symbol_initialized(symbol)
        self.step_market(symbol)
        candles = self.candle_history.get(symbol, [])
        return candles[-count:] if len(candles) >= count else candles

    async def get_current_price(self, instrument: str) -> MarketPrice:
        symbol = instrument.replace("/", "").replace("_", "").upper()
        self._ensure_symbol_initialized(symbol)
        self.step_market(symbol)
        mid = self.current_prices.get(symbol, 200.0)
        spread = 0.02 if mid < 100 else (0.03 if mid < 300 else 0.05)
        bid = round(mid - spread / 2.0, 2)
        ask = round(mid + spread / 2.0, 2)

        return MarketPrice(
            instrument=symbol,
            time=datetime.utcnow().isoformat() + "Z",
            bid=bid,
            ask=ask,
            spread_pips=round(spread, 2),  # USD Cents
            mid=mid
        )

    async def place_order(self, order: OrderRequest) -> Position:
        symbol = order.instrument.replace("/", "").replace("_", "").upper()
        self._ensure_symbol_initialized(symbol)
        self.step_market(symbol)
        price_info = await self.get_current_price(symbol)
        
        # Slippage Simulation ($0.01)
        slippage = 0.01
        entry_price = price_info.ask + slippage if order.direction == PositionDirection.BUY else price_info.bid - slippage
        entry_price = round(entry_price, 2)

        pos_id = f"stk_{uuid.uuid4().hex[:8]}"
        pos = Position(
            id=pos_id,
            instrument=symbol,
            direction=order.direction,
            units=max(1, order.units),
            entry_price=entry_price,
            current_price=entry_price,
            stop_loss=round(order.stop_loss, 2) if order.stop_loss else None,
            take_profit=round(order.take_profit, 2) if order.take_profit else None,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            open_time=datetime.utcnow().isoformat() + "Z",
            status=PositionStatus.OPEN
        )
        self.open_positions.append(pos)
        logger.info(f"[STOCK_SIMULATOR] Order ausgeführt: {order.direction.value} {pos.units} Shares {symbol} @ ${entry_price:.2f}")
        return pos

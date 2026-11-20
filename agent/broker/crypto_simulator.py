import logging
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional

from agent.broker.base_broker import BaseBroker
from agent.broker.alpaca_crypto_client import normalize_crypto_symbol
from agent.analysis.crypto_indicators import _crypto_precision
from agent.core.models import (
    Candle, MarketPrice, OrderRequest, Position,
    PositionDirection, PositionStatus, AccountSummary
)

logger = logging.getLogger(__name__)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class CryptoSimulatorBroker(BaseBroker):
    """
    High-Fidelity 24/7 Spot Crypto Paper Simulator für alle Top 20 Alpaca Kryptowährungen.
    Unterstützt fraktionale Mengen (z.B. 0.015 BTC bis Millionen SHIB/PEPE),
    realistische Krypto-Volatilität, 0.15% Spot-Gebühren/Spread sowie Partial TP1 Scale-Outs.
    """

    BASE_CRYPTO_PRICES: Dict[str, float] = {
        # Tier 1: Core Large-Cap Anchors (Nov 2026 baseline)
        "BTC/USD": 64500.00,
        "ETH/USD": 1805.00,
        "SOL/USD": 73.20,
        "XRP/USD": 0.9250,

        # Tier 2: High-Liquidity L1, Infrastructure & DeFi
        "LINK/USD": 11.85,
        "AVAX/USD": 16.40,
        "DOT/USD": 3.85,
        "UNI/USD": 6.45,
        "AAVE/USD": 142.50,
        "LTC/USD": 45.20,
        "BCH/USD": 285.00,
        "GRT/USD": 0.1480,
        "XTZ/USD": 0.6850,
        "CRV/USD": 0.4120,
        "MKR/USD": 1340.00,

        # Tier 3: High-Beta Momentum & Meme Catalysts
        "DOGE/USD": 0.1420,
        "SHIB/USD": 0.00001250,
        "PEPE/USD": 0.00000820,
        "SUSHI/USD": 0.7450,
        "BAT/USD": 0.1950,
    }

    TAKER_FEE_RATE = 0.0015  # 0.15% realistische Spot-Taker-Gebühr

    def __init__(self, initial_balance: float = 50000.0, currency: str = "USD"):
        self.currency = currency
        self.balance = initial_balance
        self.initial_equity = initial_balance
        self.daily_start_equity = initial_balance
        self.open_positions: List[Position] = []
        self.closed_positions: List[Position] = []

        self.current_prices: Dict[str, float] = dict(self.BASE_CRYPTO_PRICES)
        self.candle_history: Dict[str, List[Candle]] = {}
        self._generate_initial_candle_history()

    def _ensure_symbol_initialized(self, symbol: str, count: int = 120):
        sym = normalize_crypto_symbol(symbol)
        if sym in self.candle_history and sym in self.current_prices:
            return

        base_p = self.BASE_CRYPTO_PRICES.get(sym, 10.0)
        prec = _crypto_precision(base_p)
        # Höhere Volatilität für Altcoins / Meme-Coins
        vol_pct = 0.0022 if sym in ("BTC/USD", "ETH/USD") else (0.0045 if sym in ("DOGE/USD", "SHIB/USD", "PEPE/USD") else 0.0032)

        now = datetime.now(timezone.utc)
        candles = []
        curr_p = base_p

        for i in range(count, 0, -1):
            c_time = (now - timedelta(minutes=i * 5)).isoformat().replace("+00:00", "Z")
            drift = random.gauss(0, curr_p * vol_pct)
            open_p = curr_p
            close_p = max(base_p * 0.1, open_p + drift)
            wick = abs(random.gauss(0, curr_p * vol_pct * 0.7))
            high_p = max(open_p, close_p) + wick
            low_p = max(base_p * 0.05, min(open_p, close_p) - wick)
            vol = random.randint(25000, 650000)

            candles.append(Candle(
                time=c_time,
                open=round(open_p, prec),
                high=round(high_p, prec),
                low=round(low_p, prec),
                close=round(close_p, prec),
                volume=vol,
                complete=True
            ))
            curr_p = close_p

        self.candle_history[sym] = candles
        self.current_prices[sym] = round(curr_p, prec)

    def _generate_initial_candle_history(self, count: int = 120):
        for sym in list(self.BASE_CRYPTO_PRICES.keys()):
            self._ensure_symbol_initialized(sym, count)

    def step_market(self, instrument: str = "BTC/USD"):
        sym = normalize_crypto_symbol(instrument)
        self._ensure_symbol_initialized(sym)

        curr = self.current_prices.get(sym, 100.0)
        prec = _crypto_precision(curr)
        vol_pct = 0.0018 if sym in ("BTC/USD", "ETH/USD") else (0.0038 if sym in ("DOGE/USD", "SHIB/USD", "PEPE/USD") else 0.0028)

        delta = random.gauss(0, curr * vol_pct)
        new_price = max(curr * 0.5, round(curr + delta, prec))
        self.current_prices[sym] = new_price

        candles = self.candle_history.setdefault(sym, [])
        now_dt = datetime.now(timezone.utc)
        now_str = now_dt.isoformat().replace("+00:00", "Z")

        if candles:
            last_c = candles[-1]
            last_c.close = new_price
            last_c.high = max(last_c.high, new_price)
            last_c.low = min(last_c.low, new_price)
            last_c.volume += random.randint(1000, 15000)
            try:
                last_time = datetime.fromisoformat(last_c.time.replace("Z", "+00:00"))
                if last_time.tzinfo is None:
                    last_time = last_time.replace(tzinfo=timezone.utc)
                if (now_dt - last_time).total_seconds() > 300:
                    candles.append(Candle(
                        time=now_str,
                        open=new_price,
                        high=new_price,
                        low=new_price,
                        close=new_price,
                        volume=random.randint(20000, 120000),
                        complete=True
                    ))
                    if len(candles) > 300:
                        candles.pop(0)
            except Exception:
                pass

        self._evaluate_positions()

    def _evaluate_positions(self):
        to_close = []
        for pos in self.open_positions:
            curr_p = self.current_prices.get(pos.instrument, pos.entry_price)
            pos.current_price = curr_p
            pos.highest_price = max(pos.highest_price or pos.entry_price, curr_p)
            pos.unrealized_pnl = round((curr_p - pos.entry_price) * pos.units, 2)

            if pos.stop_loss and curr_p <= pos.stop_loss:
                to_close.append((pos, "SL_HIT", pos.stop_loss))
            elif pos.take_profit and curr_p >= pos.take_profit:
                to_close.append((pos, "TP_HIT", pos.take_profit))

        for pos, reason, exit_p in to_close:
            self._close_position_internal(pos, exit_p, reason)

    def _close_position_internal(self, pos: Position, exit_price: float, reason: str):
        gross_pnl = (exit_price - pos.entry_price) * pos.units
        exit_fee = exit_price * pos.units * self.TAKER_FEE_RATE
        realized = round(gross_pnl - exit_fee, 2)

        if pos in self.open_positions:
            self.open_positions.remove(pos)

        # Gutschrift des Verkaufserlöses abzgl. Gebühr (im Spot-Cash-Modell)
        self.balance = round(self.balance + (exit_price * pos.units - exit_fee), 2)
        pos.status = PositionStatus.CLOSED
        pos.current_price = exit_price
        pos.realized_pnl = realized
        pos.unrealized_pnl = 0.0
        pos.close_time = _utc_iso()
        pos.close_reason = reason
        self.closed_positions.append(pos)
        logger.info(f"[CRYPTO_SIM] Position {pos.id} ({pos.instrument}) geschlossen ({reason}): PnL=${realized:,.2f}")

    async def initialize(self) -> bool:
        logger.info(f"[CRYPTO_SIM] Initialisiert mit 20 Top-Kryptos & Startkapital: ${self.balance:,.2f} USD")
        return True

    async def get_account_summary(self) -> AccountSummary:
        positions_market_value = sum(p.current_price * p.units for p in self.open_positions)
        unrealized = sum(p.unrealized_pnl for p in self.open_positions)
        realized = sum(p.realized_pnl for p in self.closed_positions)
        equity = round(self.balance + positions_market_value, 2)

        daily_dd_pct = 0.0
        if self.daily_start_equity > 0:
            dd = (self.daily_start_equity - equity) / self.daily_start_equity * 100.0
            daily_dd_pct = max(0.0, round(dd, 2))

        return AccountSummary(
            account_id="SIM_ALPACA_CRYPTO_24_7",
            currency="USD",
            balance=round(self.balance, 2),
            unrealized_pl=round(unrealized, 2),
            realized_pl=round(realized, 2),
            equity=equity,
            margin_used=round(positions_market_value, 2),
            margin_available=round(max(0.0, self.balance), 2),
            open_positions_count=len(self.open_positions),
            daily_drawdown_pct=daily_dd_pct,
            daily_start_equity=self.daily_start_equity
        )

    async def get_candles(self, instrument: str, granularity: str = "M5", count: int = 100) -> List[Candle]:
        sym = normalize_crypto_symbol(instrument)
        self._ensure_symbol_initialized(sym)
        self.step_market(sym)
        candles = self.candle_history.get(sym, [])
        return candles[-count:] if len(candles) >= count else candles

    async def get_current_price(self, instrument: str) -> MarketPrice:
        sym = normalize_crypto_symbol(instrument)
        self._ensure_symbol_initialized(sym)
        self.step_market(sym)
        mid = self.current_prices.get(sym, 100.0)
        prec = _crypto_precision(mid)
        # Realistischer Spot-Spread in % (BTC/ETH enger, Tier-3 breiter)
        spread_pct = 0.08 if sym in ("BTC/USD", "ETH/USD") else (0.22 if sym in ("SHIB/USD", "PEPE/USD", "SUSHI/USD", "BAT/USD") else 0.14)
        half = mid * (spread_pct / 200.0)
        return MarketPrice(
            instrument=sym,
            time=_utc_iso(),
            bid=round(mid - half, prec),
            ask=round(mid + half, prec),
            spread_pips=spread_pct,
            mid=round(mid, prec)
        )

    async def place_order(self, order: OrderRequest) -> Position:
        sym = normalize_crypto_symbol(order.instrument)
        self._ensure_symbol_initialized(sym)
        price_info = await self.get_current_price(sym)
        prec = _crypto_precision(price_info.ask)
        entry_price = round(price_info.ask, prec)
        qty = round(float(order.units), 8)
        if qty <= 0:
            raise ValueError("Krypto-Ordermenge muss > 0 sein.")

        notional = entry_price * qty
        entry_fee = notional * self.TAKER_FEE_RATE
        total_cost = notional + entry_fee
        if total_cost > self.balance and self.balance > 10.0:
            # Begrenze automatisch auf verfügbares Spot-Cash
            qty = round(((self.balance * 0.99) / (entry_price * (1.0 + self.TAKER_FEE_RATE))), 8)
            notional = entry_price * qty
            entry_fee = notional * self.TAKER_FEE_RATE
            total_cost = notional + entry_fee

        self.balance = round(max(0.0, self.balance - total_cost), 2)
        sl_val = round(order.stop_loss, prec) if order.stop_loss else None
        tp_val = round(order.take_profit, prec) if order.take_profit else None

        pos = Position(
            id=f"cry_{uuid.uuid4().hex[:8]}",
            instrument=sym,
            direction=PositionDirection.BUY,
            units=qty,
            entry_price=entry_price,
            current_price=entry_price,
            highest_price=entry_price,
            stop_loss=sl_val,
            initial_stop_loss=sl_val,
            take_profit=tp_val,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            open_time=_utc_iso(),
            status=PositionStatus.OPEN
        )
        self.open_positions.append(pos)
        logger.info(f"[CRYPTO_SIM] Spot-Kauf: {qty} {sym} @ ${entry_price} (Notional: ${notional:,.2f})")
        return pos

    async def reduce_position(self, position_id: str, fraction: float = 0.5, reason: str = "PARTIAL_TP1") -> Optional[Position]:
        """Realisiert anteiligen Gewinn (z.B. 50% bei TP1) und lässt den Rest-Runner weiterlaufen."""
        pos = next((p for p in self.open_positions if p.id == position_id), None)
        if not pos or pos.units <= 0:
            return None

        sell_qty = round(pos.units * max(0.1, min(0.9, fraction)), 8)
        if sell_qty <= 0:
            return None

        curr_p = self.current_prices.get(pos.instrument, pos.entry_price)
        exit_fee = curr_p * sell_qty * self.TAKER_FEE_RATE
        realized = round((curr_p - pos.entry_price) * sell_qty - exit_fee, 2)

        pos.units = round(max(0.0, pos.units - sell_qty), 8)
        pos.partial_tp_taken = True
        pos.unrealized_pnl = round((curr_p - pos.entry_price) * pos.units, 2)
        self.balance = round(self.balance + (curr_p * sell_qty - exit_fee), 2)

        partial_pos = Position(
            id=f"{pos.id}_TP1",
            instrument=pos.instrument,
            direction=pos.direction,
            units=sell_qty,
            entry_price=pos.entry_price,
            current_price=curr_p,
            stop_loss=pos.stop_loss,
            initial_stop_loss=pos.initial_stop_loss,
            take_profit=pos.take_profit,
            unrealized_pnl=0.0,
            realized_pnl=realized,
            open_time=pos.open_time,
            close_time=_utc_iso(),
            status=PositionStatus.CLOSED,
            close_reason=reason
        )
        self.closed_positions.append(partial_pos)
        logger.info(f"[CRYPTO_SIM] Partial TP1 ({int(fraction*100)}%) für {pos.instrument}: +${realized:,.2f}")
        return partial_pos

    async def close_position(self, position_id: str, reason: str = "MANUAL") -> Position:
        pos = next((p for p in self.open_positions if p.id == position_id), None)
        if not pos:
            closed_match = next((p for p in self.closed_positions if p.id == position_id), None)
            if closed_match:
                return closed_match
            raise ValueError(f"Krypto-Position {position_id} nicht gefunden.")

        curr_p = self.current_prices.get(pos.instrument, pos.entry_price)
        self._close_position_internal(pos, curr_p, reason)
        return pos

    async def update_stop_loss(self, position_id: str, new_sl: float) -> Position:
        for pos in self.open_positions:
            if pos.id == position_id:
                prec = _crypto_precision(pos.entry_price)
                pos.stop_loss = round(new_sl, prec)
                return pos
        raise ValueError(f"Krypto-Position {position_id} nicht gefunden.")

    async def get_open_positions(self) -> List[Position]:
        return list(self.open_positions)

    async def get_closed_positions(self) -> List[Position]:
        return list(self.closed_positions)

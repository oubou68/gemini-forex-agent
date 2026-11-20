import logging
import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Any
import requests

from agent.broker.base_broker import BaseBroker
from agent.broker.alpaca_rate_limiter import shared_alpaca_rate_limiter, SimpleTTLCache
from agent.analysis.crypto_indicators import _crypto_precision
from agent.core.models import (
    Candle, MarketPrice, OrderRequest, Position,
    PositionDirection, PositionStatus, AccountSummary
)
from config.settings import settings

logger = logging.getLogger(__name__)

_CRYPTO_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,10}/USD$")


def normalize_crypto_symbol(symbol: str) -> str:
    """Normalisiert beliebige Eingaben (BTCUSD, BTC_USD, btc/usd) zu 'BTC/USD'."""
    raw = (symbol or "").strip().upper().replace("_", "/").replace("-", "/")
    if "/" not in raw and raw.endswith("USD") and len(raw) > 3:
        raw = f"{raw[:-3]}/USD"
    elif "/" not in raw and raw:
        raw = f"{raw}/USD"
    if not _CRYPTO_SYMBOL_RE.match(raw):
        raise ValueError(f"Ungültiges Krypto-Symbol: {symbol!r}")
    return raw


def _alpaca_position_symbol(symbol: str) -> str:
    """Alpaca /v2/positions nutzt Krypto-Symbole ohne Schrägstrich (z.B. BTCUSD)."""
    return normalize_crypto_symbol(symbol).replace("/", "")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class AlpacaCryptoClient(BaseBroker):
    """
    Alpaca Markets Crypto REST Client (Market Data v1beta3 + Spot Trading v2).
    - Nutzt Multi-Symbol Batch-Abfragen (/v1beta3/crypto/{loc}/bars & /latest/quotes) ->
      20 Kryptos in nur 2 HTTP-Calls statt 40!
    - Integriertes TTL-Caching + gemeinsamer Rate-Limiter + 429 Stale-Cache-Fallback
    - Platziert fraktionale Spot-Orders via /v2/orders (simple + gtc)
    """

    BARS_CACHE_TTL_SEC = 25.0
    QUOTE_CACHE_TTL_SEC = 8.0
    ACCOUNT_CACHE_TTL_SEC = 5.0

    def __init__(self, api_key: str, secret_key: str, environment: str = "paper", loc: Optional[str] = None):
        self.api_key = api_key
        self.secret_key = secret_key
        self.environment = environment.lower()
        self.loc = (loc or settings.ALPACA_CRYPTO_LOC or "us").lower()

        if self.environment == "live":
            self.base_url = "https://api.alpaca.markets/v2"
        else:
            self.base_url = "https://paper-api.alpaca.markets/v2"

        self.crypto_data_url = f"{settings.ALPACA_DATA_URL.rstrip('/')}/v1beta3/crypto/{self.loc}"

        if settings.ALPACA_OAUTH_TOKEN:
            self.headers = {
                "Authorization": f"Bearer {settings.ALPACA_OAUTH_TOKEN}",
                "Content-Type": "application/json",
                "Accept": "application/json"
            }
        else:
            self.headers = {
                "APCA-API-KEY-ID": self.api_key,
                "APCA-API-SECRET-KEY": self.secret_key,
                "Content-Type": "application/json",
                "Accept": "application/json"
            }

        self.open_positions_cache: List[Position] = []
        self.closed_positions_cache: List[Position] = []
        self._initial_equity: float = 100000.0
        self._daily_start_equity: float = 100000.0
        self._daily_equity_date = datetime.now(timezone.utc).date()
        self._cache = SimpleTTLCache()

    def _safe_get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Optional[requests.Response]:
        """Rate-limitierter GET-Aufruf mit automatischer 429-Erkennung."""
        backed_off, _ = shared_alpaca_rate_limiter.is_backed_off()
        if backed_off:
            return None
        if not shared_alpaca_rate_limiter.acquire(wait=False):
            return None
        resp = requests.get(url, headers=self.headers, params=params, timeout=10)
        if resp.status_code == 429:
            shared_alpaca_rate_limiter.record_rate_limit(resp.headers.get("Retry-After"))
            return resp
        if resp.status_code == 200:
            shared_alpaca_rate_limiter.record_success()
        return resp

    async def initialize(self) -> bool:
        if not settings.ALPACA_OAUTH_TOKEN and (not self.api_key or not self.secret_key):
            logger.warning("Alpaca API Key oder Secret Key fehlt für Crypto Client.")
            return False
        try:
            summary = await self.get_account_summary()
            self._initial_equity = summary.equity or summary.balance
            self._daily_start_equity = summary.equity or summary.balance
            self._daily_equity_date = datetime.now(timezone.utc).date()
            logger.info(f"Alpaca Crypto Client verbunden ({self.environment.upper()}). Equity: ${summary.equity:,.2f} USD")
            return True
        except Exception as e:
            logger.error(f"Fehler bei Alpaca Crypto Initialisierung: {e}")
            return False

    async def get_account_summary(self) -> AccountSummary:
        cached = self._cache.get("account:summary", self.ACCOUNT_CACHE_TTL_SEC)
        if cached is not None:
            return cached

        url = f"{self.base_url}/account"
        response = await asyncio.to_thread(self._safe_get, url)
        if response is None or response.status_code == 429:
            stale = self._cache.get("account:summary", self.ACCOUNT_CACHE_TTL_SEC, allow_stale=True)
            if stale is not None:
                return stale
            raise Exception("Alpaca Crypto Account API Rate Limit (429) ohne Cache.")
        if response.status_code != 200:
            raise Exception(f"Alpaca Crypto Account API Fehler ({response.status_code}): {response.text}")

        data = response.json()
        account_id = data.get("id", "alpaca-crypto-acc")
        equity = float(data.get("equity", data.get("portfolio_value", 100000.0)))
        last_equity = float(data.get("last_equity", equity))
        cash = float(data.get("cash", equity))
        buying_power = float(data.get("non_marginable_buying_power", cash))

        positions = await self.get_open_positions()
        unrealized_pl = sum(p.unrealized_pnl for p in positions)
        realized_pl = sum(p.realized_pnl for p in self.closed_positions_cache)
        margin_used = round(sum(p.current_price * p.units for p in positions), 2)

        today_utc = datetime.now(timezone.utc).date()
        if self._daily_start_equity <= 0 or today_utc != self._daily_equity_date:
            self._daily_start_equity = last_equity if last_equity > 0 else equity
            self._daily_equity_date = today_utc

        daily_dd_pct = 0.0
        if self._daily_start_equity > 0:
            dd = (self._daily_start_equity - equity) / self._daily_start_equity * 100.0
            daily_dd_pct = max(0.0, round(dd, 2))

        summary = AccountSummary(
            account_id=account_id,
            currency="USD",
            balance=round(cash, 2),
            unrealized_pl=round(unrealized_pl, 2),
            realized_pl=round(realized_pl, 2),
            equity=round(equity, 2),
            margin_used=margin_used,
            margin_available=round(buying_power, 2),
            open_positions_count=len(positions),
            daily_drawdown_pct=daily_dd_pct,
            daily_start_equity=self._daily_start_equity
        )
        self._cache.set("account:summary", summary)
        return summary

    @staticmethod
    def _parse_raw_bars(raw_bars: List[Dict[str, Any]], limit: int) -> List[Candle]:
        candles: List[Candle] = []
        for b in raw_bars[-limit:]:
            c_val = float(b.get("c", 0.0))
            prec = _crypto_precision(c_val if c_val > 0 else 1.0)
            t = b.get("t", _utc_iso())
            candles.append(Candle(
                time=t if t.endswith("Z") else t + "Z",
                open=round(float(b.get("o", 0.0)), prec),
                high=round(float(b.get("h", 0.0)), prec),
                low=round(float(b.get("l", 0.0)), prec),
                close=round(c_val, prec),
                volume=max(1, int(float(b.get("v", 1)))),
                complete=True
            ))
        return candles

    @staticmethod
    def _tf_and_start(granularity: str, count: int) -> tuple[str, str]:
        tf_map = {
            "M1": ("1Min", 1),
            "M5": ("5Min", 5),
            "M15": ("15Min", 15),
            "M30": ("30Min", 30),
            "H1": ("1Hour", 60),
            "D": ("1Day", 1440),
        }
        tf_str, mins = tf_map.get(granularity, ("5Min", 5))
        # Genug Historie zurückfragen (inkl. kleinem Puffer für dünne Nachtkerzen)
        lookback_mins = max(mins * int(count * 1.4), 60)
        start_dt = datetime.now(timezone.utc) - timedelta(minutes=lookback_mins)
        return tf_str, start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    async def get_multi_candles(
        self,
        symbols: List[str],
        granularity: str = "M5",
        count: int = 60
    ) -> Dict[str, List[Candle]]:
        """
        Batch-Abruf historischer Krypto-Kerzen für bis zu 20 Coins in minimalen HTTP-Calls
        (10 Symbole pro Request -> 2 Calls statt 20!) und Befüllung des lokalen TTL-Caches.
        """
        normalized = [normalize_crypto_symbol(s) for s in symbols]
        result: Dict[str, List[Candle]] = {}
        to_fetch: List[str] = []

        for sym in normalized:
            cache_key = f"bars:{sym}:{granularity}"
            cached = self._cache.get(cache_key, self.BARS_CACHE_TTL_SEC)
            if cached is not None and len(cached) >= min(count, 20):
                result[sym] = cached[-count:]
            else:
                to_fetch.append(sym)

        if not to_fetch:
            return result

        timeframe, start_iso = self._tf_and_start(granularity, count)
        url = f"{self.crypto_data_url}/bars"

        # Chunk in Gruppen zu 10 Symbolen (max 10 * 100 = 1000 Bars pro Call)
        for i in range(0, len(to_fetch), 10):
            chunk = to_fetch[i:i + 10]
            params = {
                "symbols": ",".join(chunk),
                "timeframe": timeframe,
                "start": start_iso,
                "limit": min(len(chunk) * max(count, 60) + 100, 5000),
            }
            response = await asyncio.to_thread(self._safe_get, url, params)
            if response is None or response.status_code != 200:
                # Fallback auf Stale-Cache für jeden Coin im Chunk
                for sym in chunk:
                    stale = self._cache.get(f"bars:{sym}:{granularity}", self.BARS_CACHE_TTL_SEC, allow_stale=True)
                    if stale:
                        result[sym] = stale[-count:]
                continue

            bars_map = response.json().get("bars", {})
            if isinstance(bars_map, dict):
                for sym in chunk:
                    raw = bars_map.get(sym, [])
                    parsed = self._parse_raw_bars(raw, max(count, 100))
                    if parsed:
                        self._cache.set(f"bars:{sym}:{granularity}", parsed)
                        result[sym] = parsed[-count:]

        return result

    async def get_multi_prices(self, symbols: List[str]) -> Dict[str, MarketPrice]:
        """
        Batch-Abruf aktueller Bid/Ask-Quotes für alle Top-20 Kryptos in einem einzigen HTTP-Call!
        """
        normalized = [normalize_crypto_symbol(s) for s in symbols]
        result: Dict[str, MarketPrice] = {}
        to_fetch: List[str] = []

        for sym in normalized:
            cached = self._cache.get(f"quote:{sym}", self.QUOTE_CACHE_TTL_SEC)
            if cached is not None:
                result[sym] = cached
            else:
                to_fetch.append(sym)

        if not to_fetch:
            return result

        url = f"{self.crypto_data_url}/latest/quotes"
        params = {"symbols": ",".join(to_fetch)}
        response = await asyncio.to_thread(self._safe_get, url, params)

        if response is not None and response.status_code == 200:
            quotes = response.json().get("quotes", {})
            if isinstance(quotes, dict):
                for sym in to_fetch:
                    q = quotes.get(sym, {})
                    bid = float(q.get("bp", 0.0))
                    ask = float(q.get("ap", 0.0))
                    if bid > 0 and ask > 0:
                        mid = (bid + ask) / 2.0
                        prec = _crypto_precision(mid)
                        spread_pct = round(((ask - bid) / max(mid, 1e-12)) * 100.0, 4)
                        t = q.get("t", _utc_iso())
                        mp = MarketPrice(
                            instrument=sym,
                            time=t if t.endswith("Z") else t + "Z",
                            bid=round(bid, prec),
                            ask=round(ask, prec),
                            spread_pips=spread_pct,
                            mid=round(mid, prec)
                        )
                        self._cache.set(f"quote:{sym}", mp)
                        result[sym] = mp

        # Stale-Cache Fallback für fehlende Symbole
        for sym in to_fetch:
            if sym not in result:
                stale = self._cache.get(f"quote:{sym}", self.QUOTE_CACHE_TTL_SEC, allow_stale=True)
                if stale is not None:
                    result[sym] = stale

        return result

    async def get_candles(self, instrument: str, granularity: str = "M5", count: int = 100) -> List[Candle]:
        symbol = normalize_crypto_symbol(instrument)
        cache_key = f"bars:{symbol}:{granularity}"
        cached = self._cache.get(cache_key, self.BARS_CACHE_TTL_SEC)
        if cached is not None and len(cached) >= min(count, 20):
            return cached[-count:]

        timeframe, start_iso = self._tf_and_start(granularity, count)
        url = f"{self.crypto_data_url}/bars"
        params = {
            "symbols": symbol,
            "timeframe": timeframe,
            "start": start_iso,
            "limit": max(1, min(int(count), 1000))
        }

        response = await asyncio.to_thread(self._safe_get, url, params)
        if response is None or response.status_code == 429:
            stale = self._cache.get(cache_key, self.BARS_CACHE_TTL_SEC, allow_stale=True)
            if stale is not None:
                return stale[-count:]
            raise Exception(f"Alpaca Crypto Bars Rate Limit (429) für {symbol} — warte auf Backoff-Ablauf.")

        if response.status_code != 200:
            stale = self._cache.get(cache_key, self.BARS_CACHE_TTL_SEC, allow_stale=True)
            if stale is not None:
                return stale[-count:]
            raise Exception(f"Alpaca Crypto Bars Fehler ({response.status_code}): {response.text}")

        bars_map = response.json().get("bars", {})
        raw_bars = bars_map.get(symbol, []) if isinstance(bars_map, dict) else []
        candles = self._parse_raw_bars(raw_bars, count)
        if candles:
            self._cache.set(cache_key, candles)
        return candles

    async def get_current_price(self, instrument: str) -> MarketPrice:
        symbol = normalize_crypto_symbol(instrument)
        cache_key = f"quote:{symbol}"
        cached = self._cache.get(cache_key, self.QUOTE_CACHE_TTL_SEC)
        if cached is not None:
            return cached

        url = f"{self.crypto_data_url}/latest/quotes"
        params = {"symbols": symbol}

        response = await asyncio.to_thread(self._safe_get, url, params)
        if response is not None and response.status_code == 200:
            quotes = response.json().get("quotes", {})
            q = quotes.get(symbol, {})
            bid = float(q.get("bp", 0.0))
            ask = float(q.get("ap", 0.0))
            if bid > 0 and ask > 0:
                mid = (bid + ask) / 2.0
                prec = _crypto_precision(mid)
                spread_pct = round(((ask - bid) / max(mid, 1e-12)) * 100.0, 4)
                t = q.get("t", _utc_iso())
                mp = MarketPrice(
                    instrument=symbol,
                    time=t if t.endswith("Z") else t + "Z",
                    bid=round(bid, prec),
                    ask=round(ask, prec),
                    spread_pips=spread_pct,
                    mid=round(mid, prec)
                )
                self._cache.set(cache_key, mp)
                return mp

        # Fallback: Stale-Cache bei Rate-Limit oder leerem Quote
        stale = self._cache.get(cache_key, self.QUOTE_CACHE_TTL_SEC, allow_stale=True)
        if stale is not None:
            return stale

        # Fallback: latest trades (nur wenn kein Rate-Limit aktiv)
        backed_off, _ = shared_alpaca_rate_limiter.is_backed_off()
        if not backed_off:
            t_url = f"{self.crypto_data_url}/latest/trades"
            t_res = await asyncio.to_thread(self._safe_get, t_url, params)
            if t_res is not None and t_res.status_code == 200:
                trade = t_res.json().get("trades", {}).get(symbol, {})
                p = float(trade.get("p", 0.0))
                if p > 0:
                    prec = _crypto_precision(p)
                    half = p * 0.0008
                    mp = MarketPrice(
                        instrument=symbol,
                        time=_utc_iso(),
                        bid=round(p - half, prec),
                        ask=round(p + half, prec),
                        spread_pips=0.16,
                        mid=round(p, prec)
                    )
                    self._cache.set(cache_key, mp)
                    return mp

        err_text = response.text if response is not None else "Rate limit backoff active"
        raise Exception(f"Konnte aktuellen Alpaca Krypto-Preis für {symbol} nicht abrufen: {err_text}")

    async def place_order(self, order: OrderRequest) -> Position:
        """Platziert eine fraktionale Alpaca Crypto Spot-Order (simple + gtc)."""
        symbol = normalize_crypto_symbol(order.instrument)
        if order.direction != PositionDirection.BUY:
            raise ValueError("Alpaca Crypto unterstützt ausschließlich Spot-Long-Käufe (BUY) für Neueinstiege.")

        qty_val = round(float(order.units), 8)
        if qty_val <= 0:
            raise ValueError("Order-Menge muss > 0 sein.")

        payload: Dict[str, Any] = {
            "symbol": symbol,
            "qty": f"{qty_val:.8f}".rstrip("0").rstrip("."),
            "side": "buy",
            "type": "market",
            "time_in_force": "gtc"
        }

        url = f"{self.base_url}/orders"

        def _request():
            shared_alpaca_rate_limiter.acquire(wait=True)
            return requests.post(url, headers=self.headers, json=payload, timeout=10)

        response = await asyncio.to_thread(_request)
        if response.status_code == 429:
            shared_alpaca_rate_limiter.record_rate_limit(response.headers.get("Retry-After"))
        if response.status_code not in (200, 201):
            raise Exception(f"Alpaca Crypto Order Fehler ({response.status_code}): {response.text}")

        self._cache.invalidate_prefix("account:")
        data = response.json()
        order_id = data.get("id", f"alpcry_{datetime.now(timezone.utc).timestamp()}")
        filled_price = float(data.get("filled_avg_price") or order.price or (await self.get_current_price(symbol)).ask)
        prec = _crypto_precision(filled_price)

        pos = Position(
            id=order_id,
            instrument=symbol,
            direction=PositionDirection.BUY,
            units=qty_val,
            entry_price=round(filled_price, prec),
            current_price=round(filled_price, prec),
            highest_price=round(filled_price, prec),
            stop_loss=round(order.stop_loss, prec) if order.stop_loss else None,
            initial_stop_loss=round(order.stop_loss, prec) if order.stop_loss else None,
            take_profit=round(order.take_profit, prec) if order.take_profit else None,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            open_time=_utc_iso(),
            status=PositionStatus.OPEN
        )
        self.open_positions_cache.append(pos)
        return pos

    async def reduce_position(self, position_id: str, fraction: float = 0.5, reason: str = "PARTIAL_TP1") -> Optional[Position]:
        """Verkauft einen prozentualen Teil (z.B. 50% bei TP1) einer offenen Krypto-Position."""
        pos = next((p for p in self.open_positions_cache if p.id == position_id), None)
        if not pos or pos.units <= 0:
            return None

        sell_qty = round(pos.units * max(0.1, min(0.9, fraction)), 8)
        if sell_qty <= 0:
            return None

        payload = {
            "symbol": pos.instrument,
            "qty": f"{sell_qty:.8f}".rstrip("0").rstrip("."),
            "side": "sell",
            "type": "market",
            "time_in_force": "gtc"
        }
        url = f"{self.base_url}/orders"

        def _request():
            shared_alpaca_rate_limiter.acquire(wait=True)
            return requests.post(url, headers=self.headers, json=payload, timeout=10)

        response = await asyncio.to_thread(_request)
        if response.status_code == 429:
            shared_alpaca_rate_limiter.record_rate_limit(response.headers.get("Retry-After"))
        if response.status_code not in (200, 201):
            logger.warning(f"Alpaca Crypto Partial Scale-Out fehlgeschlagen ({pos.instrument}): {response.text}")
            return None

        self._cache.invalidate_prefix("account:")
        curr_price = (await self.get_current_price(pos.instrument)).bid
        realized = round((curr_price - pos.entry_price) * sell_qty, 2)
        pos.units = round(max(0.0, pos.units - sell_qty), 8)
        pos.partial_tp_taken = True

        partial_record = Position(
            id=f"{pos.id}_TP1",
            instrument=pos.instrument,
            direction=pos.direction,
            units=sell_qty,
            entry_price=pos.entry_price,
            current_price=curr_price,
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
        self.closed_positions_cache.append(partial_record)
        return partial_record

    async def close_position(self, position_id: str, reason: str = "MANUAL") -> Position:
        pos_to_close = next(
            (p for p in self.open_positions_cache if p.id == position_id or p.instrument == position_id),
            None
        )
        symbol = normalize_crypto_symbol(pos_to_close.instrument if pos_to_close else position_id)
        api_sym = _alpaca_position_symbol(symbol)
        url = f"{self.base_url}/positions/{api_sym}"

        def _request():
            shared_alpaca_rate_limiter.acquire(wait=True)
            return requests.delete(url, headers=self.headers, timeout=10)

        response = await asyncio.to_thread(_request)
        if response.status_code == 429:
            shared_alpaca_rate_limiter.record_rate_limit(response.headers.get("Retry-After"))
        if response.status_code not in (200, 204, 404):
            logger.warning(f"Alpaca Crypto Close Warnung: {response.text}")

        self._cache.invalidate_prefix("account:")
        curr_price = (await self.get_current_price(symbol)).bid

        if pos_to_close:
            self.open_positions_cache.remove(pos_to_close)
            pnl = (curr_price - pos_to_close.entry_price) * pos_to_close.units
            closed = Position(
                id=pos_to_close.id,
                instrument=symbol,
                direction=pos_to_close.direction,
                units=pos_to_close.units,
                entry_price=pos_to_close.entry_price,
                current_price=curr_price,
                stop_loss=pos_to_close.stop_loss,
                initial_stop_loss=pos_to_close.initial_stop_loss,
                take_profit=pos_to_close.take_profit,
                unrealized_pnl=0.0,
                realized_pnl=round(pnl, 2),
                open_time=pos_to_close.open_time,
                close_time=_utc_iso(),
                status=PositionStatus.CLOSED,
                close_reason=reason
            )
            self.closed_positions_cache.append(closed)
            return closed

        now_iso = _utc_iso()
        return Position(
            id=position_id,
            instrument=symbol,
            direction=PositionDirection.BUY,
            units=0.0,
            entry_price=curr_price,
            current_price=curr_price,
            status=PositionStatus.CLOSED,
            close_reason=reason,
            open_time=now_iso,
            close_time=now_iso
        )

    async def update_stop_loss(self, position_id: str, new_sl: float) -> Position:
        for pos in self.open_positions_cache:
            if pos.id == position_id:
                prec = _crypto_precision(pos.entry_price)
                pos.stop_loss = round(new_sl, prec)
                return pos
        raise Exception(f"Krypto-Position {position_id} nicht gefunden.")

    async def get_open_positions(self) -> List[Position]:
        cached = self._cache.get("account:positions", self.ACCOUNT_CACHE_TTL_SEC)
        if cached is not None:
            return cached

        url = f"{self.base_url}/positions"
        response = await asyncio.to_thread(self._safe_get, url)
        if response is None or response.status_code != 200:
            return self.open_positions_cache

        data = response.json()
        if not isinstance(data, list):
            return self.open_positions_cache
        prev_by_sym = {p.instrument: p for p in self.open_positions_cache}
        live_crypto: List[Position] = []

        for item in data:
            if not isinstance(item, dict):
                continue
            if str(item.get("asset_class", "")).lower() != "crypto":
                continue
            try:
                sym = normalize_crypto_symbol(item.get("symbol", ""))
            except ValueError:
                continue
            qty = abs(float(item.get("qty", 0.0)))
            entry = float(item.get("avg_entry_price", 0.0))
            curr = float(item.get("current_price", entry))
            unrealized = float(item.get("unrealized_pl", 0.0))
            prec = _crypto_precision(entry or curr or 1.0)

            cached_pos = prev_by_sym.get(sym)
            pos = Position(
                id=cached_pos.id if cached_pos else item.get("asset_id", f"alpcry_{sym}"),
                instrument=sym,
                direction=PositionDirection.BUY,
                units=round(qty, 8),
                entry_price=round(entry, prec),
                current_price=round(curr, prec),
                highest_price=max(cached_pos.highest_price or entry, curr) if cached_pos else max(entry, curr),
                stop_loss=cached_pos.stop_loss if cached_pos else None,
                initial_stop_loss=cached_pos.initial_stop_loss if cached_pos else None,
                take_profit=cached_pos.take_profit if cached_pos else None,
                partial_tp_taken=cached_pos.partial_tp_taken if cached_pos else False,
                unrealized_pnl=round(unrealized, 2),
                realized_pnl=0.0,
                open_time=cached_pos.open_time if cached_pos else _utc_iso(),
                status=PositionStatus.OPEN
            )
            live_crypto.append(pos)

        self.open_positions_cache = live_crypto
        self._cache.set("account:positions", self.open_positions_cache)
        return self.open_positions_cache

    async def get_closed_positions(self) -> List[Position]:
        return self.closed_positions_cache

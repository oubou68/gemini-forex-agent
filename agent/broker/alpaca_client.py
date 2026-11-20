import logging
import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Any
import requests

from agent.broker.base_broker import BaseBroker
from agent.broker.alpaca_rate_limiter import shared_alpaca_rate_limiter, SimpleTTLCache
from agent.core.models import (
    Candle, MarketPrice, OrderRequest, Position,
    PositionDirection, PositionStatus, AccountSummary
)
from config.settings import settings

logger = logging.getLogger(__name__)

_ALPACA_SYMBOL_RE = re.compile(r"^[A-Z0-9.-]{1,10}$")


def _sanitize_stock_symbol(symbol: str) -> str:
    cleaned = (symbol or "").strip().replace("/", "").replace("_", "").upper()
    if not _ALPACA_SYMBOL_RE.match(cleaned):
        raise ValueError(f"Ungültiges Aktien-Symbol: {symbol!r}")
    return cleaned


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class AlpacaClient(BaseBroker):
    """
    Vollständige Implementierung der Alpaca Markets v2 REST API für US-Aktien.
    - Nutzt Multi-Symbol Batch-Endpoints (/v2/stocks/bars & /v2/stocks/quotes/latest)
      sowie gemeinsames Rate-Limiting & TTL-Caching (verhindert 429 Too Many Requests)
    - Echte Stop-Loss-Order-Updates via PATCH /v2/orders
    - Täglicher Drawdown-Reset & Closed-Trade-Sync
    """

    BARS_CACHE_TTL_SEC = 25.0
    QUOTE_CACHE_TTL_SEC = 8.0
    ACCOUNT_CACHE_TTL_SEC = 5.0

    def __init__(self, api_key: str, secret_key: str, environment: str = "paper"):
        self.api_key = api_key
        self.secret_key = secret_key
        self.environment = environment.lower()

        if self.environment == "live":
            self.base_url = "https://api.alpaca.markets/v2"
        else:
            self.base_url = "https://paper-api.alpaca.markets/v2"

        self.data_url = f"{settings.ALPACA_DATA_URL.rstrip('/')}/v2"
        self.data_feed = (settings.ALPACA_DATA_FEED or "iex").lower()

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
            logger.warning("Alpaca API Key oder Secret Key fehlt. AlpacaClient kann nicht im Live/Paper-Modus initialisiert werden.")
            return False

        try:
            summary = await self.get_account_summary()
            self._initial_equity = summary.equity or summary.balance
            self._daily_start_equity = summary.equity or summary.balance
            self._daily_equity_date = datetime.now(timezone.utc).date()
            logger.info(f"Alpaca Client erfolgreich verbunden ({self.environment.upper()}). Konto: {summary.account_id}, Equity: ${summary.equity:,.2f} {summary.currency}")
            return True
        except Exception as e:
            logger.error(f"Fehler bei Alpaca Initialisierung: {e}")
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
            raise Exception("Alpaca Account API Rate Limit (429) ohne Cache.")
        if response.status_code != 200:
            raise Exception(f"Alpaca Account API Fehler ({response.status_code}): {response.text}")

        data = response.json()
        account_id = data.get("id", "alpaca-acc")
        currency = data.get("currency", "USD")
        equity = float(data.get("equity", data.get("portfolio_value", 100000.0)))
        last_equity = float(data.get("last_equity", equity))
        balance = float(data.get("cash", equity))
        buying_power = float(data.get("buying_power", equity * 2))

        positions = await self.get_open_positions()
        unrealized_pl = sum(p.unrealized_pnl for p in positions)
        realized_pl = sum(p.realized_pnl for p in self.closed_positions_cache)
        margin_used = max(0.0, equity - float(data.get("cash", equity)))

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
            currency=currency,
            balance=balance,
            unrealized_pl=round(unrealized_pl, 2),
            realized_pl=round(realized_pl, 2),
            equity=round(equity, 2),
            margin_used=round(margin_used, 2),
            margin_available=round(buying_power, 2),
            open_positions_count=len(positions),
            daily_drawdown_pct=daily_dd_pct,
            daily_start_equity=self._daily_start_equity
        )
        self._cache.set("account:summary", summary)
        return summary

    @staticmethod
    def _parse_bars(raw_bars: List[Dict[str, Any]], limit: int) -> List[Candle]:
        candles = []
        for b in raw_bars[-limit:]:
            t = b.get("t", _utc_iso())
            candles.append(Candle(
                time=t if t.endswith("Z") else t + "Z",
                open=round(float(b.get("o", 0.0)), 2),
                high=round(float(b.get("h", 0.0)), 2),
                low=round(float(b.get("l", 0.0)), 2),
                close=round(float(b.get("c", 0.0)), 2),
                volume=int(b.get("v", 0)),
                complete=True
            ))
        return candles

    async def get_multi_candles(
        self,
        symbols: List[str],
        granularity: str = "M5",
        count: int = 60
    ) -> Dict[str, List[Candle]]:
        """Batch-Abruf historischer Aktien-Kerzen (10 Symbole pro Request -> spart 90% API-Calls!)."""
        clean_syms = [_sanitize_stock_symbol(s) for s in symbols]
        result: Dict[str, List[Candle]] = {}
        to_fetch: List[str] = []

        for sym in clean_syms:
            cached = self._cache.get(f"bars:{sym}:{granularity}", self.BARS_CACHE_TTL_SEC)
            if cached is not None and len(cached) >= min(count, 20):
                result[sym] = cached[-count:]
            else:
                to_fetch.append(sym)

        if not to_fetch:
            return result

        tf_map = {"M1": "1Min", "M5": "5Min", "M15": "15Min", "M30": "30Min", "H1": "1Hour", "D": "1Day"}
        timeframe = tf_map.get(granularity, "5Min")
        start_iso = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        url = f"{self.data_url}/stocks/bars"

        for i in range(0, len(to_fetch), 10):
            chunk = to_fetch[i:i + 10]
            params = {
                "symbols": ",".join(chunk),
                "timeframe": timeframe,
                "start": start_iso,
                "limit": min(len(chunk) * max(count, 60) + 100, 5000),
                "adjustment": "all",
                "feed": self.data_feed
            }
            response = await asyncio.to_thread(self._safe_get, url, params)
            if response is None or response.status_code != 200:
                for sym in chunk:
                    stale = self._cache.get(f"bars:{sym}:{granularity}", self.BARS_CACHE_TTL_SEC, allow_stale=True)
                    if stale:
                        result[sym] = stale[-count:]
                continue

            bars_map = response.json().get("bars", {})
            if isinstance(bars_map, dict):
                for sym in chunk:
                    parsed = self._parse_bars(bars_map.get(sym, []), max(count, 100))
                    if parsed:
                        self._cache.set(f"bars:{sym}:{granularity}", parsed)
                        result[sym] = parsed[-count:]

        return result

    async def get_multi_prices(self, symbols: List[str]) -> Dict[str, MarketPrice]:
        """Batch-Abruf aktueller Aktien-Quotes in einem einzigen HTTP-Call."""
        clean_syms = [_sanitize_stock_symbol(s) for s in symbols]
        result: Dict[str, MarketPrice] = {}
        to_fetch: List[str] = []

        for sym in clean_syms:
            cached = self._cache.get(f"quote:{sym}", self.QUOTE_CACHE_TTL_SEC)
            if cached is not None:
                result[sym] = cached
            else:
                to_fetch.append(sym)

        if not to_fetch:
            return result

        url = f"{self.data_url}/stocks/quotes/latest"
        params = {"symbols": ",".join(to_fetch), "feed": self.data_feed}
        response = await asyncio.to_thread(self._safe_get, url, params)

        if response is not None and response.status_code == 200:
            quotes = response.json().get("quotes", {})
            if isinstance(quotes, dict):
                for sym in to_fetch:
                    q = quotes.get(sym, {})
                    bid = float(q.get("bp", 0.0))
                    ask = float(q.get("ap", 0.0))
                    if bid > 0 and ask > 0:
                        t = q.get("t", _utc_iso())
                        mp = MarketPrice(
                            instrument=sym,
                            time=t if t.endswith("Z") else t + "Z",
                            bid=round(bid, 2),
                            ask=round(ask, 2),
                            spread_pips=round(ask - bid, 2),
                            mid=round((bid + ask) / 2.0, 2)
                        )
                        self._cache.set(f"quote:{sym}", mp)
                        result[sym] = mp

        for sym in to_fetch:
            if sym not in result:
                stale = self._cache.get(f"quote:{sym}", self.QUOTE_CACHE_TTL_SEC, allow_stale=True)
                if stale is not None:
                    result[sym] = stale

        return result

    async def get_candles(self, instrument: str, granularity: str = "M5", count: int = 100) -> List[Candle]:
        symbol = _sanitize_stock_symbol(instrument)
        cache_key = f"bars:{symbol}:{granularity}"
        cached = self._cache.get(cache_key, self.BARS_CACHE_TTL_SEC)
        if cached is not None and len(cached) >= min(count, 20):
            return cached[-count:]

        tf_map = {"M1": "1Min", "M5": "5Min", "M15": "15Min", "M30": "30Min", "H1": "1Hour", "D": "1Day"}
        timeframe = tf_map.get(granularity, "5Min")
        url = f"{self.data_url}/stocks/{symbol}/bars"
        params = {
            "timeframe": timeframe,
            "limit": max(1, min(int(count), 1000)),
            "adjustment": "all",
            "feed": self.data_feed
        }

        response = await asyncio.to_thread(self._safe_get, url, params)
        if response is None or response.status_code == 429:
            stale = self._cache.get(cache_key, self.BARS_CACHE_TTL_SEC, allow_stale=True)
            if stale is not None:
                return stale[-count:]
            raise Exception(f"Alpaca Bars Rate Limit (429) für {symbol} — Backoff aktiv.")

        if response.status_code != 200:
            url_alt = f"{self.data_url}/stocks/bars"
            params_alt = {
                "symbols": symbol,
                "timeframe": timeframe,
                "limit": max(1, min(int(count), 1000)),
                "feed": self.data_feed
            }
            response = await asyncio.to_thread(self._safe_get, url_alt, params_alt)
            if response is None or response.status_code != 200:
                stale = self._cache.get(cache_key, self.BARS_CACHE_TTL_SEC, allow_stale=True)
                if stale is not None:
                    return stale[-count:]
                code = response.status_code if response is not None else 429
                txt = response.text if response is not None else "Rate limit"
                raise Exception(f"Alpaca Bars Fehler ({code}): {txt}")

        data = response.json()
        raw_bars = data.get("bars", [])
        if isinstance(raw_bars, dict):
            raw_bars = raw_bars.get(symbol, [])

        candles = self._parse_bars(raw_bars, count)
        if candles:
            self._cache.set(cache_key, candles)
        return candles

    async def get_current_price(self, instrument: str) -> MarketPrice:
        symbol = _sanitize_stock_symbol(instrument)
        cache_key = f"quote:{symbol}"
        cached = self._cache.get(cache_key, self.QUOTE_CACHE_TTL_SEC)
        if cached is not None:
            return cached

        url = f"{self.data_url}/stocks/{symbol}/quotes/latest"
        params = {"feed": self.data_feed}

        response = await asyncio.to_thread(self._safe_get, url, params)
        if response is not None and response.status_code == 200:
            data = response.json()
            quote = data.get("quote", {})
            bid = float(quote.get("bp", 0.0))
            ask = float(quote.get("ap", 0.0))
            t = quote.get("t", _utc_iso())

            if bid > 0 and ask > 0:
                mid = round((bid + ask) / 2.0, 2)
                spread_cents = round(ask - bid, 2)
                mp = MarketPrice(
                    instrument=symbol,
                    time=t if t.endswith("Z") else t + "Z",
                    bid=round(bid, 2),
                    ask=round(ask, 2),
                    spread_pips=spread_cents,
                    mid=mid
                )
                self._cache.set(cache_key, mp)
                return mp

        stale = self._cache.get(cache_key, self.QUOTE_CACHE_TTL_SEC, allow_stale=True)
        if stale is not None:
            return stale

        backed_off, _ = shared_alpaca_rate_limiter.is_backed_off()
        if not backed_off:
            trade_url = f"{self.data_url}/stocks/{symbol}/trades/latest"
            t_res = await asyncio.to_thread(self._safe_get, trade_url, params)
            if t_res is not None and t_res.status_code == 200:
                t_data = t_res.json().get("trade", {})
                p = float(t_data.get("p", 100.0))
                spread_est = 0.02
                mp = MarketPrice(
                    instrument=symbol,
                    time=_utc_iso(),
                    bid=round(p - spread_est / 2, 2),
                    ask=round(p + spread_est / 2, 2),
                    spread_pips=spread_est,
                    mid=round(p, 2)
                )
                self._cache.set(cache_key, mp)
                return mp

        err_text = response.text if response is not None else "Rate limit backoff active"
        raise Exception(f"Konnte aktuellen Preis für Aktie {symbol} nicht abrufen: {err_text}")

    async def place_order(self, order: OrderRequest) -> Position:
        symbol = _sanitize_stock_symbol(order.instrument)
        side = "buy" if order.direction == PositionDirection.BUY else "sell"
        qty = max(1, int(order.units))

        payload: Dict[str, Any] = {
            "symbol": symbol,
            "qty": str(qty),
            "side": side,
            "type": order.order_type.lower(),
            "time_in_force": "day"
        }

        if order.stop_loss and order.take_profit:
            payload["order_class"] = "bracket"
            payload["stop_loss"] = {"stop_price": str(round(order.stop_loss, 2))}
            payload["take_profit"] = {"limit_price": str(round(order.take_profit, 2))}
        elif order.stop_loss:
            payload["order_class"] = "oto"
            payload["stop_loss"] = {"stop_price": str(round(order.stop_loss, 2))}
        elif order.take_profit:
            payload["order_class"] = "oto"
            payload["take_profit"] = {"limit_price": str(round(order.take_profit, 2))}

        url = f"{self.base_url}/orders"

        def _request():
            shared_alpaca_rate_limiter.acquire(wait=True)
            return requests.post(url, headers=self.headers, json=payload, timeout=10)

        response = await asyncio.to_thread(_request)
        if response.status_code == 429:
            shared_alpaca_rate_limiter.record_rate_limit(response.headers.get("Retry-After"))
        if response.status_code not in (200, 201):
            raise Exception(f"Alpaca Order Fehler ({response.status_code}): {response.text}")

        self._cache.invalidate_prefix("account:")
        data = response.json()
        order_id = data.get("id", f"alpaca_{datetime.now(timezone.utc).timestamp()}")
        filled_price = float(data.get("filled_avg_price") or order.price or (await self.get_current_price(symbol)).mid)

        pos = Position(
            id=order_id,
            instrument=symbol,
            direction=order.direction,
            units=qty,
            entry_price=round(filled_price, 2),
            current_price=round(filled_price, 2),
            stop_loss=order.stop_loss,
            initial_stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            open_time=_utc_iso(),
            status=PositionStatus.OPEN
        )
        self.open_positions_cache.append(pos)
        return pos

    async def close_position(self, position_id: str, reason: str = "MANUAL") -> Position:
        pos_to_close = next(
            (p for p in self.open_positions_cache if p.id == position_id or p.instrument == position_id),
            None
        )

        symbol = _sanitize_stock_symbol(pos_to_close.instrument if pos_to_close else position_id)
        url = f"{self.base_url}/positions/{symbol}"

        def _request():
            shared_alpaca_rate_limiter.acquire(wait=True)
            return requests.delete(url, headers=self.headers, timeout=10)

        response = await asyncio.to_thread(_request)
        if response.status_code == 429:
            shared_alpaca_rate_limiter.record_rate_limit(response.headers.get("Retry-After"))
        if response.status_code not in (200, 204, 404):
            logger.warning(f"Alpaca Position Close Warnung: {response.text}")

        self._cache.invalidate_prefix("account:")
        curr_price_info = await self.get_current_price(symbol)
        curr_price = curr_price_info.mid

        if pos_to_close:
            self.open_positions_cache.remove(pos_to_close)
            if pos_to_close.direction == PositionDirection.BUY:
                pnl = (curr_price - pos_to_close.entry_price) * pos_to_close.units
            else:
                pnl = (pos_to_close.entry_price - curr_price) * pos_to_close.units

            closed_pos = Position(
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
            self.closed_positions_cache.append(closed_pos)
            return closed_pos

        now_iso = _utc_iso()
        return Position(
            id=position_id,
            instrument=symbol,
            direction=PositionDirection.BUY,
            units=1,
            entry_price=curr_price,
            current_price=curr_price,
            status=PositionStatus.CLOSED,
            close_reason=reason,
            open_time=now_iso,
            close_time=now_iso
        )

    async def update_stop_loss(self, position_id: str, new_sl: float) -> Position:
        target_pos = next((p for p in self.open_positions_cache if p.id == position_id), None)
        if not target_pos:
            raise Exception(f"Position {position_id} nicht im lokalen Cache gefunden.")

        symbol = _sanitize_stock_symbol(target_pos.instrument)
        rounded_sl = round(new_sl, 2)

        try:
            orders_url = f"{self.base_url}/orders"
            params = {"status": "open", "symbols": symbol}
            res = await asyncio.to_thread(self._safe_get, orders_url, params)
            if res is not None and res.status_code == 200:
                open_orders = res.json()
                stop_order = next(
                    (o for o in open_orders if o.get("type") in ("stop", "stop_limit")),
                    None
                )
                if stop_order and stop_order.get("id"):
                    patch_url = f"{self.base_url}/orders/{stop_order['id']}"

                    def _patch_order():
                        shared_alpaca_rate_limiter.acquire(wait=True)
                        return requests.patch(
                            patch_url,
                            headers=self.headers,
                            json={"stop_price": str(rounded_sl)},
                            timeout=10
                        )

                    patch_res = await asyncio.to_thread(_patch_order)
                    if patch_res.status_code not in (200, 201):
                        logger.warning(f"Alpaca Stop-Loss PATCH fehlgeschlagen für {symbol}: {patch_res.text}")
                    else:
                        logger.info(f"Alpaca Stop-Loss Order {stop_order['id']} ({symbol}) live aktualisiert auf ${rounded_sl:.2f}")
        except Exception as e:
            logger.warning(f"Alpaca Stop-Loss API Sync Fehler für {symbol}: {e}")

        target_pos.stop_loss = rounded_sl
        return target_pos

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
        live_positions: List[Position] = []
        live_syms = set()

        for item in data:
            if not isinstance(item, dict):
                continue
            if str(item.get("asset_class", "")).lower() == "crypto":
                continue
            sym = item.get("symbol", "")
            live_syms.add(sym)
            side_str = item.get("side", "long").lower()
            direction = PositionDirection.BUY if side_str == "long" else PositionDirection.SELL
            qty = abs(int(float(item.get("qty", 1))))
            entry = float(item.get("avg_entry_price", 0.0))
            curr = float(item.get("current_price", entry))
            unrealized = float(item.get("unrealized_pl", 0.0))

            matched_cached = prev_by_sym.get(sym)
            sl = matched_cached.stop_loss if matched_cached else None
            initial_sl = matched_cached.initial_stop_loss if matched_cached else sl
            tp = matched_cached.take_profit if matched_cached else None

            pos = Position(
                id=item.get("asset_id", f"alpaca_{sym}"),
                instrument=sym,
                direction=direction,
                units=qty,
                entry_price=round(entry, 2),
                current_price=round(curr, 2),
                stop_loss=sl,
                initial_stop_loss=initial_sl,
                take_profit=tp,
                unrealized_pnl=round(unrealized, 2),
                realized_pnl=0.0,
                open_time=matched_cached.open_time if matched_cached else _utc_iso(),
                status=PositionStatus.OPEN
            )
            live_positions.append(pos)

        for old_sym, old_pos in prev_by_sym.items():
            if old_sym not in live_syms and not any(c.id == old_pos.id for c in self.closed_positions_cache):
                old_pos.status = PositionStatus.CLOSED
                old_pos.close_time = _utc_iso()
                old_pos.realized_pnl = old_pos.unrealized_pnl
                old_pos.unrealized_pnl = 0.0
                old_pos.close_reason = "TAKE_PROFIT" if old_pos.realized_pnl > 0 else "STOP_LOSS"
                self.closed_positions_cache.append(old_pos)

        self.open_positions_cache = live_positions
        self._cache.set("account:positions", self.open_positions_cache)
        return self.open_positions_cache

    async def get_closed_positions(self) -> List[Position]:
        return self.closed_positions_cache

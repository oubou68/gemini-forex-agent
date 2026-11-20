import asyncio
import json
import logging
import math
import re
from pathlib import Path
from typing import List, Optional
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Body, Request, Response, Depends, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agent.core.multi_agent_manager import MultiAgentManager
from agent.broker.alpaca_crypto_client import normalize_crypto_symbol
from agent.security.auth import auth_manager
from config.settings import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Multi-agent manager holding both Forex and Stock bots
agent_manager = MultiAgentManager()
active_websockets: List[WebSocket] = []

_FOREX_INST_RE = re.compile(r"^[A-Z0-9_/-]{3,15}$")
_STOCK_SYM_RE = re.compile(r"^[A-Z0-9.-]{1,10}$")
_POS_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _extract_token_from_request(request: Request) -> Optional[str]:
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header[7:].strip()
    if "aura_session" in request.cookies:
        return request.cookies["aura_session"].strip()
    return None


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _is_https_request(request: Request) -> bool:
    if settings.AUTH_COOKIE_SECURE is not None:
        return bool(settings.AUTH_COOKIE_SECURE)
    proto = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    return proto == "https" or request.url.scheme == "https"


def _validate_instrument(bot_type: str, instrument: Optional[str]) -> Optional[str]:
    if instrument is None:
        return None
    cleaned = str(instrument).strip().upper()
    if bot_type == "crypto":
        try:
            return normalize_crypto_symbol(cleaned)
        except ValueError:
            raise HTTPException(status_code=400, detail="Ungültiges Krypto-Symbol (Format z.B. BTC/USD).")
    elif bot_type == "stock":
        sym = cleaned.replace("/", "").replace("_", "")
        if not _STOCK_SYM_RE.match(sym):
            raise HTTPException(status_code=400, detail="Ungültiges Aktien-Symbol.")
        return sym
    else:
        pair = cleaned.replace("/", "_")
        if not _FOREX_INST_RE.match(pair):
            raise HTTPException(status_code=400, detail="Ungültiges Forex-Instrument.")
        return pair


def _validate_finite_range(name: str, value: float, min_val: float, max_val: float) -> float:
    if not isinstance(value, (int, float)) or math.isnan(value) or math.isinf(value):
        raise HTTPException(status_code=400, detail=f"Ungültiger numerischer Wert für {name}.")
    if value < min_val or value > max_val:
        raise HTTPException(status_code=400, detail=f"{name} muss zwischen {min_val} und {max_val} liegen.")
    return float(value)


def _is_allowed_ws_origin(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    if not origin:
        return True  # Non-browser clients / native clients
    allowed = set(settings.get_cors_origins())
    if origin in allowed:
        return True
    try:
        parsed_origin = urlparse(origin)
        host_header = websocket.headers.get("host", "")
        if parsed_origin.netloc and host_header and parsed_origin.netloc.lower() == host_header.lower():
            return True
    except Exception:
        pass
    return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    await agent_manager.initialize()
    agent_manager.subscribe_telemetry(broadcast_bot_telemetry)
    await agent_manager.start_all()
    yield
    await agent_manager.stop_all()


app = FastAPI(
    title="Gemini Autonomous Forex & Stock Trading Agents",
    lifespan=lifespan,
    docs_url="/docs" if settings.ENABLE_API_DOCS else None,
    redoc_url="/redoc" if settings.ENABLE_API_DOCS else None,
    openapi_url="/openapi.json" if settings.ENABLE_API_DOCS else None,
)

# Sichere CORS-Konfiguration (kein Wildcard '*' mit allow_credentials=True)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_cors_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com https://accounts.google.com https://apis.google.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com https://accounts.google.com; "
        "font-src 'self' https://fonts.gstatic.com https://cdnjs.cloudflare.com data:; "
        "img-src 'self' data: https://*.googleusercontent.com; "
        "connect-src 'self' ws: wss: https://accounts.google.com https://oauth2.googleapis.com; "
        "frame-src https://accounts.google.com; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none';"
    )
    if _is_https_request(request):
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


STATIC_DIR = Path(__file__).resolve().parent / "static"
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


async def broadcast_bot_telemetry(bot_type: str, telemetry_data: dict):
    if not active_websockets:
        return
    message = json.dumps({
        "type": "TELEMETRY",
        "bot_type": bot_type,
        "data": telemetry_data
    })
    disconnected = []
    for ws in list(active_websockets):
        try:
            await ws.send_text(message)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        if ws in active_websockets:
            active_websockets.remove(ws)


@app.get("/login", response_class=HTMLResponse)
async def get_login_page(request: Request):
    """Serves standalone login page or redirects to / if already logged in."""
    token = _extract_token_from_request(request)
    if token and auth_manager.verify_token(token):
        return RedirectResponse(
            url="/",
            status_code=303,
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"}
        )

    login_file = TEMPLATES_DIR / "login.html"
    if login_file.exists():
        return FileResponse(
            str(login_file),
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"}
        )
    return HTMLResponse("<h1>AURA QUANT AI Login</h1>")


@app.get("/index.html")
async def redirect_index():
    return RedirectResponse(url="/", status_code=301)


@app.get("/", response_class=HTMLResponse)
async def get_index(request: Request):
    """Serves the trading cockpit ONLY if authenticated; redirects to /login otherwise."""
    if settings.AUTH_ENABLED:
        token = _extract_token_from_request(request)
        user = auth_manager.verify_token(token) if token else None
        if not user:
            return RedirectResponse(
                url="/login",
                status_code=303,
                headers={"Cache-Control": "no-store, no-cache, must-revalidate"}
            )

    index_file = TEMPLATES_DIR / "index.html"
    if index_file.exists():
        return FileResponse(
            str(index_file),
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"}
        )
    return HTMLResponse("<h1>Gemini Dual Agent UI Loading...</h1>")


class LoginRequest(BaseModel):
    username: str
    password: str


class GoogleAuthRequest(BaseModel):
    credential: Optional[str] = None
    id_token: Optional[str] = None


async def require_auth(request: Request) -> dict:
    """Verifies that incoming request has a valid, non-revoked session token."""
    if not settings.AUTH_ENABLED:
        return {"username": settings.AUTH_USERNAME, "email": settings.GOOGLE_ALLOWED_EMAIL, "role": "admin"}

    token = _extract_token_from_request(request)
    if not token:
        raise HTTPException(status_code=401, detail="Nicht authentifiziert. Bitte einloggen.")

    user = auth_manager.verify_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Ungültige oder abgelaufene Sitzung.")

    return user


@app.get("/api/auth/config")
async def get_auth_config():
    """Returns public authentication parameters."""
    return {
        "auth_enabled": settings.AUTH_ENABLED,
        "google_client_id": settings.GOOGLE_CLIENT_ID,
        "google_allowed_email": settings.GOOGLE_ALLOWED_EMAIL
    }


@app.get("/api/auth/status")
async def get_auth_status(request: Request):
    """Checks current session status and returns user identity."""
    if not settings.AUTH_ENABLED:
        return {
            "authenticated": True,
            "user": {"username": settings.AUTH_USERNAME, "email": settings.GOOGLE_ALLOWED_EMAIL, "role": "admin"}
        }

    token = _extract_token_from_request(request)
    user = auth_manager.verify_token(token) if token else None
    if not user:
        return {"authenticated": False, "user": None}

    return {"authenticated": True, "user": user}


@app.post("/api/auth/login")
async def login(credentials: LoginRequest, request: Request, response: Response):
    """Plain username and password login with brute-force rate limiting."""
    client_ip = _get_client_ip(request)
    allowed, retry_after = auth_manager.rate_limiter.check_allowed(client_ip)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Zu viele fehlgeschlagene Anmeldeversuche. Bitte in {retry_after} Sekunden erneut versuchen.",
            headers={"Retry-After": str(retry_after)}
        )

    user = auth_manager.authenticate_local(credentials.username, credentials.password)
    if not user:
        auth_manager.rate_limiter.record_failure(client_ip)
        raise HTTPException(status_code=401, detail="Ungültiger Benutzername oder Passwort.")

    auth_manager.rate_limiter.reset(client_ip)
    token = auth_manager.create_token(user)
    max_age = settings.AUTH_SESSION_EXPIRE_HOURS * 3600
    response.set_cookie(
        key="aura_session",
        value=token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=_is_https_request(request),
        path="/"
    )
    return {
        "status": "authenticated",
        "token": token,
        "user": user
    }


@app.post("/api/auth/google")
async def google_auth(payload: GoogleAuthRequest, request: Request, response: Response):
    """Google Identity verification strictly enforced for allowed email with rate limiting."""
    client_ip = _get_client_ip(request)
    allowed, retry_after = auth_manager.rate_limiter.check_allowed(client_ip)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Zu viele fehlgeschlagene Anmeldeversuche. Bitte in {retry_after} Sekunden erneut versuchen.",
            headers={"Retry-After": str(retry_after)}
        )

    token_str = payload.credential or payload.id_token
    if not token_str:
        raise HTTPException(status_code=400, detail="Google Token / Credential fehlt.")

    try:
        user = auth_manager.authenticate_google(token_str)
    except PermissionError as e:
        auth_manager.rate_limiter.record_failure(client_ip)
        logger.warning(f"Google Login abgewiesen: {e}")
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        auth_manager.rate_limiter.record_failure(client_ip)
        logger.warning(f"Google Token ungültig: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Unerwarteter Google Auth Fehler: {e}")
        raise HTTPException(status_code=500, detail="Google Authentifizierung fehlgeschlagen.")

    auth_manager.rate_limiter.reset(client_ip)
    token = auth_manager.create_token(user)
    max_age = settings.AUTH_SESSION_EXPIRE_HOURS * 3600
    response.set_cookie(
        key="aura_session",
        value=token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=_is_https_request(request),
        path="/"
    )
    return {
        "status": "authenticated",
        "token": token,
        "user": user
    }


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    """Terminates session, revokes token server-side, and clears cookie."""
    token = _extract_token_from_request(request)
    if token:
        auth_manager.revoke_token(token)
    response.delete_cookie(key="aura_session", path="/")
    return {"status": "logged_out"}


@app.get("/api/status", dependencies=[Depends(require_auth)])
async def get_status(bot_type: Optional[str] = None):
    if bot_type == "forex":
        tel = await agent_manager.forex_agent.get_telemetry()
        return tel.model_dump()
    elif bot_type == "stock":
        tel = await agent_manager.stock_agent.get_telemetry()
        return tel.model_dump()
    elif bot_type == "crypto":
        tel = await agent_manager.crypto_agent.get_telemetry()
        return tel.model_dump()
    return await agent_manager.get_all_telemetry()


@app.get("/api/candles", dependencies=[Depends(require_auth)])
async def get_candles(
    bot_type: str = "forex",
    instrument: Optional[str] = None,
    count: int = Query(default=120, ge=1, le=500)
):
    validated_inst = _validate_instrument(bot_type, instrument)
    if bot_type == "crypto":
        symbol = validated_inst or agent_manager.crypto_agent.current_symbol
        candles = await agent_manager.crypto_agent.broker.get_candles(symbol, granularity="M5", count=count)
    elif bot_type == "stock":
        symbol = validated_inst or agent_manager.stock_agent.current_symbol
        candles = await agent_manager.stock_agent.broker.get_candles(symbol, granularity="M5", count=count)
    else:
        symbol = validated_inst or agent_manager.forex_agent.current_instrument
        candles = await agent_manager.forex_agent.broker.get_candles(symbol, granularity="M5", count=count)
    return [c.model_dump() for c in candles]


@app.get("/api/stock/universe", dependencies=[Depends(require_auth)])
async def get_stock_universe():
    return agent_manager.stock_agent.get_universe_catalog()


@app.get("/api/stock/screener", dependencies=[Depends(require_auth)])
async def get_stock_screener():
    candidates = await agent_manager.stock_agent.screen_market_universe()
    return [c.model_dump() for c in candidates]


@app.get("/api/crypto/universe", dependencies=[Depends(require_auth)])
async def get_crypto_universe():
    return agent_manager.crypto_agent.get_universe_catalog()


@app.get("/api/crypto/screener", dependencies=[Depends(require_auth)])
async def get_crypto_screener():
    candidates = await agent_manager.crypto_agent.screen_crypto_universe()
    return [c.model_dump() for c in candidates]


@app.get("/api/trades/history", dependencies=[Depends(require_auth)])
async def get_trade_history(
    bot_type: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200)
):
    if bot_type == "stock":
        tel = await agent_manager.stock_agent.get_telemetry()
        return {"bot_type": "stock", "trades": tel.closed_positions[:limit]}
    elif bot_type == "forex":
        tel = await agent_manager.forex_agent.get_telemetry()
        return {"bot_type": "forex", "trades": tel.closed_positions[:limit]}
    elif bot_type == "crypto":
        tel = await agent_manager.crypto_agent.get_telemetry()
        return {"bot_type": "crypto", "trades": tel.closed_positions[:limit]}

    forex_tel = await agent_manager.forex_agent.get_telemetry()
    stock_tel = await agent_manager.stock_agent.get_telemetry()
    crypto_tel = await agent_manager.crypto_agent.get_telemetry()
    return {
        "forex": forex_tel.closed_positions[:limit],
        "stock": stock_tel.closed_positions[:limit],
        "crypto": crypto_tel.closed_positions[:limit]
    }


@app.post("/api/control/start", dependencies=[Depends(require_auth)])
async def start_agent(payload: dict = Body(default={})):
    bot = str(payload.get("bot_type", "all")).lower()
    if bot not in ("forex", "stock", "crypto", "all"):
        raise HTTPException(status_code=400, detail="Ungültiger bot_type.")
    await agent_manager.start_bot(bot)
    return {
        "status": "started",
        "forex_running": agent_manager.forex_agent.is_running,
        "stock_running": agent_manager.stock_agent.is_running,
        "crypto_running": agent_manager.crypto_agent.is_running
    }


@app.post("/api/control/stop", dependencies=[Depends(require_auth)])
async def stop_agent(payload: dict = Body(default={})):
    bot = str(payload.get("bot_type", "all")).lower()
    if bot not in ("forex", "stock", "crypto", "all"):
        raise HTTPException(status_code=400, detail="Ungültiger bot_type.")
    await agent_manager.stop_bot(bot)
    return {
        "status": "stopped",
        "forex_running": agent_manager.forex_agent.is_running,
        "stock_running": agent_manager.stock_agent.is_running,
        "crypto_running": agent_manager.crypto_agent.is_running
    }


@app.post("/api/control/instrument", dependencies=[Depends(require_auth)])
async def set_instrument(payload: dict = Body(...)):
    bot_type = str(payload.get("bot_type", "forex")).lower()
    default_inst = "BTC/USD" if bot_type == "crypto" else ("AAPL" if bot_type == "stock" else "EUR_USD")
    raw_inst = payload.get("instrument", default_inst)
    inst = _validate_instrument(bot_type, raw_inst)
    if bot_type == "crypto":
        await agent_manager.crypto_agent.set_symbol(inst)
        return {"status": "updated", "bot_type": "crypto", "current_symbol": agent_manager.crypto_agent.current_symbol}
    elif bot_type == "stock":
        await agent_manager.stock_agent.set_symbol(inst)
        return {"status": "updated", "bot_type": "stock", "current_symbol": agent_manager.stock_agent.current_symbol}
    else:
        await agent_manager.forex_agent.set_instrument(inst)
        return {"status": "updated", "bot_type": "forex", "current_instrument": agent_manager.forex_agent.current_instrument}


@app.post("/api/control/mode", dependencies=[Depends(require_auth)])
async def set_mode(payload: dict = Body(...)):
    bot_type = str(payload.get("bot_type", "forex")).lower()
    mode = str(payload.get("mode", "simulator")).lower()
    if bot_type == "crypto":
        if mode not in ("simulator", "paper", "live"):
            raise HTTPException(status_code=400, detail="Ungültiger Crypto-Modus (erlaubt: simulator, paper, live).")
        await agent_manager.crypto_agent.set_mode(mode)
        return {"status": "updated", "bot_type": "crypto", "mode": agent_manager.crypto_agent.mode}
    elif bot_type == "stock":
        if mode not in ("simulator", "paper", "live"):
            raise HTTPException(status_code=400, detail="Ungültiger Stock-Modus (erlaubt: simulator, paper, live).")
        await agent_manager.stock_agent.set_mode(mode)
        return {"status": "updated", "bot_type": "stock", "mode": agent_manager.stock_agent.mode}
    else:
        if mode not in ("simulator", "practice", "live"):
            raise HTTPException(status_code=400, detail="Ungültiger Forex-Modus (erlaubt: simulator, practice, live).")
        await agent_manager.forex_agent.set_mode(mode)
        return {"status": "updated", "bot_type": "forex", "mode": agent_manager.forex_agent.mode}


@app.post("/api/control/close-position", dependencies=[Depends(require_auth)])
async def close_single_position(payload: dict = Body(...)):
    """Schließt gezielt eine einzelne Position (ohne andere laufende Trades zu beenden)."""
    bot_type = str(payload.get("bot_type", "forex")).lower()
    position_id = str(payload.get("position_id", "")).strip()
    if not position_id or not _POS_ID_RE.match(position_id):
        raise HTTPException(status_code=400, detail="Ungültige Position-ID.")

    try:
        if bot_type == "crypto":
            closed = await agent_manager.crypto_agent.close_single_position(position_id)
        elif bot_type == "stock":
            closed = await agent_manager.stock_agent.close_single_position(position_id)
        else:
            closed = await agent_manager.forex_agent.close_single_position(position_id)
        return {"status": "closed", "bot_type": bot_type, "position": closed.model_dump()}
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Position konnte nicht geschlossen werden: {e}")


@app.post("/api/control/emergency-close", dependencies=[Depends(require_auth)])
async def emergency_close(payload: dict = Body(default={})):
    bot_type = payload.get("bot_type")
    if bot_type is not None and str(bot_type).lower() not in ("forex", "stock", "crypto", "all"):
        raise HTTPException(status_code=400, detail="Ungültiger bot_type.")
    target = None if not bot_type or str(bot_type).lower() == "all" else str(bot_type).lower()
    results = await agent_manager.emergency_close_all(target)
    return {"status": "success", "closed_positions": results}


@app.post("/api/control/manual-order", dependencies=[Depends(require_auth)])
async def manual_order(payload: dict = Body(...)):
    bot_type = str(payload.get("bot_type", "forex")).lower()
    direction = str(payload.get("direction", "BUY")).upper().strip()
    if direction not in ("BUY", "SELL"):
        raise HTTPException(status_code=400, detail="Ungültige Order-Richtung (nur BUY oder SELL erlaubt).")

    try:
        raw_risk = float(payload.get("risk_pct", 1.0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Ungültiger risk_pct Wert.")
    risk_pct = _validate_finite_range("risk_pct", raw_risk, 0.1, 10.0)

    try:
        if bot_type == "crypto":
            sym = _validate_instrument("crypto", payload.get("instrument") or agent_manager.crypto_agent.current_symbol)
            pos = await agent_manager.crypto_agent.execute_manual_trade(sym, direction, risk_pct)
        elif bot_type == "stock":
            sym = _validate_instrument("stock", payload.get("instrument") or agent_manager.stock_agent.current_symbol)
            pos = await agent_manager.stock_agent.execute_manual_trade(sym, direction, risk_pct)
        else:
            inst = _validate_instrument("forex", payload.get("instrument") or agent_manager.forex_agent.current_instrument)
            pos = await agent_manager.forex_agent.execute_manual_trade(inst, direction, risk_pct)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"status": "executed", "bot_type": bot_type, "position": pos.model_dump()}


@app.post("/api/config/keys", dependencies=[Depends(require_auth)])
async def update_keys(payload: dict = Body(...)):
    gemini_key = payload.get("gemini_api_key")
    gemini_restrict_trading_hours = payload.get("gemini_restrict_trading_hours")
    gemini_stock_regular_hours = payload.get("gemini_stock_regular_hours_only")

    oanda_key = payload.get("oanda_api_key")
    oanda_acc = payload.get("oanda_account_id")
    oanda_mode = payload.get("oanda_mode")

    alpaca_key = payload.get("alpaca_api_key")
    alpaca_secret = payload.get("alpaca_secret_key")
    alpaca_mode = payload.get("alpaca_mode")

    if gemini_key:
        agent_manager.forex_agent.ai_analyst.set_api_key(str(gemini_key).strip())
        agent_manager.stock_agent.ai_analyst.set_api_key(str(gemini_key).strip())
        agent_manager.crypto_agent.ai_analyst.set_api_key(str(gemini_key).strip())
        agent_manager.forex_agent.log("Gemini API Key aktualisiert.", "INFO", "CONFIG")
        agent_manager.stock_agent.log("Gemini API Key aktualisiert.", "INFO", "CONFIG")
        agent_manager.crypto_agent.log("Gemini API Key aktualisiert.", "INFO", "CONFIG")

    if gemini_restrict_trading_hours is not None:
        restrict = bool(gemini_restrict_trading_hours)
        agent_manager.forex_agent.ai_analyst.set_trading_hours_restriction(restrict)
        agent_manager.stock_agent.ai_analyst.set_trading_hours_restriction(
            restrict,
            regular_hours_only=bool(gemini_stock_regular_hours) if gemini_stock_regular_hours is not None else None
        )
        settings.GEMINI_RESTRICT_TO_TRADING_HOURS = restrict
        agent_manager.forex_agent.log(f"Gemini Token-Optimierung (Handelszeiten): {restrict}", "INFO", "CONFIG")

    if oanda_key and oanda_acc:
        settings.OANDA_API_KEY = str(oanda_key).strip()
        settings.OANDA_ACCOUNT_ID = str(oanda_acc).strip()
        if oanda_mode and str(oanda_mode).lower() in ("simulator", "practice", "live"):
            await agent_manager.forex_agent.set_mode(str(oanda_mode).lower())
        agent_manager.forex_agent.log("OANDA Zugangsdaten aktualisiert.", "INFO", "CONFIG")

    if alpaca_key and alpaca_secret:
        settings.ALPACA_API_KEY = str(alpaca_key).strip()
        settings.ALPACA_SECRET_KEY = str(alpaca_secret).strip()
        if alpaca_mode and str(alpaca_mode).lower() in ("simulator", "paper", "live"):
            await agent_manager.stock_agent.set_mode(str(alpaca_mode).lower())
            await agent_manager.crypto_agent.set_mode(str(alpaca_mode).lower())
        agent_manager.stock_agent.log("Alpaca Zugangsdaten aktualisiert.", "INFO", "CONFIG")
        agent_manager.crypto_agent.log("Alpaca Crypto Zugangsdaten aktualisiert.", "INFO", "CONFIG")

    return {"status": "keys_updated"}


@app.post("/api/config/risk", dependencies=[Depends(require_auth)])
async def update_risk_config(payload: dict = Body(...)):
    """Aktualisiert und validiert Risikomanagement- und Exit-Regeln live zur Laufzeit."""
    risk_pct = _validate_finite_range("risk_per_trade_pct", float(payload["risk_per_trade_pct"]), 0.1, 10.0) if payload.get("risk_per_trade_pct") is not None else None
    max_dd = _validate_finite_range("max_daily_drawdown_pct", float(payload["max_daily_drawdown_pct"]), 0.5, 25.0) if payload.get("max_daily_drawdown_pct") is not None else None
    forex_max_pos = int(_validate_finite_range("max_open_positions", int(payload["max_open_positions"]), 1, 50)) if payload.get("max_open_positions") is not None else None
    stock_max_pos = int(_validate_finite_range("stock_max_open_positions", int(payload["stock_max_open_positions"]), 1, 50)) if payload.get("stock_max_open_positions") is not None else None
    sl_atr = _validate_finite_range("default_atr_multiplier_sl", float(payload["default_atr_multiplier_sl"]), 0.5, 10.0) if payload.get("default_atr_multiplier_sl") is not None else None
    tp_atr = _validate_finite_range("default_atr_multiplier_tp", float(payload["default_atr_multiplier_tp"]), 0.5, 15.0) if payload.get("default_atr_multiplier_tp") is not None else None
    rrr = _validate_finite_range("min_risk_reward_ratio", float(payload["min_risk_reward_ratio"]), 1.0, 10.0) if payload.get("min_risk_reward_ratio") is not None else None
    be_r = _validate_finite_range("breakeven_trigger_r", float(payload["breakeven_trigger_r"]), 0.3, 5.0) if payload.get("breakeven_trigger_r") is not None else None
    trailing_on = payload.get("trailing_stop_enabled")
    allow_ai_close = payload.get("allow_ai_close_signals")
    auto_liquidate = payload.get("auto_liquidate_on_drawdown")

    agent_manager.forex_agent.risk_manager.update_risk_parameters(
        risk_per_trade_pct=risk_pct,
        max_daily_drawdown_pct=max_dd,
        max_open_positions=forex_max_pos,
        default_atr_multiplier_sl=sl_atr,
        default_atr_multiplier_tp=tp_atr,
        min_risk_reward_ratio=rrr,
        breakeven_trigger_r=be_r,
        trailing_stop_enabled=bool(trailing_on) if trailing_on is not None else None,
        allow_ai_close_signals=bool(allow_ai_close) if allow_ai_close is not None else None,
        auto_liquidate_on_drawdown=bool(auto_liquidate) if auto_liquidate is not None else None
    )

    agent_manager.stock_agent.risk_manager.update_risk_parameters(
        risk_per_trade_pct=risk_pct,
        max_daily_drawdown_pct=max_dd,
        max_open_positions=stock_max_pos,
        default_atr_multiplier_sl=sl_atr,
        default_atr_multiplier_tp=tp_atr,
        min_risk_reward_ratio=rrr,
        breakeven_trigger_r=be_r,
        trailing_stop_enabled=bool(trailing_on) if trailing_on is not None else None,
        allow_ai_close_signals=bool(allow_ai_close) if allow_ai_close is not None else None,
        auto_liquidate_on_drawdown=bool(auto_liquidate) if auto_liquidate is not None else None
    )

    # Update Crypto Risk Manager
    agent_manager.crypto_agent.risk_manager.update_risk_parameters(
        risk_per_trade_pct=risk_pct,
        max_daily_drawdown_pct=max_dd,
        default_atr_multiplier_sl=sl_atr,
        default_atr_multiplier_tp=tp_atr,
        min_risk_reward_ratio=rrr,
        breakeven_trigger_r=be_r,
        trailing_stop_enabled=bool(trailing_on) if trailing_on is not None else None,
        allow_ai_close_signals=bool(allow_ai_close) if allow_ai_close is not None else None,
        auto_liquidate_on_drawdown=bool(auto_liquidate) if auto_liquidate is not None else None
    )

    # Synchronisiere ATR Multiplikatoren mit den AI-Analysten
    if sl_atr is not None:
        agent_manager.forex_agent.ai_analyst.atr_sl_mult = sl_atr
        agent_manager.stock_agent.ai_analyst.atr_sl_mult = sl_atr
        agent_manager.crypto_agent.ai_analyst.atr_sl_mult = sl_atr
    if tp_atr is not None:
        agent_manager.forex_agent.ai_analyst.atr_tp_mult = tp_atr
        agent_manager.stock_agent.ai_analyst.atr_tp_mult = tp_atr
        agent_manager.crypto_agent.ai_analyst.atr_tp_mult = tp_atr

    agent_manager.forex_agent.log("Risiko- und Exit-Parameter live aktualisiert.", "INFO", "CONFIG")
    agent_manager.stock_agent.log("Risiko- und Exit-Parameter live aktualisiert.", "INFO", "CONFIG")
    agent_manager.crypto_agent.log("Risiko- und Exit-Parameter live aktualisiert.", "INFO", "CONFIG")

    return {"status": "risk_config_updated"}


@app.post("/api/trades/clear", dependencies=[Depends(require_auth)])
async def clear_trade_history(payload: dict = Body(default={})):
    """Leert die Trade-Historie für einen oder alle Bots."""
    bot_type = str(payload.get("bot_type", "all")).lower()
    if bot_type not in ("forex", "stock", "crypto", "all"):
        raise HTTPException(status_code=400, detail="Ungültiger bot_type.")
    if bot_type in ("forex", "all"):
        agent_manager.forex_agent.memory.clear_memory()
        agent_manager.forex_agent.log("Trade-Historie (Forex) zurückgesetzt.", "INFO", "MEMORY")
    if bot_type in ("stock", "all"):
        agent_manager.stock_agent.memory.clear_memory()
        agent_manager.stock_agent.log("Trade-Historie (Aktien) zurückgesetzt.", "INFO", "MEMORY")
    if bot_type in ("crypto", "all"):
        agent_manager.crypto_agent.memory.clear_memory()
        agent_manager.crypto_agent.log("Trade-Historie (Krypto) zurückgesetzt.", "INFO", "MEMORY")

    return {"status": "history_cleared", "bot_type": bot_type}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: Optional[str] = Query(default=None)):
    # 1. CSWSH-Schutz: Origin-Validierung
    if not _is_allowed_ws_origin(websocket):
        logger.warning(f"WebSocket Verbindung von nicht autorisierter Origin abgewiesen: {websocket.headers.get('origin')}")
        await websocket.close(code=1008, reason="Forbidden Origin")
        return

    session_cookie = websocket.cookies.get("aura_session")

    if settings.AUTH_ENABLED:
        user = None
        if token:
            user = auth_manager.verify_token(token)
        if not user and session_cookie:
            user = auth_manager.verify_token(session_cookie)

        if not user:
            logger.warning("Unauthorisierter WebSocket-Verbindungsversuch abgewiesen.")
            await websocket.close(code=1008, reason="Unauthorized")
            return

    await websocket.accept()
    active_websockets.append(websocket)

    try:
        all_tel = await agent_manager.get_all_telemetry()
        await websocket.send_text(json.dumps({
            "type": "INITIAL_STATE",
            "forex": all_tel["forex"],
            "stock": all_tel["stock"],
            "crypto": all_tel["crypto"]
        }))

        forex_candles = await agent_manager.forex_agent.broker.get_candles(
            agent_manager.forex_agent.current_instrument, "M5", 100
        )
        await websocket.send_text(json.dumps({
            "type": "CANDLES",
            "bot_type": "forex",
            "instrument": agent_manager.forex_agent.current_instrument,
            "data": [c.model_dump() for c in forex_candles]
        }))

        stock_candles = await agent_manager.stock_agent.broker.get_candles(
            agent_manager.stock_agent.current_symbol, "M5", 100
        )
        await websocket.send_text(json.dumps({
            "type": "CANDLES",
            "bot_type": "stock",
            "instrument": agent_manager.stock_agent.current_symbol,
            "data": [c.model_dump() for c in stock_candles]
        }))

        while True:
            text_data = await websocket.receive_text()
            try:
                msg = json.loads(text_data)
                if not isinstance(msg, dict):
                    continue
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({"type": "ERROR", "detail": "Ungültiges JSON-Format."}))
                continue

            action = msg.get("action")
            bot_type = str(msg.get("bot_type", "forex")).lower()
            if bot_type == "crypto":
                target_agent = agent_manager.crypto_agent
            elif bot_type == "stock":
                target_agent = agent_manager.stock_agent
            else:
                target_agent = agent_manager.forex_agent

            if action == "START":
                await agent_manager.start_bot(bot_type)
            elif action == "STOP":
                await agent_manager.stop_bot(bot_type)
            elif action == "SCAN":
                target_sym = target_agent.current_symbol if bot_type in ("stock", "crypto") else target_agent.current_instrument
                await target_agent.run_scan_cycle(target_sym)
                await target_agent.broadcast_telemetry()
            elif action == "SWITCH_INSTRUMENT":
                try:
                    inst = _validate_instrument(bot_type, msg.get("instrument"))
                except HTTPException:
                    continue
                if not inst:
                    continue
                if bot_type == "crypto":
                    candles = await agent_manager.crypto_agent.broker.get_candles(inst, "M5", 100)
                    await websocket.send_text(json.dumps({
                        "type": "CANDLES",
                        "bot_type": "crypto",
                        "instrument": inst,
                        "data": [c.model_dump() for c in candles]
                    }))
                    asyncio.create_task(agent_manager.crypto_agent.set_symbol(inst))
                elif bot_type == "stock":
                    candles = await agent_manager.stock_agent.broker.get_candles(inst, "M5", 100)
                    await websocket.send_text(json.dumps({
                        "type": "CANDLES",
                        "bot_type": "stock",
                        "instrument": inst,
                        "data": [c.model_dump() for c in candles]
                    }))
                    asyncio.create_task(agent_manager.stock_agent.set_symbol(inst))
                else:
                    candles = await agent_manager.forex_agent.broker.get_candles(inst, "M5", 100)
                    await websocket.send_text(json.dumps({
                        "type": "CANDLES",
                        "bot_type": "forex",
                        "instrument": inst,
                        "data": [c.model_dump() for c in candles]
                    }))
                    asyncio.create_task(agent_manager.forex_agent.set_instrument(inst))
            elif action == "REQUEST_CANDLES":
                req_bot = str(msg.get("bot_type", "forex")).lower()
                try:
                    req_inst = _validate_instrument(req_bot, msg.get("instrument"))
                except HTTPException:
                    continue
                if req_bot == "crypto":
                    sym = req_inst or agent_manager.crypto_agent.current_symbol
                    candles = await agent_manager.crypto_agent.broker.get_candles(sym, "M5", 100)
                    await websocket.send_text(json.dumps({
                        "type": "CANDLES",
                        "bot_type": "crypto",
                        "instrument": sym,
                        "data": [c.model_dump() for c in candles]
                    }))
                elif req_bot == "stock":
                    sym = req_inst or agent_manager.stock_agent.current_symbol
                    candles = await agent_manager.stock_agent.broker.get_candles(sym, "M5", 100)
                    await websocket.send_text(json.dumps({
                        "type": "CANDLES",
                        "bot_type": "stock",
                        "instrument": sym,
                        "data": [c.model_dump() for c in candles]
                    }))
                else:
                    inst = req_inst or agent_manager.forex_agent.current_instrument
                    candles = await agent_manager.forex_agent.broker.get_candles(inst, "M5", 100)
                    await websocket.send_text(json.dumps({
                        "type": "CANDLES",
                        "bot_type": "forex",
                        "instrument": inst,
                        "data": [c.model_dump() for c in candles]
                    }))
            elif action == "EMERGENCY_CLOSE":
                await agent_manager.emergency_close_all(bot_type)
            elif action == "MANUAL_TRADE":
                direction = str(msg.get("direction", "BUY")).upper().strip()
                if direction not in ("BUY", "SELL"):
                    continue
                try:
                    risk_pct = max(0.1, min(10.0, float(msg.get("risk_pct", 1.0))))
                except (TypeError, ValueError):
                    risk_pct = 1.0
                try:
                    if bot_type == "crypto":
                        await agent_manager.crypto_agent.execute_manual_trade(agent_manager.crypto_agent.current_symbol, direction, risk_pct)
                    elif bot_type == "stock":
                        await agent_manager.stock_agent.execute_manual_trade(agent_manager.stock_agent.current_symbol, direction, risk_pct)
                    else:
                        await agent_manager.forex_agent.execute_manual_trade(agent_manager.forex_agent.current_instrument, direction, risk_pct)
                except ValueError as e:
                    await websocket.send_text(json.dumps({"type": "ERROR", "detail": str(e)}))

    except WebSocketDisconnect:
        if websocket in active_websockets:
            active_websockets.remove(websocket)
    except Exception as e:
        logger.debug(f"WebSocket Verbindung beendet: {e}")
        if websocket in active_websockets:
            active_websockets.remove(websocket)

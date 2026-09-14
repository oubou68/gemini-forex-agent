import asyncio
import json
import logging
from pathlib import Path
from typing import List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Body, Request, Response, Depends, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agent.core.multi_agent_manager import MultiAgentManager
from agent.security.auth import auth_manager
from config.settings import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Multi-agent manager holding both Forex and Stock bots
agent_manager = MultiAgentManager()
active_websockets: List[WebSocket] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await agent_manager.initialize()
    agent_manager.subscribe_telemetry(broadcast_bot_telemetry)
    # Automatically start autonomous loops
    await agent_manager.start_all()
    yield
    # Shutdown
    await agent_manager.stop_all()


app = FastAPI(title="Gemini Autonomous Forex & Stock Trading Agents", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    for ws in active_websockets:
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
    token = None
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    elif "aura_session" in request.cookies:
        token = request.cookies["aura_session"].strip()

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
        token = None
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
        elif "aura_session" in request.cookies:
            token = request.cookies["aura_session"].strip()

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
    """Verifies that incoming request has a valid session token (cookie or Bearer header)."""
    if not settings.AUTH_ENABLED:
        return {"username": settings.AUTH_USERNAME, "email": settings.GOOGLE_ALLOWED_EMAIL, "role": "admin"}

    token = None
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    elif "aura_session" in request.cookies:
        token = request.cookies["aura_session"].strip()

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

    token = None
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    elif "aura_session" in request.cookies:
        token = request.cookies["aura_session"].strip()

    user = auth_manager.verify_token(token) if token else None
    if not user:
        return {"authenticated": False, "user": None}

    return {"authenticated": True, "user": user}


@app.post("/api/auth/login")
async def login(credentials: LoginRequest, response: Response):
    """Plain username and password login."""
    user = auth_manager.authenticate_local(credentials.username, credentials.password)
    if not user:
        raise HTTPException(status_code=401, detail="Ungültiger Benutzername oder Passwort.")

    token = auth_manager.create_token(user)
    max_age = settings.AUTH_SESSION_EXPIRE_HOURS * 3600
    response.set_cookie(
        key="aura_session",
        value=token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/"
    )
    return {
        "status": "authenticated",
        "token": token,
        "user": user
    }


@app.post("/api/auth/google")
async def google_auth(payload: GoogleAuthRequest, response: Response):
    """Google Identity verification strictly enforced for oubou68@gmail.com."""
    token_str = payload.credential or payload.id_token
    if not token_str:
        raise HTTPException(status_code=400, detail="Google Token / Credential fehlt.")

    try:
        user = auth_manager.authenticate_google(token_str)
    except PermissionError as e:
        logger.warning(f"Google Login abgewiesen: {e}")
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        logger.warning(f"Google Token ungültig: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Unerwarteter Google Auth Fehler: {e}")
        raise HTTPException(status_code=500, detail="Google Authentifizierung fehlgeschlagen.")

    token = auth_manager.create_token(user)
    max_age = settings.AUTH_SESSION_EXPIRE_HOURS * 3600
    response.set_cookie(
        key="aura_session",
        value=token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/"
    )
    return {
        "status": "authenticated",
        "token": token,
        "user": user
    }


@app.post("/api/auth/logout")
async def logout(response: Response):
    """Terminates session and clears cookie."""
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
    return await agent_manager.get_all_telemetry()


@app.get("/api/candles", dependencies=[Depends(require_auth)])
async def get_candles(bot_type: str = "forex", instrument: Optional[str] = None, count: int = 120):
    if bot_type == "stock":
        symbol = instrument or agent_manager.stock_agent.current_symbol
        candles = await agent_manager.stock_agent.broker.get_candles(symbol, granularity="M5", count=count)
    else:
        symbol = instrument or agent_manager.forex_agent.current_instrument
        candles = await agent_manager.forex_agent.broker.get_candles(symbol, granularity="M5", count=count)
    return [c.model_dump() for c in candles]


@app.get("/api/stock/universe", dependencies=[Depends(require_auth)])
async def get_stock_universe():
    return agent_manager.stock_agent.get_universe_catalog()


@app.get("/api/stock/screener", dependencies=[Depends(require_auth)])
async def get_stock_screener():
    candidates = await agent_manager.stock_agent.screen_market_universe()
    return [c.model_dump() for c in candidates]


@app.get("/api/trades/history", dependencies=[Depends(require_auth)])
async def get_trade_history(bot_type: Optional[str] = None, limit: int = 50):
    if bot_type == "stock":
        tel = await agent_manager.stock_agent.get_telemetry()
        return {"bot_type": "stock", "trades": tel.closed_positions[:limit]}
    elif bot_type == "forex":
        tel = await agent_manager.forex_agent.get_telemetry()
        return {"bot_type": "forex", "trades": tel.closed_positions[:limit]}
    
    forex_tel = await agent_manager.forex_agent.get_telemetry()
    stock_tel = await agent_manager.stock_agent.get_telemetry()
    return {
        "forex": forex_tel.closed_positions[:limit],
        "stock": stock_tel.closed_positions[:limit]
    }


@app.post("/api/control/start", dependencies=[Depends(require_auth)])
async def start_agent(payload: dict = Body(default={})):
    bot = payload.get("bot_type", "all")
    await agent_manager.start_bot(bot)
    return {
        "status": "started",
        "forex_running": agent_manager.forex_agent.is_running,
        "stock_running": agent_manager.stock_agent.is_running
    }


@app.post("/api/control/stop", dependencies=[Depends(require_auth)])
async def stop_agent(payload: dict = Body(default={})):
    bot = payload.get("bot_type", "all")
    await agent_manager.stop_bot(bot)
    return {
        "status": "stopped",
        "forex_running": agent_manager.forex_agent.is_running,
        "stock_running": agent_manager.stock_agent.is_running
    }


@app.post("/api/control/instrument", dependencies=[Depends(require_auth)])
async def set_instrument(payload: dict = Body(...)):
    bot_type = payload.get("bot_type", "forex")
    inst = payload.get("instrument", "EUR_USD")
    if bot_type == "stock":
        await agent_manager.stock_agent.set_symbol(inst)
        return {"status": "updated", "bot_type": "stock", "current_symbol": agent_manager.stock_agent.current_symbol}
    else:
        await agent_manager.forex_agent.set_instrument(inst)
        return {"status": "updated", "bot_type": "forex", "current_instrument": agent_manager.forex_agent.current_instrument}


@app.post("/api/control/mode", dependencies=[Depends(require_auth)])
async def set_mode(payload: dict = Body(...)):
    bot_type = payload.get("bot_type", "forex")
    mode = payload.get("mode", "simulator")
    if bot_type == "stock":
        await agent_manager.stock_agent.set_mode(mode)
        return {"status": "updated", "bot_type": "stock", "mode": agent_manager.stock_agent.mode}
    else:
        await agent_manager.forex_agent.set_mode(mode)
        return {"status": "updated", "bot_type": "forex", "mode": agent_manager.forex_agent.mode}


@app.post("/api/control/emergency-close", dependencies=[Depends(require_auth)])
async def emergency_close(payload: dict = Body(default={})):
    bot_type = payload.get("bot_type")
    results = await agent_manager.emergency_close_all(bot_type)
    return {"status": "success", "closed_positions": results}


@app.post("/api/control/manual-order", dependencies=[Depends(require_auth)])
async def manual_order(payload: dict = Body(...)):
    bot_type = payload.get("bot_type", "forex")
    direction = payload.get("direction", "BUY")
    risk_pct = float(payload.get("risk_pct", 1.0))

    if bot_type == "stock":
        sym = payload.get("instrument", agent_manager.stock_agent.current_symbol)
        pos = await agent_manager.stock_agent.execute_manual_trade(sym, direction, risk_pct)
    else:
        inst = payload.get("instrument", agent_manager.forex_agent.current_instrument)
        pos = await agent_manager.forex_agent.execute_manual_trade(inst, direction, risk_pct)

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
        agent_manager.forex_agent.ai_analyst.set_api_key(gemini_key)
        agent_manager.stock_agent.ai_analyst.set_api_key(gemini_key)
        agent_manager.forex_agent.log("Gemini API Key aktualisiert.", "INFO", "CONFIG")
        agent_manager.stock_agent.log("Gemini API Key aktualisiert.", "INFO", "CONFIG")

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
        settings.OANDA_API_KEY = oanda_key
        settings.OANDA_ACCOUNT_ID = oanda_acc
        if oanda_mode:
            await agent_manager.forex_agent.set_mode(oanda_mode)
        agent_manager.forex_agent.log("OANDA Zugangsdaten aktualisiert.", "INFO", "CONFIG")

    if alpaca_key and alpaca_secret:
        settings.ALPACA_API_KEY = alpaca_key
        settings.ALPACA_SECRET_KEY = alpaca_secret
        if alpaca_mode:
            await agent_manager.stock_agent.set_mode(alpaca_mode)
        agent_manager.stock_agent.log("Alpaca Zugangsdaten aktualisiert.", "INFO", "CONFIG")

    return {"status": "keys_updated"}


@app.post("/api/config/risk", dependencies=[Depends(require_auth)])
async def update_risk_config(payload: dict = Body(...)):
    """Aktualisiert Risikomanagement- und Exit-Regeln live zur Laufzeit."""
    risk_pct = payload.get("risk_per_trade_pct")
    max_dd = payload.get("max_daily_drawdown_pct")
    forex_max_pos = payload.get("max_open_positions")
    stock_max_pos = payload.get("stock_max_open_positions")
    sl_atr = payload.get("default_atr_multiplier_sl")
    tp_atr = payload.get("default_atr_multiplier_tp")
    rrr = payload.get("min_risk_reward_ratio")
    be_r = payload.get("breakeven_trigger_r")
    trailing_on = payload.get("trailing_stop_enabled")
    allow_ai_close = payload.get("allow_ai_close_signals")
    auto_liquidate = payload.get("auto_liquidate_on_drawdown")

    # Update Forex Risk Manager
    agent_manager.forex_agent.risk_manager.update_risk_parameters(
        risk_per_trade_pct=float(risk_pct) if risk_pct is not None else None,
        max_daily_drawdown_pct=float(max_dd) if max_dd is not None else None,
        max_open_positions=int(forex_max_pos) if forex_max_pos is not None else None,
        default_atr_multiplier_sl=float(sl_atr) if sl_atr is not None else None,
        default_atr_multiplier_tp=float(tp_atr) if tp_atr is not None else None,
        min_risk_reward_ratio=float(rrr) if rrr is not None else None,
        breakeven_trigger_r=float(be_r) if be_r is not None else None,
        trailing_stop_enabled=bool(trailing_on) if trailing_on is not None else None,
        allow_ai_close_signals=bool(allow_ai_close) if allow_ai_close is not None else None,
        auto_liquidate_on_drawdown=bool(auto_liquidate) if auto_liquidate is not None else None
    )

    # Update Stock Risk Manager
    agent_manager.stock_agent.risk_manager.update_risk_parameters(
        risk_per_trade_pct=float(risk_pct) if risk_pct is not None else None,
        max_daily_drawdown_pct=float(max_dd) if max_dd is not None else None,
        max_open_positions=int(stock_max_pos) if stock_max_pos is not None else None,
        default_atr_multiplier_sl=float(sl_atr) if sl_atr is not None else None,
        default_atr_multiplier_tp=float(tp_atr) if tp_atr is not None else None,
        min_risk_reward_ratio=float(rrr) if rrr is not None else None,
        breakeven_trigger_r=float(be_r) if be_r is not None else None,
        trailing_stop_enabled=bool(trailing_on) if trailing_on is not None else None,
        allow_ai_close_signals=bool(allow_ai_close) if allow_ai_close is not None else None,
        auto_liquidate_on_drawdown=bool(auto_liquidate) if auto_liquidate is not None else None
    )

    agent_manager.forex_agent.log("Risiko- und Exit-Parameter live aktualisiert.", "INFO", "CONFIG")
    agent_manager.stock_agent.log("Risiko- und Exit-Parameter live aktualisiert.", "INFO", "CONFIG")

    return {"status": "risk_config_updated"}


@app.post("/api/trades/clear", dependencies=[Depends(require_auth)])
async def clear_trade_history(payload: dict = Body(default={})):
    """Leert die Trade-Historie für einen oder beide Bots."""
    bot_type = payload.get("bot_type", "all")
    if bot_type in ("forex", "all"):
        agent_manager.forex_agent.memory.clear_memory()
        agent_manager.forex_agent.log("Trade-Historie (Forex) zurückgesetzt.", "INFO", "MEMORY")
    if bot_type in ("stock", "all"):
        agent_manager.stock_agent.memory.clear_memory()
        agent_manager.stock_agent.log("Trade-Historie (Aktien) zurückgesetzt.", "INFO", "MEMORY")
    
    return {"status": "history_cleared", "bot_type": bot_type}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: Optional[str] = Query(default=None)):
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
        # Sende initialen State für beide Bots
        all_tel = await agent_manager.get_all_telemetry()
        await websocket.send_text(json.dumps({
            "type": "INITIAL_STATE",
            "forex": all_tel["forex"],
            "stock": all_tel["stock"]
        }))

        # Initiale Kerzen für beide Bots senden
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
            msg = json.loads(text_data)
            action = msg.get("action")
            bot_type = msg.get("bot_type", "forex").lower()

            target_agent = agent_manager.stock_agent if bot_type == "stock" else agent_manager.forex_agent

            if action == "START":
                await agent_manager.start_bot(bot_type)
            elif action == "STOP":
                await agent_manager.stop_bot(bot_type)
            elif action == "SCAN":
                target_sym = target_agent.current_symbol if bot_type == "stock" else target_agent.current_instrument
                await target_agent.run_scan_cycle(target_sym)
                await target_agent.broadcast_telemetry()
            elif action == "SWITCH_INSTRUMENT":
                inst = msg.get("instrument")
                if bot_type == "stock":
                    # 1. Fetch & return candles immediately (<5ms)
                    candles = await agent_manager.stock_agent.broker.get_candles(inst, "M5", 100)
                    await websocket.send_text(json.dumps({
                        "type": "CANDLES",
                        "bot_type": "stock",
                        "instrument": inst,
                        "data": [c.model_dump() for c in candles]
                    }))
                    # 2. Trigger active symbol update & AI scan cycle in the background
                    asyncio.create_task(agent_manager.stock_agent.set_symbol(inst))
                else:
                    # 1. Fetch & return candles immediately (<5ms)
                    candles = await agent_manager.forex_agent.broker.get_candles(inst, "M5", 100)
                    await websocket.send_text(json.dumps({
                        "type": "CANDLES",
                        "bot_type": "forex",
                        "instrument": inst,
                        "data": [c.model_dump() for c in candles]
                    }))
                    # 2. Trigger active instrument update & AI scan cycle in the background
                    asyncio.create_task(agent_manager.forex_agent.set_instrument(inst))
            elif action == "REQUEST_CANDLES":
                req_bot = msg.get("bot_type", "forex")
                req_inst = msg.get("instrument")
                if req_bot == "stock":
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
                direction = msg.get("direction", "BUY")
                risk_pct = float(msg.get("risk_pct", 1.0))
                if bot_type == "stock":
                    await agent_manager.stock_agent.execute_manual_trade(agent_manager.stock_agent.current_symbol, direction, risk_pct)
                else:
                    await agent_manager.forex_agent.execute_manual_trade(agent_manager.forex_agent.current_instrument, direction, risk_pct)

    except WebSocketDisconnect:
        if websocket in active_websockets:
            active_websockets.remove(websocket)
    except Exception as e:
        logger.debug(f"WebSocket Verbindung beendet: {e}")
        if websocket in active_websockets:
            active_websockets.remove(websocket)

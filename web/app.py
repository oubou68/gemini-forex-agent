import asyncio
import json
import logging
from pathlib import Path
from typing import List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Body
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agent.core.multi_agent_manager import MultiAgentManager
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


@app.get("/", response_class=HTMLResponse)
async def get_index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return HTMLResponse("<h1>Gemini Dual Agent UI Loading...</h1>")


@app.get("/api/status")
async def get_status(bot_type: Optional[str] = None):
    if bot_type == "forex":
        tel = await agent_manager.forex_agent.get_telemetry()
        return tel.model_dump()
    elif bot_type == "stock":
        tel = await agent_manager.stock_agent.get_telemetry()
        return tel.model_dump()
    return await agent_manager.get_all_telemetry()


@app.get("/api/candles")
async def get_candles(bot_type: str = "forex", instrument: Optional[str] = None, count: int = 120):
    if bot_type == "stock":
        symbol = instrument or agent_manager.stock_agent.current_symbol
        candles = await agent_manager.stock_agent.broker.get_candles(symbol, granularity="M5", count=count)
    else:
        symbol = instrument or agent_manager.forex_agent.current_instrument
        candles = await agent_manager.forex_agent.broker.get_candles(symbol, granularity="M5", count=count)
    return [c.model_dump() for c in candles]


@app.post("/api/control/start")
async def start_agent(payload: dict = Body(default={})):
    bot = payload.get("bot_type", "all")
    await agent_manager.start_bot(bot)
    return {
        "status": "started",
        "forex_running": agent_manager.forex_agent.is_running,
        "stock_running": agent_manager.stock_agent.is_running
    }


@app.post("/api/control/stop")
async def stop_agent(payload: dict = Body(default={})):
    bot = payload.get("bot_type", "all")
    await agent_manager.stop_bot(bot)
    return {
        "status": "stopped",
        "forex_running": agent_manager.forex_agent.is_running,
        "stock_running": agent_manager.stock_agent.is_running
    }


@app.post("/api/control/instrument")
async def set_instrument(payload: dict = Body(...)):
    bot_type = payload.get("bot_type", "forex")
    inst = payload.get("instrument", "EUR_USD")
    if bot_type == "stock":
        await agent_manager.stock_agent.set_symbol(inst)
        return {"status": "updated", "bot_type": "stock", "current_symbol": agent_manager.stock_agent.current_symbol}
    else:
        await agent_manager.forex_agent.set_instrument(inst)
        return {"status": "updated", "bot_type": "forex", "current_instrument": agent_manager.forex_agent.current_instrument}


@app.post("/api/control/mode")
async def set_mode(payload: dict = Body(...)):
    bot_type = payload.get("bot_type", "forex")
    mode = payload.get("mode", "simulator")
    if bot_type == "stock":
        await agent_manager.stock_agent.set_mode(mode)
        return {"status": "updated", "bot_type": "stock", "mode": agent_manager.stock_agent.mode}
    else:
        await agent_manager.forex_agent.set_mode(mode)
        return {"status": "updated", "bot_type": "forex", "mode": agent_manager.forex_agent.mode}


@app.post("/api/control/emergency-close")
async def emergency_close(payload: dict = Body(default={})):
    bot_type = payload.get("bot_type")
    results = await agent_manager.emergency_close_all(bot_type)
    return {"status": "success", "closed_positions": results}


@app.post("/api/control/manual-order")
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


@app.post("/api/config/keys")
async def update_keys(payload: dict = Body(...)):
    gemini_key = payload.get("gemini_api_key")
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


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
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

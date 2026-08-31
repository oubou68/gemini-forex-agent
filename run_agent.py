import argparse
import uvicorn
import logging
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.settings import settings

def main():
    parser = argparse.ArgumentParser(description="Gemini Autonomous Forex Intraday Trading Agent")
    parser.add_argument("--host", default=settings.SERVER_HOST, help="Host address for web dashboard")
    parser.add_argument("--port", type=int, default=settings.SERVER_PORT, help="Port for web dashboard")
    parser.add_argument("--mode", default=None, choices=["simulator", "practice", "live"], help="Override broker mode")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload for development")

    args = parser.parse_args()

    if args.mode:
        settings.OANDA_ENVIRONMENT = args.mode

    print("=" * 75)
    print("  [>] GEMINI DUAL AUTONOMOUS TRADING PLATFORM (FOREX & STOCKS)")
    print("=" * 75)
    print(f"  * Forex Broker (OANDA):   {settings.OANDA_ENVIRONMENT.upper()} [{settings.DEFAULT_INSTRUMENT}]")
    print(f"  * Stock Broker (Alpaca):  {settings.ALPACA_ENVIRONMENT.upper()} [{settings.DEFAULT_STOCK_SYMBOL}]")
    print(f"  * Web-Dashboard:          http://localhost:{args.port}")
    print(f"  * Gemini AI Modell:       {settings.GEMINI_MODEL}")
    print("=" * 75)

    uvicorn.run(
        "web.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info"
    )

if __name__ == "__main__":
    main()

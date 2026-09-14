import argparse
import uvicorn
import logging
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import os
from config.settings import settings

def main():
    parser = argparse.ArgumentParser(description="Gemini Autonomous Forex Intraday Trading Agent")
    parser.add_argument("--host", default=settings.SERVER_HOST, help="Host address for web dashboard")
    default_port = int(os.environ.get("PORT", settings.SERVER_PORT))
    parser.add_argument("--port", type=int, default=default_port, help="Port for web dashboard")
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
    if settings.AUTH_ENABLED:
        print(f"  * Sicherheit & Auth:      AKTIV (Login unter http://localhost:{args.port}/login)")
        print(f"    - Standard-Benutzer:    {settings.AUTH_USERNAME}")
        print(f"    - Standard-Passwort:    {settings.AUTH_PASSWORD}")
        print(f"    - Google Ident:         {settings.GOOGLE_ALLOWED_EMAIL} (exklusiv)")
    else:
        print("  * Sicherheit & Auth:      DEAKTIVIERT")
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

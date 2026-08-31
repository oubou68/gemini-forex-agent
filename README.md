# 🤖 Gemini Dual Autonome Trading-Plattform (Forex, NASDAQ-100 & Dow Jones 30)

Eine vollautonome, KI-gestützte Multi-Agenten-Trading-Plattform für den Devisenmarkt (Forex) und den gesamten US-Aktienmarkt (**NASDAQ-100**, **Dow Jones Industrial Average 30** und **Index-ETFs**), entwickelt mit **Google Gemini**, **Alpaca Markets** und der **OANDA v20 REST & Streaming API**.

Das System kombiniert quantitative technische Analysen, Multi-Symbol Intraday Screener, VWAP-Benchmarks, Opening Range Breakouts (ORB), Marktstruktur-Erkennung (Smart Money Concepts / FVGs) und modernstes Prompting mit einem strikten Risikomanager und einem interaktiven Live-Web-Cockpit.

---

## ⚡ Die zwei autonomen Trading-Bots

### 1. 🪙 Forex Intraday Agent (OANDA v20 / Simulator)
- **Märkte:** Währungspaare (`EUR/USD`, `GBP/USD`, `USD/JPY`, `AUD/USD`, `EUR/JPY`).
- **Analyse-Fokus:** EMA-Fächer (9, 21, 50, 200), RSI (14), MACD, ATR-Volatilität, Fair Value Gaps (FVG), Support/Resistance Zonen.
- **Sessions:** London Session, New York Session, London/NY Overlap, Asian Session.
- **Risikomanagement:** Lot-Sizing in Abhängigkeit vom SL-Abstand in Pips, Pip-Spread-Filter, Break-Even Nachzug bei 1R.

### 2. 📈 Stock Intraday Agent (NASDAQ-100, Dow Jones 30 & Index-ETFs / Alpaca)
- **Märkte:** Vollständiges **NASDAQ-100** Universum (AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, AVGO, COST, AMD, PLTR...), **Dow Jones 30 Blue Chips** (UNH, GS, HD, CAT, MCD, CRM, V, BA, IBM, JNJ, DIS, JPM, WMT...), sowie **Index-ETFs** (`SPY`, `QQQ`, `DIA`, `IWM`).
- **Intraday Market Radar & Screener:** Kontinuierlicher Multi-Symbol Scan über 130+ Ticker mit Rangordnung nach RVOL (Relative Volume), ORB Breakouts, VWAP-Trend und RSI.
- **Live Ticker Search:** Sofortige Ticker-Suche & Autocomplete für beliebige US-Aktien im 0ms-Schnellwechsel.
- **Analyse-Fokus:** **VWAP** (Volume-Weighted Average Price & Standardabweichungsbänder), **Opening Range Breakout (ORB 15m)**, **RVOL** (Relative Volume), EMA-Momentum, RSI, MACD, ATR in US-Dollar.
- **Sessions:** US Pre-Market, US Opening Drive (RTH Open), US Mid-Day, US Power Hour, US After-Hours.
- **Risikomanagement:** Exaktes Position Sizing in Shares bezogen auf % Risiko des USD-Portfolios, Cent-Spread-Filter, Circuit Breaker.

---

## ✨ Features im Überblick

- **Broker-Integrationen**:
  - **Alpaca Markets v2**: Nahtlose Anbindung an **Alpaca Paper Trading** und **Alpaca Live Trading**.
  - **OANDA v20**: Anbindung an **OANDA Practice (Demo)** und **OANDA Live**.
  - **Integrierte High-Fidelity Simulator-Broker**: Sowohl für Forex (€) als auch US-Aktien ($) voll funktionsfähig ohne API-Keys!
- **Gemini AI Decision Engine**:
  - Generiert strukturierte JSON-Entscheidungen (`BUY`, `SELL`, `HOLD`, `CLOSE`).
  - Liefert Konfidenz-Score (0-100%), Trading-These, Invalidation-Level und dynamische Stop-Loss / Take-Profit Level (Mindest-RRR 1:1.5 bis 1:3).
  - Post-Trade-Lernfähigkeit & Reflektionsgedächtnis (Memory).
- **Kapitalschutz & Risikomanagement**:
  - Dynamisches Position Sizing (%-Risiko pro Trade bezogen auf Kontokapital und SL-Abstand).
  - Daily Drawdown Circuit Breaker (automatischer Handelsstopp bei >= 3% Tagesverlust).
  - Automatisches Stop-Loss Nachziehen auf Break-Even bei 1R Profit.
- **Modernes Live-Web-Cockpit**:
  - **Bot-Switching Navigation Tabs**: Nahtloses Umschalten zwischen Forex-Bot und Stock-Bot.
  - TradingView Lightweight Candlestick-Chart mit EMA- & **VWAP-Overlays**.
  - Live AI-Gedankenstrom ("AI Thought Box").
  - Interaktive Tabelle offener Positionen mit 1-Click Schließen & Notstopp.
  - Manuelle Quick-Trade Buttons (BUY/SELL) mit Risikoschieberegler.
  - Konfigurations-Modal für Gemini, OANDA und Alpaca API-Keys.

---

## 📂 Projektstruktur

```
gemini-forex-agent/
├── agent/
│   ├── ai/
│   │   ├── gemini_analyst.py       # Forex Gemini Engine & Strukturierte Entscheidungen
│   │   ├── stock_analyst.py        # Stock Gemini Engine (VWAP & Equities)
│   │   ├── prompts.py              # Forex Trading Prompts
│   │   ├── stock_prompts.py        # Stock & Wall Street Trading Prompts
│   │   └── memory.py               # Trade-Historie & Performance-Reflektion
│   ├── analysis/
│   │   ├── indicators.py           # Forex Indikatoren (EMA, RSI, MACD, ATR, BB)
│   │   ├── stock_indicators.py     # Stock Indikatoren (VWAP, ORB, RVOL, EMAs)
│   │   ├── market_structure.py     # S/R Zonen, Swing High/Low, FVGs
│   │   ├── session.py              # Forex Session Analyzer
│   │   └── stock_session.py        # US Stock Session Analyzer (Pre-Market, RTH, Power Hour)
│   ├── broker/
│   │   ├── base_broker.py          # Abstraktes Broker-Interface
│   │   ├── alpaca_client.py        # Alpaca REST v2 API Client (Stocks)
│   │   ├── stock_simulator.py      # High-Fidelity Stock Simulator (USD)
│   │   ├── oanda_client.py         # OANDA v20 REST Client (Forex)
│   │   └── simulator.py            # High-Fidelity Forex Simulator (EUR)
│   ├── core/
│   │   ├── models.py               # Pydantic Datenmodelle (Candle, VWAP, Position, etc.)
│   │   ├── orchestrator.py         # Forex Trading Agent Orchestrator
│   │   ├── stock_orchestrator.py   # Stock Trading Agent Orchestrator
│   │   └── multi_agent_manager.py  # Dual-Agent Koordinator & Dispatcher
│   └── risk/
│       └── risk_manager.py         # Lot-Size / Shares, Spread-Check, Drawdown-Schutz
├── config/
│   ├── default_config.yaml         # Standardparameter für Forex & Stocks
│   └── settings.py                 # Pydantic Settings & .env Loader
├── web/
│   ├── static/
│   │   ├── css/style.css           # Obsidian Glassmorphism Styling
│   │   ├── js/app.js               # Multi-Bot Client, VWAP-Rendering & Charts
│   │   └── index.html              # Multi-Agent Web-Cockpit Interface
│   └── app.py                      # FastAPI REST & WebSocket Server
├── tests/                          # Vollständige Pytest Test-Suite (14 Tests)
├── run_agent.py                    # Startskript
├── requirements.txt                # Python-Abhängigkeiten
└── .env.example                    # Konfigurationsvorlage
```

---

## 🚀 Schnellstart

### 1. Installation

```powershell
cd C:\Users\oubou\.gemini\antigravity-ide\scratch\gemini-forex-agent
& "C:\Users\oubou\AppData\Local\Programs\Python\Python312\python.exe" -m pip install -r requirements.txt
```

### 2. Konfiguration (`.env`)

Kopieren Sie die Vorlage und tragen Sie bei Bedarf Ihre Keys ein:
```powershell
cp .env.example .env
```

Beispiel `.env`:
```env
# Google Gemini
GEMINI_API_KEY=AIzaSy...
GEMINI_MODEL=gemini-2.5-flash

# OANDA Broker (Forex)
OANDA_API_KEY=your_oanda_v20_key
OANDA_ACCOUNT_ID=101-004-12345678-001
OANDA_ENVIRONMENT=simulator  # "simulator", "practice" oder "live"

# Alpaca Broker (Stocks)
ALPACA_API_KEY=your_alpaca_key_id
ALPACA_SECRET_KEY=your_alpaca_secret_key
ALPACA_ENVIRONMENT=simulator  # "simulator", "paper" oder "live"
```

> **Tipp:** Sie können die Plattform sofort im Modus `simulator` starten – beide Bots (Forex & Stocks) laufen out-of-the-box komplett offline und risikofrei!

### 3. Starten des Web-Cockpits

```powershell
& "C:\Users\oubou\AppData\Local\Programs\Python\Python312\python.exe" run_agent.py
```

Öffnen Sie anschließend im Browser:  
👉 **`http://localhost:8000`**

---

## 🧪 Tests ausführen

```powershell
& "C:\Users\oubou\AppData\Local\Programs\Python\Python312\python.exe" -m pytest tests/ -v
```

// Gemini Multi-Agent Intraday Trading Platform (Forex & Stocks) - Frontend Client

let socket = null;
let activeBot = "forex"; // "forex" | "stock"
let currentRenderedKey = ""; // Tracks currently rendered bot:instrument

const FOREX_INSTRUMENTS = ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "EUR_JPY"];
const STOCK_SYMBOLS = ["AAPL", "NVDA", "TSLA", "SPY", "QQQ", "MSFT", "AMD"];

// Agent States Cache
const botStates = {
  forex: {
    instrument: "EUR_USD",
    isRunning: true,
    mode: "simulator",
    telemetry: null
  },
  stock: {
    instrument: "AAPL",
    isRunning: true,
    mode: "simulator",
    telemetry: null
  }
};

// High-Performance In-Memory Chart Cache
// Key: `${botType}:${symbol}` -> { uniqueCandles, uniqueVolumes, ema9Data, ema21Data, ema50Data, vwapData }
const chartDataCache = new Map();

// Lightweight Charts Variables
let chart = null;
let candleSeries = null;
let volumeSeries = null;
let ema9Series = null;
let ema21Series = null;
let ema50Series = null;
let vwapSeries = null;
let chartResizeObserver = null;

// DOM Elements
const tabForex = document.getElementById("tabForex");
const tabStock = document.getElementById("tabStock");
const dotForexMini = document.getElementById("dotForexMini");
const dotStockMini = document.getElementById("dotStockMini");
const instrumentSelector = document.getElementById("instrumentSelector");

const equityTitle = document.getElementById("equityTitle");
const equityValue = document.getElementById("equityValue");
const balanceValue = document.getElementById("balanceValue");
const unrealizedPnlBadge = document.getElementById("unrealizedPnlBadge");
const winRateValue = document.getElementById("winRateValue");
const profitFactorValue = document.getElementById("profitFactorValue");
const totalTradesValue = document.getElementById("totalTradesValue");
const drawdownValue = document.getElementById("drawdownValue");
const riskStatusPill = document.getElementById("riskStatusPill");
const sessionTitle = document.getElementById("sessionTitle");
const sessionValue = document.getElementById("sessionValue");
const spreadContainer = document.getElementById("spreadContainer");
const currentRateBadge = document.getElementById("currentRateBadge");

// Chart & Header DOM
const chartPairTitle = document.getElementById("chartPairTitle");
const chartTfBadge = document.getElementById("chartTfBadge");
const chartBotTag = document.getElementById("chartBotTag");
const vwapLegendItem = document.getElementById("vwapLegendItem");

// Indicators DOM
const indMatrixTitle = document.getElementById("indMatrixTitle");
const trendBiasBadge = document.getElementById("trendBiasBadge");
const indRsi = document.getElementById("indRsi");
const rsiBar = document.getElementById("rsiBar");
const indMacd = document.getElementById("indMacd");
const indMacdHist = document.getElementById("indMacdHist");
const indAtr = document.getElementById("indAtr");
const indVolRegime = document.getElementById("indVolRegime");

const lblSlot4 = document.getElementById("lblSlot4");
const valSlot4 = document.getElementById("valSlot4");
const subSlot4 = document.getElementById("subSlot4");

const lblSlot5 = document.getElementById("lblSlot5");
const valSlot5 = document.getElementById("valSlot5");
const subSlot5 = document.getElementById("subSlot5");

const lblSlot6 = document.getElementById("lblSlot6");
const valSlot6 = document.getElementById("valSlot6");
const subSlot6 = document.getElementById("subSlot6");

// AI Brain DOM
const aiBrainTitle = document.getElementById("aiBrainTitle");
const aiConfidenceBadge = document.getElementById("aiConfidenceBadge");
const aiConfidenceValue = document.getElementById("aiConfidenceValue");
const aiSignalTag = document.getElementById("aiSignalTag");
const aiSignalText = document.getElementById("aiSignalText");
const aiSetupBadge = document.getElementById("aiSetupBadge");
const aiThesisText = document.getElementById("aiThesisText");
const aiReasoningDetails = document.getElementById("aiReasoningDetails");
const lvlEntry = document.getElementById("lvlEntry");
const lvlSl = document.getElementById("lvlSl");
const lvlTp1 = document.getElementById("lvlTp1");
const lvlRrr = document.getElementById("lvlRrr");

// Control & State DOM
const agentStatusDot = document.getElementById("agentStatusDot");
const agentStatusLabel = document.getElementById("agentStatusLabel");
const modeLabel = document.getElementById("modeLabel");
const btnToggleAgent = document.getElementById("btnToggleAgent");
const btnScanNow = document.getElementById("btnScanNow");
const btnEmergencyClose = document.getElementById("btnEmergencyClose");
const openPositionsCount = document.getElementById("openPositionsCount");
const positionsTableBody = document.getElementById("positionsTableBody");
const logStream = document.getElementById("logStream");
const logHeaderTitle = document.getElementById("logHeaderTitle");

// Manual Trade DOM
const manualRiskSlider = document.getElementById("manualRiskSlider");
const manualRiskLabel = document.getElementById("manualRiskLabel");
const btnManualBuy = document.getElementById("btnManualBuy");
const btnManualSell = document.getElementById("btnManualSell");

// Settings Modal DOM
const btnOpenSettings = document.getElementById("btnOpenSettings");
const btnCloseSettings = document.getElementById("btnCloseSettings");
const btnCancelSettings = document.getElementById("btnCancelSettings");
const btnSaveSettings = document.getElementById("btnSaveSettings");
const settingsModal = document.getElementById("settingsModal");
const inputGeminiKey = document.getElementById("inputGeminiKey");
const selectOandaMode = document.getElementById("selectOandaMode");
const inputOandaKey = document.getElementById("inputOandaKey");
const inputOandaAccount = document.getElementById("inputOandaAccount");
const selectAlpacaMode = document.getElementById("selectAlpacaMode");
const inputAlpacaKey = document.getElementById("inputAlpacaKey");
const inputAlpacaSecret = document.getElementById("inputAlpacaSecret");

// Initialize Lightweight Charts
function initChart() {
  const chartContainer = document.getElementById("tradingviewChart");
  if (!chartContainer) return;
  chartContainer.innerHTML = "";

  chart = LightweightCharts.createChart(chartContainer, {
    layout: {
      background: { color: "#161f30" },
      textColor: "#94a3b8",
      fontSize: 11,
      fontFamily: "'JetBrains Mono', monospace",
    },
    grid: {
      vertLines: { color: "rgba(255, 255, 255, 0.04)" },
      horzLines: { color: "rgba(255, 255, 255, 0.04)" },
    },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Normal,
    },
    rightPriceScale: {
      borderColor: "rgba(255, 255, 255, 0.08)",
      autoScale: true,
    },
    timeScale: {
      borderColor: "rgba(255, 255, 255, 0.08)",
      timeVisible: true,
      secondsVisible: false,
    },
  });

  candleSeries = chart.addCandlestickSeries({
    upColor: "#10b981",
    downColor: "#f43f5e",
    borderDownColor: "#f43f5e",
    borderUpColor: "#10b981",
    wickDownColor: "#f43f5e",
    wickUpColor: "#10b981",
  });

  volumeSeries = chart.addHistogramSeries({
    color: "rgba(59, 130, 246, 0.3)",
    priceFormat: { type: "volume" },
    priceScaleId: "",
  });
  volumeSeries.priceScale().applyOptions({
    scaleMargins: { top: 0.82, bottom: 0 },
  });

  // EMAs
  ema9Series = chart.addLineSeries({ color: "#3b82f6", lineWidth: 1.5, title: "EMA 9" });
  ema21Series = chart.addLineSeries({ color: "#f59e0b", lineWidth: 1.5, title: "EMA 21" });
  ema50Series = chart.addLineSeries({ color: "#8b5cf6", lineWidth: 1.5, title: "EMA 50" });

  // VWAP Series for Stocks
  vwapSeries = chart.addLineSeries({ color: "#00f2fe", lineWidth: 2, title: "VWAP" });

  // GPU & Container ResizeObserver for zero-lag responsiveness
  if (window.ResizeObserver) {
    chartResizeObserver = new ResizeObserver((entries) => {
      if (!entries || !entries.length || !chart) return;
      const entry = entries[0];
      const width = Math.floor(entry.contentRect.width);
      const height = Math.floor(entry.contentRect.height);
      if (width > 0 && height > 0) {
        chart.applyOptions({ width, height });
      }
    });
    chartResizeObserver.observe(chartContainer);
  } else {
    window.addEventListener("resize", () => {
      if (chart && chartContainer) {
        chart.applyOptions({ width: chartContainer.clientWidth, height: chartContainer.clientHeight });
      }
    });
  }
}

// Fast ISO Date to Unix Epoch Parser
function fastParseTimestamp(timeVal) {
  if (typeof timeVal === "number") return timeVal > 1e11 ? Math.floor(timeVal / 1000) : timeVal;
  if (!timeVal) return Math.floor(Date.now() / 1000);
  const ts = Date.parse(timeVal);
  return isNaN(ts) ? Math.floor(Date.now() / 1000) : Math.floor(ts / 1000);
}

// High-Performance Single-Pass Processor for Candles & Indicators
function processCandlesData(candles, botType) {
  if (!candles || candles.length === 0) return null;

  const n = candles.length;
  const candleData = new Array(n);
  const volumeData = new Array(n);

  for (let i = 0; i < n; i++) {
    const c = candles[i];
    const timestamp = fastParseTimestamp(c.time);
    const isOpenCloseUp = c.close >= c.open;
    candleData[i] = {
      time: timestamp,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    };
    volumeData[i] = {
      time: timestamp,
      value: c.volume || 10,
      color: isOpenCloseUp ? "rgba(16, 185, 129, 0.25)" : "rgba(244, 63, 94, 0.25)",
    };
  }

  // Deduplicate & sort if necessary
  const uniqueCandles = [];
  const uniqueVolumes = [];
  const seenTimes = new Set();

  for (let i = 0; i < candleData.length; i++) {
    const t = candleData[i].time;
    if (!seenTimes.has(t)) {
      seenTimes.add(t);
      uniqueCandles.push(candleData[i]);
      uniqueVolumes.push(volumeData[i]);
    }
  }

  uniqueCandles.sort((a, b) => a.time - b.time);
  uniqueVolumes.sort((a, b) => a.time - b.time);

  const count = uniqueCandles.length;
  if (count === 0) return null;

  // Single-pass indicator calculation for EMA 9, 21, 50 & VWAP
  const ema9Data = [];
  const ema21Data = [];
  const ema50Data = [];
  const vwapData = [];

  const k9 = 2 / 10;
  const k21 = 2 / 22;
  const k50 = 2 / 51;

  let ema9 = uniqueCandles[0].close;
  let ema21 = uniqueCandles[0].close;
  let ema50 = uniqueCandles[0].close;

  let cumVol = 0;
  let cumTpVol = 0;
  const isStock = botType === "stock";

  for (let i = 0; i < count; i++) {
    const c = uniqueCandles[i];
    const price = c.close;
    const time = c.time;

    if (i === 0) {
      ema9 = price;
      ema21 = price;
      ema50 = price;
    } else {
      ema9 = price * k9 + ema9 * (1 - k9);
      ema21 = price * k21 + ema21 * (1 - k21);
      ema50 = price * k50 + ema50 * (1 - k50);
    }

    if (i >= 8) ema9Data.push({ time, value: parseFloat(ema9.toFixed(isStock ? 2 : 5)) });
    if (i >= 20) ema21Data.push({ time, value: parseFloat(ema21.toFixed(isStock ? 2 : 5)) });
    if (i >= 49) ema50Data.push({ time, value: parseFloat(ema50.toFixed(isStock ? 2 : 5)) });

    if (isStock) {
      const vol = uniqueVolumes[i] ? uniqueVolumes[i].value : 1;
      const tp = (c.high + c.low + c.close) / 3;
      cumVol += vol;
      cumTpVol += (tp * vol);
      const vwapVal = cumTpVol / (cumVol + 1e-9);
      vwapData.push({ time, value: parseFloat(vwapVal.toFixed(2)) });
    }
  }

  return {
    uniqueCandles,
    uniqueVolumes,
    ema9Data,
    ema21Data,
    ema50Data,
    vwapData,
    botType
  };
}

// Render processed chart data to canvas in a single frame
function renderProcessedChart(processed, fitView = false) {
  if (!chart || !candleSeries || !processed) return;

  requestAnimationFrame(() => {
    candleSeries.setData(processed.uniqueCandles);
    volumeSeries.setData(processed.uniqueVolumes);
    ema9Series.setData(processed.ema9Data);
    ema21Series.setData(processed.ema21Data);
    ema50Series.setData(processed.ema50Data);

    if (processed.botType === "stock") {
      vwapLegendItem.style.display = "inline-flex";
      vwapSeries.setData(processed.vwapData);
    } else {
      vwapLegendItem.style.display = "none";
      vwapSeries.setData([]);
    }

    if (fitView) {
      chart.timeScale().fitContent();
    }
  });
}

// Convert Candles, Store in Cache and Render
function setChartData(candles, botType = activeBot, instrument = null, fitView = false) {
  if (!candles || candles.length === 0) return;

  const currentInst = instrument || botStates[botType].instrument;
  const cacheKey = `${botType}:${currentInst}`;

  const processed = processCandlesData(candles, botType);
  if (!processed) return;

  chartDataCache.set(cacheKey, processed);

  // If this matches the currently active bot & instrument, render to screen
  if (botType === activeBot && currentInst === botStates[activeBot].instrument) {
    const isNewSymbol = currentRenderedKey !== cacheKey;
    currentRenderedKey = cacheKey;
    renderProcessedChart(processed, fitView || isNewSymbol);
  }
}

// Render Dynamic Instrument Pills with Smart DOM Caching
function renderInstrumentPills() {
  const list = activeBot === "stock" ? STOCK_SYMBOLS : FOREX_INSTRUMENTS;
  const current = botStates[activeBot].instrument;

  instrumentSelector.innerHTML = "";
  const fragment = document.createDocumentFragment();

  list.forEach((inst) => {
    const btn = document.createElement("button");
    const isActive = inst === current;
    btn.className = `pill ${isActive ? "active" : ""}`;
    btn.textContent = activeBot === "stock" ? `$${inst}` : inst.replace("_", "/");
    btn.dataset.symbol = inst;
    btn.addEventListener("click", () => switchInstrument(inst));
    fragment.appendChild(btn);
  });

  instrumentSelector.appendChild(fragment);
}

// Instant Instrument Switcher with 0ms Perceived Latency
function switchInstrument(symbol) {
  const prevSymbol = botStates[activeBot].instrument;
  if (prevSymbol === symbol && currentRenderedKey === `${activeBot}:${symbol}`) return;

  botStates[activeBot].instrument = symbol;
  const isStock = activeBot === "stock";

  // 1. Instant Tactile Feedback: Update Pills
  const pills = instrumentSelector.querySelectorAll(".pill");
  pills.forEach((p) => {
    if (p.dataset.symbol === symbol) {
      p.classList.add("active");
    } else {
      p.classList.remove("active");
    }
  });

  // 2. Instant Header & Tag Update
  chartPairTitle.textContent = `${isStock ? "$" + symbol : symbol.replace("_", "/")} • M5 Kerzen`;
  btnManualBuy.querySelector(".active-instrument-label").textContent = `${isStock ? "$" + symbol : symbol.replace("_", "/")} (${isStock ? "Shares" : "Units"})`;
  btnManualSell.querySelector(".active-instrument-label").textContent = `${isStock ? "$" + symbol : symbol.replace("_", "/")} (${isStock ? "Shares" : "Units"})`;

  // 3. Instant Render from Cache (0ms latency)
  const cacheKey = `${activeBot}:${symbol}`;
  if (chartDataCache.has(cacheKey)) {
    currentRenderedKey = cacheKey;
    renderProcessedChart(chartDataCache.get(cacheKey), true);
  }

  // 4. Silently send WebSocket command in background for fresh live data
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({
      action: "SWITCH_INSTRUMENT",
      bot_type: activeBot,
      instrument: symbol
    }));
  }
}

// Bot Switcher Tab Event with Instant Cache Render
function switchBotTab(targetBot) {
  if (activeBot === targetBot) return;
  activeBot = targetBot;

  if (activeBot === "forex") {
    tabForex.classList.add("active");
    tabStock.classList.remove("active");
    chartBotTag.textContent = "FOREX";
    chartBotTag.style.background = "rgba(59, 130, 246, 0.2)";
    chartBotTag.style.color = "var(--accent-blue)";
    chartBotTag.style.borderColor = "rgba(59, 130, 246, 0.4)";
    indMatrixTitle.textContent = "Quant & Forex Indikatoren";
    aiBrainTitle.textContent = "Gemini Forex Decision Engine";
    logHeaderTitle.textContent = "Forex Agent Audit & Activity Log";
  } else {
    tabStock.classList.add("active");
    tabForex.classList.remove("active");
    chartBotTag.textContent = "ALPACAS STOCKS";
    chartBotTag.style.background = "rgba(16, 185, 129, 0.2)";
    chartBotTag.style.color = "var(--accent-emerald)";
    chartBotTag.style.borderColor = "rgba(16, 185, 129, 0.4)";
    indMatrixTitle.textContent = "VWAP, ORB & Equities Matrix";
    aiBrainTitle.textContent = "Gemini Stock Decision Engine (US Equities)";
    logHeaderTitle.textContent = "Alpaca Stock Agent Audit Log";
  }

  renderInstrumentPills();

  const currentInst = botStates[activeBot].instrument;
  const cacheKey = `${activeBot}:${currentInst}`;

  // Instant render from cache
  if (chartDataCache.has(cacheKey)) {
    currentRenderedKey = cacheKey;
    renderProcessedChart(chartDataCache.get(cacheKey), true);
  }

  // Update UI with stored telemetry
  if (botStates[activeBot].telemetry) {
    updateTelemetryUI(botStates[activeBot].telemetry, activeBot);
  }

  // Request latest candles for active bot & symbol in background
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({
      action: "REQUEST_CANDLES",
      bot_type: activeBot,
      instrument: currentInst
    }));
  }
}

// WebSocket Connection
function connectWebSocket() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${protocol}//${window.location.host}/ws`;

  socket = new WebSocket(wsUrl);

  socket.onopen = () => {
    console.log("WebSocket verbunden.");
  };

  socket.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      if (msg.type === "INITIAL_STATE") {
        if (msg.forex) {
          botStates.forex.telemetry = msg.forex;
          botStates.forex.instrument = msg.forex.current_instrument || "EUR_USD";
          botStates.forex.isRunning = msg.forex.is_running;
          botStates.forex.mode = msg.forex.mode;
        }
        if (msg.stock) {
          botStates.stock.telemetry = msg.stock;
          botStates.stock.instrument = msg.stock.current_instrument || "AAPL";
          botStates.stock.isRunning = msg.stock.is_running;
          botStates.stock.mode = msg.stock.mode;
        }
        updateMiniIndicators();
        renderInstrumentPills();
        if (botStates[activeBot].telemetry) {
          updateTelemetryUI(botStates[activeBot].telemetry, activeBot);
        }
      } else if (msg.type === "TELEMETRY") {
        const botType = msg.bot_type || "forex";
        botStates[botType].telemetry = msg.data;
        botStates[botType].isRunning = msg.data.is_running;
        botStates[botType].mode = msg.data.mode;
        if (msg.data.current_instrument) {
          botStates[botType].instrument = msg.data.current_instrument;
        }
        updateMiniIndicators();

        if (botType === activeBot) {
          updateTelemetryUI(msg.data, botType);
        }
      } else if (msg.type === "CANDLES") {
        const botType = msg.bot_type || "forex";
        const inst = msg.instrument || botStates[botType].instrument;
        setChartData(msg.data, botType, inst, false);
      }
    } catch (e) {
      console.error("Fehler beim Verarbeiten der WebSocket Nachricht:", e);
    }
  };

  socket.onclose = () => {
    console.log("WebSocket getrennt. Reconnecting in 3s...");
    setTimeout(connectWebSocket, 3000);
  };
}

function updateMiniIndicators() {
  if (dotForexMini) {
    dotForexMini.className = `bot-status-indicator ${botStates.forex.isRunning ? "active" : "paused"}`;
  }
  if (dotStockMini) {
    dotStockMini.className = `bot-status-indicator ${botStates.stock.isRunning ? "active" : "paused"}`;
  }
}

// Update UI Components with Telemetry Data
function updateTelemetryUI(data, botType) {
  if (!data) return;

  const isStock = botType === "stock";
  const currencySymbol = isStock ? "$" : "€";

  // 1. Account & Status
  const acc = data.account || {};
  equityTitle.textContent = isStock ? "KONTO-EQUITY (USD)" : "KONTO-EQUITY (EUR)";
  equityValue.textContent = isStock ? `$${formatCurrency(acc.equity || 100000)}` : `${formatCurrency(acc.equity || 10000)} €`;
  balanceValue.textContent = isStock ? `$${formatCurrency(acc.balance || 100000)}` : `${formatCurrency(acc.balance || 10000)} €`;

  const unPnl = acc.unrealized_pl || 0.0;
  unrealizedPnlBadge.textContent = `${unPnl >= 0 ? "+" : ""}${isStock ? "$" : ""}${formatCurrency(unPnl)}${isStock ? "" : " €"}`;
  unrealizedPnlBadge.className = `pnl-badge ${unPnl >= 0 ? "positive" : "negative"}`;

  const stats = data.stats || {};
  winRateValue.textContent = `${stats.win_rate_pct || 0}%`;
  profitFactorValue.textContent = stats.profit_factor || "0.0";
  totalTradesValue.textContent = stats.total_trades || 0;

  drawdownValue.textContent = `${acc.daily_drawdown_pct || 0.0}%`;
  if ((acc.daily_drawdown_pct || 0) >= 3.0) {
    riskStatusPill.textContent = "STOPP";
    riskStatusPill.className = "status-pill negative";
  } else {
    riskStatusPill.textContent = "SICHER";
    riskStatusPill.className = "status-pill safe";
  }

  // 2. Active Agent Status
  const isRunning = data.is_running;
  if (isRunning) {
    agentStatusDot.className = "status-dot pulse";
    agentStatusLabel.textContent = `${botType.toUpperCase()} AKTIV`;
    btnToggleAgent.innerHTML = '<i class="fa-solid fa-pause"></i> <span>Pausieren</span>';
    btnToggleAgent.className = "btn btn-primary";
  } else {
    agentStatusDot.className = "status-dot paused";
    agentStatusLabel.textContent = `${botType.toUpperCase()} PAUSIERT`;
    btnToggleAgent.innerHTML = '<i class="fa-solid fa-play"></i> <span>Starten</span>';
    btnToggleAgent.className = "btn btn-secondary";
  }

  modeLabel.textContent = `${isStock ? "ALPACA" : "OANDA"} ${(data.mode || "SIMULATOR").toUpperCase()}`;

  // 3. Market & Session
  const price = data.market_price || {};
  const currentInst = data.current_instrument || (isStock ? "AAPL" : "EUR_USD");
  chartPairTitle.textContent = `${isStock ? "$" + currentInst : currentInst.replace("_", "/")} • M5 Kerzen`;

  if (price.mid) {
    currentRateBadge.textContent = price.mid.toFixed(isStock ? 2 : (currentInst.includes("JPY") ? 3 : 5));
  }

  if (isStock) {
    spreadContainer.innerHTML = `Spread: <strong id="spreadValue">$${(price.spread_pips || 0.02).toFixed(2)}</strong>`;
    sessionTitle.textContent = "US HANDELSSITZUNG";
  } else {
    spreadContainer.innerHTML = `Spread: <strong id="spreadValue">${price.spread_pips || "--"} Pips</strong>`;
    sessionTitle.textContent = "FOREX SESSION";
  }

  const struct = data.market_structure || {};
  sessionValue.textContent = struct.active_session || (isStock ? "US_RTH" : "GLOBAL");

  // 4. Indicators & Dynamic Slots
  const ind = data.indicators || {};
  if (ind.rsi_14 !== undefined && ind.rsi_14 !== null) {
    indRsi.textContent = ind.rsi_14.toFixed(1);
    rsiBar.style.width = `${Math.min(100, Math.max(0, ind.rsi_14))}%`;
  }
  if (ind.macd !== undefined) {
    indMacd.textContent = ind.macd.toFixed(isStock ? 2 : 5);
    indMacdHist.textContent = `Hist: ${(ind.macd_hist || 0).toFixed(isStock ? 2 : 5)}`;
  }
  if (ind.atr_14 !== undefined) {
    indAtr.textContent = isStock ? `$${ind.atr_14.toFixed(2)}` : ind.atr_14.toFixed(5);
    indVolRegime.textContent = `Regime: ${ind.volatility_regime || "NORMAL"}`;
  }

  if (isStock) {
    // Slot 4: VWAP
    lblSlot4.textContent = "VWAP (Intraday Benchmark)";
    valSlot4.textContent = ind.vwap ? `$${ind.vwap.toFixed(2)}` : "--";
    subSlot4.textContent = `Bänder: $${(ind.vwap_upper || 0).toFixed(2)} / $${(ind.vwap_lower || 0).toFixed(2)}`;

    // Slot 5: Opening Range Breakout (ORB)
    lblSlot5.textContent = "Opening Range (ORB 15m)";
    valSlot5.textContent = ind.orb_high ? `High: $${ind.orb_high.toFixed(2)}` : "--";
    subSlot5.textContent = ind.orb_low ? `Low: $${ind.orb_low.toFixed(2)}` : "--";

    // Slot 6: RVOL (Relative Volume)
    lblSlot6.textContent = "Relative Volume (RVOL)";
    valSlot6.textContent = ind.rvol ? `${ind.rvol}x` : "1.0x";
    subSlot6.textContent = struct.orb_bias || "INSIDE_RANGE";
  } else {
    // Forex Standard
    lblSlot4.textContent = "Bollinger Bänder (20,2)";
    valSlot4.textContent = `U: ${(ind.bb_upper || 0).toFixed(5)}`;
    subSlot4.textContent = `L: ${(ind.bb_lower || 0).toFixed(5)}`;

    lblSlot5.textContent = "Support / Resistance";
    valSlot5.textContent = `Res: ${(struct.nearest_resistance || 0).toFixed(5)}`;
    subSlot5.textContent = `Sup: ${(struct.nearest_support || 0).toFixed(5)}`;

    lblSlot6.textContent = "Fair Value Gap (FVG)";
    valSlot6.textContent = struct.fvg_bullish ? "Bullish FVG" : (struct.fvg_bearish ? "Bearish FVG" : "Kein Gap");
    subSlot6.textContent = `Swings: ${(struct.swing_high || 0).toFixed(4)} / ${(struct.swing_low || 0).toFixed(4)}`;
  }

  trendBiasBadge.textContent = ind.trend_bias || "NEUTRAL";
  trendBiasBadge.className = `badge ${ind.trend_bias === "BULLISH" ? "positive" : (ind.trend_bias === "BEARISH" ? "negative" : "")}`;

  // 5. Gemini AI Decision
  const dec = data.last_decision;
  if (dec) {
    aiConfidenceValue.textContent = `${Math.round(dec.confidence || 50)}%`;
    aiSignalText.textContent = `SIGNAL: ${dec.action}`;
    aiSignalTag.className = `signal-tag ${dec.action.toLowerCase()}`;
    aiSetupBadge.textContent = `Setup: ${dec.setup_type || "NONE"}`;
    aiThesisText.textContent = dec.thesis_summary || "Warten auf nächste Marktanalyse...";
    aiReasoningDetails.innerHTML = dec.reasoning ? dec.reasoning.replace(/;/g, "<br>• ") : "Keine Detail-Konfluenzen.";

    lvlEntry.textContent = dec.entry_price ? (isStock ? `$${dec.entry_price.toFixed(2)}` : dec.entry_price.toFixed(5)) : "--";
    lvlSl.textContent = dec.stop_loss ? (isStock ? `$${dec.stop_loss.toFixed(2)}` : dec.stop_loss.toFixed(5)) : "--";
    lvlTp1.textContent = dec.take_profit_1 ? (isStock ? `$${dec.take_profit_1.toFixed(2)}` : dec.take_profit_1.toFixed(5)) : "--";
    lvlRrr.textContent = dec.risk_reward_ratio ? `1 : ${dec.risk_reward_ratio}` : "--";
  }

  // 6. Positions Table
  const openPos = data.open_positions || [];
  openPositionsCount.textContent = openPos.length;
  renderPositionsTable(openPos, isStock);

  // 7. Manual Quick Trade Buttons
  const unitLabel = isStock ? "Shares" : "Units";
  btnManualBuy.innerHTML = `<i class="fa-solid fa-arrow-up"></i> BUY <span class="active-instrument-label">${isStock ? "$" + currentInst : currentInst.replace("_", "/")} (${unitLabel})</span>`;
  btnManualSell.innerHTML = `<i class="fa-solid fa-arrow-down"></i> SELL <span class="active-instrument-label">${isStock ? "$" + currentInst : currentInst.replace("_", "/")} (${unitLabel})</span>`;

  // 8. Logs
  if (data.recent_logs && data.recent_logs.length > 0) {
    renderLogs(data.recent_logs);
  }
}

function renderPositionsTable(positions, isStock) {
  positionsTableBody.innerHTML = "";
  if (!positions || positions.length === 0) {
    positionsTableBody.innerHTML = '<tr><td colspan="8" class="empty-state">Keine offenen Positionen aktiv.</td></tr>';
    return;
  }

  const fragment = document.createDocumentFragment();
  positions.forEach((p) => {
    const tr = document.createElement("tr");
    const pnl = p.unrealized_pnl || 0.0;
    const pnlClass = pnl >= 0 ? "positive" : "negative";
    const prefix = pnl >= 0 ? "+" : "";
    const pnlFormatted = isStock ? `${prefix}$${formatCurrency(pnl)}` : `${prefix}${formatCurrency(pnl)} €`;
    const slTp = `${p.stop_loss ? (isStock ? "$" + p.stop_loss.toFixed(2) : p.stop_loss.toFixed(5)) : "--"} / ${p.take_profit ? (isStock ? "$" + p.take_profit.toFixed(2) : p.take_profit.toFixed(5)) : "--"}`;

    tr.innerHTML = `
      <td class="font-mono">${p.id.substring(0, 8)}</td>
      <td><strong>${isStock ? "$" + p.instrument : p.instrument.replace("_", "/")}</strong></td>
      <td><span class="type-badge ${p.direction.toLowerCase()}">${p.direction}</span></td>
      <td>${p.units} ${isStock ? "Shares" : "Units"}</td>
      <td class="font-mono">${isStock ? "$" + p.entry_price.toFixed(2) : p.entry_price.toFixed(5)}</td>
      <td class="font-mono font-xs">${slTp}</td>
      <td class="font-mono ${pnlClass}"><strong>${pnlFormatted}</strong></td>
      <td><button class="btn btn-close-pos" data-id="${p.id}"><i class="fa-solid fa-xmark"></i> Schließen</button></td>
    `;
    fragment.appendChild(tr);
  });
  positionsTableBody.appendChild(fragment);

  // Attach close buttons
  document.querySelectorAll(".btn-close-pos").forEach((btn) => {
    btn.addEventListener("click", () => closeSinglePosition(btn.dataset.id));
  });
}

function renderLogs(logs) {
  logStream.innerHTML = "";
  const fragment = document.createDocumentFragment();
  logs.forEach((l) => {
    const div = document.createElement("div");
    div.className = `log-entry ${l.category || "SYSTEM"}`;
    div.innerHTML = `<span class="log-time">${l.timestamp || "--:--:--"}</span> [${l.category || "SYSTEM"}] ${l.message}`;
    fragment.appendChild(div);
  });
  logStream.appendChild(fragment);
  logStream.scrollTop = logStream.scrollHeight;
}

function formatCurrency(num) {
  return (num || 0).toLocaleString("de-DE", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// Actions & Event Handlers
async function toggleAgentState() {
  const currentRunning = botStates[activeBot].isRunning;
  const endpoint = currentRunning ? "/api/control/stop" : "/api/control/start";
  try {
    await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bot_type: activeBot })
    });
  } catch (e) {
    console.error("Fehler beim Umschalten des Agentenstatus:", e);
  }
}

async function triggerScanNow() {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ action: "SCAN", bot_type: activeBot }));
  }
}

async function triggerEmergencyClose() {
  if (confirm(`Möchtest du wirklich alle offenen Positionen für ${activeBot.toUpperCase()} sofort schließen?`)) {
    try {
      await fetch("/api/control/emergency-close", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ bot_type: activeBot })
      });
    } catch (e) {
      console.error("Fehler beim Notstopp:", e);
    }
  }
}

async function closeSinglePosition(posId) {
  try {
    await fetch("/api/control/emergency-close", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bot_type: activeBot })
    });
  } catch (e) {
    console.error("Fehler beim Schließen der Position:", e);
  }
}

async function executeManualOrder(direction) {
  const riskPct = parseFloat(manualRiskSlider.value);
  try {
    await fetch("/api/control/manual-order", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        bot_type: activeBot,
        instrument: botStates[activeBot].instrument,
        direction: direction,
        risk_pct: riskPct
      })
    });
  } catch (e) {
    console.error("Fehler bei manueller Order:", e);
  }
}

// Modal Tabs Switcher
document.querySelectorAll(".modal-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".modal-tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".modal-tab-content").forEach((c) => c.classList.remove("active"));
    tab.classList.add("active");
    const targetId = tab.dataset.target;
    const targetContent = document.getElementById(targetId);
    if (targetContent) targetContent.classList.add("active");
  });
});

// Save Settings Event
async function saveSettings() {
  const payload = {
    gemini_api_key: inputGeminiKey.value.trim(),
    oanda_api_key: inputOandaKey.value.trim(),
    oanda_account_id: inputOandaAccount.value.trim(),
    oanda_mode: selectOandaMode.value,
    alpaca_api_key: inputAlpacaKey.value.trim(),
    alpaca_secret_key: inputAlpacaSecret.value.trim(),
    alpaca_mode: selectAlpacaMode.value
  };

  try {
    const res = await fetch("/api/config/keys", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    if (res.ok) {
      settingsModal.classList.remove("active");
      alert("Einstellungen erfolgreich gespeichert und angewendet!");
    }
  } catch (e) {
    console.error("Fehler beim Speichern der Einstellungen:", e);
  }
}

// Event Listeners Setup
document.addEventListener("DOMContentLoaded", () => {
  initChart();
  renderInstrumentPills();
  connectWebSocket();

  tabForex.addEventListener("click", () => switchBotTab("forex"));
  tabStock.addEventListener("click", () => switchBotTab("stock"));

  btnToggleAgent.addEventListener("click", toggleAgentState);
  btnScanNow.addEventListener("click", triggerScanNow);
  btnEmergencyClose.addEventListener("click", triggerEmergencyClose);

  manualRiskSlider.addEventListener("input", (e) => {
    manualRiskLabel.textContent = `${parseFloat(e.target.value).toFixed(1)}%`;
  });

  btnManualBuy.addEventListener("click", () => executeManualOrder("BUY"));
  btnManualSell.addEventListener("click", () => executeManualOrder("SELL"));

  btnOpenSettings.addEventListener("click", () => settingsModal.classList.add("active"));
  btnCloseSettings.addEventListener("click", () => settingsModal.classList.remove("active"));
  btnCancelSettings.addEventListener("click", () => settingsModal.classList.remove("active"));
  btnSaveSettings.addEventListener("click", saveSettings);
});

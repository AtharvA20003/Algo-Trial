import pandas as pd
import os
import logging
import requests
import time
import json
from datetime import datetime

# ===== FILES =====
STATE_FILE = "paper_trading_state.json"
LOG_FILE   = "paper_trading_logC.txt"

# ===== CONFIG =====
TRAILING_PCT  = 0.005    # 0.5%
STOP_LOSS_PCT = 0.002    # 0.2%
FEE_PCT       = 0.0004   # 0.04% per side (entry + exit)
START_BALANCE = 50.0

# ===== STRATEGIES =====
strategies = [
    {"symbol": "BTCUSDT", "interval": "15m", "rsi": False},
    {"symbol": "BTCUSDT", "interval": "1h",  "rsi": True },
    {"symbol": "ETHUSDT", "interval": "30m", "rsi": True },
    {"symbol": "ETHUSDT", "interval": "1h",  "rsi": False},
]

# ===== LOGGING SETUP =====
if not os.path.exists(LOG_FILE):
    open(LOG_FILE, "w").close()

logger = logging.getLogger()
logger.setLevel(logging.INFO)
if logger.hasHandlers():
    logger.handlers.clear()

file_handler = logging.FileHandler(LOG_FILE)
file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))
logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))
logger.addHandler(console_handler)


# ===== STATE HELPERS =====
def strategy_key(config):
    """Unique key per strategy config."""
    tag = "rsi" if config["rsi"] else "norsi"
    return f"{config['symbol']}_{config['interval']}_{tag}"


def load_state():
    """Load all strategy states from disk, or create fresh ones."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_strategy_state(state, key):
    """Return existing state for a strategy, or a fresh default."""
    if key not in state:
        state[key] = {
            "balance":          START_BALANCE,
            "position":         None,          # "LONG" | "SHORT" | null
            "entry_price":      0.0,
            "trailing_stop":    None,
            "trades":           0,
            "wins":             0,
            "losses":           0,
            "max_profit_long":  None,           # best single LONG trade net $
            "max_loss_long":    None,           # worst single LONG trade net $
            "max_profit_short": None,
            "max_loss_short":   None,
            "start_balance":    START_BALANCE,  # fixed reference for total ROI
        }
    return state[key]


# ===== DATA =====
def get_data(symbol="BTCUSDT", interval="15m", limit=100):
    url = "https://data-api.binance.vision/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}

    try:
        response = requests.get(url, params=params, timeout=10)
        data = response.json()

        if not isinstance(data, list):
            logger.error(f"API ERROR for {symbol}: {data}")
            return pd.DataFrame()

        if len(data) == 0:
            logger.warning(f"EMPTY DATA for {symbol}")
            return pd.DataFrame()

        df = pd.DataFrame(data, columns=[
            "time","open","high","low","close","volume",
            "close_time","qav","trades","tbbav","tbqav","ignore"
        ])
        df["close"] = df["close"].astype(float)
        df["high"]  = df["high"].astype(float)
        df["low"]   = df["low"].astype(float)

        return df

    except Exception as e:
        logger.error(f"REQUEST FAILED for {symbol}: {e}")
        return pd.DataFrame()


# ===== RSI =====
def calculate_rsi(df, period=14):
    delta = df["close"].diff()
    gain  = delta.where(delta > 0, 0).rolling(period).mean()
    loss  = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs    = gain / loss
    return 100 - (100 / (1 + rs))


# ===== SIGNALS =====
def generate_signal(df):
    current_price = df["close"].iloc[-1]
    ma20          = df["close"].rolling(20).mean().iloc[-1]
    trend         = "UP" if current_price > ma20 else "DOWN"
    momentum      = current_price - df["close"].iloc[-10]
    resistance    = df["high"].rolling(20).max().iloc[-2]
    support       = df["low"].rolling(20).min().iloc[-2]

    if current_price > resistance:
        raw = "BUY"
    elif current_price < support:
        raw = "SELL"
    else:
        raw = "HOLD"

    if raw == "BUY"  and momentum > 0 and trend == "UP":   return "BUY"
    if raw == "SELL" and momentum < 0 and trend == "DOWN": return "SELL"
    return "HOLD"


def generate_signal_with_rsi(df):
    current_price = df["close"].iloc[-1]
    ma20          = df["close"].rolling(20).mean().iloc[-1]
    trend         = "UP" if current_price > ma20 else "DOWN"
    momentum      = current_price - df["close"].iloc[-10]
    rsi           = calculate_rsi(df).iloc[-1]
    resistance    = df["high"].rolling(20).max().iloc[-2]
    support       = df["low"].rolling(20).min().iloc[-2]

    if current_price > resistance:
        raw = "BUY"
    elif current_price < support:
        raw = "SELL"
    else:
        raw = "HOLD"

    if raw == "BUY"  and momentum > 0 and trend == "UP"   and rsi < 70: return "BUY"
    if raw == "SELL" and momentum < 0 and trend == "DOWN" and rsi > 30: return "SELL"
    return "HOLD"


# ===== TRADE LOGIC =====
def update_max(current_val, new_val, mode):
    """
    mode='max' → keep highest value (best profit)
    mode='min' → keep lowest value  (worst loss)
    Returns None if new_val is None, otherwise updates correctly.
    """
    if current_val is None:
        return new_val
    return max(current_val, new_val) if mode == "max" else min(current_val, new_val)


def process_tick(st, signal, current_price):
    """
    Apply one price tick + signal to a strategy state dict (in-place).
    Returns a short action string for logging.
    """
    action = "HOLD"

    # ---- ENTRY ----
    if st["position"] is None:
        if signal == "BUY":
            st["position"]      = "LONG"
            st["entry_price"]   = current_price
            st["trailing_stop"] = current_price * (1 - TRAILING_PCT)
            st["trades"]       += 1
            action = f"ENTER LONG  @ {current_price:.4f}"

        elif signal == "SELL":
            st["position"]      = "SHORT"
            st["entry_price"]   = current_price
            st["trailing_stop"] = current_price * (1 + TRAILING_PCT)
            st["trades"]       += 1
            action = f"ENTER SHORT @ {current_price:.4f}"

    # ---- MANAGE LONG ----
    elif st["position"] == "LONG":
        # Raise trailing stop if price moved up
        new_trail = current_price * (1 - TRAILING_PCT)
        if new_trail > st["trailing_stop"]:
            st["trailing_stop"] = new_trail

        hit_trail     = current_price <= st["trailing_stop"]
        hit_stoploss  = current_price <= st["entry_price"] * (1 - STOP_LOSS_PCT)

        if hit_trail or hit_stoploss:
            profit_pct  = (current_price - st["entry_price"]) / st["entry_price"]
            gross       = st["balance"] * profit_pct
            fee         = st["balance"] * FEE_PCT * 2
            net         = gross - fee

            st["balance"] += net
            st["wins"]    += 1 if net > 0 else 0
            st["losses"]  += 1 if net <= 0 else 0

            st["max_profit_long"] = update_max(st["max_profit_long"], net, "max")
            st["max_loss_long"]   = update_max(st["max_loss_long"],   net, "min")

            reason = "trailing-stop" if hit_trail else "stop-loss"
            action = (
                f"EXIT LONG  @ {current_price:.4f} | "
                f"net={net:+.4f}$ ({reason}) | "
                f"balance={st['balance']:.4f}$"
            )
            st["position"]      = None
            st["trailing_stop"] = None
            st["entry_price"]   = 0.0
        else:
            action = f"HOLD LONG  @ {current_price:.4f} | trail={st['trailing_stop']:.4f}"

    # ---- MANAGE SHORT ----
    elif st["position"] == "SHORT":
        # Lower trailing stop if price moved down
        new_trail = current_price * (1 + TRAILING_PCT)
        if new_trail < st["trailing_stop"]:
            st["trailing_stop"] = new_trail

        hit_trail    = current_price >= st["trailing_stop"]
        hit_stoploss = current_price >= st["entry_price"] * (1 + STOP_LOSS_PCT)

        if hit_trail or hit_stoploss:
            profit_pct  = (st["entry_price"] - current_price) / st["entry_price"]
            gross       = st["balance"] * profit_pct
            fee         = st["balance"] * FEE_PCT * 2
            net         = gross - fee

            st["balance"] += net
            st["wins"]    += 1 if net > 0 else 0
            st["losses"]  += 1 if net <= 0 else 0

            st["max_profit_short"] = update_max(st["max_profit_short"], net, "max")
            st["max_loss_short"]   = update_max(st["max_loss_short"],   net, "min")

            reason = "trailing-stop" if hit_trail else "stop-loss"
            action = (
                f"EXIT SHORT @ {current_price:.4f} | "
                f"net={net:+.4f}$ ({reason}) | "
                f"balance={st['balance']:.4f}$"
            )
            st["position"]      = None
            st["trailing_stop"] = None
            st["entry_price"]   = 0.0
        else:
            action = f"HOLD SHORT @ {current_price:.4f} | trail={st['trailing_stop']:.4f}"

    return action


# ===== SUMMARY LOGGING =====
def log_summary(state):
    logger.info("=" * 60)
    logger.info("  PAPER TRADING SUMMARY — %s", datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
    logger.info("=" * 60)

    total_start = 0.0
    total_end   = 0.0

    for config in strategies:
        key = strategy_key(config)
        if key not in state:
            continue

        st  = state[key]
        pnl = st["balance"] - st["start_balance"]
        roi = (pnl / st["start_balance"]) * 100

        total_start += st["start_balance"]
        total_end   += st["balance"]

        win_rate = (st["wins"] / st["trades"] * 100) if st["trades"] > 0 else 0.0

        logger.info(
            "[%s %s | rsi=%-5s]  bal=$%.4f | P&L=%+.4f$ (%+.2f%%) | "
            "trades=%d  W=%d  L=%d  WR=%.1f%%",
            config["symbol"], config["interval"],
            str(config["rsi"]),
            st["balance"], pnl, roi,
            st["trades"], st["wins"], st["losses"], win_rate,
        )

        # Best / worst trade breakdown
        if st["max_profit_long"] is not None or st["max_loss_long"] is not None:
            logger.info(
                "  └─ LONG   best=%s  worst=%s",
                f"${st['max_profit_long']:+.4f}" if st["max_profit_long"] is not None else "n/a",
                f"${st['max_loss_long']:+.4f}"   if st["max_loss_long"]   is not None else "n/a",
            )
        if st["max_profit_short"] is not None or st["max_loss_short"] is not None:
            logger.info(
                "  └─ SHORT  best=%s  worst=%s",
                f"${st['max_profit_short']:+.4f}" if st["max_profit_short"] is not None else "n/a",
                f"${st['max_loss_short']:+.4f}"   if st["max_loss_short"]   is not None else "n/a",
            )

    total_pnl = total_end - total_start
    total_roi = (total_pnl / total_start * 100) if total_start > 0 else 0.0
    logger.info("-" * 60)
    logger.info(
        "  COMBINED  start=$%.2f  now=$%.4f  P&L=%+.4f$ (%+.2f%%)",
        total_start, total_end, total_pnl, total_roi,
    )
    logger.info("=" * 60)


# ===== MAIN LOOP =====
def run_bot(state):
    for config in strategies:
        key = strategy_key(config)
        st  = get_strategy_state(state, key)

        df = get_data(symbol=config["symbol"], interval=config["interval"], limit=100)

        if df.empty or len(df) < 25:
            logger.warning("Skipping %s %s — insufficient data", config["symbol"], config["interval"])
            continue

        signal = generate_signal_with_rsi(df) if config["rsi"] else generate_signal(df)

        # Use the last *closed* candle price for execution realism
        current_price = df["close"].iloc[-2]

        action = process_tick(st, signal, current_price)

        logger.info(
            "%s | %s | rsi=%-5s | sig=%-4s | %s",
            config["symbol"], config["interval"], str(config["rsi"]), signal, action,
        )


if __name__ == "__main__":
    CYCLES      = 2       # how many cycles per GitHub Actions run
    SLEEP_SEC   = 900     # 15 minutes between cycles

    state = load_state()

    for cycle in range(1, CYCLES + 1):
        logger.info("--- Cycle %d/%d ---", cycle, CYCLES)
        run_bot(state)
        save_state(state)           # persist after every cycle
        log_summary(state)          # print P&L snapshot

        if cycle < CYCLES:
            time.sleep(SLEEP_SEC)
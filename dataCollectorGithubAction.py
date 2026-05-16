import pandas as pd
import requests
import json
import os
import time
from datetime import datetime

# ===== CONFIG =====
STATE_FILE = "state.json"
LOG_FILE = "trade_log.txt"

START_BALANCE = 50

TRAILING_PCT = 0.005
STOP_LOSS_PCT = 0.002
FEE_PCT = 0.0004

CYCLES = 2
SLEEP_SEC = 900

strategies = [
    {"symbol": "BTCUSDT", "interval": "15m", "rsi": False},
    {"symbol": "BTCUSDT", "interval": "1h",  "rsi": True},
    {"symbol": "ETHUSDT", "interval": "1h",  "rsi": False},
]

# ===== STATE =====
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def get_key(cfg):
    return f"{cfg['symbol']}_{cfg['interval']}_{cfg['rsi']}"

def get_state(state, key):
    if key not in state:
        state[key] = {
            "balance": START_BALANCE,
            "position": None,
            "entry_price": 0,
            "trailing_stop": None,
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "max_profit_long": None,
            "max_loss_long": None,
            "max_profit_short": None,
            "max_loss_short": None,
        }
    return state[key]

# ===== LOG =====
def log(msg):
    text = f"{datetime.utcnow()} | {msg}"
    print(text)

    with open(LOG_FILE, "a") as f:
        f.write(text + "\n")

# ===== DATA =====
def get_data(symbol, interval, limit=100):
    url = "https://data-api.binance.vision/api/v3/klines"

    r = requests.get(url, params={
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    })

    data = r.json()

    df = pd.DataFrame(data, columns=[
        "time","open","high","low","close","volume",
        "close_time","qav","trades","tbbav","tbqav","ignore"
    ])

    df["close"] = df["close"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)

    return df

# ===== RSI =====
def calculate_rsi(df, period=14):
    delta = df["close"].diff()

    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()

    rs = gain / loss

    return 100 - (100 / (1 + rs))

# ===== SIGNAL =====
def generate_signal(df, use_rsi=False):
    current = df["close"].iloc[-1]

    ma20 = df["close"].rolling(20).mean().iloc[-1]
    trend = "UP" if current > ma20 else "DOWN"

    momentum = current - df["close"].iloc[-10]

    resistance = df["high"].rolling(20).max().iloc[-2]
    support = df["low"].rolling(20).min().iloc[-2]

    signal = "HOLD"

    if current > resistance:
        signal = "BUY"

    elif current < support:
        signal = "SELL"

    if use_rsi:
        rsi = calculate_rsi(df).iloc[-1]

        if signal == "BUY" and momentum > 0 and trend == "UP" and rsi < 70:
            return "BUY"

        elif signal == "SELL" and momentum < 0 and trend == "DOWN" and rsi > 30:
            return "SELL"

        return "HOLD"

    else:
        if signal == "BUY" and momentum > 0 and trend == "UP":
            return "BUY"

        elif signal == "SELL" and momentum < 0 and trend == "DOWN":
            return "SELL"

        return "HOLD"

# ===== TRADE ENGINE =====
def process_trade(st, signal, price):

    # ===== ENTRY =====
    if st["position"] is None:

        if signal == "BUY":
            st["position"] = "LONG"
            st["entry_price"] = price
            st["trailing_stop"] = price * (1 - TRAILING_PCT)
            st["trades"] += 1

            return f"ENTER LONG @ {price:.2f}"

        elif signal == "SELL":
            st["position"] = "SHORT"
            st["entry_price"] = price
            st["trailing_stop"] = price * (1 + TRAILING_PCT)
            st["trades"] += 1

            return f"ENTER SHORT @ {price:.2f}"

    # ===== LONG =====
    elif st["position"] == "LONG":

        new_trail = price * (1 - TRAILING_PCT)

        if new_trail > st["trailing_stop"]:
            st["trailing_stop"] = new_trail

        exit_trade = (
            price <= st["trailing_stop"] or
            price <= st["entry_price"] * (1 - STOP_LOSS_PCT)
        )

        if exit_trade:

            profit_pct = (price - st["entry_price"]) / st["entry_price"]

            gross = st["balance"] * profit_pct
            fee = st["balance"] * FEE_PCT * 2

            net = gross - fee

            st["balance"] += net

            if net > 0:
                st["wins"] += 1
            else:
                st["losses"] += 1

            st["max_profit_long"] = (
                net if st["max_profit_long"] is None
                else max(st["max_profit_long"], net)
            )

            st["max_loss_long"] = (
                net if st["max_loss_long"] is None
                else min(st["max_loss_long"], net)
            )

            st["position"] = None

            return f"EXIT LONG @ {price:.2f} | net={net:.2f}"

    # ===== SHORT =====
    elif st["position"] == "SHORT":

        new_trail = price * (1 + TRAILING_PCT)

        if new_trail < st["trailing_stop"]:
            st["trailing_stop"] = new_trail

        exit_trade = (
            price >= st["trailing_stop"] or
            price >= st["entry_price"] * (1 + STOP_LOSS_PCT)
        )

        if exit_trade:

            profit_pct = (st["entry_price"] - price) / st["entry_price"]

            gross = st["balance"] * profit_pct
            fee = st["balance"] * FEE_PCT * 2

            net = gross - fee

            st["balance"] += net

            if net > 0:
                st["wins"] += 1
            else:
                st["losses"] += 1

            st["max_profit_short"] = (
                net if st["max_profit_short"] is None
                else max(st["max_profit_short"], net)
            )

            st["max_loss_short"] = (
                net if st["max_loss_short"] is None
                else min(st["max_loss_short"], net)
            )

            st["position"] = None

            return f"EXIT SHORT @ {price:.2f} | net={net:.2f}"

    return "HOLD"

# ===== SUMMARY =====
def print_summary(state):

    log("=" * 50)

    for key, st in state.items():

        wr = 0

        if st["trades"] > 0:
            wr = (st["wins"] / st["trades"]) * 100

        pnl = st["balance"] - START_BALANCE

        log(
            f"{key} | "
            f"bal={st['balance']:.2f} | "
            f"PnL={pnl:.2f} | "
            f"trades={st['trades']} | "
            f"WR={wr:.2f}%"
        )

    log("=" * 50)

# ===== MAIN =====
def run_bot(state):

    for cfg in strategies:

        key = get_key(cfg)

        st = get_state(state, key)

        df = get_data(cfg["symbol"], cfg["interval"])

        signal = generate_signal(df, cfg["rsi"])

        price = df["close"].iloc[-2]

        action = process_trade(st, signal, price)

        log(
            f"{cfg['symbol']} | "
            f"{cfg['interval']} | "
            f"RSI={cfg['rsi']} | "
            f"Signal={signal} | "
            f"{action}"
        )

if __name__ == "__main__":

    state = load_state()

    for cycle in range(CYCLES):

        run_bot(state)

        save_state(state)

        print_summary(state)

        if cycle < CYCLES - 1:
            time.sleep(SLEEP_SEC)
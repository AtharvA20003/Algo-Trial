from binance.client import Client
import pandas as pd
import os

API_KEY = os.environ.get("API_KEY")
API_SECRET = os.environ.get("API_SECRET")

client = Client(API_KEY, API_SECRET)

import logging
filename = "paper_trading_logs.txt"
logging.basicConfig(filename=filename,
                    format='%(asctime)s %(levelname)s: %(message)s',
                    filemode='a')
logger = logging.getLogger()
logger.setLevel(logging.INFO)

strategies = [
    {"symbol": "BTCUSDT", "interval": "15m", "rsi": False},
    {"symbol": "BTCUSDT", "interval": "1h", "rsi": True},
    {"symbol": "ETHUSDT", "interval": "30m", "rsi": True},
    {"symbol": "ETHUSDT", "interval": "1h", "rsi": False},
]

def get_data(symbol="XRPUSDT", interval="15m", limit=100):
    klines = client.get_klines(symbol = symbol, interval = interval, limit = limit)
    df = pd.DataFrame(klines, columns = [
        "time","open","high", "low","close","volume",
        "close_time","qav","trades","tbbav","tbqav","ignore"
    ])
    df["close"] = df["close"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)

    return df

def generate_signal(df):
    signal = "HOLD"
    # current price
    current_price = df["close"].iloc[-1]
    # MA20
    ma20 = df["close"].rolling(window=20).mean().iloc[-1]
    trend = "UP" if current_price > ma20 else "DOWN"

    momentum = current_price - df["close"].iloc[-10]

    # BREAKOUT LEVELS
    resistence = df["high"].rolling(window=20).max().iloc[-2]
    support = df["low"].rolling(window=20).min().iloc[-2]

    # RAW SIGNAL
    if current_price > resistence:
        signal = "BUY"
    elif current_price < support:
        signal = "SELL"
    
    # FINAL SIGNAL
    if signal == "BUY" and momentum > 0 and trend == "UP":
        return "BUY"
    elif signal == "SELL" and momentum < 0 and trend == "DOWN":
        return "SELL"
    else:
        return "HOLD"

def generate_signal_with_RSI(df):
    signal = "HOLD"

    current_price = df["close"].iloc[-1]

    # MA20 (trend)
    ma20 = df["close"].rolling(window=20).mean().iloc[-1]
    trend = "UP" if current_price > ma20 else "DOWN"

    # Momentum
    momentum = current_price - df["close"].iloc[-10]

    # RSI
    rsi = calculate_rsi(df).iloc[-1]

    # Breakout
    resistance = df["high"].rolling(window=20).max().iloc[-2]
    support = df["low"].rolling(window=20).min().iloc[-2]

    if current_price > resistance:
        signal = "BUY"
    elif current_price < support:
        signal = "SELL"

    # FINAL FILTER (RSI added)
    if signal == "BUY" and momentum > 0 and trend == "UP" and rsi < 70:
        return "BUY"
    elif signal == "SELL" and momentum < 0 and trend == "DOWN" and rsi > 30:
        return "SELL"
    else:
        return "HOLD"
    
def calculate_rsi(df, period=14):
    delta = df["close"].diff()

    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()

    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))

    return rsi


def run_bot():
    for config in strategies:
        df = get_data(symbol=config["symbol"], interval=config["interval"], limit=100)
        signal = ''
        if config["rsi"] == True:
            signal = generate_signal_with_RSI(df)
        else:
            signal = generate_signal(df)

        current_price = df["close"].iloc[-2]  # closed candle

        print(f"{config['symbol']} | {config['interval']} | {signal} | Price: {current_price}")
        logger.info(f"${config['symbol']} | ${config['interval']} | ${signal} | Price : ${current_price}")

if __name__ == "__main__":
    run_bot()
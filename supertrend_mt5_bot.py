# supertrend_mt5_bot.py
import time
import logging
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import MetaTrader5 as mt5
import telegram

# ----------------- CONFIG -----------------
ACCOUNT = 257188317            # <- change to your MT5 account number
PASSWORD = "Viplove1234@"
SERVER = "Exness-MT5Real36"   # <- change to your server name (Exness MT5 server)
SYMBOL = "EURUSD"             # <- Exness symbol for crypto, change as required
TIMEFRAME = mt5.TIMEFRAME_M5  # mt5.TIMEFRAME_M1 / M5 / M15 / H1 etc.
LOT = 0.01                    # example lot (very small). change per instrument and risk.
ATR_PERIOD = 10
SUPERTREND_MULTIPLIER = 3.0
SL_BUFFER_POINTS = 0          # extra buffer for SL in symbol points
TP_MULTIPLIER = 1.5           # target = SL * TP_MULTIPLIER
CHECK_INTERVAL = 10           # seconds between checks (>= candle timeframe recommended)

# Telegram alerts (optional)
TELEGRAM_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID = "YOUR_CHAT_ID"

# Safety limits
MAX_DAILY_LOSS = 100.0        # currency units. Bot will stop trading if daily loss exceeds this
MAX_TRADES_PER_DAY = 5

LOG_FILE = "supertrend_bot.log"
# ------------------------------------------

# setup logging
logging.basicConfig(filename=LOG_FILE,
                    level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')

def telegram_send(text):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
            bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
        except Exception as e:
            logging.exception("Telegram send failed: %s", e)

# Initialize MT5
def mt5_init():
    if not mt5.initialize():
        logging.error("MT5 initialize() failed, error code = %s", mt5.last_error())
        raise RuntimeError("MT5 initialize failed")
    # login
    logged = mt5.login(ACCOUNT, password=PASSWORD, server=SERVER)
    if not logged:
        logging.error("MT5 login failed: %s", mt5.last_error())
        raise RuntimeError("MT5 login failed")
    logging.info("MT5 initialized and logged in: account %s", ACCOUNT)
    telegram_send(f"MT5 connected: account {ACCOUNT}")

# fetch candles as pandas DataFrame
def fetch_ohlcv(symbol, timeframe, n=500):
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, n)
    if rates is None or len(rates) == 0:
        raise RuntimeError("No rates returned from MT5 for symbol " + symbol)
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df.set_index('time', inplace=True)
    return df

# ATR
def atr(df, n=14):
    high = df['high']
    low = df['low']
    close = df['close']
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    atr = tr.rolling(n, min_periods=1).mean()
    return atr

# Supertrend calculation
def compute_supertrend(df, period=10, multiplier=3.0):
    # df must have columns: high, low, close
    atr_series = atr(df, period)
    hl2 = (df['high'] + df['low']) / 2
    basic_upperband = hl2 + multiplier * atr_series
    basic_lowerband = hl2 - multiplier * atr_series

    final_upperband = basic_upperband.copy()
    final_lowerband = basic_lowerband.copy()

    for i in range(1, len(df)):
        if (basic_upperband.iloc[i] < final_upperband.iloc[i-1]) or (df['close'].iloc[i-1] > final_upperband.iloc[i-1]):
            final_upperband.iloc[i] = basic_upperband.iloc[i]
        else:
            final_upperband.iloc[i] = final_upperband.iloc[i-1]

        if (basic_lowerband.iloc[i] > final_lowerband.iloc[i-1]) or (df['close'].iloc[i-1] < final_lowerband.iloc[i-1]):
            final_lowerband.iloc[i] = basic_lowerband.iloc[i]
        else:
            final_lowerband.iloc[i] = final_lowerband.iloc[i-1]

    supertrend = pd.Series(index=df.index, dtype='float64')
    trend = pd.Series(index=df.index, dtype='int')  # +1 -> up, -1 -> down

    # initial
    supertrend.iloc[0] = final_upperband.iloc[0]
    trend.iloc[0] = -1  # default down

    for i in range(1, len(df)):
        price = df['close'].iloc[i]
        if trend.iloc[i-1] == -1:
            if price > final_upperband.iloc[i]:
                trend.iloc[i] = 1
            else:
                trend.iloc[i] = -1
        else:
            if price < final_lowerband.iloc[i]:
                trend.iloc[i] = -1
            else:
                trend.iloc[i] = 1

        if trend.iloc[i] == 1:
            supertrend.iloc[i] = final_lowerband.iloc[i]
        else:
            supertrend.iloc[i] = final_upperband.iloc[i]

    df['supertrend'] = supertrend
    df['st_trend'] = trend
    df['atr'] = atr_series
    return df

# Place market order helper
def place_market_order(symbol, volume, order_type, sl_price=None, tp_price=None):
    # order_type: mt5.ORDER_TYPE_BUY or mt5.ORDER_TYPE_SELL
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        raise RuntimeError("Symbol not found in MT5: " + symbol)
    if not symbol_info.visible:
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError("Failed to select symbol " + symbol)

    point = symbol_info.point
    deviation = 20  # slippage in points
    price = mt5.symbol_info_tick(symbol).ask if order_type == mt5.ORDER_TYPE_BUY else mt5.symbol_info_tick(symbol).bid

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "price": price,
        "deviation": deviation,
        "magic": 234000,          # your EA magic number
        "comment": "SupertrendBot",
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    # set SL/TP if provided (in absolute price)
    if sl_price is not None:
        request["sl"] = sl_price
    if tp_price is not None:
        request["tp"] = tp_price

    result = mt5.order_send(request)
    if result is None:
        logging.error("order_send returned None")
        return None
    logging.info("Order send result: %s", result)
    return result

# Helper to compute SL/TP prices given side and atr/sl multiplier
def compute_sl_tp(last_close, side, atr_value, multiplier=SUPERTREND_MULTIPLIER):
    # For buy: SL = last_close - atr*multiplier, TP = last_close + (last_close - SL)*TP_MULTIPLIER
    if side == 'buy':
        sl = last_close - atr_value * multiplier
        tp = last_close + (last_close - sl) * TP_MULTIPLIER
    else:
        sl = last_close + atr_value * multiplier
        tp = last_close - (sl - last_close) * TP_MULTIPLIER
    return sl, tp

# Track daily stats
class DailyStats:
    def __init__(self):
        self.reset()

    def reset(self):
        self.trades = 0
        self.pnl = 0.0
        self.date = datetime.now().date()

    def update_trade(self, profit):
        today = datetime.now().date()
        if today != self.date:
            self.reset()
        self.trades += 1
        self.pnl += profit

daily_stats = DailyStats()

# Main loop
def run():
    mt5_init()
    telegram_send("Supertrend bot started for symbol: " + SYMBOL)
    last_signal = None
    while True:
        try:
            # reset daily stats at midnight (or new day)
            if daily_stats.date != datetime.now().date():
                daily_stats.reset()

            # safety checks
            if daily_stats.pnl <= -abs(MAX_DAILY_LOSS):
                msg = f"Daily loss limit reached: {daily_stats.pnl}. Stopping trading for today."
                logging.warning(msg)
                telegram_send(msg)
                time.sleep(60 * 60)  # sleep 1 hour before checking again
                continue

            if daily_stats.trades >= MAX_TRADES_PER_DAY:
                logging.info("Max trades per day reached: %s", daily_stats.trades)
                time.sleep(60 * 60)  # sleep 1 hour
                continue

            df = fetch_ohlcv(SYMBOL, TIMEFRAME, n=500)
            df = compute_supertrend(df, period=ATR_PERIOD, multiplier=SUPERTREND_MULTIPLIER)

            # consider latest completed candle (last row)
            latest = df.iloc[-1]
            prev = df.iloc[-2]

            # signal logic: trend change from prev to latest
            # trend: +1 up, -1 down
            if (prev['st_trend'] == -1) and (latest['st_trend'] == 1):
                signal = 'buy'
            elif (prev['st_trend'] == 1) and (latest['st_trend'] == -1):
                signal = 'sell'
            else:
                signal = None

            # Avoid duplicate signals on same candle
            if signal and signal != last_signal:
                last_signal = signal
                # compute SL/TP using ATR (use last close)
                atr_val = latest['atr']
                last_close = latest['close']
                sl_price, tp_price = compute_sl_tp(last_close, signal, atr_val, multiplier=1.0)  # use multiplier 1 here
                # check price formatting for symbol decimals
                symbol_info = mt5.symbol_info(SYMBOL)
                digits = symbol_info.digits if symbol_info else 5
                sl_price = round(sl_price, digits)
                tp_price = round(tp_price, digits)
                volume = LOT

                order_type = mt5.ORDER_TYPE_BUY if signal == 'buy' else mt5.ORDER_TYPE_SELL
                result = place_market_order(SYMBOL, volume, order_type, sl_price=sl_price, tp_price=tp_price)
                if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                    msg = f"Placed {signal.upper()} order: {SYMBOL} vol={volume} sl={sl_price} tp={tp_price}"
                    logging.info(msg)
                    telegram_send(msg)
                    # Note: we don't instantly know PnL until closed. For simplicity, we don't track each trade PnL here.
                    daily_stats.trades += 1
                else:
                    logging.error("Order failed or returned with retcode: %s", getattr(result, 'retcode', None))
                    telegram_send(f"Order failed: {getattr(result, 'retcode', None)}")

            # Sleep until near next candle. Simple approach: sleep fixed seconds.
            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            logging.exception("Error in main loop: %s", e)
            telegram_send("Bot error: " + str(e))
            time.sleep(5)

if __name__ == "__main__":
    run()


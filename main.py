import os
import time
import requests
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# ==============================================================================
# CONFIGURATION TELEGRAM & ENVIRONNEMENT
# ==============================================================================
TELEGRAM_BOT_TOKEN = "8900570872:AAGHVeWBDobqPqJ4D_b74npZ_I89uMY5-_A"
TELEGRAM_CHAT_ID = "6365221307"

sent_signals = set()
MAX_SENT_SIGNALS = 3000

def send_telegram_signal(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'HTML'}
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            print("Message Telegram envoyé avec succès !")
    except Exception as e:
        print(f"Erreur d'envoi Telegram : {e}")

def format_price(symbol, price):
    """Formatage dynamique des prix selon la classe d'actif."""
    if price is None:
        return "0.00"
    if any(k in symbol for k in ["BTC", "ETH", "BITCOIN", "ETHEREUM", "NASDAQ", "US30", "SP500", "GOLD", "SILVER"]):
        return f"{price:.2f}"
    return f"{price:.5f}"

# ==============================================================================
# PANIER D'ACTIFS — TICKERS STABLES YAHOO FINANCE (2-2-2-2)
# ==============================================================================
SYMBOLS = {
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X",
    "GOLD": "GC=F", "SILVER": "SI=F",
    "NASDAQ": "^IXIC", "US30": "^DJI",
    "BITCOIN": "BTC-USD", "ETHEREUM": "ETH-USD"
}

SMT_PARTNER = {
    "EURUSD": "GBPUSD", "GBPUSD": "EURUSD",
    "GOLD": "SILVER", "SILVER": "GOLD",
    "NASDAQ": "US30", "US30": "NASDAQ",
    "BITCOIN": "ETHEREUM", "ETHEREUM": "BITCOIN"
}

GRANULARITIES = {
    "D1": {"period": "60d", "interval": "1d"},
    "H1": {"period": "60d", "interval": "1h"},
    "M15": {"period": "7d", "interval": "15m"},
    "M5": {"period": "5d", "interval": "5m"},
    "M1": {"period": "2d", "interval": "1m"}
}

def calculate_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def fetch_multi_tf_data(ticker):
    dfs = {}
    for tf_name, params in GRANULARITIES.items():
        try:
            data = yf.download(tickers=ticker, period=params["period"], interval=params["interval"], progress=False)
            if not data.empty and len(data) >= 15:
                if isinstance(data.columns, pd.MultiIndex):
                    data.columns = [col[0] for col in data.columns]
                data.columns = [c.lower() for c in data.columns]
                dfs[tf_name] = data
            else:
                dfs[tf_name] = pd.DataFrame()
        except Exception:
            dfs[tf_name] = pd.DataFrame()

    h1 = dfs.get("H1", pd.DataFrame())
    if not h1.empty:
        agg_dict = {"open": "first", "high": "max", "low": "min", "close": "last"}
        if "volume" in h1.columns:
            agg_dict["volume"] = "sum"
        h4 = h1.resample("4h").agg(agg_dict).dropna()
        dfs["H4"] = h4 if len(h4) >= 15 else pd.DataFrame()
    else:
        dfs["H4"] = pd.DataFrame()

    return dfs

# ==============================================================================
# ÉTAT PERSISTANT AMD
# ==============================================================================
asia_ranges = {}          
london_manipulation = {}  

def day_key(symbol, dt_utc):
    return f"{symbol}_{dt_utc.strftime('%Y-%m-%d')}"

def cleanup_amd_state(dt_utc):
    cutoff = (dt_utc - timedelta(days=2)).strftime('%Y-%m-%d')
    for store in (asia_ranges, london_manipulation):
        for k in list(store.keys()):
            if k.split('_')[-1] < cutoff:
                del store[k]

def get_amd_window(now_utc):
    utc_h = now_utc.hour + now_utc.minute / 60
    london_local = now_utc.astimezone(ZoneInfo("Europe/London"))
    ny_local = now_utc.astimezone(ZoneInfo("America/New_York"))
    london_h = london_local.hour + london_local.minute / 60
    ny_h = ny_local.hour + ny_local.minute / 60

    if 0.0 <= utc_h < 8.0:
        return "ASIA"
    elif 7.0 <= london_h < 10.0:
        return "LONDON"
    elif 7.5 <= ny_h < 10.5:
        return "NY"
    return None

def is_generic_cascade_window(symbol, now_utc):
    if any(k in symbol for k in ["BTC", "ETH", "BITCOIN", "ETHEREUM"]):
        return True

    utc_h = now_utc.hour + now_utc.minute / 60
    london_local = now_utc.astimezone(ZoneInfo("Europe/London"))
    ny_local = now_utc.astimezone(ZoneInfo("America/New_York"))
    london_h = london_local.hour + london_local.minute / 60
    ny_h = ny_local.hour + ny_local.minute / 60

    if 7.0 <= london_h < 10.0:
        return True
    elif 7.5 <= ny_h < 10.5:
        return True
    elif 10.5 <= ny_h < 12.5: 
        return True
    elif 0.0 <= utc_h < 5.0 and any(k in symbol for k in ["JPY", "AUD", "NZD"]):
        return True
    return False

def update_asia_range(symbol, df_m15, dt_utc):
    if df_m15 is None or df_m15.empty:
        return
    try:
        idx_utc = df_m15.index.tz_convert('UTC') if df_m15.index.tz is not None else df_m15.index.tz_localize('UTC')
    except Exception:
        idx_utc = df_m15.index

    today_str = dt_utc.strftime('%Y-%m-%d')
    mask_today = idx_utc.strftime('%Y-%m-%d') == today_str
    mask_hours = (idx_utc.hour >= 0) & (idx_utc.hour < 8)
    asia_candles = df_m15[mask_today & mask_hours]
    if asia_candles.empty:
        return

    key = day_key(symbol, dt_utc)
    high = float(asia_candles['high'].max())
    low = float(asia_candles['low'].min())
    existing = asia_ranges.get(key)
    if existing:
        asia_ranges[key] = {"high": max(existing["high"], high), "low": min(existing["low"], low)}
    else:
        asia_ranges[key] = {"high": high, "low": low}

# ==============================================================================
# DÉTECTION MANIPULATION & DISTRIBUTION
# ==============================================================================
def detect_manipulation(df_m15, level_high, level_low):
    if df_m15 is None or len(df_m15) < 6 or level_high is None:
        return None
    highs = df_m15['high'].values
    lows = df_m15['low'].values
    closes = df_m15['close'].values
    opens = df_m15['open'].values

    sweep_buy = lows[-1] < level_low and closes[-1] > level_low
    sweep_sell = highs[-1] > level_high and closes[-1] < level_high

    body_impulse = abs(closes[-2] - opens[-2])
    body_mean = np.mean(np.abs(closes[-6:-1] - opens[-6:-1]))
    displacement_buy = (closes[-2] > opens[-2]) and (body_impulse > body_mean * 0.6 if body_mean else False)
    displacement_sell = (closes[-2] < opens[-2]) and (body_impulse > body_mean * 0.6 if body_mean else False)

    bullish_fvg = lows[-1] > highs[-3]
    bearish_fvg = highs[-1] < lows[-3]

    if sweep_buy and displacement_buy and bullish_fvg:
        return "BUY"
    if sweep_sell and displacement_sell and bearish_fvg:
        return "SELL"
    return None

def detect_distribution(df_m15, expected_direction):
    if df_m15 is None or len(df_m15) < 40:
        return False
    ema15 = calculate_ema(df_m15['close'], 15).iloc[-1]
    ema40 = calculate_ema(df_m15['close'], 40).iloc[-1]
    if expected_direction == "BUY":
        return ema15 > ema40 and df_m15['close'].iloc[-1] > df_m15['open'].iloc[-1]
    else:
        return ema15 < ema40 and df_m15['close'].iloc[-1] < df_m15['open'].iloc[-1]

# ==============================================================================
# SMT DIVERGENCE CORRIGÉE
# ==============================================================================
def check_smt_divergence_correct(direction, partner_symbol, partner_dfs, dt_utc):
    """Vérifie la SMT en calculant le range propre du partenaire."""
    partner_m15 = partner_dfs.get("M15", pd.DataFrame())
    if partner_m15.empty:
        return False
    
    partner_key = day_key(partner_symbol, dt_utc)
    partner_rng = asia_ranges.get(partner_key)
    if not partner_rng:
        return False

    p_low_last = partner_m15['low'].iloc[-1]
    p_high_last = partner_m15['high'].iloc[-1]

    if direction == "BUY":
        return p_low_last > partner_rng["low"]
    else:
        return p_high_last < partner_rng["high"]

# ==============================================================================
# ÉVALUATEUR FRACTAL SMC
# ==============================================================================
def evaluate_fractal_layer(df_htf, df_itf, df_ltf, level_label):
    if df_htf.empty or df_itf.empty or df_ltf.empty:
        return None, None, None, None, None, None

    htf_high = df_htf['high'].iloc[-5:].max()
    htf_low = df_htf['low'].iloc[-5:].min()
    htf_trend = "BUY" if df_htf['close'].iloc[-1] > df_htf['open'].iloc[-1] else "SELL"

    itf_high = df_itf['high'].iloc[-10:].max()
    itf_low = df_itf['low'].iloc[-10:].min()
    itf_close = df_itf['close'].iloc[-1]

    sweep_buy = (df_itf['low'].iloc[-2] < itf_low) and (itf_close > itf_low)
    sweep_sell = (df_itf['high'].iloc[-2] > itf_high) and (itf_close < itf_high)

    ltf_low = df_ltf['low'].iloc[-15:].min()
    ltf_high = df_ltf['high'].iloc[-15:].max()
    leg_height = ltf_high - ltf_low
    current_price = df_ltf['close'].iloc[-1]
    candle_id = str(df_ltf.index[-1])

    if leg_height == 0:
        return None, None, None, None, None, None

    ote_buy_min, ote_buy_max = ltf_high - (leg_height * 0.79), ltf_high - (leg_height * 0.618)
    ote_sell_min, ote_sell_max = ltf_low + (leg_height * 0.618), ltf_low + (leg_height * 0.79)

    in_ote_buy = ote_buy_min <= current_price <= ote_buy_max
    in_ote_sell = ote_sell_min <= current_price <= ote_sell_max

    ema15 = calculate_ema(df_ltf['close'], 15).iloc[-1]
    ema40 = calculate_ema(df_ltf['close'], 40).iloc[-1]
    ema_bullish = ema15 > ema40
    ema_bearish = ema15 < ema40

    if htf_trend == "BUY" and sweep_buy and in_ote_buy and ema_bullish:
        return "BUY", f"CONFIG 4 (Multi-Confluence [{level_label}])", current_price, ltf_low, htf_high, candle_id
    elif htf_trend == "SELL" and sweep_sell and in_ote_sell and ema_bearish:
        return "SELL", f"CONFIG 4 (Multi-Confluence [{level_label}])", current_price, ltf_high, htf_low, candle_id

    if htf_trend == "BUY" and in_ote_buy:
        return "BUY", f"CONFIG 1 (SMC OTE Retest [{level_label}])", current_price, ltf_low, htf_high, candle_id
    elif htf_trend == "SELL" and in_ote_sell:
        return "SELL", f"CONFIG 1 (SMC OTE Retest [{level_label}])", current_price, ltf_high, htf_low, candle_id

    if sweep_buy:
        return "BUY", f"CONFIG 2 (CRT Sweep [{level_label}])", current_price, df_itf['low'].iloc[-2], htf_high, candle_id
    elif sweep_sell:
        return "SELL", f"CONFIG 2 (CRT Sweep [{level_label}])", current_price, df_itf['high'].iloc[-2], htf_low, candle_id

    if ema_bullish and in_ote_buy:
        return "BUY", f"CONFIG 3 (EMA & OTE [{level_label}])", current_price, ltf_low, ltf_high + (leg_height * 1.5), candle_id
    elif ema_bearish and in_ote_sell:
        return "SELL", f"CONFIG 3 (EMA & OTE [{level_label}])", current_price, ltf_high, ltf_low - (leg_height * 1.5), candle_id

    return None, None, None, None, None, None

def analyze_symbol_cascade(dfs):
    df_d1 = dfs.get("D1", pd.DataFrame())
    df_h4 = dfs.get("H4", pd.DataFrame())
    df_h1 = dfs.get("H1", pd.DataFrame())
    df_m15 = dfs.get("M15", pd.DataFrame())
    df_m5 = dfs.get("M5", pd.DataFrame())
    df_m1 = dfs.get("M1", pd.DataFrame())

    res = evaluate_fractal_layer(df_d1, df_h1, df_m5, "SWING: D1/H1/M5")
    if res[0]:
        return res
    res = evaluate_fractal_layer(df_h4, df_m15, df_m5, "INTRADAY: H4/M15/M5")
    if res[0]:
        return res
    res = evaluate_fractal_layer(df_h1, df_m5, df_m1, "SCALPING: H1/M5/M1")
    if res[0]:
        return res
    return None, None, None, None, None, None

# ==============================================================================
# ENVOI DES SIGNAUX (TELEGRAM)
# ==============================================================================
def send_amd_signal(symbol, direction, phase_label, df_m15, now_utc):
    signal_id = f"{symbol}_{phase_label}_{direction}_{df_m15.index[-1]}"
    if signal_id in sent_signals:
        return
    sent_signals.add(signal_id)
    if len(sent_signals) > MAX_SENT_SIGNALS:
        sent_signals.clear()

    price = float(df_m15['close'].iloc[-1])
    if direction == "BUY":
        sl = float(df_m15['low'].iloc[-3:].min())
        risk = price - sl
        tp = price + risk * 2.5
    else:
        sl = float(df_m15['high'].iloc[-3:].max())
        risk = sl - price
        tp = price - risk * 2.5

    smt_tag = ""
    partner = SMT_PARTNER.get(symbol)
    if partner:
        try:
            partner_dfs = fetch_multi_tf_data(SYMBOLS[partner])
            if check_smt_divergence_correct(direction, partner, partner_dfs, now_utc):
                smt_tag = f"\n🔗 <b>SMT confirmée :</b> {partner} n'a pas suivi (Divergence institutionnelle)"
        except Exception as e:
            print(f"Erreur SMT pour {symbol}: {e}")

    msg = (
        f"🏛️ <b>SIGNAL AMD — {phase_label}</b> 🏛️\n\n"
        f"📌 <b>Actif :</b> {symbol}\n"
        f"📈 <b>Direction :</b> {direction}\n"
        f"🎯 <b>Prix d'entrée :</b> {format_price(symbol, price)}\n"
        f"🛑 <b>Stop Loss :</b> {format_price(symbol, sl)}\n"
        f"✅ <b>Take Profit (R:R 1:2.5) :</b> {format_price(symbol, tp)}"
        f"{smt_tag}"
    )
    send_telegram_signal(msg)

def send_generic_signal(symbol, direction, config, price, sl, tp, candle_id):
    signal_id = f"{symbol}_{config}_{direction}_{candle_id}"
    if signal_id in sent_signals:
        return
    sent_signals.add(signal_id)
    if len(sent_signals) > MAX_SENT_SIGNALS:
        sent_signals.clear()

    risk = abs(price - sl)
    tp_rr = price + (risk * 2.5) if direction == "BUY" else price - (risk * 2.5)
    tp_final = tp if tp != 0 else tp_rr

    msg = (
        f"🚨 <b>SIGNAL TRADITIONNEL SMC</b> 🚨\n\n"
        f"📌 <b>Actif :</b> {symbol}\n"
        f"📈 <b>Direction :</b> {direction}\n"
        f"⚙️ <b>Stratégie :</b> {config}\n\n"
        f"🎯 <b>Prix d'entrée :</b> {format_price(symbol, price)}\n"
        f"🛑 <b>Stop Loss :</b> {format_price(symbol, sl)}\n"
        f"✅ <b>Take Profit (R:R 1:2.5) :</b> {format_price(symbol, tp_final)}"
    )
    send_telegram_signal(msg)

# ==============================================================================
# BOUCLE PRINCIPALE
# ==============================================================================
def run_bot():
    send_telegram_signal("⚡ <b>Bot Traditionnel AMD + SMT + Fractalité en ligne !</b>")

    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            window = get_amd_window(now_utc)
            cleanup_amd_state(now_utc)

            for symbol_name, ticker in SYMBOLS.items():
                try:
                    dfs = fetch_multi_tf_data(ticker)
                    df_m15 = dfs.get("M15", pd.DataFrame())
                    key = day_key(symbol_name, now_utc)

                    if window == "ASIA":
                        update_asia_range(symbol_name, df_m15, now_utc)

                    elif window == "LONDON":
                        rng = asia_ranges.get(key)
                        if rng:
                            direction = detect_manipulation(df_m15, rng["high"], rng["low"])
                            if direction:
                                london_manipulation[key] = direction
                                send_amd_signal(symbol_name, direction, "MANIPULATION (Londres)", df_m15, now_utc)

                    elif window == "NY":
                        expected = london_manipulation.get(key)
                        if expected and detect_distribution(df_m15, expected):
                            send_amd_signal(symbol_name, expected, "DISTRIBUTION (New York)", df_m15, now_utc)

                    if is_generic_cascade_window(symbol_name, now_utc):
                        direction2, config2, price2, sl2, tp2, candle_id2 = analyze_symbol_cascade(dfs)
                        if direction2:
                            send_generic_signal(symbol_name, direction2, config2, price2, sl2, tp2, candle_id2)

                except Exception as e:
                    print(f"Erreur d'analyse sur {symbol_name}: {e}")
                    continue

        except Exception as e:
            print(f"Erreur globale Boucle: {e}")

        time.sleep(180)

if __name__ == "__main__":
    while True:
        try:
            run_bot()
        except Exception as e:
            print(f"Redémarrage automatique du script: {e}")
            time.sleep(30)

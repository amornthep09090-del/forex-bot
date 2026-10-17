import os
import time
from datetime import datetime, timezone, timedelta
import requests
import pandas as pd
import numpy as np
import yfinance as yf

# ดึงค่า Token และ User ID จากระบบ Railway
LINE_SIGNAL_TOKEN = os.getenv("LINE_SIGNAL_TOKEN", "").strip()
LINE_RESULT_TOKEN = os.getenv("LINE_RESULT_TOKEN", "").strip()
LINE_USER_ID = os.getenv("LINE_USER_ID", "").strip()

if not LINE_SIGNAL_TOKEN or not LINE_RESULT_TOKEN or not LINE_USER_ID:
    print("❌ กรุณาตั้งค่า LINE_SIGNAL_TOKEN, LINE_RESULT_TOKEN และ LINE_USER_ID ให้ครบถ้วน")
    exit(1)

tz_thai = timezone(timedelta(hours=7))

# รายชื่อคู่เงินรวมทั้งคู่หลักและคู่ OTC ยอดฮิตในแพลตฟอร์ม
SYMBOLS = {
    # คู่หลักสากล
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X", "USDCAD": "USDCAD=X", "NZDUSD": "NZDUSD=X",
    "USDCHF": "USDCHF=X", "EURGBP": "EURGBP=X", "EURJPY": "EURJPY=X",
    "GBPJPY": "GBPJPY=X", "AUDJPY": "AUDJPY=X", "EURAUD": "EURAUD=X",
    "EURCAD": "EURCAD=X", "GBPCAD": "GBPCAD=X", "GBPAUD": "GBPAUD=X",
    "AUDNZD": "AUDNZD=X", "CHFJPY": "CHFJPY=X", "CADJPY": "CADJPY=X",
    "AUDCAD": "AUDCAD=X",
    # คู่ OTC ยอดนิยม (ใช้ Yahoo Symbol เทียบเคียงโครงสร้างราคา)
    "EURUSD-OTC": "EURUSD=X", "GBPUSD-OTC": "GBPUSD=X", "USDJPY-OTC": "USDJPY=X",
    "AUDUSD-OTC": "AUDUSD=X", "EURJPY-OTC": "EURJPY=X", "GBPJPY-OTC": "GBPJPY=X"
}

stats = {asset: {"win": 0, "loss": 0, "tie": 0, "consecutive_loss": 0, "cooldown_until": 0} for asset in SYMBOLS}
global_stats = {"win": 0, "loss": 0, "tie": 0}

def send_signal_message(text):
    url = "https://api.line.me/v2/bot/message/push"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {LINE_SIGNAL_TOKEN}"}
    payload = {"to": LINE_USER_ID, "messages": [{"type": "text", "text": text}]}
    try:
        requests.post(url, headers=headers, json=payload, timeout=10)
    except Exception as e:
        print(f"⚠️ Signal Exception: {e}")

def send_result_message(text):
    url = "https://api.line.me/v2/bot/message/push"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {LINE_RESULT_TOKEN}"}
    payload = {"to": LINE_USER_ID, "messages": [{"type": "text", "text": text}]}
    try:
        requests.post(url, headers=headers, json=payload, timeout=10)
    except Exception as e:
        print(f"⚠️ Result Exception: {e}")

def get_candles(asset, retries=3):
    for _ in range(retries):
        try:
            df = yf.Ticker(SYMBOLS[asset]).history(period="5d", interval="1m")
            if df is not None and not df.empty:
                df = df.rename(columns=str.lower)
                idx = df.index
                df.index = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
                return df[["open", "high", "low", "close"]].astype(float)
        except Exception:
            pass
        time.sleep(1.5)
    return None

def analyze_market_with_score(df):
    if len(df) < 25:
        return None, 0, 0, 0
    df = df.copy()
    delta = df["close"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=7).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=7).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = (100 - (100 / (1 + rs))).fillna(50)

    df["sma20"] = df["close"].rolling(window=20).mean()
    df["std20"] = df["close"].rolling(window=20).std()
    df["bb_upper"] = df["sma20"] + (df["std20"] * 1.8)
    df["bb_lower"] = df["sma20"] - (df["std20"] * 1.8)

    last = df.iloc[-1]
    rsi_val = last["rsi"]
    signal = None
    score = 0

    if (last["close"] <= last["bb_lower"]) and (rsi_val < 32):
        signal = "BUY 🟢 (CALL)"
        score = (32 - rsi_val) + abs(last["bb_lower"] - last["close"]) * 1000
    elif (last["close"] >= last["bb_upper"]) and (rsi_val > 68):
        signal = "SELL 🔴 (PUT)"
        score = (rsi_val - 68) + abs(last["close"] - last["bb_upper"]) * 1000

    return signal, last["close"], rsi_val, score

print("✅ บอทโหมดประหยัดโควตา LINE เริ่มทำงานแล้ว")

current_target_assets = []
last_session_check = None
last_pre_scan = None
last_action_min = None
pending_signal = None

def select_top_5_assets():
    asset_scores = []
    for asset in SYMBOLS:
        df = get_candles(asset, retries=2)
        if df is None or len(df) < 25:
            continue
        
        df_sub = df.tail(50)
        delta = df_sub["close"].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=7).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=7).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        
        volatility = df_sub["close"].std()
        avg_rsi_deviation = abs(rsi - 50).mean()
        score = volatility * avg_rsi_deviation

        asset_scores.append((asset, score))

    asset_scores.sort(key=lambda x: x[1], reverse=True)
    top_5 = [item[0] for item in asset_scores[:5]]
    
    if len(top_5) < 5:
        top_5 = ["EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", "EURUSD", "GBPUSD"]

    return top_5

while True:
    try:
        current_time = time.time()
        now_thai = datetime.now(tz_thai)
        hour = now_thai.hour

        session_id = f"{now_thai.strftime('%Y-%m-%d')}-{'AM' if hour < 12 else 'PM'}"

        # อัปเดตรายชื่อ 5 คู่เด่นเงียบๆ ทุกเที่ยงวัน/เที่ยงคืน (ไม่ส่งข้อความหา LINE เพื่อประหยัดโควตา)
        if last_session_check != session_id:
            last_session_check = session_id
            current_target_assets = select_top_5_assets()

        now_utc = pd.Timestamp.now(tz="UTC")
        current_min = now_utc.floor("min")
        current_sec = now_utc.second

        # 1. วินาทีที่ 50: สแกนล่วงหน้า (Pre-Signal) ส่งเฉพาะตอนเจอสัญญาณเท่านั้น
        if current_sec >= 50 and last_pre_scan != current_min:
            last_pre_scan = current_min
            pending_signal = None
            highest_score = -1

            for asset in current_target_assets:
                if current_time < stats[asset]["cooldown_until"]:
                    continue

                df = get_candles(asset, retries=1)
                if df is None or len(df) < 25:
                    continue

                signal, entry_price, rsi_val, score = analyze_market_with_score(df)

                if signal and score > highest_score:
                    highest_score = score
                    pending_signal = {
                        "asset": asset,
                        "signal": signal,
                        "price": entry_price,
                        "rsi": rsi_val
                    }

            if pending_signal:
                pre_time_str = (now_thai + timedelta(minutes=1)).strftime("%H:%M")
                send_signal_message(
                    f"⏳ เตรียมตัวให้พร้อม! (Pre-Signal)\n"
                    f"🎯 เล็งคู่: {pending_signal['asset']}\n"
                    f"📊 ทิศทาง: {pending_signal['signal']}\n"
                    f"⏰ เตรียมกดตอนเวลา {pre_time_str} น."
                )

        # 2. วินาทีที่ 1: ส่งสัญญาณจริงและประเมินผล
        if current_sec >= 1 and last_action_min != current_min:
            last_action_min = current_min

            if pending_signal:
                asset = pending_signal["asset"]
                signal = pending_signal["signal"]
                entry_price = pending_signal["price"]
                rsi_val = pending_signal["rsi"]
                time_str = now_thai.strftime("%H:%M:%S")

                signal_msg = (
                    f"🚨 **ลุยเลย! เปิดออเดอร์** ({asset})\n"
                    f"⏰ เวลา: {time_str}\n"
                    f"🎯 แนะนำ: {signal}\n"
                    f"💵 ราคาเปิด: {entry_price:.5f}\n"
                    f"📈 RSI: {rsi_val:.1f}"
                )
                send_signal_message(signal_msg)

                target_close = current_min + pd.Timedelta(minutes=1, seconds=3)
                while pd.Timestamp.now(tz="UTC") < target_close:
                    time.sleep(0.3)

                res_df = get_candles(asset, retries=3)
                if res_df is not None and current_min in res_df.index:
                    eval_c = res_df.loc[current_min]
                    open_p, close_p = float(eval_c["open"]), float(eval_c["close"])

                    if ("BUY" in signal and close_p > open_p) or ("SELL" in signal and close_p < open_p):
                        res_txt = "✅ WIN (ชนะ)"
                        stats[asset]["win"] += 1
                        global_stats["win"] += 1
                        stats[asset]["consecutive_loss"] = 0
                    elif close_p == open_p:
                        res_txt = "⚖️ TIE (เสมอ)"
                        stats[asset]["tie"] += 1
                        global_stats["tie"] += 1
                    else:
                        res_txt = "❌ LOSS (แพ้)"
                        stats[asset]["loss"] += 1
                        global_stats["loss"] += 1
                        stats[asset]["consecutive_loss"] += 1

                        if stats[asset]["consecutive_loss"] >= 2:
                            stats[asset]["cooldown_until"] = time.time() + 1800
                            # แจ้งเตือนเงียบๆ เฉพาะตอนติด Cooldown
                            send_signal_message(f"🧠 AI Notice: คู่เงิน {asset} แพ้ติดกัน 2 ไม้! พัก 30 นาที")

                    s = stats[asset]
                    total = s["win"] + s["loss"]
                    wr = (s["win"] / total * 100) if total > 0 else 0.0

                    result_msg = (
                        f"🏁 สรุปผลลัพธ์ ({asset})\n"
                        f"ผล: {res_txt}\n"
                        f"🏆 ชนะ {s['win']} | แพ้ {s['loss']}\n"
                        f"🔥 Win Rate คู่นี้: {wr:.2f}%"
                    )
                    send_result_message(result_msg)

                pending_signal = None

        time.sleep(0.5)
    except Exception as e:
        print(f"⚠️ Loop Error: {e}")
        time.sleep(5)

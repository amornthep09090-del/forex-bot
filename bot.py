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
    print("❌ กรุณาตั้งค่า LINE_SIGNAL_TOKEN, LINE_RESULT_TOKEN และ LINE_USER_ID ใน Railway Variables ให้ครบถ้วน")
    exit(1)

tz_thai = timezone(timedelta(hours=7))

# รายชื่อคู่เงินสากลทั้งหมด
SYMBOLS = {
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X", "USDCAD": "USDCAD=X", "NZDUSD": "NZDUSD=X",
    "USDCHF": "USDCHF=X", "EURGBP": "EURGBP=X", "EURJPY": "EURJPY=X",
    "GBPJPY": "GBPJPY=X", "AUDJPY": "AUDJPY=X", "EURAUD": "EURAUD=X",
    "EURCAD": "EURCAD=X", "GBPCAD": "GBPCAD=X", "GBPAUD": "GBPAUD=X",
    "AUDNZD": "AUDNZD=X", "CHFJPY": "CHFJPY=X", "CADJPY": "CADJPY=X",
    "AUDCAD": "AUDCAD=X",
}

stats = {asset: {"win": 0, "loss": 0, "tie": 0, "consecutive_loss": 0, "cooldown_until": 0} for asset in SYMBOLS}
global_stats = {"win": 0, "loss": 0, "tie": 0}

# ฟังก์ชันส่งข้อความ "จุดเข้า" (บอทสัญญาณ)
def send_signal_message(text):
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_SIGNAL_TOKEN}"
    }
    payload = {
        "to": LINE_USER_ID,
        "messages": [{"type": "text", "text": text}]
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if r.status_code != 200:
            print(f"⚠️ Signal LINE Error: {r.text}")
    except Exception as e:
        print(f"⚠️ Signal Exception: {e}")

# ฟังก์ชันส่งข้อความ "สรุปผล" (บอทผลลัพธ์)
def send_result_message(text):
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_RESULT_TOKEN}"
    }
    payload = {
        "to": LINE_USER_ID,
        "messages": [{"type": "text", "text": text}]
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if r.status_code != 200:
            print(f"⚠️ Result LINE Error: {r.text}")
    except Exception as e:
        print(f"⚠️ Result Exception: {e}")

def get_candles(asset, retries=3):
    for _ in range(retries):
        try:
            df = yf.Ticker(SYMBOLS[asset]).history(period="1d", interval="1m")
            if df is not None and not df.empty:
                df = df.rename(columns=str.lower)
                idx = df.index
                df.index = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
                return df[["open", "high", "low", "close"]].astype(float)
        except Exception:
            pass
        time.sleep(1)
    return None

def wait_for_candle(asset, target_time, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        df = get_candles(asset, retries=1)
        if df is not None and target_time in df.index:
            return df
        time.sleep(3)
    return None

def analyze_market(df):
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
    signal = None
    if (last["close"] <= last["bb_lower"]) and (last["rsi"] < 45):
        signal = "BUY 🟢 (CALL)"
    elif (last["close"] >= last["bb_upper"]) and (last["rsi"] > 55):
        signal = "SELL 🔴 (PUT)"

    return signal, last["close"], last["rsi"]

print("✅ บอท Worker แยก 2 บอท เริ่มทำงานบน Railway แล้ว")
send_signal_message(f"🤖 บอทสัญญาณเริ่มทำงาน!\n📊 กำลังเฝ้าคู่เงินสากลทั้งหมด {len(SYMBOLS)} คู่")

last_scan = None
last_heartbeat = time.time()

while True:
    try:
        current_time = time.time()

        # รายงานตัวสถานะทำงานปกติทุก 30 นาที
        if current_time - last_heartbeat >= 1800:
            last_heartbeat = current_time
            time_str = datetime.now(tz_thai).strftime("%H:%M:%S")
            send_signal_message(f"🟢 บอทยังทำงานปกติ (Heartbeat)\n⏰ เวลาไทย: {time_str}\n📊 เฝ้ากราฟ {len(SYMBOLS)} คู่")

        now_utc = pd.Timestamp.now(tz="UTC")
        now_min = now_utc.floor("min")

        if now_utc.second >= 1 and last_scan != now_min:
            last_scan = now_min
            prev_min = now_min - pd.Timedelta(minutes=1)

            for asset in SYMBOLS:
                if current_time < stats[asset]["cooldown_until"]:
                    continue

                df = wait_for_candle(asset, prev_min, timeout=6)
                if df is None:
                    continue

                df_filtered = df[df.index < now_min]
                if len(df_filtered) < 30:
                    continue

                signal, entry_price, rsi_val = analyze_market(df_filtered)

                if signal:
                    time_str = datetime.now(tz_thai).strftime("%H:%M:%S")
                    
                    # 1. ส่งสัญญาณจุดเข้าผ่าน บอทที่ 1
                    signal_msg = (
                        f"🚨 สัญญาณเข้าออเดอร์ ({asset})\n"
                        f"⏰ เวลาไทย: {time_str}\n"
                        f"🎯 แนะนำกด: {signal}\n"
                        f"💵 ราคา: {entry_price:.5f}\n"
                        f"📈 RSI: {rsi_val:.1f}"
                    )
                    send_signal_message(signal_msg)

                    target_close = now_min + pd.Timedelta(minutes=1, seconds=5)
                    while pd.Timestamp.now(tz="UTC") < target_close:
                        time.sleep(0.5)

                    res_df = wait_for_candle(asset, now_min, timeout=15)
                    if res_df is not None and now_min in res_df.index:
                        eval_c = res_df.loc[now_min]
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
                                stats[asset]["cooldown_until"] = time.time() + 900
                                send_signal_message(f"🧠 AI Notice: คู่เงิน {asset} แพ้ติดกัน 2 ครั้ง พักชั่วคราว 15 นาที")

                        # 2. ส่งผลลัพธ์ผ่าน บอทที่ 2
                        s = stats[asset]
                        total = s["win"] + s["loss"]
                        wr = (s["win"] / total * 100) if total > 0 else 0.0

                        result_msg = (
                            f"🏁 สรุปผลลัพธ์ ({asset})\n"
                            f"ผล: {res_txt}\n"
                            f"🏆 ชนะ {s['win']} | แพ้ {s['loss']}\n"
                            f"🔥 Win Rate คู่คู่นี้: {wr:.2f}%"
                        )
                        send_result_message(result_msg)

                time.sleep(0.2)
        else:
            time.sleep(0.5)
    except Exception as e:
        print(f"⚠️ Loop Error: {e}")
        time.sleep(5)

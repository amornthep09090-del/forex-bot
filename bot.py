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

# รายชื่อคู่เงินสากลทั้ง 19 คู่
SYMBOLS = {
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X", "USDCAD": "USDCAD=X", "NZDUSD": "NZDUSD=X",
    "USDCHF": "USDCHF=X", "EURGBP": "EURGBP=X", "EURJPY": "EURJPY=X",
    "GBPJPY": "GBPJPY=X", "AUDJPY": "AUDJPY=X", "EURAUD": "EURAUD=X",
    "EURCAD": "EURCAD=X", "GBPCAD": "GBPCAD=X", "GBPAUD": "GBPAUD=X",
    "AUDNZD": "AUDNZD=X", "CHFJPY": "CHFJPY=X", "CADJPY": "CADJPY=X",
    "AUDCAD": "AUDCAD=X",
}

# ระบบสถิติและการเรียนรู้ AI (พัก 30 นาทีเมื่อแพ้ 2 ไม้ติด)
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
        time.sleep(2)
    return None

def analyze_market_with_score(df):
    """วิเคราะห์และประเมินคะแนนความมั่นใจ (Confidence Score) เฉพาะจุดที่น่าเข้าจริงๆ"""
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

    # เงื่อนไขเข้มข้น: ต้องทะลุขอบ Bollinger Bands และ RSI อยู่ในโซน Overbought/Oversold จัดๆ เท่านั้น
    if (last["close"] <= last["bb_lower"]) and (rsi_val < 32):
        signal = "BUY 🟢 (CALL)"
        score = (32 - rsi_val) + abs(last["bb_lower"] - last["close"]) * 1000
    elif (last["close"] >= last["bb_upper"]) and (rsi_val > 68):
        signal = "SELL 🔴 (PUT)"
        score = (rsi_val - 68) + abs(last["close"] - last["bb_upper"]) * 1000

    return signal, last["close"], rsi_val, score

print("✅ บอท AI โหมดคัดเฉพาะไม้ชัวร์ (High Confidence Only) เริ่มทำงานแล้ว")
send_signal_message("🤖 AI High-Confidence Mode พร้อม! จะส่งสัญญาณเฉพาะไม้ที่มั่นใจจริงๆ เท่านั้น (ไม่มั่นใจจะนิ่งเงียบ)")

last_scan = None
last_heartbeat = time.time()

while True:
    try:
        current_time = time.time()

        # รายงานสถานะทุก 30 นาที
        if current_time - last_heartbeat >= 1800:
            last_heartbeat = current_time
            time_str = datetime.now(tz_thai).strftime("%H:%M:%S")
            tot = global_stats["win"] + global_stats["loss"]
            wr_all = (global_stats["win"] / tot * 100) if tot > 0 else 0.0
            send_signal_message(f"🟢 บอทยังเฝ้ากราฟปกติ\n⏰ เวลา: {time_str}\n📊 สถิติรวม: ชนะ {global_stats['win']} | แพ้ {global_stats['loss']} (Win Rate: {wr_all:.2f}%)")

        now_utc = pd.Timestamp.now(tz="UTC")
        now_min = now_utc.floor("min")

        if now_utc.second >= 1 and last_scan != now_min:
            last_scan = now_min
            prev_min = now_min - pd.Timedelta(minutes=1)

            best_candidate = None
            highest_score = -1

            # 1. สแกนทั้ง 19 คู่เงินเพื่อหา "ตัวที่มั่นใจที่สุด" ในนาทีนี้
            for asset in SYMBOLS:
                # ข้ามคู่ที่ติด Cooldown (พัก 30 นาที)
                if current_time < stats[asset]["cooldown_until"]:
                    continue

                df = wait_for_candle(asset, prev_min, timeout=3)
                if df is None:
                    continue

                df_filtered = df[df.index < now_min]
                if len(df_filtered) < 25:
                    continue

                signal, entry_price, rsi_val, score = analyze_market_with_score(df_filtered)

                # ถ้าผ่านเกณฑ์และมีคะแนนความมั่นใจสูงที่สุดในรอบนี้
                if signal and score > highest_score:
                    highest_score = score
                    best_candidate = {
                        "asset": asset,
                        "signal": signal,
                        "price": entry_price,
                        "rsi": rsi_val
                    }

            # 2. ถ้ามีคู่ที่ผ่านเกณฑ์ความมั่นใจ ค่อยส่งสัญญาณ (ถ้านาทีไหนไม่มั่นใจ จะข้ามไปเลยไม่ส่งอะไร)
            if best_candidate:
                asset = best_candidate["asset"]
                signal = best_candidate["signal"]
                entry_price = best_candidate["price"]
                rsi_val = best_candidate["rsi"]
                time_str = datetime.now(tz_thai).strftime("%H:%M:%S")

                signal_msg = (
                    f"💎 สัญญาณเน้นๆ ความมั่นใจสูง ({asset})\n"
                    f"⏰ เวลา: {time_str}\n"
                    f"🎯 แนะนำ: {signal}\n"
                    f"💵 ราคา: {entry_price:.5f}\n"
                    f"📈 RSI: {rsi_val:.1f}"
                )
                send_signal_message(signal_msg)

                # รอจบแท่งเทียน 1 นาทีเพื่อประเมินผล WIN / LOSS
                target_close = now_min + pd.Timedelta(minutes=1, seconds=3)
                while pd.Timestamp.now(tz="UTC") < target_close:
                    time.sleep(0.3)

                res_df = wait_for_candle(asset, now_min, timeout=12)
                if res_df is not None and now_min in res_df.index:
                    eval_c = res_df.loc[now_min]
                    open_p, close_p = float(eval_c["open"]), float(eval_c["close"])

                    if ("BUY" in signal and close_p > open_p) or ("SELL" in signal and close_p < open_p):
                        res_txt = "✅ WIN (ชนะ)"
                        stats[asset]["win"] += 1
                        global_stats["win"] += 1
                        stats[asset]["consecutive_loss"] = 0 # รีเซ็ตเมื่อชนะ
                    elif close_p == open_p:
                        res_txt = "⚖️ TIE (เสมอ)"
                        stats[asset]["tie"] += 1
                        global_stats["tie"] += 1
                    else:
                        res_txt = "❌ LOSS (แพ้)"
                        stats[asset]["loss"] += 1
                        global_stats["loss"] += 1
                        stats[asset]["consecutive_loss"] += 1

                        # 🧠 AI เรียนรู้ตัวเอง: แพ้ติดกัน 2 ไม้ติด สั่งพักคู่เงินนี้ 30 นาทีทันที
                        if stats[asset]["consecutive_loss"] >= 2:
                            stats[asset]["cooldown_until"] = time.time() + 1800  # 1800 วินาที = 30 นาที
                            send_signal_message(f"🧠 AI Notice: คู่เงิน {asset} แพ้ติดกัน 2 ไม้ติด! สั่งพักการเทรด 30 นาทีเรียบร้อย")

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

        time.sleep(0.5)
    except Exception as e:
        print(f"⚠️ Loop Error: {e}")
        time.sleep(5)

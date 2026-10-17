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

def is_market_open():
    """ตรวจสอบว่าตลาด Forex เปิดทำการอยู่หรือไม่ (ปิดวันเสาร์-อาทิตย์)"""
    now_utc = datetime.now(timezone.utc)
    weekday = now_utc.weekday()
    hour = now_utc.hour

    if weekday == 5:
        return False
    if weekday == 6 and hour < 22:
        return False
    if weekday == 4 and hour >= 22:
        return False
    return True

def get_candles(asset, retries=3):
    """ฟังก์ชันดึงกราฟพร้อมระบบพยายามใหม่ (Retry) อัตโนมัติ ป้องกันข้อมูลหลุด"""
    for attempt in range(retries):
        try:
            # ขยาย period เป็น 5d เพื่อความเสถียรในการดึงข้อมูลย้อนหลัง
            df = yf.Ticker(SYMBOLS[asset]).history(period="5d", interval="1m")
            if df is not None and not df.empty:
                df = df.rename(columns=str.lower)
                idx = df.index
                df.index = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
                return df[["open", "high", "low", "close"]].astype(float)
        except Exception as e:
            # เงียบไว้หรือพิมพ์เตือนเบาๆ ไม่ให้ Log แดงพรืด
            pass
        time.sleep(1.5)
    return None

def analyze_market_with_score(df):
    """วิเคราะห์และประเมินคะแนนความมั่นใจ เฉพาะจุดที่กราฟสวยจริงๆ"""
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

print("✅ บอท AI โหมดทนทานพิเศษ (Robust Pre-Signal) เริ่มทำงานแล้ว")
send_signal_message("🤖 AI Robust Mode พร้อม! อัปเกรดระบบดึงข้อมูลให้เสถียรขึ้นเรียบร้อย")

last_pre_scan = None
last_action_min = None
last_heartbeat = time.time()
pending_candidate = None

while True:
    try:
        current_time = time.time()

        if not is_market_open():
            time.sleep(60)
            continue

        # รายงานสถานะทุก 30 นาที
        if current_time - last_heartbeat >= 1800:
            last_heartbeat = current_time
            time_str = datetime.now(tz_thai).strftime("%H:%M:%S")
            tot = global_stats["win"] + global_stats["loss"]
            wr_all = (global_stats["win"] / tot * 100) if tot > 0 else 0.0
            send_signal_message(f"🟢 ตลาดเปิด | บอทยังเฝ้ากราฟปกติ\n⏰ เวลา: {time_str}\n📊 สถิติรวม: ชนะ {global_stats['win']} | แพ้ {global_stats['loss']} (Win Rate: {wr_all:.2f}%)")

        now_utc = pd.Timestamp.now(tz="UTC")
        current_min = now_utc.floor("min")
        current_sec = now_utc.second

        # 1. วินาทีที่ 50: สแกนล่วงหน้า (Pre-Scan)
        if current_sec >= 50 and last_pre_scan != current_min:
            last_pre_scan = current_min
            pending_candidate = None
            highest_score = -1

            for asset in SYMBOLS:
                if current_time < stats[asset]["cooldown_until"]:
                    continue

                df = get_candles(asset, retries=2)
                if df is None or len(df) < 25:
                    continue

                signal, entry_price, rsi_val, score = analyze_market_with_score(df)

                if signal and score > highest_score:
                    highest_score = score
                    pending_candidate = {
                        "asset": asset,
                        "signal": signal,
                        "price": entry_price,
                        "rsi": rsi_val
                    }

            if pending_candidate:
                pre_time_str = (datetime.now(tz_thai) + timedelta(minutes=1)).strftime("%H:%M")
                pre_msg = (
                    f"⏳ เตรียมตัวให้พร้อม! (Pre-Signal)\n"
                    f"🎯 เล็งคู่: {pending_candidate['asset']}\n"
                    f"📊 ทิศทางที่คาดการณ์: {pending_candidate['signal']}\n"
                    f"⏰ เปิดจอรอเตรียมกดตอนเวลา {pre_time_str} น."
                )
                send_signal_message(pre_msg)

        # 2. วินาทีที่ 1 ของนาทีใหม่: ส่งสัญญาณจริง
        if current_sec >= 1 and last_action_min != current_min:
            last_action_min = current_min

            if pending_candidate:
                asset = pending_candidate["asset"]
                signal = pending_candidate["signal"]
                entry_price = pending_candidate["price"]
                rsi_val = pending_candidate["rsi"]
                time_str = datetime.now(tz_thai).strftime("%H:%M:%S")

                signal_msg = (
                    f"🚨 **ลุยเลย! เปิดออเดอร์** ({asset})\n"
                    f"⏰ เวลา: {time_str}\n"
                    f"🎯 แนะนำ: {signal}\n"
                    f"💵 ราคาเปิด: {entry_price:.5f}\n"
                    f"📈 RSI: {rsi_val:.1f}"
                )
                send_signal_message(signal_msg)

                # รอประเมินผลจบแท่ง 1 นาที
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

                        # แพ้ 2 ไม้ติด พัก 30 นาที
                        if stats[asset]["consecutive_loss"] >= 2:
                            stats[asset]["cooldown_until"] = time.time() + 1800
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

                pending_candidate = None

        time.sleep(0.5)
    except Exception as e:
        print(f"⚠️ Loop Error: {e}")
        time.sleep(5)

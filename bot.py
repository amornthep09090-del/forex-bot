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

# เลือกคู่เงินหลักที่เคลื่อนไหวเร็วและ Volume สูงเพื่อให้ติดออเดอร์ง่าย
SYMBOLS = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
}

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
        requests.post(url, headers=headers, json=payload, timeout=10)
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
        time.sleep(3)
    return None

print("✅ บอทโหมด 1 นาที 1 ออเดอร์ เริ่มทำงานบน Railway แล้ว")
send_signal_message("🚀 บอทโหมดซิ่ง (1 นาที 1 ออเดอร์) เริ่มทำงานแล้ว เตรียมรับสัญญาณรัวๆ!")

last_scan = None

while True:
    try:
        now_utc = pd.Timestamp.now(tz="UTC")
        now_min = now_utc.floor("min")

        if now_utc.second >= 1 and last_scan != now_min:
            last_scan = now_min
            prev_min = now_min - pd.Timedelta(minutes=1)

            # เลือกคู่เงินแรกมาออกออเดอร์ทันทีทุกๆ นาทีเพื่อให้สลับกันทำกำไร
            asset = "EURUSD"
            df = wait_for_candle(asset, prev_min, timeout=5)
            
            if df is not None and len(df) > 2:
                last_c = df.iloc[-1]
                prev_c = df.iloc[-2]
                
                # เงื่อนไขแบบออกทุกนาที (ดูทิศทางแท่งเทียนก่อนหน้า)
                if last_c["close"] >= last_c["open"]:
                    signal = "BUY 🟢 (CALL)"
                else:
                    signal = "SELL 🔴 (PUT)"

                time_str = datetime.now(tz_thai).strftime("%H:%M:%S")
                
                # 1. ส่งสัญญาณจุดเข้า
                signal_msg = (
                    f"🚨 สัญญาณซิ่ง 1 นาที ({asset})\n"
                    f"⏰ เวลา: {time_str}\n"
                    f"🎯 แนะนำ: {signal}\n"
                    f"💵 ราคา: {last_c['close']:.5f}"
                )
                send_signal_message(signal_msg)

                # รอผลลัพธ์ของแท่งถัดไป (1 นาทีจบ)
                target_close = now_min + pd.Timedelta(minutes=1, seconds=3)
                while pd.Timestamp.now(tz="UTC") < target_close:
                    time.sleep(0.5)

                res_df = wait_for_candle(asset, now_min, timeout=15)
                if res_df is not None and now_min in res_df.index:
                    eval_c = res_df.loc[now_min]
                    open_p, close_p = float(eval_c["open"]), float(eval_c["close"])

                    if ("BUY" in signal and close_p > open_p) or ("SELL" in signal and close_p < open_p):
                        res_txt = "✅ WIN (ชนะ)"
                    elif close_p == open_p:
                        res_txt = "⚖️ TIE (เสมอ)"
                    else:
                        res_txt = "❌ LOSS (แพ้)"

                    # 2. ส่งผลลัพธ์
                    result_msg = (
                        f"🏁 ผลลัพธ์ 1 นาที ({asset})\n"
                        f"ผล: {res_txt}\n"
                        f"💵 เปิด: {open_p:.5f} | ปิด: {close_p:.5f}"
                    )
                    send_result_message(result_msg)

        time.sleep(0.5)
    except Exception as e:
        print(f"⚠️ Loop Error: {e}")
        time.sleep(5)

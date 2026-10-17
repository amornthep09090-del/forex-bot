import os
import time
from datetime import datetime, timezone, timedelta
import requests
import pandas as pd
import numpy as np
import yfinance as yf

# ดึงค่า Discord Webhook 2 ช่องหลัก
DISCORD_SIGNAL_WEBHOOK = os.getenv("DISCORD_SIGNAL_WEBHOOK", "").strip()
DISCORD_RESULT_WEBHOOK = os.getenv("DISCORD_RESULT_WEBHOOK", "").strip()

if not DISCORD_SIGNAL_WEBHOOK or not DISCORD_RESULT_WEBHOOK:
    print("❌ กรุณาตั้งค่า DISCORD_SIGNAL_WEBHOOK และ DISCORD_RESULT_WEBHOOK ใน Railway Variables")
    exit(1)

tz_thai = timezone(timedelta(hours=7))

# แยกหมวดหมู่: OTC 5 คู่ + คู่ธรรมดา (Standard) 1 คู่
STANDARD_SYMBOLS = {
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X", "USDCAD": "USDCAD=X", "NZDUSD": "NZDUSD=X",
    "USDCHF": "USDCHF=X", "EURGBP": "EURGBP=X", "EURJPY": "EURJPY=X",
    "GBPJPY": "GBPJPY=X", "AUDJPY": "AUDJPY=X", "EURAUD": "EURAUD=X",
    "EURCAD": "EURCAD=X", "GBPCAD": "GBPCAD=X", "GBPAUD": "GBPAUD=X",
    "AUDNZD": "AUDNZD=X", "CHFJPY": "CHFJPY=X", "CADJPY": "CADJPY=X",
    "AUDCAD": "AUDCAD=X"
}

OTC_SYMBOLS = {
    "EURUSD-OTC": "EURUSD=X", "GBPUSD-OTC": "GBPUSD=X", "USDJPY-OTC": "USDJPY=X",
    "AUDUSD-OTC": "AUDUSD=X", "EURJPY-OTC": "EURJPY=X", "GBPJPY-OTC": "GBPJPY=X",
    "USDCAD-OTC": "USDCAD=X", "AUDCAD-OTC": "AUDCAD=X", "NZDUSD-OTC": "NZDUSD=X"
}

ALL_SYMBOLS = {**STANDARD_SYMBOLS, **OTC_SYMBOLS}

stats = {asset: {"win": 0, "loss": 0, "tie": 0, "consecutive_loss": 0, "cooldown_until": 0} for asset in ALL_SYMBOLS}
global_stats = {"win": 0, "loss": 0, "tie": 0}
last_market_result = "WIN" # ใช้เก็บสถานะไม้ก่อนหน้าเพื่อวิเคราะห์การทบไม้

def send_signal_message(text):
    try:
        requests.post(DISCORD_SIGNAL_WEBHOOK, json={"content": text}, timeout=10)
    except Exception as e:
        print(f"⚠️ Signal Webhook Error: {e}")

def send_result_message(text):
    try:
        requests.post(DISCORD_RESULT_WEBHOOK, json={"content": text}, timeout=10)
    except Exception as e:
        print(f"⚠️ Result Webhook Error: {e}")

def get_candles(symbol_code, retries=3):
    for _ in range(retries):
        try:
            df = yf.Ticker(symbol_code).history(period="5d", interval="1m")
            if df is not None and not df.empty:
                df = df.rename(columns=str.lower)
                idx = df.index
                df.index = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
                return df[["open", "high", "low", "close"]].astype(float)
        except Exception:
            pass
        time.sleep(1.5)
    return None

def analyze_market_flexible(df):
    """
    วิเคราะห์แบบยืดหยุ่น: ไม่ต้องรออินดิเคเตอร์ครบ ขอแค่ RSI หลุดโซน หรือราคาแตะกรอบ ก็สามารถออกออเดอร์ได้เลย
    """
    if len(df) < 20:
        return None, 0, 0, 0
    df = df.copy()
    delta = df["close"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=7).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=7).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = (100 - (100 / (1 + rs))).fillna(50)

    df["sma20"] = df["close"].rolling(window=20).mean()
    df["std20"] = df["close"].rolling(window=20).std()
    df["bb_upper"] = df["sma20"] + (df["std20"] * 1.7)
    df["bb_lower"] = df["sma20"] - (df["std20"] * 1.7)

    last = df.iloc[-1]
    rsi_val = last["rsi"]
    signal = None
    score = 0

    # เงื่อนไขแบบผ่อนปรน (ออกออเดอร์ง่ายขึ้น ไม่ต้องรอเงื่อนไขแน่นเกินไป)
    if (last["close"] <= last["bb_lower"]) or (rsi_val < 38):
        signal = "BUY 🟢 (CALL)"
        score = abs(38 - rsi_val) + 1
    elif (last["close"] >= last["bb_upper"]) or (rsi_val > 62):
        signal = "SELL 🔴 (PUT)"
        score = abs(rsi_val - 62) + 1

    return signal, last["close"], rsi_val, score

print("✅ บอทโหมดอิสระ วิเคราะห์เอง และแนะนำการทบไม้ เริ่มทำงานแล้ว")
send_signal_message("🤖 **AI Autonomous Bot เชื่อมต่อสำเร็จ!** พร้อมวิเคราะห์กราฟแบบยืดหยุ่นและลุยตลาดอัตโนมัติ")
send_result_message("📊 **AI Statistics & Martingale Advisor พร้อมทำงาน!** ห้องนี้จะสรุปผลและให้คำแนะนำเรื่องการทบไม้")

current_target_assets = []
last_session_check = None
last_pre_scan = None
last_action_min = None
pending_signal = None

def select_top_assets():
    otc_scores = []
    for asset, code in OTC_SYMBOLS.items():
        df = get_candles(code, retries=2)
        if df is None or len(df) < 20:
            continue
        df_sub = df.tail(30)
        score = df_sub["close"].std()
        otc_scores.append((asset, score))
    
    otc_scores.sort(key=lambda x: x[1], reverse=True)
    top_otc = [item[0] for item in otc_scores[:5]]
    if len(top_otc) < 5:
        top_otc = ["EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", "AUDUSD-OTC", "EURJPY-OTC"]

    std_scores = []
    for asset, code in STANDARD_SYMBOLS.items():
        df = get_candles(code, retries=2)
        if df is None or len(df) < 20:
            continue
        df_sub = df.tail(30)
        score = df_sub["close"].std()
        std_scores.append((asset, score))

    std_scores.sort(key=lambda x: x[1], reverse=True)
    top_std = [item[0] for item in std_scores[:1]]
    if len(top_std) < 1:
        top_std = ["EURUSD"]

    return top_otc + top_std

while True:
    try:
        current_time = time.time()
        now_thai = datetime.now(tz_thai)
        hour = now_thai.hour
        session_id = f"{now_thai.strftime('%Y-%m-%d')}-{'AM' if hour < 12 else 'PM'}"

        if last_session_check != session_id:
            last_session_check = session_id
            current_target_assets = select_top_assets()
            
            otc_list = ", ".join([f"`{a}`" for a in current_target_assets[:5]])
            std_item = f"`{current_target_assets[5]}`" if len(current_target_assets) > 5 else "`EURUSD`"
            
            session_name = "รอบเช้า/ดึก (00:00 - 12:00)" if hour < 12 else "รอบบ่าย/ค่ำ (12:00 - 00:00)"
            msg = (
                f"🔄 **อัปเดตคู่เงินโฟกัส (OTC 5 + Standard 1)**\n"
                f"📅 ช่วงเวลา: **{session_name}**\n"
                f"🌐 **คู่ธรรมดา**: {std_item}\n"
                f"📊 **คู่ OTC**: {otc_list}"
            )
            send_signal_message(msg)
            send_result_message(msg)

        now_utc = pd.Timestamp.now(tz="UTC")
        current_min = now_utc.floor("min")
        current_sec = now_utc.second

        # 1. วินาทีที่ 50: สแกนหาจังหวะล่วงหน้าแบบยืดหยุ่น
        if current_sec >= 50 and last_pre_scan != current_min:
            last_pre_scan = current_min
            pending_signal = None
            highest_score = -1

            for asset in current_target_assets:
                if current_time < stats[asset]["cooldown_until"]:
                    continue

                symbol_code = ALL_SYMBOLS[asset]
                df = get_candles(symbol_code, retries=1)
                if df is None or len(df) < 20:
                    continue

                signal, entry_price, rsi_val, score = analyze_market_flexible(df)

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
                    f"⏳ **เตรียมตัว! (Pre-Signal จังหวะยืดหยุ่น)**\n"
                    f"🎯 เล็งคู่: `{pending_signal['asset']}`\n"
                    f"📊 ทิศทาง: {pending_signal['signal']}\n"
                    f"⏰ เตรียมกดตอนเวลา **{pre_time_str}** น."
                )

        # 2. วินาทีที่ 1: ส่งสัญญาณจริงและประเมินผลพร้อมคำแนะนำการทบไม้
        if current_sec >= 1 and last_action_min != current_min:
            last_action_min = current_min

            if pending_signal:
                asset = pending_signal["asset"]
                signal = pending_signal["signal"]
                entry_price = pending_signal["price"]
                rsi_val = pending_signal["rsi"]
                time_str = now_thai.strftime("%H:%M:%S")

                signal_msg = (
                    f"🚨 **เปิดออเดอร์!** (`{asset}`)\n"
                    f"⏰ เวลา: {time_str}\n"
                    f"🎯 ทิศทาง: {signal}\n"
                    f"💵 ราคาเปิด: `{entry_price:.5f}`\n"
                    f"📈 RSI: `{rsi_val:.1f}`"
                )
                send_signal_market = signal_msg

                # แนะนำการทบไม้ล่วงหน้าตามผลไม้ก่อนหน้า
                if last_market_result == "LOSS":
                    signal_msg += f"\n💡 *AI Tip: ไม้ที่แล้วแพ้ ไม้นี้พิจารณา **ทบไม้ (Martingale x2)** ด้วยความระมัดระวัง*"
                
                send_signal_message(signal_msg)

                target_close = current_min + pd.Timedelta(minutes=1, seconds=3)
                while pd.Timestamp.now(tz="UTC") < target_close:
                    time.sleep(0.3)

                symbol_code = ALL_SYMBOLS[asset]
                res_df = get_candles(symbol_code, retries=3)
                if res_df is not None and current_min in res_df.index:
                    eval_c = res_df.loc[current_min]
                    open_p, close_p = float(eval_c["open"]), float(eval_c["close"])

                    if ("BUY" in signal and close_p > open_p) or ("SELL" in signal and close_p < open_p):
                        res_txt = "✅ **WIN (ชนะ)**"
                        stats[asset]["win"] += 1
                        global_stats["win"] += 1
                        stats[asset]["consecutive_loss"] = 0
                        last_market_result = "WIN"
                        martingale_advice = "🎉 ไม้ที่แล้วชนะ! **ไม่ต้องทบไม้** กลับมาเดินเงินปกติครับ"
                    elif close_p == open_p:
                        res_txt = "⚖️ **TIE (เสมอ)**"
                        stats[asset]["tie"] += 1
                        global_stats["tie"] += 1
                        last_market_result = "TIE"
                        martingale_advice = "⚖️ เสมอ! แนะนำให้ **รอดูท่าทีก่อนหรือใช้ทุนเท่าเดิม**"
                    else:
                        res_txt = "❌ **LOSS (แพ้)**"
                        stats[asset]["loss"] += 1
                        global_stats["loss"] += 1
                        stats[asset]["consecutive_loss"] += 1
                        last_market_result = "LOSS"
                        
                        if stats[asset]["consecutive_loss"] >= 2:
                            stats[asset]["cooldown_until"] = time.time() + 1800
                            martingale_advice = f"🚨 **แพ้ติดกัน 2 ไม้แล้ว!** แนะนำให้ **ปล่อยผ่าน (หยุดพักคู่นี้ 30 นาที)** ห้ามทบต่อเพื่อเซฟพอร์ต"
                        else:
                            martingale_advice = "💡 **ไม้แรกแพ้:** พิจารณา **ทบไม้ (Martingale x2)** ในไม้ถัดไปตามแผนบริหารทุน"

                    s = stats[asset]
                    total_asset = s["win"] + s["loss"]
                    wr_asset = (s["win"] / total_asset * 100) if total_asset > 0 else 0.0

                    tot_global = global_stats["win"] + global_stats["loss"]
                    wr_global = (global_stats["win"] / tot_global * 100) if tot_global > 0 else 0.0

                    result_msg = (
                        f"📊 **สรุปผล & คำแนะนำการทบไม้** (`{asset}`)\n"
                        f"----------------------------------------\n"
                        f"📌 ผลลัพธ์: {res_txt}\n"
                        f"💬 **คำแนะนำจาก AI**: {martingale_advice}\n"
                        f"📈 **สถิติคู่ `{asset}`**: ชนะ `{s['win']}` | แพ้ `{s['loss']}` (Win Rate: `{wr_asset:.2f}%`)\n"
                        f"🌐 **สถิติภาพรวม**: Win Rate รวม `{wr_global:.2f}%`\n"
                        f"----------------------------------------"
                    )
                    send_result_message(result_msg)

                pending_signal = None

        time.sleep(0.5)
    except Exception as e:
        print(f"⚠️ Loop Error: {e}")
        time.sleep(5)

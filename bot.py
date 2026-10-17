import os
import time
from datetime import datetime, timezone, timedelta
import requests
import pandas as pd
import numpy as np
import yfinance as yf

# ดึงค่า Discord Webhook ทั้ง 3 ห้องจาก Railway Variables
DISCORD_MAIN_WEBHOOK = os.getenv("DISCORD_MAIN_WEBHOOK", "").strip()
DISCORD_OTC_WEBHOOK = os.getenv("DISCORD_OTC_WEBHOOK", "").strip()
DISCORD_RESULT_WEBHOOK = os.getenv("DISCORD_RESULT_WEBHOOK", "").strip()

if not DISCORD_MAIN_WEBHOOK or not DISCORD_OTC_WEBHOOK or not DISCORD_RESULT_WEBHOOK:
    print("❌ กรุณาตั้งค่า Webhook ให้ครบทั้ง 3 ช่อง (Main, OTC, Result) ใน Railway Variables")
    exit(1)

tz_thai = timezone(timedelta(hours=7))

# รายการคู่เงินหลักทั้งหมด (Standard)
STANDARD_SYMBOLS = {
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X", "USDCAD": "USDCAD=X", "NZDUSD": "NZDUSD=X",
    "USDCHF": "USDCHF=X", "EURGBP": "EURGBP=X", "EURJPY": "EURJPY=X",
    "GBPJPY": "GBPJPY=X", "AUDJPY": "AUDJPY=X", "EURAUD": "EURAUD=X",
    "EURCAD": "EURCAD=X", "GBPCAD": "GBPCAD=X", "GBPAUD": "GBPAUD=X",
    "AUDNZD": "AUDNZD=X", "CHFJPY": "CHFJPY=X", "CADJPY": "CADJPY=X",
    "AUDCAD": "AUDCAD=X"
}

# รายการคู่ OTC ทั้งหมด
OTC_SYMBOLS = {
    "EURUSD-OTC": "EURUSD=X", "GBPUSD-OTC": "GBPUSD=X", "USDJPY-OTC": "USDJPY=X",
    "AUDUSD-OTC": "AUDUSD=X", "EURJPY-OTC": "EURJPY=X", "GBPJPY-OTC": "GBPJPY=X",
    "USDCAD-OTC": "USDCAD=X", "AUDCAD-OTC": "AUDCAD=X", "NZDUSD-OTC": "NZDUSD=X",
    "EURGBP-OTC": "EURGBP=X", "AUDJPY-OTC": "AUDJPY=X"
}

ALL_SYMBOLS = {**STANDARD_SYMBOLS, **OTC_SYMBOLS}

stats = {asset: {"win": 0, "loss": 0, "tie": 0, "consecutive_loss": 0, "cooldown_until": 0} for asset in ALL_SYMBOLS}
global_stats = {"win": 0, "loss": 0, "tie": 0}
last_result_type = {"MAIN": "WIN", "OTC": "WIN"}

def send_main_message(text):
    try:
        requests.post(DISCORD_MAIN_WEBHOOK, json={"content": text}, timeout=10)
    except Exception as e:
        print(f"⚠️ Main Webhook Error: {e}")

def send_otc_message(text):
    try:
        requests.post(DISCORD_OTC_WEBHOOK, json={"content": text}, timeout=10)
    except Exception as e:
        print(f"⚠️ OTC Webhook Error: {e}")

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

    if (last["close"] <= last["bb_lower"]) or (rsi_val < 38):
        signal = "BUY 🟢 (CALL)"
        score = abs(38 - rsi_val) + 1
    elif (last["close"] >= last["bb_upper"]) or (rsi_val > 62):
        signal = "SELL 🔴 (PUT)"
        score = abs(rsi_val - 62) + 1

    return signal, last["close"], rsi_val, score

print("✅ บอทสแกนทุกคู่เงิน (แยก 3 ห้อง Main, OTC, Result) เริ่มทำงานแล้ว")
send_main_message("🌐 **Main Market Bot เชื่อมต่อสำเร็จ!** ห้องนี้สแกนและส่งสัญญาณคู่เงินหลัก (Standard)")
send_otc_message("📊 **OTC Market Bot เชื่อมต่อสำเร็จ!** ห้องนี้สแกนและส่งสัญญาณคู่เงิน OTC ทั้งหมด")
send_result_message("🏆 **Global Statistics Bot เชื่อมต่อสำเร็จ!** ห้องนี้รวบรวมผลแพ้ชนะ สถิติรวม และคำแนะนำการทบไม้")

last_pre_scan = None
last_action_min = None
pending_signals = {"MAIN": None, "OTC": None}

while True:
    try:
        current_time = time.time()
        now_thai = datetime.now(tz_thai)
        now_utc = pd.Timestamp.now(tz="UTC")
        current_min = now_utc.floor("min")
        current_sec = now_utc.second

        # 1. วินาทีที่ 50: สแกนหาจังหวะล่วงหน้า
        if current_sec >= 50 and last_pre_scan != current_min:
            last_pre_scan = current_min
            pending_signals = {"MAIN": None, "OTC": None}

            # สแกนหาคู่หลักที่ดีที่สุด
            best_std_score = -1
            for asset, code in STANDARD_SYMBOLS.items():
                if current_time < stats[asset]["cooldown_until"]:
                    continue
                df = get_candles(code, retries=1)
                if df is None or len(df) < 20:
                    continue
                signal, entry_price, rsi_val, score = analyze_market_flexible(df)
                if signal and score > best_std_score:
                    best_std_score = score
                    pending_signals["MAIN"] = {"asset": asset, "signal": signal, "price": entry_price, "rsi": rsi_val}

            # สแกนหาคู่ OTC ที่ดีที่สุด
            best_otc_score = -1
            for asset, code in OTC_SYMBOLS.items():
                if current_time < stats[asset]["cooldown_until"]:
                    continue
                df = get_candles(code, retries=1)
                if df is None or len(df) < 20:
                    continue
                signal, entry_price, rsi_val, score = analyze_market_flexible(df)
                if signal and score > best_otc_score:
                    best_otc_score = score
                    pending_signals["OTC"] = {"asset": asset, "signal": signal, "price": entry_price, "rsi": rsi_val}

            pre_time_str = (now_thai + timedelta(minutes=1)).strftime("%H:%M")
            if pending_signals["MAIN"]:
                p_std = pending_signals["MAIN"]
                send_main_message(
                    f"⏳ **[Main Pre-Signal] เตรียมตัว!**\n"
                    f"🎯 เล็งคู่: `{p_std['asset']}` | ทิศทาง: {p_std['signal']}\n"
                    f"⏰ กดตอนเวลา **{pre_time_str}** น."
                )
            if pending_signals["OTC"]:
                p_otc = pending_signals["OTC"]
                send_otc_message(
                    f"⏳ **[OTC Pre-Signal] เตรียมตัว!**\n"
                    f"🎯 เล็งคู่: `{p_otc['asset']}` | ทิศทาง: {p_otc['signal']}\n"
                    f"⏰ กดตอนเวลา **{pre_time_str}** น."
                )

        # 2. วินาทีที่ 1: ส่งสัญญาณจริงและประเมินผลส่งเข้าห้องสถิติต่างหาก
        if current_sec >= 1 and last_action_min != current_min:
            last_action_min = current_min
            time_str = now_thai.strftime("%H:%M:%S")

            # ประมวลผลฝั่งคู่เงินหลัก (Main)
            if pending_signals["MAIN"]:
                p_std = pending_signals["MAIN"]
                asset = p_std["asset"]
                signal = p_std["signal"]
                
                msg = (
                    f"🚨 **[Main] ลุยเลย! เปิดออเดอร์** (`{asset}`)\n"
                    f"⏰ เวลา: {time_str} | ทิศทาง: {signal}\n"
                    f"💵 ราคา: `{p_std['price']:.5f}` | RSI: `{p_std['rsi']:.1f}`"
                )
                if last_result_type["MAIN"] == "LOSS":
                    msg += f"\n💡 *AI Tip: ไม้หลักที่แล้วแพ้ พิจารณา **ทบไม้ (Martingale x2)***"
                send_main_message(msg)

                target_close = current_min + pd.Timedelta(minutes=1, seconds=3)
                while pd.Timestamp.now(tz="UTC") < target_close:
                    time.sleep(0.3)

                res_df = get_candles(STANDARD_SYMBOLS[asset], retries=3)
                if res_df is not None and current_min in res_df.index:
                    eval_c = res_df.loc[current_min]
                    open_p, close_p = float(eval_c["open"]), float(eval_c["close"])

                    if ("BUY" in signal and close_p > open_p) or ("SELL" in signal and close_p < open_p):
                        res_txt = "✅ **WIN (ชนะ)**"
                        stats[asset]["win"] += 1
                        global_stats["win"] += 1
                        stats[asset]["consecutive_loss"] = 0
                        last_result_type["MAIN"] = "WIN"
                        advice = "🎉 ชนะ! เดินเงินปกติ ไม่ต้องทบ"
                    elif close_p == open_p:
                        res_txt = "⚖️ **TIE (เสมอ)**"
                        stats[asset]["tie"] += 1
                        global_stats["tie"] += 1
                        last_result_type["MAIN"] = "TIE"
                        advice = "⚖️ เสมอ ใช้ทุนเท่าเดิม"
                    else:
                        res_txt = "❌ **LOSS (แพ้)**"
                        stats[asset]["loss"] += 1
                        global_stats["loss"] += 1
                        stats[asset]["consecutive_loss"] += 1
                        last_result_type["MAIN"] = "LOSS"
                        if stats[asset]["consecutive_loss"] >= 2:
                            stats[asset]["cooldown_until"] = time.time() + 1800
                            advice = "🚨 แพ้ติดกัน 2 ไม้! พักคู่นี้ 30 นาที"
                        else:
                            advice = "💡 แพ้ไม้แรก พิจารณาทบไม้ (Martingale x2)"

                    s = stats[asset]
                    tot = s["win"] + s["loss"]
                    wr = (s["win"] / tot * 100) if tot > 0 else 0.0
                    
                    tot_global = global_stats["win"] + global_stats["loss"]
                    wr_global = (global_stats["win"] / tot_global * 100) if tot_global > 0 else 0.0

                    send_result_message(
                        f"📊 **[Main Result]** (`{asset}`)\n"
                        f"----------------------------------------\n"
                        f"📌 ผลลัพธ์: {res_txt}\n"
                        f"💬 **คำแนะนำ**: {advice}\n"
                        f"📈 **สถิติคู่ `{asset}`**: ชนะ `{s['win']}` | แพ้ `{s['loss']}` (Win Rate: `{wr:.2f}%`)\n"
                        f"🌐 **สถิติภาพรวมทั้งหมด**: ชนะ `{global_stats['win']}` | แพ้ `{global_stats['loss']}` (Win Rate รวม: `{wr_global:.2f}%`)\n"
                        f"----------------------------------------"
                    )

            # ประมวลผลฝั่งคู่ OTC
            if pending_signals["OTC"]:
                p_otc = pending_signals["OTC"]
                asset = p_otc["asset"]
                signal = p_otc["signal"]
                
                msg = (
                    f"🚨 **[OTC] ลุยเลย! เปิดออเดอร์** (`{asset}`)\n"
                    f"⏰ เวลา: {time_str} | ทิศทาง: {signal}\n"
                    f"💵 ราคา: `{p_otc['price']:.5f}` | RSI: `{p_otc['rsi']:.1f}`"
                )
                if last_result_type["OTC"] == "LOSS":
                    msg += f"\n💡 *AI Tip: ไม้ OTC ที่แล้วแพ้ พิจารณา **ทบไม้ (Martingale x2)***"
                send_otc_message(msg)

                target_close = current_min + pd.Timedelta(minutes=1, seconds=3)
                while pd.Timestamp.now(tz="UTC") < target_close:
                    time.sleep(0.3)

                res_df = get_candles(OTC_SYMBOLS[asset], retries=3)
                if res_df is not None and current_min in res_df.index:
                    eval_c = res_df.loc[current_min]
                    open_p, close_p = float(eval_c["open"]), float(eval_c["close"])

                    if ("BUY" in signal and close_p > open_p) or ("SELL" in signal and close_p < open_p):
                        res_txt = "✅ **WIN (ชนะ)**"
                        stats[asset]["win"] += 1
                        global_stats["win"] += 1
                        stats[asset]["consecutive_loss"] = 0
                        last_result_type["OTC"] = "WIN"
                        advice = "🎉 ชนะ! เดินเงินปกติ ไม่ต้องทบ"
                    elif close_p == open_p:
                        res_txt = "⚖️ **TIE (เสมอ)**"
                        stats[asset]["tie"] += 1
                        global_stats["tie"] += 1
                        last_result_type["OTC"] = "TIE"
                        advice = "⚖️ เสมอ ใช้ทุนเท่าเดิม"
                    else:
                        res_txt = "❌ **LOSS (แพ้)**"
                        stats[asset]["loss"] += 1
                        global_stats["loss"] += 1
                        stats[asset]["consecutive_loss"] += 1
                        last_result_type["OTC"] = "LOSS"
                        if stats[asset]["consecutive_loss"] >= 2:
                            stats[asset]["cooldown_until"] = time.time() + 1800
                            advice = "🚨 แพ้ติดกัน 2 ไม้! พักคู่นี้ 30 นาที"
                        else:
                            advice = "💡 แพ้ไม้แรก พิจารณาทบไม้ (Martingale x2)"

                    s = stats[asset]
                    tot = s["win"] + s["loss"]
                    wr = (s["win"] / tot * 100) if tot > 0 else 0.0
                    
                    tot_global = global_stats["win"] + global_stats["loss"]
                    wr_global = (global_stats["win"] / tot_global * 100) if tot_global > 0 else 0.0

                    send_result_message(
                        f"📊 **[OTC Result]** (`{asset}`)\n"
                        f"----------------------------------------\n"
                        f"📌 ผลลัพธ์: {res_txt}\n"
                        f"💬 **คำแนะนำ**: {advice}\n"
                        f"📈 **สถิติคู่ `{asset}`**: ชนะ `{s['win']}` | แพ้ `{s['loss']}` (Win Rate: `{wr:.2f}%`)\n"
                        f"🌐 **สถิติภาพรวมทั้งหมด**: ชนะ `{global_stats['win']}` | แพ้ `{global_stats['loss']}` (Win Rate รวม: `{wr_global:.2f}%`)\n"
                        f"----------------------------------------"
                    )

            pending_signals = {"MAIN": None, "OTC": None}

        time.sleep(0.5)
    except Exception as e:
        print(f"⚠️ Loop Error: {e}")
        time.sleep(5)

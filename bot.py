import os
import time
from datetime import datetime, timezone, timedelta
import threading
import requests
import pandas as pd
import numpy as np
import yfinance as yf

DISCORD_MAIN_WEBHOOK = os.getenv("DISCORD_MAIN_WEBHOOK", "").strip()
DISCORD_OTC_WEBHOOK = os.getenv("DISCORD_OTC_WEBHOOK", "").strip()
DISCORD_RESULT_WEBHOOK = os.getenv("DISCORD_RESULT_WEBHOOK", "").strip()

if not DISCORD_MAIN_WEBHOOK or not DISCORD_OTC_WEBHOOK or not DISCORD_RESULT_WEBHOOK:
    print("❌ กรุณาตั้งค่า Webhook ให้ครบทั้ง 3 ช่องใน Railway Variables")
    exit(1)

tz_thai = timezone(timedelta(hours=7))

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
            df = yf.Ticker(symbol_code).history(period="2d", interval="1m")
            if df is not None and not df.empty:
                df = df.rename(columns=str.lower)
                idx = df.index
                df.index = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
                df = df[["open", "high", "low", "close"]].astype(float)
                if len(df) >= 15:
                    return df
        except Exception:
            pass
        time.sleep(1)
    return None

def analyze_market_flexible(df):
    if df is None or len(df) < 15:
        return None, 0, 0, 0
    df = df.copy()
    delta = df["close"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=7).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=7).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = (100 - (100 / (1 + rs)))

    df["sma20"] = df["close"].rolling(window=15).mean()
    df["std20"] = df["close"].rolling(window=15).std()
    df["bb_upper"] = df["sma20"] + (df["std20"] * 1.6)
    df["bb_lower"] = df["sma20"] - (df["std20"] * 1.6)

    last = df.iloc[-1]
    rsi_val = float(last["rsi"])
    if pd.isna(rsi_val):
        rsi_val = 50.0

    signal = None
    score = 0

    if (last["close"] <= last["bb_lower"]) or (rsi_val < 38):
        signal = "BUY 🟢 (CALL)"
        score = abs(38 - rsi_val) + 1
    elif (last["close"] >= last["bb_upper"]) or (rsi_val > 62):
        signal = "SELL 🔴 (PUT)"
        score = abs(rsi_val - 62) + 1

    return signal, float(last["close"]), rsi_val, score

def evaluate_trade_background(market_type, asset, symbol_code, signal, signal_min):
    """ฟังก์ชันทำงานเบื้องหลังเพื่อรอเช็กผล WIN/LOSS แบบไม่บล็อกบอทหลัก"""
    try:
        # รอให้แท่งเทียนวิ่งจบและข้อมูลอัปเดตบนเซิร์ฟเวอร์ (ประมาณ 65 วินาที)
        target_close = signal_min + pd.Timedelta(minutes=1, seconds=65)
        while pd.Timestamp.now(tz="UTC") < target_close:
            time.sleep(1)

        res_df = get_candles(symbol_code, retries=5)
        if res_df is not None and signal_min in res_df.index:
            eval_c = res_df.loc[signal_min]
            open_p, close_p = float(eval_c["open"]), float(eval_c["close"])

            if ("BUY" in signal and close_p > open_p) or ("SELL" in signal and close_p < open_p):
                res_txt = "✅ **WIN (ชนะ)**"
                stats[asset]["win"] += 1
                global_stats["win"] += 1
                stats[asset]["consecutive_loss"] = 0
                last_result_type[market_type] = "WIN"
                advice = "🎉 ชนะ! เดินเงินปกติ ไม่ต้องทบ"
            elif close_p == open_p:
                res_txt = "⚖️ **TIE (เสมอ)**"
                stats[asset]["tie"] += 1
                global_stats["tie"] += 1
                last_result_type[market_type] = "TIE"
                advice = "⚖️ เสมอ ใช้ทุนเท่าเดิม"
            else:
                res_txt = "❌ **LOSS (แพ้)**"
                stats[asset]["loss"] += 1
                global_stats["loss"] += 1
                stats[asset]["consecutive_loss"] += 1
                last_result_type[market_type] = "LOSS"
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

            title_tag = "Main Result Report" if market_type == "MAIN" else "OTC Result Report"
            send_result_message(
                f"📊 **[{title_tag}]**\n"
                f"----------------------------------------\n"
                f"🎯 คู่เงิน: `{asset}`\n"
                f"📌 ผลลัพธ์: {res_txt}\n"
                f"💬 คำแนะนำ: {advice}\n"
                f"📈 สถิติคู่ `{asset}`: ชนะ `{s['win']}` | แพ้ `{s['loss']}` (Win Rate: `{wr:.2f}%`)\n"
                f"🌐 สถิติภาพรวมทั้งหมด: ชนะ `{global_stats['win']}` | แพ้ `{global_stats['loss']}` (Win Rate รวม: `{wr_global:.2f}%`)\n"
                f"----------------------------------------"
            )
    except Exception as e:
        print(f"⚠️ Evaluation Error: {e}")

print("✅ บอทระบบ Multi-threading เริ่มทำงานแล้ว")
send_main_message("🌐 **Main Market Bot พร้อมทำงาน!**")
send_otc_message("📊 **OTC Market Bot พร้อมทำงาน!**")
send_result_message("🏆 **Global Statistics Bot พร้อมรายงานผล WIN/LOSS แล้ว!**")

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

            best_std_score = -1
            for asset, code in STANDARD_SYMBOLS.items():
                if current_time < stats[asset]["cooldown_until"]:
                    continue
                df = get_candles(code, retries=1)
                signal, entry_price, rsi_val, score = analyze_market_flexible(df)
                if signal and score > best_std_score:
                    best_std_score = score
                    pending_signals["MAIN"] = {"asset": asset, "signal": signal, "price": entry_price, "rsi": rsi_val}

            best_otc_score = -1
            for asset, code in OTC_SYMBOLS.items():
                if current_time < stats[asset]["cooldown_until"]:
                    continue
                df = get_candles(code, retries=1)
                signal, entry_price, rsi_val, score = analyze_market_flexible(df)
                if signal and score > best_otc_score:
                    best_otc_score = score
                    pending_signals["OTC"] = {"asset": asset, "signal": signal, "price": entry_price, "rsi": rsi_val}

            pre_time_str = (now_thai + timedelta(minutes=1)).strftime("%H:%M")
            
            if pending_signals["MAIN"]:
                p_std = pending_signals["MAIN"]
                send_main_message(
                    f"⏳ **[Main Pre-Signal] เตรียมตัว!**\n"
                    f"----------------------------------------\n"
                    f"🎯 คู่เงิน: `{p_std['asset']}`\n"
                    f"📊 ทิศทาง: {p_std['signal']}\n"
                    f"⏰ เตรียมกดตอนเวลา: **{pre_time_str}** น.\n"
                    f"----------------------------------------"
                )
                
            if pending_signals["OTC"]:
                p_otc = pending_signals["OTC"]
                send_otc_message(
                    f"⏳ **[OTC Pre-Signal] เตรียมตัว!**\n"
                    f"----------------------------------------\n"
                    f"🎯 คู่เงิน: `{p_otc['asset']}`\n"
                    f"📊 ทิศทาง: {p_otc['signal']}\n"
                    f"⏰ เตรียมกดตอนเวลา: **{pre_time_str}** น.\n"
                    f"----------------------------------------"
                )

        # 2. วินาทีที่ 1: ส่งสัญญาณจริงและเปิดเธรดรอประเมินผลเบื้องหลัง
        if current_sec >= 1 and last_action_min != current_min:
            last_action_min = current_min
            action_time_str = now_thai.strftime("%H:%M:%S")
            signal_min = current_min

            # ประมวลผล Main
            if pending_signals["MAIN"]:
                p_std = pending_signals["MAIN"]
                asset = p_std["asset"]
                signal = p_std["signal"]
                
                msg = (
                    f"🚨 **[Main] ลุยเลย! เปิดออเดอร์**\n"
                    f"----------------------------------------\n"
                    f"🎯 คู่เงิน: `{asset}`\n"
                    f"⏰ เวลา: `{action_time_str}`\n"
                    f"📈 ทิศทาง: {signal}\n"
                    f"💵 ราคาเปิด: `{p_std['price']:.5f}`\n"
                    f"📉 ค่า RSI: `{p_std['rsi']:.1f}`"
                )
                if last_result_type["MAIN"] == "LOSS":
                    msg += f"\n\n💡 *AI Tip: ไม้หลักที่แล้วแพ้ พิจารณา **ทบไม้ (Martingale x2)***"
                msg += f"\n----------------------------------------"
                send_main_message(msg)

                # เปิด Background Thread รอเช็กผลแบบไม่บล็อกบอท
                t = threading.Thread(target=evaluate_trade_background, args=("MAIN", asset, STANDARD_SYMBOLS[asset], signal, signal_min))
                t.start()

            # ประมวลผล OTC
            if pending_signals["OTC"]:
                p_otc = pending_signals["OTC"]
                asset = p_otc["asset"]
                signal = p_otc["signal"]
                
                msg = (
                    f"🚨 **[OTC] ลุยเลย! เปิดออเดอร์**\n"
                    f"----------------------------------------\n"
                    f"🎯 คู่เงิน: `{asset}`\n"
                    f"⏰ เวลา: `{action_time_str}`\n"
                    f"📈 ทิศทาง: {signal}\n"
                    f"💵 ราคาเปิด: `{p_otc['price']:.5f}`\n"
                    f"📉 ค่า RSI: `{p_otc['rsi']:.1f}`"
                )
                if last_result_type["OTC"] == "LOSS":
                    msg += f"\n\n💡 *AI Tip: ไม้ OTC ที่แล้วแพ้ พิจารณา **ทบไม้ (Martingale x2)***"
                msg += f"\n----------------------------------------"
                send_otc_message(msg)

                # เปิด Background Thread รอเช็กผลแบบไม่บล็อกบอท
                t = threading.Thread(target=evaluate_trade_background, args=("OTC", asset, OTC_SYMBOLS[asset], signal, signal_min))
                t.start()

            pending_signals = {"MAIN": None, "OTC": None}

        time.sleep(0.5)
    except Exception as e:
        print(f"⚠️ Loop Error: {e}")
        time.sleep(5)

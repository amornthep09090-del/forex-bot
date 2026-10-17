import os
import time
import threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

import requests
import pandas as pd
import numpy as np
import yfinance as yf

# ======================= ตั้งค่า =======================
DISCORD_MAIN_WEBHOOK = os.getenv("DISCORD_MAIN_WEBHOOK", "").strip()
DISCORD_OTC_WEBHOOK = os.getenv("DISCORD_OTC_WEBHOOK", "").strip()
DISCORD_RESULT_WEBHOOK = os.getenv("DISCORD_RESULT_WEBHOOK", "").strip()

# ปรับได้ผ่าน Railway Variables (ไม่ตั้งก็ใช้ค่าเริ่มต้น)
MAX_DATA_AGE_SEC = int(os.getenv("MAX_DATA_AGE_SEC", "240"))    # แท่งล่าสุดเก่าเกินนี้ = ข้อมูลไม่สด
MIN_FRESH_RATIO = float(os.getenv("MIN_FRESH_RATIO", "0.5"))    # ต้องมีคู่เงินสดอย่างน้อยกี่ % ถึงถือว่าดึงข้อมูลได้
PAIR_COOLDOWN_SEC = int(os.getenv("PAIR_COOLDOWN_SEC", "180"))  # กันส่งคู่เดิมซ้ำถี่ ๆ
FAIL_TO_PAUSE = int(os.getenv("FAIL_TO_PAUSE", "2"))            # ดึงข้อมูลพลาดติดกันกี่รอบถึงพัก
OK_TO_RESUME = int(os.getenv("OK_TO_RESUME", "2"))              # ดึงได้ปกติติดกันกี่รอบถึงกลับมา
SCAN_SECOND = 45                                                # วินาทีที่เริ่มสแกนล่วงหน้า

if not DISCORD_MAIN_WEBHOOK or not DISCORD_OTC_WEBHOOK or not DISCORD_RESULT_WEBHOOK:
    print("❌ กรุณาตั้งค่า Webhook ให้ครบทั้ง 3 ช่องใน Railway Variables")
    raise SystemExit(1)

tz_thai = timezone(timedelta(hours=7))
BOOT_ID = os.urandom(2).hex()  # ไว้เช็คว่ามีบอทรันซ้อนกันหลายตัวหรือไม่

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
UNIQUE_TICKERS = sorted(set(ALL_SYMBOLS.values()))  # ดึงทีเดียวต่อ ticker ใช้ร่วมกันทั้ง Main/OTC

stats = {asset: {"win": 0, "loss": 0, "tie": 0, "consecutive_loss": 0, "cooldown_until": 0} for asset in ALL_SYMBOLS}
global_stats = {"win": 0, "loss": 0, "tie": 0}
last_result_type = {"MAIN": "WIN", "OTC": "WIN"}
last_signal_time = {}
stats_lock = threading.Lock()


# ======================= ส่ง Discord =======================
def _post(url, text, tag):
    for attempt in range(2):
        try:
            r = requests.post(url, json={"content": text}, timeout=10)
            if r.status_code == 429:  # โดน rate limit ของ Discord -> รอแล้วลองใหม่
                try:
                    wait = float(r.json().get("retry_after", 2))
                except Exception:
                    wait = 2.0
                time.sleep(min(wait, 10))
                continue
            if r.status_code >= 400:
                print(f"⚠️ {tag} Webhook HTTP {r.status_code}: {r.text[:200]}")
            return
        except Exception as e:
            print(f"⚠️ {tag} Webhook Error: {e}")
            time.sleep(1)


def send_main_message(text):
    _post(DISCORD_MAIN_WEBHOOK, text, "Main")


def send_otc_message(text):
    _post(DISCORD_OTC_WEBHOOK, text, "OTC")


def send_result_message(text):
    _post(DISCORD_RESULT_WEBHOOK, text, "Result")


def broadcast(text):
    send_main_message(text)
    send_otc_message(text)
    send_result_message(text)


# ======================= ดึงข้อมูล =======================
def get_candles(symbol_code, retries=2):
    for i in range(retries):
        try:
            df = yf.Ticker(symbol_code).history(period="2d", interval="1m")
            if df is not None and not df.empty:
                df = df.rename(columns=str.lower)
                idx = df.index
                df.index = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
                df = df[["open", "high", "low", "close"]].astype(float).dropna()
                df = df[~df.index.duplicated(keep="last")]
                if len(df) >= 15:
                    return df
        except Exception as e:
            print(f"⚠️ get_candles {symbol_code}: {e}")
        time.sleep(1 + i)
    return None


def fetch_all(tickers):
    """ดึงหลาย ticker พร้อมกัน (เร็วกว่าดึงทีละตัวมาก)"""
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(get_candles, tickers))
    return dict(zip(tickers, results))


def is_fresh(df, now_utc=None):
    """ข้อมูลต้อง 'สด' และ 'ราคาขยับ' ไม่ใช่ข้อมูลค้างจากตลาดปิด"""
    if df is None or len(df) < 15:
        return False
    now_utc = now_utc or pd.Timestamp.now(tz="UTC")
    age = (now_utc - df.index[-1]).total_seconds()
    if age > MAX_DATA_AGE_SEC:
        return False
    if df["close"].tail(15).nunique() <= 1:
        return False
    return True


def market_closed_by_calendar(now_utc):
    """ตลาด Forex ปิดช่วงสุดสัปดาห์ (ประมาณ ศ. 22:00 UTC ถึง อา. 21:00 UTC)
    ใช้แค่ประหยัดการดึงข้อมูล ตัวตัดสินจริงคือ is_fresh()"""
    wd = now_utc.weekday()  # จันทร์=0 ... อาทิตย์=6
    if wd == 5:
        return True
    if wd == 4 and now_utc.hour >= 22:
        return True
    if wd == 6 and now_utc.hour < 21:
        return True
    return False


# ======================= วิเคราะห์ =======================
def analyze_market_flexible(df):
    if df is None or len(df) < 15:
        return None, 0, 0, 0
    df = df.copy()
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(window=7).mean()
    loss = (-delta.clip(upper=0)).rolling(window=7).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.where(~((loss == 0) & (gain > 0)), 100.0)  # ขึ้นล้วน = RSI 100
    df["rsi"] = rsi

    df["sma20"] = df["close"].rolling(window=15).mean()
    df["std20"] = df["close"].rolling(window=15).std()
    df["bb_upper"] = df["sma20"] + (df["std20"] * 1.6)
    df["bb_lower"] = df["sma20"] - (df["std20"] * 1.6)

    last = df.iloc[-1]
    rsi_val = float(last["rsi"])
    price = float(last["close"])

    # ไม่มี RSI / ไม่มีความผันผวน = ข้อมูลนิ่ง ไม่ออกสัญญาณ
    if pd.isna(rsi_val) or pd.isna(last["bb_lower"]) or not (last["std20"] > 0):
        return None, price, 50.0, 0

    signal = None
    score = 0
    if (last["close"] <= last["bb_lower"]) or (rsi_val < 38):
        signal = "BUY 🟢 (CALL)"
        score = abs(38 - rsi_val) + 1
    elif (last["close"] >= last["bb_upper"]) or (rsi_val > 62):
        signal = "SELL 🔴 (PUT)"
        score = abs(rsi_val - 62) + 1

    return signal, price, rsi_val, score


def build_pending(data, scan_min):
    """เลือกสัญญาณที่ดีที่สุดของแต่ละตลาด จากข้อมูลที่ดึงมาแล้ว"""
    pending = {"MAIN": None, "OTC": None}
    now_ts = time.time()
    now_utc = pd.Timestamp.now(tz="UTC")
    for market, symbols in (("MAIN", STANDARD_SYMBOLS), ("OTC", OTC_SYMBOLS)):
        best = -1
        for asset, code in symbols.items():
            if now_ts < stats[asset]["cooldown_until"]:
                continue
            if now_ts < last_signal_time.get(asset, 0) + PAIR_COOLDOWN_SEC:
                continue
            df = data.get(code)
            if not is_fresh(df, now_utc):
                continue
            signal, price, rsi_val, score = analyze_market_flexible(df)
            if signal and score > best:
                best = score
                pending[market] = {"asset": asset, "signal": signal, "price": price, "rsi": rsi_val}
    return pending


# ======================= ประเมินผล (เธรดเบื้องหลัง) =======================
def evaluate_trade_background(market_type, asset, symbol_code, signal, signal_min):
    title_tag = "Main Result Report" if market_type == "MAIN" else "OTC Result Report"
    try:
        # รอให้แท่งของ signal_min ปิด
        close_at = signal_min + pd.Timedelta(minutes=1, seconds=8)
        while pd.Timestamp.now(tz="UTC") < close_at:
            time.sleep(1)

        # ลองดึงซ้ำจนกว่าจะได้แท่งที่ต้องการ (สูงสุด ~5 นาที) ไม่เจอก็รายงานว่าประเมินไม่ได้
        deadline = signal_min + pd.Timedelta(minutes=5)
        next_min = signal_min + pd.Timedelta(minutes=1)
        eval_c = None
        while pd.Timestamp.now(tz="UTC") < deadline:
            res_df = get_candles(symbol_code, retries=2)
            if res_df is not None and signal_min in res_df.index:
                waited = (pd.Timestamp.now(tz="UTC") - close_at).total_seconds()
                # มีแท่งถัดไปแล้ว = แท่งนี้ปิดสมบูรณ์ หรือรอเกิน 90 วิก็ใช้เลย
                if next_min in res_df.index or waited >= 90:
                    eval_c = res_df.loc[signal_min]
                    break
            time.sleep(10)

        if eval_c is None:
            send_result_message(
                f"⚠️ **[{title_tag}]**\n"
                f"ประเมินผล `{asset}` ไม่ได้ (ดึงแท่งเทียนไม่ได้) — ไม่นับเข้าสถิติ"
            )
            return

        open_p, close_p = float(eval_c["open"]), float(eval_c["close"])

        with stats_lock:
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

            s = dict(stats[asset])
            g = dict(global_stats)

        tot = s["win"] + s["loss"]
        wr = (s["win"] / tot * 100) if tot > 0 else 0.0
        tot_g = g["win"] + g["loss"]
        wr_g = (g["win"] / tot_g * 100) if tot_g > 0 else 0.0

        send_result_message(
            f"📊 **[{title_tag}]**\n"
            f"----------------------------------------\n"
            f"🎯 คู่เงิน: `{asset}`\n"
            f"📌 ผลลัพธ์: {res_txt}\n"
            f"💬 คำแนะนำ: {advice}\n"
            f"📈 สถิติคู่ `{asset}`: ชนะ `{s['win']}` | แพ้ `{s['loss']}` (Win Rate: `{wr:.2f}%`)\n"
            f"🌐 สถิติภาพรวมทั้งหมด: ชนะ `{g['win']}` | แพ้ `{g['loss']}` (Win Rate รวม: `{wr_g:.2f}%`)\n"
            f"----------------------------------------"
        )
    except Exception as e:
        print(f"⚠️ Evaluation Error: {e}")
        try:
            send_result_message(f"⚠️ ประเมินผล `{asset}` ผิดพลาด: `{e}` — ไม่นับเข้าสถิติ")
        except Exception:
            pass


# ======================= ลูปหลัก =======================
def main():
    print("✅ บอทระบบรายงานผลพร้อมใช้งานแล้ว")
    send_main_message(f"🌐 **Main Market Bot พร้อมทำงาน!** (run `{BOOT_ID}`)")
    send_otc_message(f"📊 **OTC Market Bot พร้อมทำงาน!** (run `{BOOT_ID}`)")
    send_result_message(f"🏆 **Global Statistics Bot พร้อมรายงานผล WIN/LOSS แล้ว!** (run `{BOOT_ID}`)")

    health = {"paused": False, "fail": 0, "ok": 0, "skip": 0}
    last_pre_scan = None
    last_action_min = None
    pending_signals = {"MAIN": None, "OTC": None}
    pending_minute = None

    def pause(reason):
        if not health["paused"]:
            health["paused"] = True
            print(f"⏸️ พักบอท: {reason}")
            broadcast(f"⏸️ **บอทพักชั่วคราว** — {reason}\nจะกลับมาทำงานเองเมื่อดึงข้อมูลได้ปกติ")

    def resume():
        if health["paused"]:
            health["paused"] = False
            print("▶️ กลับมาทำงาน")
            broadcast("▶️ **บอทกลับมาทำงานแล้ว** — ข้อมูลตลาดพร้อมใช้งานปกติ")

    while True:
        try:
            now_utc = pd.Timestamp.now(tz="UTC")
            current_min = now_utc.floor("min")
            current_sec = now_utc.second

            # 1. สแกนล่วงหน้า (วินาที SCAN_SECOND)
            if current_sec >= SCAN_SECOND and last_pre_scan != current_min:
                last_pre_scan = current_min
                pending_signals = {"MAIN": None, "OTC": None}
                pending_minute = None

                if market_closed_by_calendar(now_utc):
                    health["ok"] = 0
                    pause("ตลาด Forex ปิดช่วงสุดสัปดาห์")
                elif health["skip"] > 0:
                    health["skip"] -= 1  # backoff: ข้ามรอบนี้ ไม่ยิงขอข้อมูลถี่
                else:
                    data = fetch_all(UNIQUE_TICKERS)
                    check_utc = pd.Timestamp.now(tz="UTC")
                    fresh_n = sum(1 for df in data.values() if is_fresh(df, check_utc))
                    need = max(3, int(len(UNIQUE_TICKERS) * MIN_FRESH_RATIO))
                    healthy = fresh_n >= need

                    if healthy:
                        health["fail"] = 0
                        health["ok"] += 1
                        if health["paused"] and health["ok"] >= OK_TO_RESUME:
                            resume()
                        if not health["paused"]:
                            pending_signals = build_pending(data, current_min)
                            pending_minute = current_min
                    else:
                        health["ok"] = 0
                        health["fail"] += 1
                        print(f"⚠️ ข้อมูลสดแค่ {fresh_n}/{len(UNIQUE_TICKERS)} คู่ (ครั้งที่ {health['fail']})")
                        if health["fail"] >= FAIL_TO_PAUSE:
                            pause(f"ดึงข้อมูลตลาดไม่ได้/ข้อมูลไม่อัปเดต (สดแค่ {fresh_n}/{len(UNIQUE_TICKERS)} คู่)")
                        # backoff: 0,1,3,4,4... นาที (ไม่เกิน ~5 นาทีต่อรอบ)
                        health["skip"] = min(2 ** (health["fail"] - 1) - 1, 4)

                if pending_minute is not None:
                    pre_time_str = (current_min + pd.Timedelta(minutes=1)).tz_convert(tz_thai).strftime("%H:%M")
                    if pending_signals["MAIN"]:
                        p = pending_signals["MAIN"]
                        send_main_message(
                            f"⏳ **[Main Pre-Signal] เตรียมตัว!**\n"
                            f"----------------------------------------\n"
                            f"🎯 คู่เงิน: `{p['asset']}`\n"
                            f"📊 ทิศทาง: {p['signal']}\n"
                            f"⏰ เตรียมกดตอนเวลา: **{pre_time_str}** น.\n"
                            f"----------------------------------------"
                        )
                    if pending_signals["OTC"]:
                        p = pending_signals["OTC"]
                        send_otc_message(
                            f"⏳ **[OTC Pre-Signal] เตรียมตัว!**\n"
                            f"----------------------------------------\n"
                            f"🎯 คู่เงิน: `{p['asset']}`\n"
                            f"📊 ทิศทาง: {p['signal']}\n"
                            f"⏰ เตรียมกดตอนเวลา: **{pre_time_str}** น.\n"
                            f"----------------------------------------"
                        )

            # 2. วินาทีที่ 1: ส่งสัญญาณจริง (ต้องเป็นนาทีถัดจากที่สแกน และไม่เลทเกิน 20 วิ)
            if (
                current_sec >= 1
                and last_action_min != current_min
                and (pending_minute is None or current_min != pending_minute)
            ):
                last_action_min = current_min
                valid = (
                    not health["paused"]
                    and pending_minute is not None
                    and current_min == pending_minute + pd.Timedelta(minutes=1)
                    and current_sec <= 20
                )
                if valid:
                    action_time_str = datetime.now(tz_thai).strftime("%H:%M:%S")
                    signal_min = current_min

                    for market, symbols, sender, label in (
                        ("MAIN", STANDARD_SYMBOLS, send_main_message, "Main"),
                        ("OTC", OTC_SYMBOLS, send_otc_message, "OTC"),
                    ):
                        p = pending_signals[market]
                        if not p:
                            continue
                        asset, signal = p["asset"], p["signal"]
                        last_signal_time[asset] = time.time()

                        msg = (
                            f"🚨 **[{label}] ลุยเลย! เปิดออเดอร์**\n"
                            f"----------------------------------------\n"
                            f"🎯 คู่เงิน: `{asset}`\n"
                            f"⏰ เวลา: `{action_time_str}`\n"
                            f"📈 ทิศทาง: {signal}\n"
                            f"💵 ราคาเปิด: `{p['price']:.5f}`\n"
                            f"📉 ค่า RSI: `{p['rsi']:.1f}`"
                        )
                        if last_result_type[market] == "LOSS":
                            msg += f"\n\n💡 *AI Tip: ไม้ {label} ที่แล้วแพ้ พิจารณา **ทบไม้ (Martingale x2)***"
                        msg += f"\n----------------------------------------\n🆔 run `{BOOT_ID}`"
                        sender(msg)

                        threading.Thread(
                            target=evaluate_trade_background,
                            args=(market, asset, symbols[asset], signal, signal_min),
                            daemon=True,
                        ).start()

                pending_signals = {"MAIN": None, "OTC": None}
                pending_minute = None

            time.sleep(0.5)
        except Exception as e:
            print(f"⚠️ Loop Error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()

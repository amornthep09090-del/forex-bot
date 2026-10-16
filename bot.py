import os
import time
from datetime import datetime, timezone, timedelta
import requests
import pandas as pd
import numpy as np
import yfinance as yf
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# ดึงค่าความปลอดภัยจากระบบ Railway
LINE_ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN", "").strip()
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()

if not LINE_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    print("❌ กรุณาตั้งค่า LINE_ACCESS_TOKEN และ LINE_CHANNEL_SECRET ใน Railway Variables ให้ครบถ้วน")
    exit(1)

line_bot_api = LineBotApi(LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
app = Flask(__name__)

tz_thai = timezone(timedelta(hours=7))

# สถานะบอท (เริ่มต้นให้ทำงานอัตโนมัติ)
bot_running = True

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

# สถิติแยกตามคู่เงิน และสถิติรวมทั้งหมด (Global Stats)
stats = {asset: {"win": 0, "loss": 0, "tie": 0, "consecutive_loss": 0, "cooldown_until": 0} for asset in SYMBOLS}
global_stats = {"win": 0, "loss": 0, "tie": 0}

# รับข้อความสั่งการจาก LINE (Webhook)
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    global bot_running
    text = event.message.text.strip().lower()
    reply_text = ""

    if text in ["เปิด", "start", "on"]:
        bot_running = True
        reply_text = "🟢 บอทถูกเปิดใช้งาน (Start) เรียบร้อยแล้ว กำลังเฝ้ากราฟปกติครับ"
    elif text in ["ปิด", "stop", "off"]:
        bot_running = False
        reply_text = "🔴 บอทถูกระงับการทำงานชั่วคราว (Stop) แล้ว พิมพ์ 'เปิด' เพื่อเริ่มใหม่"
    elif text in ["สถิติ", "stats", "winrate"]:
        total = global_stats["win"] + global_stats["loss"]
        wr = (global_stats["win"] / total * 100) if total > 0 else 0.0
        reply_text = (
            f"📊 สถิติรวมทั้งหมด (Global Stats)\n"
            f"━━━━━━━━━━━━━━\n"
            f"🏆 ชนะ: {global_stats['win']} ครั้ง\n"
            f"❌ แพ้: {global_stats['loss']} ครั้ง\n"
            f"⚖️ เสมอ: {global_stats['tie']} ครั้ง\n"
            f"🔥 Win Rate รวม: {wr:.2f}%"
        )
    else:
        reply_text = "🤖 คำสั่งที่ใช้ได้:\n- พิมพ์ 'เปิด' เพื่อเริ่มบอท\n- พิมพ์ 'ปิด' เพื่อหยุดบอท\n- พิมพ์ 'สถิติ' ดูผลงานรวม"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

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

# ฟังก์ชันรันลูปบอทเบื้องหลัง (Background Scanner)
def background_bot_loop():
    global bot_running
    last_scan = None
    print("✅ ระบบสแกนกราฟเบื้องหลังเริ่มทำงานแล้ว")
    
    while True:
        try:
            if not bot_running:
                time.sleep(2)
                continue

            now_utc = pd.Timestamp.now(tz="UTC")
            now_min = now_utc.floor("min")

            if now_utc.second >= 1 and last_scan != now_min:
                last_scan = now_min
                prev_min = now_min - pd.Timedelta(minutes=1)
                current_time = time.time()

                for asset in SYMBOLS:
                    if not bot_running:
                        break
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
                        print(f"[{time_str}] พบสัญญาณ {asset}: {signal}")

                        target_close = now_min + pd.Timedelta(minutes=1, seconds=5)
                        while pd.Timestamp.now(tz="UTC") < target_close:
                            time.sleep(0.5)

                        res_df = wait_for_candle(asset, now_min, timeout=15)
                        if res_df is not None and now_min in res_df.index:
                            eval_c = res_df.loc[now_min]
                            open_p, close_p = float(eval_c["open"]), float(eval_c["close"])

                            if ("BUY" in signal and close_p > open_p) or ("SELL" in signal and close_p < open_p):
                                stats[asset]["win"] += 1
                                global_stats["win"] += 1
                                stats[asset]["consecutive_loss"] = 0
                            elif close_p == open_p:
                                stats[asset]["tie"] += 1
                                global_stats["tie"] += 1
                            else:
                                stats[asset]["loss"] += 1
                                global_stats["loss"] += 1
                                stats[asset]["consecutive_loss"] += 1

                                if stats[asset]["consecutive_loss"] >= 2:
                                    stats[asset]["cooldown_until"] = time.time() + 900

                    time.sleep(0.2)
            else:
                time.sleep(0.5)
        except Exception as e:
            print(f"⚠️ Loop Error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    import threading
    t = threading.Thread(target=background_bot_loop)
    t.daemon = True
    t.start()
    
    # ดึงค่าพอร์ตจากระบบ Railway โดยตรง (ตั้งค่าให้รองรับพอร์ต 8080)
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

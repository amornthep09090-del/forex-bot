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
DISCORD_NEWS_WEBHOOK = os.getenv("DISCORD_NEWS_WEBHOOK", "").strip()  # ช่องวิเคราะห์ข่าว (ไม่บังคับ)

# ปรับได้ผ่าน Railway Variables (ไม่ตั้งก็ใช้ค่าเริ่มต้น)
MAX_DATA_AGE_SEC = int(os.getenv("MAX_DATA_AGE_SEC", "240"))    # แท่งล่าสุดเก่าเกินนี้ = ข้อมูลไม่สด
MIN_FRESH_RATIO = float(os.getenv("MIN_FRESH_RATIO", "0.5"))    # ต้องมีคู่เงินสดอย่างน้อยกี่ % ถึงถือว่าดึงข้อมูลได้
PAIR_COOLDOWN_SEC = int(os.getenv("PAIR_COOLDOWN_SEC", "180"))  # กันส่งคู่เดิมซ้ำถี่ ๆ
FAIL_TO_PAUSE = int(os.getenv("FAIL_TO_PAUSE", "2"))            # ดึงข้อมูลพลาดติดกันกี่รอบถึงพัก
OK_TO_RESUME = int(os.getenv("OK_TO_RESUME", "2"))              # ดึงได้ปกติติดกันกี่รอบถึงกลับมา
SCAN_SECOND = 45                                                # วินาทีที่เริ่มสแกนล่วงหน้า

NEWS_FILTER = os.getenv("NEWS_FILTER", "1") == "1"              # 1 = ไม่ออกสัญญาณช่วงข่าวแรง
NEWS_BEFORE_MIN = int(os.getenv("NEWS_BEFORE_MIN", "15"))       # หยุดก่อนข่าวกี่นาที
NEWS_AFTER_MIN = int(os.getenv("NEWS_AFTER_MIN", "30"))         # หยุดหลังข่าวกี่นาที
NEWS_BRIEF_HOUR = int(os.getenv("NEWS_BRIEF_HOUR", "7"))        # ส่งสรุปข่าวกี่โมง (เวลาไทย)
NEWS_BRIEF_ON_BOOT = os.getenv("NEWS_BRIEF_ON_BOOT", "0") == "1"  # 1 = ส่งสรุปข่าวทันทีตอนบอทเริ่ม (ไว้ทดสอบช่อง)

if not DISCORD_MAIN_WEBHOOK or not DISCORD_OTC_WEBHOOK or not DISCORD_RESULT_WEBHOOK:
    print("❌ กรุณาตั้งค่า Webhook ให้ครบทั้ง 3 ช่องใน Railway Variables")
    raise SystemExit(1)

tz_thai = timezone(timedelta(hours=7))
BOOT_ID = os.urandom(2).hex()  # ไว้เช็คว่ามีบอทรันซ้อนกันหลายตัวหรือไม่
THAI_DAYS = ["จันทร์", "อังคาร", "พุธ", "พฤหัสฯ", "ศุกร์", "เสาร์", "อาทิตย์"]

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
CURRENCIES = sorted({a[:3] for a in STANDARD_SYMBOLS} | {a[3:6] for a in STANDARD_SYMBOLS})

stats = {asset: {"win": 0, "loss": 0, "tie": 0, "consecutive_loss": 0, "cooldown_until": 0} for asset in ALL_SYMBOLS}
global_stats = {"win": 0, "loss": 0, "tie": 0}
last_result_type = {"MAIN": "WIN", "OTC": "WIN"}
last_signal_time = {}
stats_lock = threading.Lock()

DIR_TEXT = {"BUY": "🟢 BUY (CALL)", "SELL": "🔴 SELL (PUT)"}
COLOR_GREEN, COLOR_RED, COLOR_GRAY, COLOR_ORANGE = 0x2ECC71, 0xE74C3C, 0x95A5A6, 0xF39C12


# ======================= ส่ง Discord =======================
def make_embed(title, color, description=None, fields=None, footer=None):
    e = {"title": title, "color": color}
    if description:
        e["description"] = description
    if fields:
        e["fields"] = [{"name": n, "value": v, "inline": i} for n, v, i in fields]
    if footer:
        e["footer"] = {"text": footer}
    return e


def _post(url, tag, content=None, embed=None):
    if not url:
        return
    payload = {"allowed_mentions": {"parse": []}}
    if content:
        payload["content"] = content
    if embed:
        payload["embeds"] = [embed]
    for _ in range(2):
        try:
            r = requests.post(url, json=payload, timeout=10)
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


def send_main(content=None, embed=None):
    _post(DISCORD_MAIN_WEBHOOK, "Main", content, embed)


def send_otc(content=None, embed=None):
    _post(DISCORD_OTC_WEBHOOK, "OTC", content, embed)


def send_result(content=None, embed=None):
    _post(DISCORD_RESULT_WEBHOOK, "Result", content, embed)


def send_news(content=None, embed=None):
    _post(DISCORD_NEWS_WEBHOOK, "News", content, embed)


def broadcast(embed):
    send_main(embed=embed)
    send_otc(embed=embed)
    send_result(embed=embed)


# ======================= ดึงข้อมูลราคา =======================
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


# ======================= ข่าวเศรษฐกิจ =======================
NEWS_URLS = [
    "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
]
NEWS_REFRESH_SEC = 3 * 3600   # รีเฟรชปฏิทินทุก 3 ชม.
NEWS_RETRY_SEC = 900          # ถ้าดึงพลาด ลองใหม่ทุก 15 นาที (แหล่งข้อมูลจำกัดจำนวนครั้ง)
news_state = {"events": None, "fetched_at": 0.0, "last_try": 0.0}


def _parse_event_time(s):
    ts = pd.Timestamp(s)
    if ts.tzinfo is None:  # ถ้าไม่มี timezone ให้ถือเป็นเวลา US/Eastern
        ts = ts.tz_localize("America/New_York", ambiguous=True, nonexistent="shift_forward")
    return ts.tz_convert("UTC")


def fetch_news_calendar():
    last_err = None
    for url in NEWS_URLS:
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            r.raise_for_status()
            out = []
            for e in r.json():
                impact = str(e.get("impact", "")).strip().capitalize()
                if impact not in ("High", "Medium"):
                    continue
                ccy = str(e.get("country") or e.get("currency") or "").strip().upper()
                if ccy not in CURRENCIES:
                    continue
                try:
                    ts = _parse_event_time(e["date"])
                except Exception:
                    continue
                out.append({"time": ts, "ccy": ccy, "impact": impact, "title": str(e.get("title", "")).strip()})
            return sorted(out, key=lambda x: x["time"])
        except Exception as ex:
            last_err = ex
    raise RuntimeError(f"ดึงปฏิทินข่าวไม่ได้: {last_err}")


def refresh_news():
    now = time.time()
    if news_state["events"] is not None and now - news_state["fetched_at"] < NEWS_REFRESH_SEC:
        return
    if now - news_state["last_try"] < NEWS_RETRY_SEC:
        return
    news_state["last_try"] = now
    try:
        events = fetch_news_calendar()
        news_state["events"] = events
        news_state["fetched_at"] = now
        print(f"📰 โหลดปฏิทินข่าวแล้ว {len(events)} รายการ (High/Medium)")
    except Exception as e:
        print(f"⚠️ {e}")


def news_blocked(asset, at_time):
    """คืนข่าวแรงที่ชนกับเวลา at_time (ก่อน NEWS_BEFORE_MIN / หลัง NEWS_AFTER_MIN นาที) ไม่มีคืน None"""
    events = news_state["events"]
    if not events:
        return None
    ccys = {asset[:3], asset[3:6]}
    for e in events:
        if e["impact"] != "High" or e["ccy"] not in ccys:
            continue
        diff_min = (at_time - e["time"]).total_seconds() / 60
        if -NEWS_BEFORE_MIN <= diff_min <= NEWS_AFTER_MIN:
            return e
    return None


def _event_time_label(e, today_th):
    t = e["time"].tz_convert(tz_thai)
    s = t.strftime("%H:%M")
    if t.date() != today_th:
        s += " (พรุ่งนี้)"
    return s


def build_news_embed(now_utc):
    events = news_state["events"] or []
    end = now_utc + pd.Timedelta(hours=24)
    todays = [e for e in events if now_utc <= e["time"] < end]
    high = [e for e in todays if e["impact"] == "High"]
    med = [e for e in todays if e["impact"] == "Medium"]
    high_ccy = {e["ccy"] for e in high}
    med_ccy = {e["ccy"] for e in med} - high_ccy
    busy = high_ccy | med_ccy

    pairs = list(STANDARD_SYMBOLS)
    clash = [p for p in pairs if p[:3] in high_ccy and p[3:6] in high_ccy]
    calm = [p for p in pairs if p[:3] not in busy and p[3:6] not in busy]
    if len(calm) < 3:  # ถ้าคู่ปลอดข่าวน้อยเกิน ผ่อนเงื่อนไขเหลือแค่ไม่มีข่าวแรง
        calm = [p for p in pairs if p[:3] not in high_ccy and p[3:6] not in high_ccy]

    now_th = now_utc.tz_convert(tz_thai)
    today_th = now_th.date()
    title = f"📰 สรุปข่าววันนี้ · {THAI_DAYS[now_th.weekday()]} {now_th.strftime('%d/%m')}"

    fields = []
    if high:
        lines = [f"`{_event_time_label(e, today_th)}` **{e['ccy']}** {e['title'][:38]}" for e in high[:10]]
        if len(high) > 10:
            lines.append(f"…และอีก {len(high) - 10} ข่าว")
        fields.append(("🔴 ข่าวแรง (เวลาไทย)", "\n".join(lines), False))
    if med_ccy:
        fields.append(("🟠 ข่าวกลาง · ระวังสกุล", ", ".join(sorted(med_ccy)), False))
    if clash:
        fields.append(("⛔ ข่าวชนกัน 2 สกุล · เลี่ยง", ", ".join(clash[:12]), False))
    fields.append((
        "✅ น่าโฟกัส (ไม่มีข่าวสำคัญ)",
        ", ".join(calm[:12]) if calm else "ไม่มี — วันนี้ทุกคู่มีข่าว ระวังเป็นพิเศษ",
        False,
    ))

    if high:
        desc = "ช่วงข่าวแรงราคามักวิ่งแรงและเด้งกลับยาก บอทจะไม่ส่งสัญญาณคู่ที่เกี่ยวข้องในช่วงนั้นให้เอง"
    else:
        desc = "ไม่มีข่าวแรงใน 24 ชม. ข้างหน้า ตลาดน่าจะค่อนข้างเงียบ"

    footer = (
        f"สรุปจากตารางข่าว ไม่ได้ทำนายทิศทาง · บอทเว้น {NEWS_BEFORE_MIN} นาทีก่อน–{NEWS_AFTER_MIN} นาทีหลังข่าวแรง"
        if NEWS_FILTER else "สรุปจากตารางข่าว ไม่ได้ทำนายทิศทาง"
    )
    return make_embed(title, COLOR_RED if high else COLOR_GREEN, desc, fields, footer)


def news_worker():
    """เธรดแยก: รีเฟรชปฏิทินข่าว + ส่งสรุปข่าวรายวัน"""
    last_brief_date = None
    warned_date = None
    force = NEWS_BRIEF_ON_BOOT
    while True:
        try:
            refresh_news()
            now_th = datetime.now(tz_thai)
            today = now_th.date()
            due = (
                now_th.weekday() < 5
                and NEWS_BRIEF_HOUR <= now_th.hour < NEWS_BRIEF_HOUR + 2
                and last_brief_date != today
            )
            if DISCORD_NEWS_WEBHOOK and (due or force):
                if news_state["events"] is None:
                    if warned_date != today:
                        warned_date = today
                        send_news(embed=make_embed(
                            "⚠️ ดึงปฏิทินข่าวไม่ได้", COLOR_ORANGE,
                            "วันนี้บอทยังไม่มีตัวกรองข่าว จะลองดึงใหม่อัตโนมัติ"))
                else:
                    send_news(embed=build_news_embed(pd.Timestamp.now(tz="UTC")))
                    last_brief_date = today
                    force = False
        except Exception as e:
            print(f"⚠️ News worker error: {e}")
        time.sleep(60)


# ======================= วิเคราะห์สัญญาณ =======================
def analyze_market_flexible(df):
    """คืน (signal, price, rsi, score) โดย signal เป็น 'BUY' / 'SELL' / None"""
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
        signal = "BUY"
        score = abs(38 - rsi_val) + 1
    elif (last["close"] >= last["bb_upper"]) or (rsi_val > 62):
        signal = "SELL"
        score = abs(rsi_val - 62) + 1

    return signal, price, rsi_val, score


def build_pending(data, scan_min):
    """เลือกสัญญาณที่ดีที่สุดของแต่ละตลาด จากข้อมูลที่ดึงมาแล้ว"""
    pending = {"MAIN": None, "OTC": None}
    now_ts = time.time()
    now_utc = pd.Timestamp.now(tz="UTC")
    entry_time = scan_min + pd.Timedelta(minutes=1)
    for market, symbols in (("MAIN", STANDARD_SYMBOLS), ("OTC", OTC_SYMBOLS)):
        best = -1
        for asset, code in symbols.items():
            if now_ts < stats[asset]["cooldown_until"]:
                continue
            if now_ts < last_signal_time.get(asset, 0) + PAIR_COOLDOWN_SEC:
                continue
            if NEWS_FILTER and news_blocked(asset, entry_time):
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
    label = "Main" if market_type == "MAIN" else "OTC"
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
            send_result(embed=make_embed(
                f"⚠️ ประเมินผลไม่ได้ · {asset}", COLOR_GRAY,
                "ดึงแท่งเทียนไม่ได้ ไม่นับเข้าสถิติ", footer=label))
            return

        open_p, close_p = float(eval_c["open"]), float(eval_c["close"])

        with stats_lock:
            if (signal == "BUY" and close_p > open_p) or (signal == "SELL" and close_p < open_p):
                icon, word, color = "✅", "WIN", COLOR_GREEN
                stats[asset]["win"] += 1
                global_stats["win"] += 1
                stats[asset]["consecutive_loss"] = 0
                last_result_type[market_type] = "WIN"
                advice = "เดินเงินปกติ ไม่ต้องทบ"
            elif close_p == open_p:
                icon, word, color = "⚖️", "TIE", COLOR_GRAY
                stats[asset]["tie"] += 1
                global_stats["tie"] += 1
                last_result_type[market_type] = "TIE"
                advice = "เสมอ ใช้ทุนเท่าเดิม"
            else:
                icon, word, color = "❌", "LOSS", COLOR_RED
                stats[asset]["loss"] += 1
                global_stats["loss"] += 1
                stats[asset]["consecutive_loss"] += 1
                last_result_type[market_type] = "LOSS"
                if stats[asset]["consecutive_loss"] >= 2:
                    stats[asset]["cooldown_until"] = time.time() + 1800
                    advice = "🚨 แพ้ติดกัน 2 ไม้ พักคู่นี้ 30 นาที"
                else:
                    advice = "แพ้ไม้แรก พิจารณาทบไม้ (Martingale x2)"

            s = dict(stats[asset])
            g = dict(global_stats)

        tot = s["win"] + s["loss"]
        wr = (s["win"] / tot * 100) if tot > 0 else 0.0
        tot_g = g["win"] + g["loss"]
        wr_g = (g["win"] / tot_g * 100) if tot_g > 0 else 0.0

        sig_time = signal_min.tz_convert(tz_thai).strftime("%H:%M")
        send_result(embed=make_embed(
            f"{icon} {word} · {asset}", color, advice,
            fields=[
                (f"📈 {asset}", f"`{s['win']}W – {s['loss']}L` · **{wr:.0f}%**", True),
                ("🌐 รวมทั้งหมด", f"`{g['win']}W – {g['loss']}L` · **{wr_g:.0f}%**", True),
            ],
            footer=f"{label} · {signal} · ไม้ {sig_time}",
        ))
    except Exception as e:
        print(f"⚠️ Evaluation Error: {e}")
        try:
            send_result(embed=make_embed(
                f"⚠️ ประเมินผลผิดพลาด · {asset}", COLOR_GRAY, f"`{e}` — ไม่นับเข้าสถิติ"))
        except Exception:
            pass


# ======================= ลูปหลัก =======================
def main():
    print("✅ บอทพร้อมใช้งานแล้ว")
    send_main(f"🌐 **Main Bot พร้อมทำงาน** · run `{BOOT_ID}`")
    send_otc(f"📊 **OTC Bot พร้อมทำงาน** · run `{BOOT_ID}`")
    send_result(f"🏆 **Stats Bot พร้อมรายงานผล** · run `{BOOT_ID}`")
    if DISCORD_NEWS_WEBHOOK:
        send_news(f"📰 **News Bot พร้อมทำงาน** · ส่งสรุปข่าวทุกวันจันทร์–ศุกร์ {NEWS_BRIEF_HOUR:02d}:00 น. · run `{BOOT_ID}`")
    else:
        print("ℹ️ ยังไม่ได้ตั้ง DISCORD_NEWS_WEBHOOK — ข้ามการส่งสรุปข่าว (ตัวกรองข่าวยังทำงานปกติ)")

    threading.Thread(target=news_worker, daemon=True).start()

    health = {"paused": False, "fail": 0, "ok": 0, "skip": 0}
    last_pre_scan = None
    last_action_min = None
    pending_signals = {"MAIN": None, "OTC": None}
    pending_minute = None

    def pause(reason):
        if not health["paused"]:
            health["paused"] = True
            print(f"⏸️ พักบอท: {reason}")
            broadcast(make_embed("⏸️ บอทพักชั่วคราว", COLOR_ORANGE,
                                 f"{reason}\nจะกลับมาทำงานเองเมื่อข้อมูลปกติ"))

    def resume():
        if health["paused"]:
            health["paused"] = False
            print("▶️ กลับมาทำงาน")
            broadcast(make_embed("▶️ บอทกลับมาทำงานแล้ว", COLOR_GREEN, "ข้อมูลตลาดพร้อมใช้งานปกติ"))

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
                    for market, sender in (("MAIN", send_main), ("OTC", send_otc)):
                        p = pending_signals[market]
                        if p:
                            sender(f"⏳ **{p['asset']}** · {DIR_TEXT[p['signal']]} · เตรียมกด **{pre_time_str}**")

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
                        ("MAIN", STANDARD_SYMBOLS, send_main, "Main"),
                        ("OTC", OTC_SYMBOLS, send_otc, "OTC"),
                    ):
                        p = pending_signals[market]
                        if not p:
                            continue
                        asset, signal = p["asset"], p["signal"]
                        last_signal_time[asset] = time.time()

                        decimals = 3 if "JPY" in asset else 5
                        desc = "⏱️ หมดอายุ **1 นาที**"
                        if last_result_type[market] == "LOSS":
                            desc += "\n💡 ไม้ที่แล้วแพ้ พิจารณาทบไม้ (Martingale x2)"
                        sender(embed=make_embed(
                            f"{DIR_TEXT[signal]} · {asset}",
                            COLOR_GREEN if signal == "BUY" else COLOR_RED,
                            desc,
                            fields=[
                                ("⏰ เวลา", f"`{action_time_str}`", True),
                                ("💵 ราคาเปิด", f"`{p['price']:.{decimals}f}`", True),
                                ("📉 RSI", f"`{p['rsi']:.1f}`", True),
                            ],
                            footer=f"{label} · run {BOOT_ID}",
                        ))

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

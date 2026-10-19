import os
import time
import json
import math
import signal
import threading
from collections import deque
from datetime import datetime, timezone, timedelta

import requests
import pandas as pd
import numpy as np
import yfinance as yf

# ======================= ตั้งค่า =======================
DISCORD_MAIN_WEBHOOK = os.getenv("DISCORD_MAIN_WEBHOOK", "").strip()
DISCORD_RESULT_WEBHOOK = os.getenv("DISCORD_RESULT_WEBHOOK", "").strip()
DISCORD_NEWS_WEBHOOK = os.getenv("DISCORD_NEWS_WEBHOOK", "").strip()

DATA_DIR = os.getenv("DATA_DIR", "./data")
DATA_DIR_IS_DEFAULT = "DATA_DIR" not in os.environ

MAX_DATA_AGE_SEC = int(os.getenv("MAX_DATA_AGE_SEC", "300"))
MIN_FRESH_RATIO = float(os.getenv("MIN_FRESH_RATIO", "0.35"))
PAIR_COOLDOWN_SEC = int(os.getenv("PAIR_COOLDOWN_SEC", "180"))
FAIL_TO_PAUSE = int(os.getenv("FAIL_TO_PAUSE", "3"))
OK_TO_RESUME = int(os.getenv("OK_TO_RESUME", "1"))
SCAN_SECOND = 45

NEWS_FILTER = os.getenv("NEWS_FILTER", "1") == "1"
NEWS_BEFORE_MIN = int(os.getenv("NEWS_BEFORE_MIN", "15"))
NEWS_AFTER_MIN = int(os.getenv("NEWS_AFTER_MIN", "30"))
NEWS_BRIEF_HOUR = int(os.getenv("NEWS_BRIEF_HOUR", "7"))
NEWS_BRIEF_ON_BOOT = os.getenv("NEWS_BRIEF_ON_BOOT", "0") == "1"

# --- ตัวโมเดลเรียนรู้ ---
PAYOUT = float(os.getenv("PAYOUT", "0.85"))
WARMUP_SAMPLES = int(os.getenv("WARMUP_SAMPLES", "500"))
LEARNING_RATE = float(os.getenv("LEARNING_RATE", "0.002"))
MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "0.52"))
TOP_PERCENT = float(os.getenv("TOP_PERCENT", "20"))
REQUIRE_EDGE = os.getenv("REQUIRE_EDGE", "0") == "1"
MIN_ATR_PCT = float(os.getenv("MIN_ATR_PCT", "0.00001"))

if not DISCORD_MAIN_WEBHOOK or not DISCORD_RESULT_WEBHOOK:
    print("❌ กรุณาตั้งค่า DISCORD_MAIN_WEBHOOK และ DISCORD_RESULT_WEBHOOK ใน Railway Variables")
    raise SystemExit(1)

tz_thai = timezone(timedelta(hours=7))
BOOT_ID = os.urandom(2).hex()
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
UNIQUE_TICKERS = sorted(set(STANDARD_SYMBOLS.values()))
CURRENCIES = sorted({a[:3] for a in STANDARD_SYMBOLS} | {a[3:6] for a in STANDARD_SYMBOLS})

stats = {a: {"win": 0, "loss": 0, "tie": 0, "consecutive_loss": 0, "cooldown_until": 0} for a in STANDARD_SYMBOLS}
global_stats = {"win": 0, "loss": 0, "tie": 0}
last_signal_time = {}
stats_lock = threading.Lock()
trade_log_lock = threading.Lock()

DATA_CACHE = {}
CACHE_LOCK = threading.Lock()

DIR_TEXT = {"BUY": "🟢 BUY (CALL)", "SELL": "🔴 SELL (PUT)"}
COLOR_GREEN, COLOR_RED, COLOR_GRAY, COLOR_ORANGE, COLOR_BLUE = 0x2ECC71, 0xE74C3C, 0x95A5A6, 0xF39C12, 0x3498DB


def breakeven():
    return 1.0 / (1.0 + PAYOUT)


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
            if r.status_code == 429:
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


def send_result(content=None, embed=None):
    _post(DISCORD_RESULT_WEBHOOK, "Result", content, embed)


def send_news(content=None, embed=None):
    _post(DISCORD_NEWS_WEBHOOK, "News", content, embed)


def send_analysis(content=None, embed=None):
    _post(DISCORD_NEWS_WEBHOOK or DISCORD_RESULT_WEBHOOK, "Analysis", content, embed)


def broadcast(embed):
    send_main(embed=embed)
    send_result(embed=embed)


# ======================= ดึงข้อมูลราคา =======================
def _clean_df(df):
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)
    req = ["open", "high", "low", "close"]
    if not all(c in df.columns for c in req):
        return None
    df = df[req].dropna(how="any")
    if df.empty:
        return None
    df = df.astype(float)
    idx = df.index
    df.index = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    df.index = df.index.floor("min")
    df = df[~df.index.duplicated(keep="last")].sort_index()
    
    prev_c = df["close"].shift(1)
    same_oc = (df["open"] == df["close"]) & prev_c.notna()
    df.loc[same_oc, "open"] = prev_c[same_oc]
    df["high"] = df[["open", "high", "close"]].max(axis=1)
    df["low"] = df[["open", "low", "close"]].min(axis=1)
    return df if len(df) >= 40 else None


def get_candles(symbol_code, retries=2):
    for i in range(retries):
        try:
            df = yf.Ticker(symbol_code).history(period="2d", interval="1m", timeout=10)
            df = _clean_df(df)
            if df is not None:
                with CACHE_LOCK:
                    DATA_CACHE[symbol_code] = df
                return df
        except Exception as e:
            print(f"⚠️ get_candles {symbol_code}: {e}")
        time.sleep(1 + i)
    return None


def fetch_all(tickers):
    out = {}
    try:
        raw = yf.download(
            tickers=tickers,
            period="2d",
            interval="1m",
            group_by="ticker",
            threads=True,
            progress=False,
            timeout=15
        )
        if raw is not None and not raw.empty:
            if len(tickers) == 1:
                t = tickers[0]
                df = _clean_df(raw)
                if df is not None:
                    out[t] = df
            else:
                lvl0 = set(raw.columns.get_level_values(0))
                for t in tickers:
                    if t in lvl0:
                        df = _clean_df(raw[t])
                        if df is not None:
                            out[t] = df
    except Exception as e:
        print(f"⚠️ Batch download error: {e}")

    missing = [t for t in tickers if t not in out]
    for t in missing[:5]:
        df = get_candles(t, retries=1)
        if df is not None:
            out[t] = df

    with CACHE_LOCK:
        for k, v in out.items():
            DATA_CACHE[k] = v
        return dict(DATA_CACHE)


def is_fresh(df, now_utc=None):
    if df is None or len(df) < 40:
        return False
    now_utc = now_utc or pd.Timestamp.now(tz="UTC")
    age = (now_utc - df.index[-1]).total_seconds()
    if age > MAX_DATA_AGE_SEC:
        return False
    if df["close"].tail(20).nunique() <= 1:
        return False
    return True


def market_closed_by_calendar(now_utc):
    wd = now_utc.weekday()
    if wd == 5:
        return True
    if wd == 4 and now_utc.hour >= 22:
        return True
    if wd == 6 and now_utc.hour < 21:
        return True
    return False


# ======================= ข่าวเศรษฐกิจ =======================
NEWS_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.json",
]
NEWS_REFRESH_SEC = 3 * 3600
NEWS_RETRY_SEC = 900
news_state = {"events": None, "fetched_at": 0.0, "last_try": 0.0}


def _parse_event_time(s):
    ts = pd.Timestamp(s)
    if ts.tzinfo is None:
        ts = ts.tz_localize("America/New_York", ambiguous=True, nonexistent="shift_forward")
    return ts.tz_convert("UTC")


def fetch_news_calendar():
    last_err = None
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    for url in NEWS_URLS:
        try:
            r = requests.get(url, headers=headers, timeout=15)
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


def news_near(asset, at_time, minutes=30):
    events = news_state["events"]
    if not events:
        return None
    ccys = {asset[:3], asset[3:6]}
    for e in events:
        if e["ccy"] not in ccys:
            continue
        if abs((at_time - e["time"]).total_seconds()) / 60 <= minutes:
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
    if len(calm) < 3:
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


# ======================= ฟีเจอร์ (12 ตัวชี้วัด) + โมเดลเรียนรู้ออนไลน์ =======================
FEATURES = [
    "z", "bb_pos", "rsi7", "rsi14", "stoch_k",
    "ret1", "ret3", "wick", "streak",
    "trend", "dist50", "macd_h"
]
PRIOR_W = np.array([
    -0.07, -0.06, -0.05, -0.03, -0.04,
    -0.03, -0.02,  0.05, -0.02,
     0.01, -0.01,  0.02
])
L2_PULL = 0.015
GUARD_TAGS = ["against_trend", "streak_against", "vol_spike", "quiet_session", "near_news"]

FEATURE_TEXT = {
    "z": ("ราคาพุ่งทะลุค่าเฉลี่ย (Z-Score)", "ราคาร่วงหลุดค่าเฉลี่ย (Z-Score)"),
    "bb_pos": ("ชนกรอบบน Bollinger Band", "ชนกรอบล่าง Bollinger Band"),
    "rsi7": ("RSI(7) เข้าเขตซื้อมากเกินไป", "RSI(7) เข้าเขตขายมากเกินไป"),
    "rsi14": ("RSI(14) สูง", "RSI(14) ต่ำ"),
    "stoch_k": ("Stochastic สูงสุดขีด", "Stochastic ต่ำสุดขีด"),
    "ret1": ("แท่งปัจจุบันพุ่งแรง", "แท่งปัจจุบันร่วงแรง"),
    "ret3": ("3 แท่งล่าสุดขึ้นต่อเนื่อง", "3 แท่งล่าสุดลงต่อเนื่อง"),
    "wick": ("ไส้เทียนล่างยาว (มีแรงซื้อดันกลับ)", "ไส้เทียนบนยาว (มีแรงขายตบลง)"),
    "streak": ("แท่งเขียวติดต่อกัน", "แท่งแดงติดต่อกัน"),
    "trend": ("เทรนด์ขาขึ้น (EMA20>EMA50)", "เทรนด์ขาลง (EMA20<EMA50)"),
    "dist50": ("ราคาห่างเหนือ EMA50 มาก", "ราคาห่างใต้ EMA50 มาก"),
    "macd_h": ("โมเมนตัม MACD หนุนขาขึ้น", "โมเมนตัม MACD หนุนขาลง"),
}
TAG_SHORT = {
    "against_trend": "สวนเทรนด์",
    "streak_against": "สวนแท่งที่วิ่งต่อเนื่อง",
    "vol_spike": "ผันผวนพุ่ง",
    "quiet_session": "ช่วงตลาดเงียบ",
    "near_news": "ใกล้ข่าว",
    "low_conf": "ความมั่นใจต่ำ",
    "noise": "แพ้เฉียด (ราคาขยับน้อย)",
    "big_move": "แท่งวิ่งแรงสวนทาง",
}


class OnlineModel:
    def __init__(self):
        self.lock = threading.RLock()
        self.w = PRIOR_W.copy()
        self.b = 0.0
        self.n = 0
        self.acc_all = deque(maxlen=3000)
        self.acc_hc = deque(maxlen=1000)
        self.tag_hist = {t: deque(maxlen=1000) for t in GUARD_TAGS}
        self.conf_hist = deque(maxlen=500)

    def predict(self, x):
        z = float(self.w @ x + self.b)
        z = max(-30.0, min(30.0, z))
        return 1.0 / (1.0 + math.exp(-z))

    def update(self, x, y):
        with self.lock:
            p = self.predict(x)
            err = p - y
            self.w = self.w - LEARNING_RATE * (err * x + L2_PULL * (self.w - PRIOR_W))
            self.b = self.b - LEARNING_RATE * err
            self.n += 1

    def to_dict(self):
        with self.lock:
            return {
                "w": [float(v) for v in self.w], "b": float(self.b), "n": int(self.n),
                "acc_all": list(self.acc_all), "acc_hc": list(self.acc_hc),
                "tag_hist": {k: list(v) for k, v in self.tag_hist.items()},
                "conf_hist": [float(v) for v in self.conf_hist],
            }

    def from_dict(self, d):
        with self.lock:
            w = np.array(d.get("w", []), dtype=float)
            if w.shape == PRIOR_W.shape:
                self.w = w
            else:
                self.w = PRIOR_W.copy()
            self.b = float(d.get("b", 0.0))
            self.n = int(d.get("n", 0))
            self.acc_all = deque(d.get("acc_all", []), maxlen=3000)
            self.acc_hc = deque(d.get("acc_hc", []), maxlen=1000)
            for k in GUARD_TAGS:
                self.tag_hist[k] = deque(d.get("tag_hist", {}).get(k, []), maxlen=1000)
            self.conf_hist = deque(d.get("conf_hist", []), maxlen=500)


model = OnlineModel()
pending_labels = []
app_state = {"last_weekly": None}


def _rsi_wilder(close, n):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1.0 / n, min_periods=n, adjust=False).mean().iloc[-1]
    loss = (-delta.clip(upper=0)).ewm(alpha=1.0 / n, min_periods=n, adjust=False).mean().iloc[-1]
    if not (np.isfinite(gain) and np.isfinite(loss)):
        return None
    if loss == 0:
        return 100.0 if gain > 0 else 50.0
    return float(100.0 - (100.0 / (1.0 + gain / loss)))


def compute_features(df):
    d = df.tail(120)
    if len(d) < 35:
        return None
    o, h, l, c = d["open"], d["high"], d["low"], d["close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr14 = tr.ewm(alpha=1.0 / 14, min_periods=14, adjust=False).mean().iloc[-1]
    atr5 = tr.ewm(alpha=1.0 / 5, min_periods=5, adjust=False).mean().iloc[-1]
    atr30 = tr.ewm(alpha=1.0 / 30, min_periods=25, adjust=False).mean().iloc[-1]
    if not (np.isfinite(atr14) and atr14 > 0 and np.isfinite(atr30) and atr30 > 0 and np.isfinite(atr5)):
        return None

    sd20 = c.rolling(20).std().iloc[-1]
    sma20 = c.rolling(20).mean().iloc[-1]
    if not (np.isfinite(sd20) and sd20 > 0 and np.isfinite(sma20)):
        return None

    rsi7, rsi14 = _rsi_wilder(c, 7), _rsi_wilder(c, 14)
    if rsi7 is None or rsi14 is None:
        return None

    last = float(c.iloc[-1])
    z = (last - sma20) / sd20
    bb_pos = (last - sma20) / (2.0 * sd20)

    low14 = l.rolling(14).min().iloc[-1]
    high14 = h.rolling(14).max().iloc[-1]
    stoch_k = ((last - low14) / (high14 - low14) * 100.0) if (high14 > low14) else 50.0

    ema20 = float(c.ewm(span=20, adjust=False).mean().iloc[-1])
    ema50 = float(c.ewm(span=50, adjust=False).mean().iloc[-1])
    ema12_s = c.ewm(span=12, adjust=False).mean()
    ema26_s = c.ewm(span=26, adjust=False).mean()
    macd_line = ema12_s - ema26_s
    macd_sig = macd_line.ewm(span=9, adjust=False).mean()
    macd_h = float((macd_line.iloc[-1] - macd_sig.iloc[-1]) / atr14)

    ob, hb, lb, cb = float(o.iloc[-1]), float(h.iloc[-1]), float(l.iloc[-1]), float(c.iloc[-1])
    rng = hb - lb
    wick = ((min(ob, cb) - lb) - (hb - max(ob, cb))) / rng if rng > 0 else 0.0

    dirs = np.sign((c - o).iloc[::-1].values)
    streak = 0
    if len(dirs) and dirs[0] != 0:
        first = dirs[0]
        for v in dirs:
            if v == first:
                streak += 1
            else:
                break
        streak = int(first) * min(streak, 6)

    raw = {
        "z": float(z),
        "bb_pos": float(bb_pos),
        "rsi7": float(rsi7),
        "rsi14": float(rsi14),
        "stoch_k": float(stoch_k),
        "ret1": float((cb - ob) / atr14),
        "ret3": float((last - float(c.iloc[-4])) / atr14),
        "wick": float(wick),
        "streak": int(streak),
        "trend": float((ema20 - ema50) / atr14),
        "dist50": float((last - ema50) / atr14),
        "macd_h": float(macd_h),
        "volratio": float(atr5 / atr30 - 1.0),
        "atr_abs": float(atr14),
        "atr_pct": float(atr14 / last) if last else 0.0,
        "price": last,
        "hour": int(d.index[-1].hour),
    }
    if not all(np.isfinite(v) for v in raw.values()):
        return None
    return raw


def to_vector(raw):
    return np.array([
        np.clip(raw["z"], -3.0, 3.0),
        np.clip(raw["bb_pos"], -1.5, 1.5),
        (raw["rsi7"] - 50.0) / 25.0,
        (raw["rsi14"] - 50.0) / 25.0,
        (raw["stoch_k"] - 50.0) / 25.0,
        np.clip(raw["ret1"], -3.0, 3.0),
        np.clip(raw["ret3"], -3.0, 3.0),
        np.clip(raw["wick"], -1.0, 1.0),
        np.clip(raw["streak"] / 3.0, -2.0, 2.0),
        np.clip(raw["trend"], -3.0, 3.0),
        np.clip(raw["dist50"], -3.0, 3.0),
        np.clip(raw["macd_h"], -2.0, 2.0),
    ], dtype=float)


def pre_tags(raw, direction, asset, entry_time):
    sgn = 1 if direction == "BUY" else -1
    tags = []
    if raw["trend"] * sgn < -1.0:
        tags.append("against_trend")
    if raw["streak"] * sgn <= -3:
        tags.append("streak_against")
    if raw["volratio"] > 0.6:
        tags.append("vol_spike")
    if raw["hour"] >= 21 or raw["hour"] < 6:
        tags.append("quiet_session")
    if news_near(asset, entry_time):
        tags.append("near_news")
    return tags


def why_text(x, direction):
    sgn = 1 if direction == "BUY" else -1
    with model.lock:
        contrib = model.w * x
    order = np.argsort(-(sgn * contrib))
    parts = []
    for i in order[:2]:
        if sgn * contrib[i] <= 0.005:
            break
        pos, neg = FEATURE_TEXT[FEATURES[i]]
        parts.append(pos if x[i] > 0 else neg)
    return " · ".join(parts) if parts else "ความได้เปรียบเชิงสถิติจากโมเดล"


def warmup_from_history(data):
    if model.n >= WARMUP_SAMPLES:
        return 0
    added = 0
    for asset, code in STANDARD_SYMBOLS.items():
        df = data.get(code)
        if df is None or len(df) < 100:
            continue
        for idx_pos in range(len(df) - 46, len(df) - 1):
            sub = df.iloc[:idx_pos + 1]
            nxt = df.iloc[idx_pos + 1]
            raw = compute_features(sub)
            if raw is None:
                continue
            o, c = float(nxt["open"]), float(nxt["close"])
            if o == c:
                o = float(sub["close"].iloc[-1])
            if c == o:
                continue
            x = to_vector(raw)
            p = model.predict(x)
            conf = p if p >= 0.5 else 1.0 - p
            direction = "BUY" if p >= 0.5 else "SELL"
            tags = pre_tags(raw, direction, asset, df.index[idx_pos + 1])
            y = 1.0 if c > o else 0.0
            correct = 1 if (p >= 0.5) == (y == 1.0) else 0
            with model.lock:
                model.acc_all.append(correct)
                if conf >= MIN_CONFIDENCE:
                    model.acc_hc.append(correct)
                for t in tags:
                    if t in model.tag_hist:
                        model.tag_hist[t].append(correct)
                model.update(x, y)
            added += 1
    if added > 0:
        save_all()
        print(f"⚡ Warmup จากกราฟย้อนหลังสำเร็จ +{added} ตัวอย่าง (รวม {model.n:,} ตัวอย่าง)")
    return added


def make_candidates(data, scan_min, now_utc):
    cands = []
    entry_time = scan_min + pd.Timedelta(minutes=1)
    for asset, code in STANDARD_SYMBOLS.items():
        df = data.get(code)
        if not is_fresh(df, now_utc):
            continue
        raw = compute_features(df)
        if raw is None:
            continue
        x = to_vector(raw)
        p = model.predict(x)
        direction = "BUY" if p >= 0.5 else "SELL"
        conf = p if p >= 0.5 else 1.0 - p
        tags = pre_tags(raw, direction, asset, entry_time)
        pending_labels.append({
            "asset": asset, "scan_min": scan_min, "x": x, "p": p, "conf": conf,
            "dir": direction, "tags": tags, "atr_abs": raw["atr_abs"],
            "ref_price": raw["price"],
        })
        cands.append({
            "asset": asset, "dir": direction, "conf": conf, "x": x, "raw": raw,
            "tags": tags, "price": raw["price"],
        })
    return cands


def learn_from_pending(data, now_utc):
    global pending_labels
    keep, trained = [], 0
    one_min = pd.Timedelta(minutes=1)
    for s in pending_labels:
        target = s["scan_min"] + one_min
        age = (now_utc - target).total_seconds()
        if age > 600:
            continue
        df = data.get(STANDARD_SYMBOLS[s["asset"]])
        if df is None or target not in df.index or not ((target + one_min) in df.index or age >= 110):
            keep.append(s)
            continue
        row = df.loc[target]
        o, c = float(row["open"]), float(row["close"])
        if c == o:
            o = float(s.get("ref_price", o))
        if c == o:
            continue
        y = 1.0 if c > o else 0.0
        correct = 1 if (s["p"] >= 0.5) == (y == 1.0) else 0
        with model.lock:
            model.acc_all.append(correct)
            if s["conf"] >= MIN_CONFIDENCE:
                model.acc_hc.append(correct)
            for t in s["tags"]:
                if t in model.tag_hist:
                    model.tag_hist[t].append(correct)
            model.update(s["x"], y)
        trained += 1
    pending_labels = keep
    return trained


def edge_status():
    with model.lock:
        hc = list(model.acc_hc)
    if len(hc) < 150:
        return True, None, len(hc)
    acc = sum(hc) / len(hc)
    return acc >= breakeven(), acc, len(hc)


def guard_blocks(tags):
    with model.lock:
        for t in tags:
            h = model.tag_hist.get(t)
            if h is not None and len(h) >= 250 and (sum(h) / len(h)) <= 0.48:
                return t
    return None


def select_best(cands, scan_min):
    now_ts = time.time()
    entry_time = scan_min + pd.Timedelta(minutes=1)
    best = None
    for c in sorted(cands, key=lambda c: -c["conf"]):
        asset = c["asset"]
        if now_ts < stats[asset]["cooldown_until"]:
            continue
        if now_ts < last_signal_time.get(asset, 0) + PAIR_COOLDOWN_SEC:
            continue
        if NEWS_FILTER and news_blocked(asset, entry_time):
            continue
        if c["raw"]["atr_pct"] < MIN_ATR_PCT:
            continue
        if guard_blocks(c["tags"]):
            continue
        sgn = 1 if c["dir"] == "BUY" else -1
        if c["raw"]["ret1"] * sgn < -2.2:
            continue
        best = c
        break
    if best is None:
        return None

    with model.lock:
        model.conf_hist.append(best["conf"])
        hist = list(model.conf_hist)
    thr = MIN_CONFIDENCE
    if len(hist) >= 60 and TOP_PERCENT < 100:
        thr = max(thr, float(np.percentile(hist, 100 - TOP_PERCENT)))
    if best["conf"] < thr:
        return None
    best = dict(best)
    best["why"] = why_text(best["x"], best["dir"])
    best["scan_min"] = scan_min
    return best


# ======================= บันทึก/โหลดข้อมูล =======================
MODEL_PATH = lambda: os.path.join(DATA_DIR, "model.json")
TRADES_PATH = lambda: os.path.join(DATA_DIR, "trades.jsonl")
STATE_PATH = lambda: os.path.join(DATA_DIR, "state.json")


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def save_all():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        _atomic_write(MODEL_PATH(), json.dumps(model.to_dict()))
        _atomic_write(STATE_PATH(), json.dumps(app_state))
    except Exception as e:
        print(f"⚠️ บันทึกข้อมูลไม่สำเร็จ: {e}")


def load_all():
    try:
        with open(MODEL_PATH(), "r", encoding="utf-8") as f:
            model.from_dict(json.load(f))
        print(f"🧠 โหลดโมเดลแล้ว (เรียนรู้ไป {model.n:,} ตัวอย่าง)")
    except FileNotFoundError:
        print("🧠 ยังไม่มีโมเดลเดิม เริ่มเรียนรู้ใหม่")
    except Exception as e:
        print(f"⚠️ โหลดโมเดลไม่สำเร็จ ({e}) เริ่มใหม่")
    try:
        with open(STATE_PATH(), "r", encoding="utf-8") as f:
            app_state.update(json.load(f))
    except Exception:
        pass


def log_trade(rec):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with trade_log_lock:
            with open(TRADES_PATH(), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"⚠️ บันทึกไม้ไม่สำเร็จ: {e}")


def load_trades():
    out = []
    try:
        with trade_log_lock:
            with open(TRADES_PATH(), "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except Exception:
                            pass
    except FileNotFoundError:
        pass
    return out


def rebuild_stats(trades):
    with stats_lock:
        for t in trades:
            a = t.get("asset")
            if a not in stats:
                continue
            res = t.get("res")
            if res == "WIN":
                stats[a]["win"] += 1
                global_stats["win"] += 1
                stats[a]["consecutive_loss"] = 0
            elif res == "LOSS":
                stats[a]["loss"] += 1
                global_stats["loss"] += 1
                stats[a]["consecutive_loss"] += 1
            elif res == "TIE":
                stats[a]["tie"] += 1
                global_stats["tie"] += 1


# ======================= วิเคราะห์ไม้แพ้ =======================
def analyze_loss(order, open_p, close_p):
    raw = order["raw"]
    atr = raw["atr_abs"]
    sgn = 1 if order["dir"] == "BUY" else -1
    move = (close_p - open_p) / atr
    against = -sgn * move

    tags = list(order["tags"])
    if order["conf"] < 0.55:
        tags.append("low_conf")
    if against < 0.25:
        tags.append("noise")
    if against > 1.2:
        tags.append("big_move")

    lines = []
    for t in tags:
        if t == "against_trend":
            lines.append(f"สวนเทรนด์หลัก (EMA20−EMA50 = {raw['trend']:+.1f} ATR)")
        elif t == "streak_against":
            lines.append(f"สวนแท่งที่วิ่งต่อเนื่อง {abs(raw['streak'])} แท่ง ราคายังไม่มีสัญญาณหยุด")
        elif t == "vol_spike":
            lines.append(f"ความผันผวนพุ่ง ({raw['volratio'] * 100:+.0f}% เทียบค่าเฉลี่ย)")
        elif t == "quiet_session":
            lines.append("ช่วงตลาดเงียบ (ดึก/เช้ามืด) ราคาสุ่มง่าย")
        elif t == "near_news":
            ev = news_near(order["asset"], order["scan_min"] + pd.Timedelta(minutes=1))
            if ev:
                lines.append(f"ใกล้ข่าว {ev['ccy']} {ev['title'][:30]} ({ev['time'].tz_convert(tz_thai).strftime('%H:%M')})")
            else:
                lines.append("ใกล้เวลาข่าว")
        elif t == "low_conf":
            lines.append(f"ความมั่นใจต่ำ ({order['conf'] * 100:.0f}%) สัญญาณไม่ชัด")
        elif t == "noise":
            lines.append(f"แพ้เฉียด แท่งขยับสวนทางแค่ {against:.2f} ATR ใกล้เคียงการสุ่ม")
        elif t == "big_move":
            lines.append(f"แท่งวิ่งแรงสวนทาง {against:.1f} ATR (เกินที่ตัวชี้วัดคาด)")
    if not lines:
        lines.append("ไม่พบปัจจัยเด่น ถือเป็นการสุ่มตามปกติของไม้ 1 นาที")

    learn = []
    with model.lock:
        for t in tags:
            h = model.tag_hist.get(t)
            if h is not None and len(h) >= 50:
                learn.append(f"{TAG_SHORT[t]}: โมเดลทายถูก {sum(h) / len(h) * 100:.0f}% (จาก {len(h)} ตัวอย่างล่าสุด)")
        n = model.n
    learn.append(f"โมเดลเรียนรู้สะสม {n:,} ตัวอย่าง และปรับน้ำหนักต่อเนื่องทุกนาที")
    return tags, move, against, lines, learn


# ======================= ประเมินผล (แก้ดีเลย์ แจ้งผลไว) =======================
def evaluate_trade_background(order, signal_min):
    asset, signal = order["asset"], order["dir"]
    symbol_code = STANDARD_SYMBOLS[asset]
    try:
        close_at = signal_min + pd.Timedelta(minutes=1, seconds=2)
        while pd.Timestamp.now(tz="UTC") < close_at:
            time.sleep(0.5)

        deadline = signal_min + pd.Timedelta(minutes=2, seconds=30)
        next_min = signal_min + pd.Timedelta(minutes=1)
        eval_c = None
        open_p = float(order.get("price", 0.0))
        close_p = None

        while pd.Timestamp.now(tz="UTC") < deadline:
            res_df = get_candles(symbol_code, retries=1)
            if res_df is not None:
                if signal_min in res_df.index:
                    row = res_df.loc[signal_min]
                    c_val = float(row["close"])
                    o_val = float(row["open"])
                    waited = (pd.Timestamp.now(tz="UTC") - close_at).total_seconds()
                    if (next_min in res_df.index) or (c_val != open_p) or (waited >= 12):
                        eval_c = row
                        if o_val != c_val and open_p == 0.0:
                            open_p = o_val
                        close_p = c_val
                        break
                elif next_min in res_df.index:
                    eval_c = res_df.loc[next_min]
                    close_p = float(eval_c["open"])
                    break
            time.sleep(3)

        if eval_c is None or close_p is None:
            send_result(embed=make_embed(
                f"⚠️ ประเมินผลไม่ได้ · {asset}", COLOR_GRAY, "ดึงแท่งเทียนไม่ได้ ไม่นับเข้าสถิติ"))
            return

        with stats_lock:
            if (signal == "BUY" and close_p > open_p) or (signal == "SELL" and close_p < open_p):
                icon, word, color, res = "✅", "WIN", COLOR_GREEN, "WIN"
                stats[asset]["win"] += 1
                global_stats["win"] += 1
                stats[asset]["consecutive_loss"] = 0
                advice = "ชนะ"
            elif close_p == open_p:
                icon, word, color, res = "⚖️", "TIE", COLOR_GRAY, "TIE"
                stats[asset]["tie"] += 1
                global_stats["tie"] += 1
                advice = "เสมอ ได้เงินต้นคืน"
            else:
                icon, word, color, res = "❌", "LOSS", COLOR_RED, "LOSS"
                stats[asset]["loss"] += 1
                global_stats["loss"] += 1
                stats[asset]["consecutive_loss"] += 1
                if stats[asset]["consecutive_loss"] >= 2:
                    stats[asset]["cooldown_until"] = time.time() + 1800
                    advice = "🚨 แพ้ติดกัน 2 ไม้ พักคู่นี้ 30 นาที · ดูวิเคราะห์ไม้แพ้ในช่องวิเคราะห์"
                else:
                    advice = "แพ้ · ดูวิเคราะห์ไม้แพ้ในช่องวิเคราะห์"
            s = dict(stats[asset])
            g = dict(global_stats)

        tags, move, against, lines, learn = [], 0.0, 0.0, [], []
        try:
            if res == "LOSS":
                tags, move, against, lines, learn = analyze_loss(order, open_p, close_p)
            else:
                move = (close_p - open_p) / order["raw"]["atr_abs"]
        except Exception as e:
            print(f"⚠️ analyze_loss error: {e}")

        log_trade({
            "t": signal_min.isoformat(), "asset": asset, "dir": signal, "conf": round(order["conf"], 4),
            "res": res, "open": open_p, "close": close_p, "move_atr": round(float(move), 3),
            "tags": tags, "hour": order["raw"]["hour"],
        })

        tot = s["win"] + s["loss"]
        wr = (s["win"] / tot * 100) if tot > 0 else 0.0
        tot_g = g["win"] + g["loss"]
        wr_g = (g["win"] / tot_g * 100) if tot_g > 0 else 0.0

        decimals = 3 if "JPY" in asset else 5
        sig_time = signal_min.tz_convert(tz_thai).strftime("%H:%M")
        send_result(embed=make_embed(
            f"{icon} {word} · {asset}", color,
            f"{advice}\n💵 เปิด `{open_p:.{decimals}f}` ➔ ปิด `{close_p:.{decimals}f}`",
            fields=[
                (f"📈 {asset}", f"`{s['win']}W – {s['loss']}L` · **{wr:.0f}%**", True),
                ("🌐 รวมทั้งหมด", f"`{g['win']}W – {g['loss']}L` · **{wr_g:.0f}%**", True),
            ],
            footer=f"{signal} · มั่นใจ {order['conf'] * 100:.0f}% · ไม้ {sig_time}",
        ))

        if res == "LOSS":
            send_analysis(embed=make_embed(
                f"🔍 วิเคราะห์ไม้แพ้ · {asset}", COLOR_RED,
                f"{DIR_TEXT[signal]} · ไม้ {sig_time} · มั่นใจ {order['conf'] * 100:.0f}%\n"
                f"💵 เปิด `{open_p:.{decimals}f}` ➔ ปิด `{close_p:.{decimals}f}` ({move:+.2f} ATR)",
                fields=[
                    ("🧩 สาเหตุที่น่าจะเป็น", "\n".join(f"• {x}" for x in lines)[:1000], False),
                    ("🤔 ทำไมบอทเลือกทิศนี้", order.get("why", "-")[:300], False),
                    ("🧠 บอทเรียนรู้อะไรจากไม้นี้", "\n".join(f"• {x}" for x in learn)[:1000], False),
                ],
                footer=f"{asset} แพ้ติดกัน {s['consecutive_loss']} ไม้",
            ))
    except Exception as e:
        print(f"⚠️ Evaluation Error: {e}")
        try:
            send_result(embed=make_embed(
                f"⚠️ ประเมินผลผิดพลาด · {asset}", COLOR_GRAY, f"`{e}` — ไม่นับเข้าสถิติ"))
        except Exception:
            pass


# ======================= สรุปประจำสัปดาห์ =======================
def build_weekly_embed(now_th):
    start_th = (now_th - timedelta(days=now_th.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = pd.Timestamp(start_th).tz_convert("UTC")
    end_utc = pd.Timestamp(now_th).tz_convert("UTC")
    trades = []
    for t in load_trades():
        try:
            ts = pd.Timestamp(t["t"])
            ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        except Exception:
            continue
        if start_utc <= ts <= end_utc:
            trades.append(t)

    title = f"📅 สรุปประจำสัปดาห์ · {start_th.strftime('%d/%m')}–{(start_th + timedelta(days=5)).strftime('%d/%m')}"
    be = breakeven() * 100

    with model.lock:
        n_model = model.n
        acc_all = list(model.acc_all)
        acc_hc = list(model.acc_hc)
    model_line = f"เรียนรู้สะสม **{n_model:,}** ตัวอย่าง"
    if len(acc_all) >= 100:
        model_line += f"\nทายถูก (ทุกคู่ทุกนาที): **{sum(acc_all) / len(acc_all) * 100:.1f}%** (n={len(acc_all)})"
    if len(acc_hc) >= 50:
        model_line += f"\nทายถูก (ความมั่นใจสูง): **{sum(acc_hc) / len(acc_hc) * 100:.1f}%** (n={len(acc_hc)})"

    if not trades:
        return make_embed(title, COLOR_GRAY, "สัปดาห์นี้ไม่มีออเดอร์",
                          fields=[("🧠 โมเดล", model_line, False)],
                          footer=f"จุดคุ้มทุนที่ payout {PAYOUT * 100:.0f}% = {be:.1f}%")

    w = sum(1 for t in trades if t["res"] == "WIN")
    l = sum(1 for t in trades if t["res"] == "LOSS")
    tie = sum(1 for t in trades if t["res"] == "TIE")
    wr = w / (w + l) * 100 if (w + l) else 0.0
    net = w * PAYOUT - l

    fields = [
        ("📊 ผลรวม", f"✅ ชนะ **{w}** · ⚖️ เสมอ **{tie}** · ❌ แพ้ **{l}**\nอัตราชนะ **{wr:.1f}%** (ไม่นับเสมอ)", False),
        ("💰 ผลสมมติ (เดิมพันเท่ากัน)", f"**{net:+.2f}** หน่วย · จุดคุ้มทุน {be:.1f}%", False),
    ]

    by_pair = {}
    for t in trades:
        d = by_pair.setdefault(t["asset"], [0, 0])
        if t["res"] == "WIN":
            d[0] += 1
        elif t["res"] == "LOSS":
            d[1] += 1
    ranked = [(a, d[0], d[1]) for a, d in by_pair.items() if d[0] + d[1] >= 3]
    ranked.sort(key=lambda x: x[1] / (x[1] + x[2]), reverse=True)
    if ranked:
        best = ranked[:2]
        worst = [r for r in ranked[-2:] if r not in best]
        lines = [f"👍 `{a}` {ww}W-{ll}L ({ww / (ww + ll) * 100:.0f}%)" for a, ww, ll in best]
        lines += [f"👎 `{a}` {ww}W-{ll}L ({ww / (ww + ll) * 100:.0f}%)" for a, ww, ll in worst]
        fields.append(("🎯 รายคู่ (≥3 ไม้)", "\n".join(lines), False))

    buckets = [("< 55%", 0.0, 0.55), ("55–60%", 0.55, 0.60), ("≥ 60%", 0.60, 1.01)]
    bl = []
    for name, lo, hi in buckets:
        sub = [t for t in trades if lo <= t["conf"] < hi and t["res"] in ("WIN", "LOSS")]
        if sub:
            bw = sum(1 for t in sub if t["res"] == "WIN")
            bl.append(f"{name}: {bw}W-{len(sub) - bw}L ({bw / len(sub) * 100:.0f}%)")
    if bl:
        fields.append(("🎚️ ตามความมั่นใจ", "\n".join(bl), False))

    tag_count = {}
    for t in trades:
        if t["res"] == "LOSS":
            for tg in t.get("tags", []):
                tag_count[tg] = tag_count.get(tg, 0) + 1
    if tag_count and l:
        top = sorted(tag_count.items(), key=lambda x: -x[1])[:3]
        fields.append(("🧩 สาเหตุแพ้บ่อย", "\n".join(f"• {TAG_SHORT.get(k, k)} — {v}/{l} ไม้แพ้" for k, v in top), False))

    fields.append(("🧠 โมเดล", model_line, False))

    if wr >= be and net > 0:
        verdict, color = "สัปดาห์นี้เหนือจุดคุ้มทุน แต่ตัวอย่างยังน้อย ดูต่อหลายสัปดาห์ก่อนสรุป", COLOR_GREEN
    else:
        verdict, color = "สัปดาห์นี้ยังต่ำกว่าจุดคุ้มทุน อย่าเพิ่งเพิ่มเงินเดิมพัน", COLOR_ORANGE
    return make_embed(title, color, verdict, fields, f"ออเดอร์ทั้งหมด {len(trades)} ไม้")


# ======================= เธรดเบื้องหลัง =======================
def background_worker():
    last_brief_date = None
    warned_date = None
    force = NEWS_BRIEF_ON_BOOT
    last_save = time.time()
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

            week_id = now_th.strftime("%G-W%V")
            weekly_due = (
                (now_th.weekday() == 6 or (now_th.weekday() == 5 and now_th.hour >= 8))
                and app_state.get("last_weekly") != week_id
            )
            if weekly_due:
                send_result(embed=build_weekly_embed(now_th))
                app_state["last_weekly"] = week_id
                save_all()

            if time.time() - last_save >= 300:
                save_all()
                last_save = time.time()
        except Exception as e:
            print(f"⚠️ Background worker error: {e}")
        time.sleep(60)


# ======================= ลูปหลัก =======================
def main():
    print("🚀 เริ่มต้นระบบบอท...")
    os.makedirs(DATA_DIR, exist_ok=True)
    load_all()
    rebuild_stats(load_trades())
    if DATA_DIR_IS_DEFAULT:
        print("⚠️ ไม่ได้ตั้ง DATA_DIR — ถ้าไม่ผูก Railway Volume ข้อมูลที่เรียนรู้/ประวัติไม้จะหายเมื่อ Redeploy")

    stop_event = threading.Event()

    def _graceful(*_):
        print("🛑 กำลังบันทึกข้อมูลก่อนปิดระบบ...")
        stop_event.set()
        save_all()
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGTERM, _graceful)
        signal.signal(signal.SIGINT, _graceful)
    except Exception:
        pass

    print("📡 กำลังส่งข้อความทดสอบเข้า Discord...")
    send_main(f"🟢 **ระบบเริ่มทำงานแล้ว** · run `{BOOT_ID}` · กำลังเชื่อมต่อข้อมูลตลาดและเตรียมโมเดล...")
    send_result(f"🏆 **Stats Bot พร้อมรายงานผล** · run `{BOOT_ID}`")
    if DISCORD_NEWS_WEBHOOK:
        send_news(f"📰 **ช่องวิเคราะห์พร้อมแล้ว** · สรุปข่าวจันทร์–ศุกร์ {NEWS_BRIEF_HOUR:02d}:00 น. · run `{BOOT_ID}`")

    if model.n < WARMUP_SAMPLES:
        print("⏳ กำลังดึงกราฟย้อนหลังเพื่อเรียนรู้ด่วน (Instant Warmup)...")
        init_data = fetch_all(UNIQUE_TICKERS)
        print(f"📥 ดึงกราฟสำเร็จ {len(init_data)}/{len(UNIQUE_TICKERS)} คู่เงิน")
        added = warmup_from_history(init_data)
        send_main(f"⚡ **Instant Warmup เสร็จสิ้น** · เรียนรู้เพิ่ม `+{added}` ตัวอย่าง (สะสม **{model.n:,}** / {WARMUP_SAMPLES} ตัวอย่าง)")
    else:
        send_main(f"🧠 **โหลดโมเดลเดิมสำเร็จ** · เรียนรู้สะสมแล้ว **{model.n:,}** ตัวอย่าง พร้อมสแกนตลาดทันที!")

    threading.Thread(target=background_worker, daemon=True).start()

    health = {"paused": False, "fail": 0, "ok": 0, "skip": 0}
    announce = {"warm": False, "ready": model.n >= WARMUP_SAMPLES, "edge_closed": False}
    last_pre_scan = None
    last_action_min = None
    pending_order = None
    pending_minute = None

    def pause(reason):
        if not health["paused"]:
            health["paused"] = True
            print(f"⏸️ พักบอท: {reason}")
            broadcast(make_embed("⏸️ บอทพักชั่วคราว", COLOR_ORANGE, f"{reason}\nจะกลับมาทำงานเองเมื่อข้อมูลปกติ"))

    def resume():
        if health["paused"]:
            health["paused"] = False
            print("▶️ กลับมาทำงาน")
            broadcast(make_embed("▶️ บอทกลับมาทำงานแล้ว", COLOR_GREEN, "ข้อมูลตลาดพร้อมใช้งานปกติ"))

    while not stop_event.is_set():
        try:
            now_utc = pd.Timestamp.now(tz="UTC")
            current_min = now_utc.floor("min")
            current_sec = now_utc.second

            if current_sec >= SCAN_SECOND and last_pre_scan != current_min:
                last_pre_scan = current_min
                pending_order = None
                pending_minute = None

                if market_closed_by_calendar(now_utc):
                    health["ok"] = 0
                    pause("ตลาด Forex ปิดช่วงสุดสัปดาห์")
                elif health["skip"] > 0:
                    health["skip"] -= 1
                else:
                    data = fetch_all(UNIQUE_TICKERS)
                    check_utc = pd.Timestamp.now(tz="UTC")
                    fresh_n = sum(1 for df in data.values() if is_fresh(df, check_utc))
                    need = max(3, int(len(UNIQUE_TICKERS) * MIN_FRESH_RATIO))

                    if fresh_n >= need:
                        health["fail"] = 0
                        health["ok"] += 1
                        if health["paused"] and health["ok"] >= OK_TO_RESUME:
                            resume()
                        if not health["paused"]:
                            if model.n < WARMUP_SAMPLES:
                                warmup_from_history(data)
                            trained_now = learn_from_pending(data, check_utc)
                            cands = make_candidates(data, current_min, check_utc)
                            print(f"📊 สแกน {current_min.strftime('%H:%M')} UTC: สด {fresh_n}/19 คู่ | เรียนรู้เพิ่ม +{trained_now} (รวม {model.n:,}) | ตัวเลือก {len(cands)} คู่")

                            ready = model.n >= WARMUP_SAMPLES
                            if not ready and not announce["warm"]:
                                announce["warm"] = True
                                send_main(f"🧠 กำลังเรียนรู้เพิ่มเติม ({model.n}/{WARMUP_SAMPLES} ตัวอย่าง)")
                            if ready and not announce["ready"]:
                                announce["ready"] = True
                                send_main(f"✅ เรียนรู้ครบแล้ว ({model.n:,} ตัวอย่าง) เริ่มคัดเลือกออเดอร์ที่มั่นใจที่สุด")

                            if ready:
                                ok_edge, acc, n_hc = edge_status()
                                if REQUIRE_EDGE and not ok_edge:
                                    if not announce["edge_closed"]:
                                        announce["edge_closed"] = True
                                        send_main(embed=make_embed(
                                            "🛑 งดออกสัญญาณชั่วคราว", COLOR_ORANGE,
                                            f"โมเดลทายถูก {acc * 100:.1f}% (n={n_hc}) ต่ำกว่าจุดคุ้มทุน {breakeven() * 100:.1f}%\n"
                                            "ยังเรียนรู้ต่อ จะกลับมาออกสัญญาณเมื่อวัดผลได้เหนือจุดคุ้มทุน"))
                                else:
                                    if announce["edge_closed"]:
                                        announce["edge_closed"] = False
                                        send_main("✅ โมเดลกลับมาเหนือจุดคุ้มทุน เริ่มออกสัญญาณอีกครั้ง")
                                    order = select_best(cands, current_min)
                                    if order:
                                        pending_order = order
                                        pending_minute = current_min
                    else:
                        health["ok"] = 0
                        health["fail"] += 1
                        print(f"⚠️ ข้อมูลสดแค่ {fresh_n}/{len(UNIQUE_TICKERS)} คู่ (ครั้งที่ {health['fail']})")
                        if health["fail"] >= FAIL_TO_PAUSE:
                            pause(f"ดึงข้อมูลตลาดไม่ได้/ข้อมูลไม่อัปเดต (สดแค่ {fresh_n}/{len(UNIQUE_TICKERS)} คู่)")
                        health["skip"] = min(2 ** (health["fail"] - 1) - 1, 3)

                if pending_order is not None:
                    pre_time_str = (current_min + pd.Timedelta(minutes=1)).tz_convert(tz_thai).strftime("%H:%M")
                    p = pending_order
                    send_main(f"⏳ **{p['asset']}** · {DIR_TEXT[p['dir']]} · มั่นใจ **{p['conf'] * 100:.0f}%** · เตรียมกด **{pre_time_str}**")

            if (
                current_sec >= 1
                and last_action_min != current_min
                and (pending_minute is None or current_min != pending_minute)
            ):
                last_action_min = current_min
                valid = (
                    not health["paused"]
                    and pending_order is not None
                    and pending_minute is not None
                    and current_min == pending_minute + pd.Timedelta(minutes=1)
                    and current_sec <= 20
                )
                if valid:
                    p = pending_order
                    asset = p["asset"]
                    last_signal_time[asset] = time.time()
                    action_time_str = datetime.now(tz_thai).strftime("%H:%M:%S")
                    decimals = 3 if "JPY" in asset else 5
                    send_main(embed=make_embed(
                        f"{DIR_TEXT[p['dir']]} · {asset}",
                        COLOR_GREEN if p["dir"] == "BUY" else COLOR_RED,
                        f"⏱️ หมดอายุ **1 นาที**\n💡 {p['why']}",
                        fields=[
                            ("⏰ เวลา", f"`{action_time_str}`", True),
                            ("💵 ราคาเปิด", f"`{p['price']:.{decimals}f}`", True),
                            ("🎯 มั่นใจ", f"`{p['conf'] * 100:.0f}%`", True),
                        ],
                        footer=f"Main · run {BOOT_ID}",
                    ))
                    threading.Thread(
                        target=evaluate_trade_background, args=(p, current_min), daemon=True
                    ).start()

                pending_order = None
                pending_minute = None

            time.sleep(0.5)
        except SystemExit:
            break
        except Exception as e:
            print(f"⚠️ Loop Error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()

"""株価データの取得と指標の計算。

データ源は Yahoo Finance のチャートAPI（v8）。APIキー不要。
  日本株  7203     → https://query1.finance.yahoo.com/v8/finance/chart/7203.T
  米国株  NVDA     → .../chart/NVDA
  指数    日経平均  → .../chart/^N225

stooq は 2026年3月から APIキー必須になったため、既定では使わない。
.env に STOOQ_API_KEY を入れておくと、Yahoo で取れなかったときの控えとして使う。

取得できなかった銘柄は株価欄が出ないだけで、アプリ本体は動く。
"""

from __future__ import annotations

import csv
import io
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests

VERSION = "v25"

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
COOKIE_URL = "https://fc.yahoo.com/"
STOOQ_URL = "https://stooq.com/q/d/l/"
# 既定のUser-Agentだと弾かれるのでブラウザ相当を名乗る
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 指数はティッカーが無いので名前から引く。候補は上から順に試す。
INDEX_ALIASES: dict[str, list[str]] = {
    "日経平均": ["^N225"], "日経平均株価": ["^N225"], "日経225": ["^N225"], "日経": ["^N225"],
    "N225": ["^N225"], "NI225": ["^N225"], "NKY": ["^N225"], "JP225": ["^N225"],
    "TOPIX": ["^TOPX", "1306.T"], "東証株価指数": ["^TOPX", "1306.T"],
    "S&P500": ["^GSPC"], "SP500": ["^GSPC"], "SPX": ["^GSPC"], "S&P": ["^GSPC"],
    "NASDAQ": ["^IXIC"], "ナスダック": ["^IXIC"], "NASDAQ総合": ["^IXIC"],
    "NASDAQ100": ["^NDX"], "NDX": ["^NDX"], "ナスダック100": ["^NDX"],
    "ダウ": ["^DJI"], "NYダウ": ["^DJI"], "DJIA": ["^DJI"], "ダウ平均": ["^DJI"],
    "グロース250": ["2516.T"], "マザーズ": ["2516.T"], "東証グロース": ["2516.T"],
    "ドル円": ["JPY=X"], "USDJPY": ["JPY=X"], "為替": ["JPY=X"],
}


# カタカナ社名 → 米国ティッカー（英語名が取れなかったときの保険）
US_ALIASES = {
    "サンディスク": "SNDK", "エヌビディア": "NVDA", "アップル": "AAPL",
    "マイクロソフト": "MSFT", "アルファベット": "GOOGL", "グーグル": "GOOGL",
    "アマゾン": "AMZN", "テスラ": "TSLA", "メタ": "META", "インテル": "INTC",
    "ブロードコム": "AVGO", "マイクロン": "MU", "アーム": "ARM",
    "ネットフリックス": "NFLX", "コインベース": "COIN", "スーパーマイクロ": "SMCI",
    "オラクル": "ORCL", "セールスフォース": "CRM", "アドビ": "ADBE",
    "クアルコム": "QCOM", "ラムリサーチ": "LRCX", "アプライドマテリアルズ": "AMAT",
    "パランティア": "PLTR", "ロビンフッド": "HOOD", "イーライリリー": "LLY",
}


def normalize_key(text: str) -> str:
    return re.sub(r"[\s　・％%]", "", (text or "")).upper()


def resolve_symbols(name: str, ticker: str, market: str,
                    overrides: dict | None = None) -> list[str]:
    """銘柄名・コードから、試すべき Yahoo シンボルの候補を返す。

    overrides（config.json の symbol_overrides）が最優先。
    AIが証券コードを取り違える銘柄や、英語社名が拾えず検索できない銘柄をここで救う。
    """
    nkey = normalize_key(name)
    tkey = normalize_key(ticker)

    if overrides:
        forced = overrides.get(nkey) or (overrides.get(tkey) if tkey else "")
        if forced:
            return [forced]

    for alias, syms in INDEX_ALIASES.items():
        akey = normalize_key(alias)
        if nkey == akey or (tkey and tkey == akey):
            return list(syms)

    if not ticker:
        return []
    if re.fullmatch(r"\d{4}[A-Z]?", ticker):          # 日本株（新形式の英字入りも）
        return [f"{ticker.upper()}.T"]
    if market == "JP":
        return [f"{ticker.upper()}.T"]
    if re.fullmatch(r"[A-Z][A-Z.\-]{0,6}", ticker):   # 米国株
        return [ticker.upper()]
    return []


def to_symbol(name: str, ticker: str, market: str) -> str:
    """代表シンボル（先頭候補）。表示やログ用。"""
    cands = resolve_symbols(name, ticker, market)
    return cands[0] if cands else ""


def _get_crumb(session: requests.Session, timeout: int = 15) -> str:
    """検索APIに必要な認証トークンを取る。株価取得(chart)には不要。

    Cookieを受け取ってから getcrumb を叩く、という2段構えが必要。
    取れなければ空文字を返し、検索は諦める（株価は出ないがアプリは動く）。
    """
    cached = getattr(session, "_yahoo_crumb", None)
    if cached is not None:
        return cached

    crumb = ""
    try:
        # Cookieを受け取る（404が返ることもあるがCookieは付く）
        session.get(COOKIE_URL, headers={"User-Agent": UA}, timeout=timeout)
        resp = session.get(CRUMB_URL, headers={"User-Agent": UA}, timeout=timeout)
        if resp.status_code == 200:
            text = resp.text.strip()
            # 正常なcrumbは短い文字列。HTMLが返ってきたら失敗扱い
            if text and len(text) < 40 and "<" not in text:
                crumb = text
    except requests.RequestException:
        pass

    session._yahoo_crumb = crumb
    return crumb


def search_symbol(name: str, market: str, session: requests.Session, timeout: int = 20):
    """銘柄名からシンボルを検索する。AIが証券コードを返さなかったときの受け皿。

    「キオクシア」→ 285A.T のように、日本語の会社名からでも引ける。
    """
    params = {"q": name, "quotesCount": 10, "newsCount": 0}
    crumb = _get_crumb(session)
    if crumb:
        params["crumb"] = crumb

    try:
        resp = session.get(
            SEARCH_URL, params=params,
            headers={"User-Agent": UA, "Accept": "application/json"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            hint = "（認証トークンを取得できませんでした）" if not crumb else ""
            return "", f"検索HTTP {resp.status_code}{hint}"
        quotes = resp.json().get("quotes") or []
    except (requests.RequestException, ValueError) as exc:
        return "", f"検索エラー: {exc}"

    if not quotes:
        return "", "候補なし"

    def label(q):
        return q.get("shortname") or q.get("longname") or q.get("symbol", "")

    # 株式のみ。市場が分かっていればそちらを優先する
    equities = [q for q in quotes if (q.get("quoteType") or "").upper() in ("EQUITY", "INDEX", "ETF")]
    pool = equities or quotes

    if market == "JP":
        for q in pool:
            if str(q.get("symbol", "")).endswith(".T"):
                return q["symbol"], label(q)
    if market == "US":
        for q in pool:
            sym = str(q.get("symbol", ""))
            if sym and "." not in sym and not sym.startswith("^"):
                return sym, label(q)

    first = pool[0]
    return first.get("symbol", ""), label(first)


# ---------------------------------------------------------------- 取得


def fetch_yahoo(symbol: str, session: requests.Session, timeout: int = 30):
    """(日足リスト（古い順）, メタ情報, エラー理由) を返す。"""
    try:
        resp = session.get(
            YAHOO_URL.format(symbol=symbol),
            params={"range": "1y", "interval": "1d"},
            headers={"User-Agent": UA, "Accept": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return None, None, f"通信エラー: {exc}"

    if resp.status_code == 404:
        return None, None, "この銘柄コードは見つかりませんでした"
    if resp.status_code == 429:
        return None, None, "アクセスが多すぎます（しばらく待つと戻ります）"
    if resp.status_code != 200:
        return None, None, f"HTTP {resp.status_code}"

    try:
        payload = resp.json()
    except ValueError:
        return None, None, "JSONではない応答が返りました"

    chart = payload.get("chart") or {}
    if chart.get("error"):
        desc = (chart["error"] or {}).get("description", "")
        return None, None, f"取得エラー: {desc or chart['error']}"

    results = chart.get("result") or []
    if not results:
        return None, None, "データが空でした"

    res = results[0]
    meta = res.get("meta") or {}
    stamps = res.get("timestamp") or []
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    opens = quote.get("open") or []

    rows = []
    for i, ts in enumerate(stamps):
        c = closes[i] if i < len(closes) else None
        if c is None:            # 休場日などは飛ばす
            continue
        rows.append({
            "date": datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d"),
            "close": float(c),
            "open": float(opens[i]) if i < len(opens) and opens[i] is not None else float(c),
            "high": float(highs[i]) if i < len(highs) and highs[i] is not None else float(c),
            "low": float(lows[i]) if i < len(lows) and lows[i] is not None else float(c),
            "volume": float(volumes[i]) if i < len(volumes) and volumes[i] is not None else 0.0,
        })

    rows.sort(key=lambda x: x["date"])

    # 取引時間中は当日のバーが「途中経過」なので落とす。
    # そのまま使うと出来高が半日分になり、平均との比較が実態とずれる。
    if rows and is_market_open(meta):
        tz = timezone(timedelta(seconds=meta.get("gmtoffset") or 0))
        today_local = datetime.now(tz).strftime("%Y-%m-%d")
        if rows[-1]["date"] >= today_local:
            rows = rows[:-1]
            meta = {**meta, "dropped_partial": True}

    if len(rows) < 2:
        return None, None, "日足が少なすぎます"
    return rows, meta, None


def fetch_intraday(symbol: str, session: requests.Session, days: int = 5, timeout: int = 30):
    """1分足を取る。価格帯別出来高（どの値段で売買が集中したか）を作るために使う。"""
    try:
        resp = session.get(
            YAHOO_URL.format(symbol=symbol),
            params={"range": f"{days}d", "interval": "1m"},
            headers={"User-Agent": UA, "Accept": "application/json"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        return None, f"通信エラー: {exc}"

    chart = payload.get("chart") or {}
    if chart.get("error") or not (chart.get("result") or []):
        return None, "分足データがありません"

    res = chart["result"][0]
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    volumes = quote.get("volume") or []

    bars = []
    for i in range(len(closes)):
        c, v = closes[i], (volumes[i] if i < len(volumes) else None)
        if c is None or not v:
            continue
        h = highs[i] if i < len(highs) and highs[i] is not None else c
        l = lows[i] if i < len(lows) and lows[i] is not None else c
        bars.append(((float(h) + float(l) + float(c)) / 3, float(v)))

    if len(bars) < 20:
        return None, "分足が少なすぎます"
    return bars, None


def volume_profile(bars: list[tuple], buckets: int = 24) -> dict | None:
    """1分足から価格帯別出来高を作る。

    poc  … 最も出来高が多かった価格帯（目先の攻防ライン）
    va   … 出来高の7割が収まる価格帯（いわゆるバリューエリア）
    """
    if not bars:
        return None
    prices_ = [p for p, _ in bars]
    lo, hi = min(prices_), max(prices_)
    if hi <= lo:
        return None

    width = (hi - lo) / buckets
    hist = [0.0] * buckets
    for price, vol in bars:
        idx = min(int((price - lo) / width), buckets - 1)
        hist[idx] += vol

    total = sum(hist)
    if total <= 0:
        return None

    poc_idx = max(range(buckets), key=lambda i: hist[i])

    # POCから上下に広げて出来高の70%を含む範囲を求める
    lo_i = hi_i = poc_idx
    covered = hist[poc_idx]
    while covered < total * 0.7 and (lo_i > 0 or hi_i < buckets - 1):
        below = hist[lo_i - 1] if lo_i > 0 else -1
        above = hist[hi_i + 1] if hi_i < buckets - 1 else -1
        if above >= below:
            hi_i += 1
            covered += hist[hi_i]
        else:
            lo_i -= 1
            covered += hist[lo_i]

    digits = 0 if hi >= 1000 else 2
    return {
        "low": round(lo, digits),
        "high": round(hi, digits),
        "buckets": [round(v) for v in hist],
        "poc": round(lo + width * (poc_idx + 0.5), digits),
        "va_low": round(lo + width * lo_i, digits),
        "va_high": round(lo + width * (hi_i + 1), digits),
    }


def build_chart(rows: list[dict], span: int = 120) -> dict:
    """日足チャート用のデータ。JSONを小さくするため列ごとの配列で持つ。"""
    tail = rows[-span:]
    closes_all = [r["close"] for r in rows]

    d = 0 if (tail and tail[-1]["close"] >= 1000) else 2

    def ma(period: int):
        out = []
        for i in range(len(rows) - len(tail), len(rows)):
            if i + 1 < period:
                out.append(None)
            else:
                out.append(round(sum(closes_all[i + 1 - period:i + 1]) / period, d))
        return out
    return {
        "d": [r["date"][5:] for r in tail],                 # MM-DD
        "o": [round(r.get("open", r["close"]), d) for r in tail],
        "h": [round(r["high"], d) for r in tail],
        "l": [round(r["low"], d) for r in tail],
        "c": [round(r["close"], d) for r in tail],
        "v": [int(r["volume"]) for r in tail],
        "ma25": ma(25),
        "ma75": ma(75),
    }


JST = timezone(timedelta(hours=9))


def market_window_open(symbol: str, market: str, now: datetime | None = None) -> bool:
    """その銘柄の市場がいま開いていそうか。通信せずに時計だけで大まかに判断する。

    取引時間中は株価を短い間隔で取り直し、閉まっている間は取りに行かないための判定。
    多少広めに取ってあるので、開いているのに取りに行かない、ということは起きない。
    """
    now = (now or datetime.now(timezone.utc)).astimezone(JST)
    wd = now.weekday()                      # 月=0 … 日=6
    mins = now.hour * 60 + now.minute
    sym = (symbol or "").upper()

    if sym.endswith("=X"):                  # 為替は平日ほぼ24時間
        return wd < 5 or (wd == 6 and now.hour >= 7)

    if sym.endswith(".T") or sym in ("^N225", "^TOPX") or (not sym and market == "JP"):
        # 東証 9:00〜15:30（昼休みも含めて広めに）
        return wd < 5 and 9 * 60 <= mins <= 15 * 60 + 40

    # 米国市場は日本時間の夜〜早朝。夏時間・冬時間の差を吸収して広めに取る
    return (wd < 5 and mins >= 22 * 60) or (1 <= wd <= 5 and mins <= 6 * 60 + 10)


def is_market_open(meta: dict) -> bool:
    """いま通常取引の時間内かどうか。判断できなければ False。"""
    period = ((meta.get("currentTradingPeriod") or {}).get("regular") or {})
    start, end = period.get("start"), period.get("end")
    if not start or not end:
        return False
    now = datetime.now(timezone.utc).timestamp()
    return start <= now < end


def fetch_stooq(symbol: str, session: requests.Session, apikey: str, timeout: int = 30):
    """控えの取得先。2026年3月以降 APIキーが必須。"""
    if not apikey:
        return None, "stooqのAPIキーが設定されていません"
    try:
        resp = session.get(
            STOOQ_URL, params={"s": symbol, "i": "d", "apikey": apikey},
            headers={"User-Agent": UA}, timeout=timeout,
        )
    except requests.RequestException as exc:
        return None, f"通信エラー: {exc}"

    body = resp.text.strip()
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}"
    if "apikey" in body[:200].lower() or "<html" in body[:200].lower():
        return None, "APIキーが無効か、CSVではなくHTMLが返りました"

    rows = []
    for r in csv.DictReader(io.StringIO(body)):
        try:
            rows.append({"date": r["Date"], "close": float(r["Close"]),
                         "open": float(r.get("Open") or r["Close"]),
                         "high": float(r["High"]), "low": float(r["Low"]),
                         "volume": float(r.get("Volume") or 0)})
        except (TypeError, ValueError, KeyError):
            continue
    if len(rows) < 2:
        return None, "データが少なすぎます"
    rows.sort(key=lambda x: x["date"])
    return rows, None


def stooq_symbol(yahoo_symbol: str) -> str:
    """Yahoo形式のシンボルを stooq 形式に読み替える。"""
    s = yahoo_symbol
    if s.endswith(".T"):
        return s[:-2].lower() + ".jp"
    if s.startswith("^"):
        return {"^N225": "^nkx", "^GSPC": "^spx", "^IXIC": "^ndq",
                "^NDX": "^ndx", "^DJI": "^dji", "^TOPX": "^tpx"}.get(s, "")
    if re.fullmatch(r"[A-Z.\-]+", s):
        return s.lower() + ".us"
    return ""


# ---------------------------------------------------------------- 指標


def rsi(closes: list[float], period: int = 14):
    """Wilder方式のRSI。データが足りなければ None。"""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for prev, cur in zip(closes, closes[1:]):
        d = cur - prev
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return round(100 - 100 / (1 + avg_gain / avg_loss), 1)


def sma(values: list[float], period: int):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def summarize(rows: list[dict], meta: dict | None = None) -> dict:
    """日足から、アプリに出す指標一式を作る。"""
    meta = meta or {}
    closes = [r["close"] for r in rows]
    volumes = [r["volume"] for r in rows]
    last, prev = rows[-1], rows[-2]

    change_pct = round((last["close"] / prev["close"] - 1) * 100, 2) if prev["close"] else None

    ma25 = sma(closes, 25)
    ma75 = sma(closes, 75)

    trend = "判定不可"
    if ma25 and ma75:
        c = last["close"]
        if c > ma25 > ma75:
            trend = "上昇基調"
        elif c < ma25 < ma75:
            trend = "下降基調"
        elif c > ma25:
            trend = "戻り歩調"
        elif c < ma25:
            trend = "調整中"
        else:
            trend = "もみ合い"

    vol_avg = sma(volumes, 25)
    vol_ratio = None
    if vol_avg and vol_avg > 0 and last["volume"]:
        vol_ratio = round(last["volume"] / vol_avg, 2)

    r = rsi(closes)
    zone = "" if r is None else ("買われすぎ" if r >= 70 else "売られすぎ" if r <= 30 else "中立")

    spark_src = closes[-60:]
    step = max(1, len(spark_src) // 40)
    spark = [round(v, 2) for v in spark_src[::step]][-40:]

    high52 = max(closes) if len(closes) >= 30 else None
    low52 = min(closes) if len(closes) >= 30 else None
    pos52 = None
    if high52 and low52 and high52 > low52:
        pos52 = round((last["close"] - low52) / (high52 - low52) * 100)

    digits = 0 if abs(last["close"]) >= 1000 else 2

    # 取引時間中は「いまの株価」も持たせる。
    # RSIや移動平均は確定した日足だけで計算し、表示する現在値だけを別に持つ。
    live = live_change = live_time = None
    if meta.get("dropped_partial"):
        raw_live = meta.get("regularMarketPrice")
        try:
            raw_live = float(raw_live) if raw_live is not None else None
        except (TypeError, ValueError):
            raw_live = None
        if raw_live and last["close"]:
            live = round(raw_live, digits if digits else 2)
            live_change = round((raw_live / last["close"] - 1) * 100, 2)
            live_time = meta.get("regularMarketTime")

    return {
        "date": last["date"],
        "close": round(last["close"], digits if digits else 2),
        "change_pct": change_pct,
        "live": live,
        "live_change_pct": live_change,
        "live_time": live_time,
        "volume": int(last["volume"]) if last["volume"] else None,
        "volume_ratio": vol_ratio,
        "rsi14": r,
        "rsi_zone": zone,
        "ma25": round(ma25, 2) if ma25 else None,
        "ma75": round(ma75, 2) if ma75 else None,
        "trend": trend,
        "pos52": pos52,
        "spark": spark,
        "currency": meta.get("currency", ""),
        "exchange_name": meta.get("fullExchangeName", ""),
        "market_open": bool(meta.get("dropped_partial")),
    }


# ---------------------------------------------------------------- まとめ役


def fetch_one(name: str, ticker: str, market: str, session: requests.Session,
              stooq_key: str = "", allow_search: bool = True, log=None,
              name_en: str = "", overrides: dict | None = None):
    """候補シンボルを順に試して、最初に取れたものを返す。"""
    candidates = resolve_symbols(name, ticker, market, overrides)
    reasons = []
    searched = False

    def by_name():
        """銘柄名からシンボルを引いて株価を取る。AIのコードが無い/間違っているときの受け皿。

        Yahooの検索は日本語クエリを受け付けない（HTTP 400）ため、英語社名で引く。
        """
        found = matched = ""

        # 1) 英語社名で検索（AIが name_en を返していれば最も確実）
        query = (name_en or "").strip() or (name if name.isascii() else "")
        if query:
            found, matched = search_symbol(query, market, session)

        # 2) だめならカタカナ社名の対応表（米国株向け）
        if not found:
            key = normalize_key(name)
            for alias, sym in US_ALIASES.items():
                if key == normalize_key(alias):
                    found, matched = sym, "米国銘柄の対応表"
                    break

        if not found:
            reasons.append(f"名前解決: {matched or '英語社名が無いため検索できません'}")
            return None
        if found in candidates:
            return None
        rows_, meta_, reason_ = fetch_yahoo(found, session)
        if not rows_:
            reasons.append(f"{found}: {reason_}")
            return None
        if log:
            log(f"    {name} → {found}（{matched}）として株価を取得します")
        return {**summarize(rows_, meta_), "symbol": found, "source": "yahoo-search",
                "_rows": rows_}

    # 証券コードが無い（AIが拾えなかった）ときは、先に銘柄名で検索する
    if not candidates and allow_search and name:
        searched = True
        hit = by_name()
        if hit:
            return hit, None
        return None, " / ".join(reasons) or "シンボルを特定できませんでした"

    if not candidates:
        return None, "シンボルを特定できませんでした"

    for sym in candidates:
        rows, meta, reason = fetch_yahoo(sym, session)
        if rows:
            return {**summarize(rows, meta), "symbol": sym, "source": "yahoo",
                    "_rows": rows}, None
        reasons.append(f"{sym}: {reason}")

    # AIの返したコードが全部外れたら、銘柄名で引き直す
    if allow_search and not searched and name:
        hit = by_name()
        if hit:
            return hit, None

    if stooq_key:                      # 控え
        for sym in candidates:
            ssym = stooq_symbol(sym)
            if not ssym:
                continue
            rows, reason = fetch_stooq(ssym, session, stooq_key)
            if rows:
                return {**summarize(rows), "symbol": sym, "source": "stooq"}, None
            reasons.append(f"{ssym}: {reason}")

    return None, " / ".join(reasons)


def update_prices(stocks: list[dict], cache: dict, cfg: dict, log=print,
                  charts: dict | None = None, charts_prev: dict | None = None) -> dict:
    """index の銘柄一覧をもとに株価キャッシュを更新して返す。

    charts を渡すと、日足チャートと価格帯別出来高もそこに書き込む。
    """
    refresh_hours = cfg.get("price_refresh_hours", 12)
    open_minutes = cfg.get("price_refresh_minutes_open", 20)
    profile_hours = cfg.get("profile_refresh_hours", 12)
    pause = cfg.get("price_pause_seconds", 2)
    stooq_key = os.environ.get("STOOQ_API_KEY", "").strip()
    overrides = {normalize_key(k): v.strip()
                 for k, v in (cfg.get("symbol_overrides") or {}).items() if v and v.strip()}
    now = datetime.now(timezone.utc)
    entries = dict(cache.get("symbols", {}))

    session = requests.Session()
    fetched = skipped = failed = 0


    for i, st in enumerate(stocks):
        key = st.get("key") or st.get("name", "")
        old = entries.get(key)
        # 取引時間中は短い間隔で取り直す。閉まっている間は1日1回で十分。
        sym_hint = (old or {}).get("symbol", "") or to_symbol(
            st.get("name", ""), st.get("ticker", ""), st.get("market", ""))
        trading = market_window_open(sym_hint, st.get("market", ""), now)
        interval = timedelta(minutes=open_minutes) if trading else timedelta(hours=refresh_hours)
        if old and old.get("fetched_at"):
            try:
                if now - datetime.fromisoformat(old["fetched_at"]) < interval:
                    skipped += 1
                    continue
            except ValueError:
                pass

        if fetched or failed:
            time.sleep(pause)
        data, reason = fetch_one(st.get("name", ""), st.get("ticker", ""),
                                 st.get("market", ""), session, stooq_key,
                                 log=log, name_en=st.get("name_en", ""),
                                 overrides=overrides)
        if data is None:
            failed += 1
            log(f"    ! {st.get('name', key)}: {reason}")
            if old:
                entries[key] = old      # 取れなければ前回の値を残す
            continue

        rows = data.pop("_rows", None)
        entries[key] = {**data, "fetched_at": now.isoformat()}
        fetched += 1

        if charts is not None and rows:
            entry = {"symbol": data["symbol"], "name": st.get("name", ""),
                     "currency": data.get("currency", ""),
                     "daily": build_chart(rows, cfg.get("chart_days", 120))}
            if cfg.get("enable_volume_profile", True):
                # 価格帯別出来高は通信が1回増えるので、株価ほど頻繁には取り直さない。
                prev = (charts_prev or {}).get(key) or {}
                reuse = False
                if prev.get("profile") and prev.get("profile_at"):
                    try:
                        age = now - datetime.fromisoformat(prev["profile_at"])
                        reuse = age < timedelta(hours=profile_hours)
                    except ValueError:
                        reuse = False
                if reuse:
                    entry["profile"] = prev["profile"]
                    entry["profile_days"] = prev.get("profile_days", cfg.get("profile_days", 5))
                    entry["profile_at"] = prev["profile_at"]
                else:
                    bars, why = fetch_intraday(data["symbol"], session,
                                              cfg.get("profile_days", 5))
                    if bars:
                        prof = volume_profile(bars)
                        if prof:
                            entry["profile"] = prof
                            entry["profile_days"] = cfg.get("profile_days", 5)
                            entry["profile_at"] = now.isoformat()
                    else:
                        log(f"    （{st.get('name', '')}: 価格帯別出来高は取得できませんでした / {why}）")
            charts[key] = entry

    log(f"  株価: 取得 {fetched} / キャッシュ流用 {skipped} / 失敗 {failed}")
    return {"updated_at": now.isoformat(), "symbols": entries, "fetched": fetched}


def attach(stocks: list[dict], cache: dict) -> None:
    """各銘柄に price を紐づける（キャッシュに無ければ付けない）。"""
    entries = cache.get("symbols", {})
    for st in stocks:
        data = entries.get(st.get("key") or st.get("name", ""))
        if data:
            st["price"] = data

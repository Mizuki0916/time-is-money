"""4人の株系YouTuberの新着動画を拾って要約し、PWA用の JSON を生成する。

  python collect.py              通常実行（新着だけ処理）
  python collect.py --limit 2    今回処理する本数の上限
  python collect.py --video ID   特定の動画だけ強制的に処理し直す
  python collect.py --rebuild    要約はし直さず index.json だけ作り直す
  python collect.py --dry-run    LLM を呼ばず、拾える動画と字幕の有無だけ表示
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# Windows のコンソール(CP932)で変換できない文字が来ても落ちないようにする
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv が無くても動かす
    def load_dotenv(*_args, **_kwargs):
        return False

import llm
import prices

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
DATA = ROOT / "docs" / "data"
STATE_PATH = ROOT / "state.json"
JST = timezone(timedelta(hours=9))

BLOCKED = "YouTubeにアクセスを弾かれました（短時間に取りすぎた一時的な制限です）"

_CURL_CFFI = None

RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

SYSTEM_PROMPT = """あなたは日本の個人投資家のために、株式系YouTube動画の文字起こしを構造化するアナリストアシスタントです。

最重要ルール:
- 文字起こしに実際に出てきた内容だけを書く。推測で数値や理由を作らない。
- 動画内で語られていない項目は必ず空文字 "" にする。それらしい数字を埋めてはいけない。
- 出力は指定された JSON のみ。前置きも後書きも書かない。

各フィールドの決め方:
- stance: 買い・上昇を示唆 → "bullish" / 売り・下落・警戒を示唆 → "bearish" / 様子見・判断保留・両論併記 → "neutral"
- reasons: その銘柄を取り上げた根拠を1〜3個。1個あたり全角40〜80字。決算・チャート形状・材料・需給など具体的に。
- timestamp: その銘柄の話が始まる位置を "MM:SS"（1時間超なら "H:MM:SS"）。文字起こしの [ ] 内の時刻を使う。
- confidence: 動画のメイントピックとして詳しく解説 → "high" / 中くらい → "medium" / 名前が出た程度 → "low"
- name_en: 株価データの照合に使うので必ず入れる。上場企業の正式な英語社名（Kioxia Holdings、Suncall、Terra Drone など）。
- ticker: 日本株は4桁の証券コード（例 7203、新形式の 285A も可）、米国株はティッカー（例 NVDA）を必ず入れる。
  よく知られた銘柄なら文字起こしに出てこなくても分かる範囲で補ってよい。まったく確信が持てないときだけ空文字にする。
- 日経平均・TOPIX・S&P500・NASDAQ などの指数も銘柄として扱ってよい。
- 個別銘柄の話が一切ない動画は picks を空配列 [] にする。
- チャンネル宣伝、雑談、他サービスの案内は無視する。"""

USER_PROMPT_TEMPLATE = """# 動画情報
チャンネル: {channel}
タイトル: {title}
公開日時: {published}

# 文字起こし（[分:秒] 付き）
{transcript}

# 出力してほしい JSON
{{
  "overview": "この動画で何が語られたかの要約。3〜4文、全角150〜250字。",
  "market_view": "相場全体・地合いについての見解があれば1〜2文。無ければ空文字。",
  "market": "JP か US か MIX。判断できなければ空文字",
  "picks": [
    {{
      "name": "銘柄名（日本株は日本語名、米国株は企業名）",
      "name_en": "その企業の英語社名（例 Toyota Motor / Kioxia Holdings / SanDisk）。分からなければ空文字",
      "ticker": "日本株の証券コード(例 7203 / 285A) または 米国株ティッカー(例 NVDA)。確信が無いときだけ空文字",
      "market": "JP か US か OTHER",
      "stance": "bullish か bearish か neutral",
      "stance_note": "スタンスの一言要約（全角15〜30字）例: 決算通過後の押し目を待つ",
      "target_price": "動画内で語られた目標株価。無ければ空文字",
      "entry": "動画内で語られた買い場・エントリー価格帯。無ければ空文字",
      "stop": "動画内で語られた損切りライン。無ければ空文字",
      "reasons": ["根拠1", "根拠2", "根拠3"],
      "timestamp": "MM:SS",
      "confidence": "high か medium か low"
    }}
  ]
}}"""

DIGEST_SYSTEM = """あなたは日本の個人投資家に向けて、複数の株式系YouTuberの解説をまとめ、
「いま何を見ておけばよいか」を短くまとめるアナリストです。

最重要ルール:
- 与えられた各動画の要約に書かれている内容だけを使う。一般論や推測を足さない。
- チャートの着眼点は、動画で実際に語られた価格水準・指標・イベントに基づいて書く。
- 特定の銘柄を買うよう勧める書き方はしない。あくまで「見るべき点」を示す。
- 出力は指定された JSON のみ。"""

DIGEST_PROMPT = """# 直近の動画の要約（新しい順）
{material}

# 出力してほしい JSON
{{
  "headline": "いまの相場を一言で（全角20〜30字）。例: 半導体主導で戻りを試すが上値は重い",
  "trend": "いま何が起きているかを2〜3文、全角100〜160字。複数の動画に共通して出てくる話題を優先する。",
  "watch_points": [
    "チャートを見るときの着眼点。全角30〜60字。例: 日経平均は48,000円の節目を終値で超えられるかが当面の分かれ目",
    "同上（2つ目）",
    "同上（3つ目）"
  ],
  "consensus": "4人の見方の一致点と食い違いを1〜2文、全角60〜120字。",
  "mood": "全体の空気。bullish か bearish か neutral"
}}"""


# ---------------------------------------------------------------- ユーティリティ


def log(msg: str) -> None:
    print(msg, flush=True)


def run_text(cmd, cwd=None, timeout=120) -> subprocess.CompletedProcess:
    """外部コマンドを実行して、出力をUTF-8として読む。

    text=True だけだとWindowsではCP932で読もうとし、gitやyt-dlpが返す
    日本語（コミットメッセージ・動画タイトル）で UnicodeDecodeError になる。
    読めない文字は捨てて、処理そのものは止めない。
    """
    return subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout,
                          encoding="utf-8", errors="replace")


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log(f"  ! {path.name} が読めませんでした（{exc}）。初期値で続行します")
        return default


def save_json(path: Path, obj, compact: bool = False) -> None:
    """compact=True は数値の羅列（チャート）向け。改行を入れないぶん半分以下になる。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        if compact:
            json.dump(obj, fh, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, fh, ensure_ascii=False, indent=1)
    tmp.replace(path)


def fmt_ts(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def ts_to_seconds(ts: str) -> int:
    if not ts:
        return 0
    parts = [p for p in re.split(r"[:：]", ts.strip()) if p.strip().isdigit()]
    if not parts:
        return 0
    total = 0
    for p in parts:
        total = total * 60 + int(p)
    return total


# ---------------------------------------------------------------- RSS


def fetch_channel_videos(channel: dict, session: requests.Session) -> list[dict]:
    url = RSS_URL.format(channel["id"])
    try:
        resp = session.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except requests.RequestException as exc:
        log(f"  ! {channel['name']} のRSS取得に失敗: {exc}")
        return []

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        log(f"  ! {channel['name']} のRSS解析に失敗: {exc}")
        return []

    feed_title = root.findtext("atom:title", default="", namespaces=NS)
    if feed_title and feed_title != channel["name"]:
        log(f"    （YouTube上のチャンネル名: {feed_title}）")

    videos = []
    for entry in root.findall("atom:entry", NS):
        vid = entry.findtext("yt:videoId", default="", namespaces=NS)
        if not vid:
            continue
        # 公開日時は必ず UTC の ISO 文字列に揃える（文字列比較で並べ替え・絞り込みするため）
        published = entry.findtext("atom:published", default="", namespaces=NS)
        try:
            published = (
                datetime.fromisoformat(published.replace("Z", "+00:00"))
                .astimezone(timezone.utc).isoformat()
            )
        except ValueError:
            pass
        group = entry.find("media:group", NS)
        description = ""
        if group is not None:
            description = group.findtext("media:description", default="", namespaces=NS) or ""
        videos.append(
            {
                "video_id": vid,
                "title": entry.findtext("atom:title", default="(無題)", namespaces=NS),
                "published": published,
                "description": description[:400],
                "channel_id": channel["id"],
                "channel_name": channel["name"],
                "channel_short": channel.get("short", channel["name"][:2]),
                "slot": channel.get("slot", 1),
            }
        )
    return videos


# ---------------------------------------------------------------- 字幕


def _fetch_snippets(video_id: str, languages: list[str]):
    """youtube-transcript-api の新旧どちらの API でも動くようにする。"""
    from youtube_transcript_api import YouTubeTranscriptApi

    if hasattr(YouTubeTranscriptApi, "fetch"):  # v1.x 以降（インスタンス API）
        api = YouTubeTranscriptApi()
        fetched = api.fetch(video_id, languages=languages)
        return [
            {"text": s.text, "start": s.start, "duration": s.duration}
            for s in fetched
        ]

    raw = YouTubeTranscriptApi.get_transcript(video_id, languages=languages)  # v0.x
    return [
        {"text": r.get("text", ""), "start": r.get("start", 0.0), "duration": r.get("duration", 0.0)}
        for r in raw
    ]


def _has_curl_cffi() -> bool:
    """impersonation に必要な curl_cffi が入っているか。"""
    global _CURL_CFFI
    if _CURL_CFFI is None:
        try:
            import curl_cffi  # noqa: F401
            _CURL_CFFI = True
        except ImportError:
            _CURL_CFFI = False
    return _CURL_CFFI


def _fetch_snippets_ytdlp(video_id: str, languages: list[str], cookies_browser: str = "",
                          impersonate: str = "chrome"):
    """yt-dlp 経由で字幕を取る。youtube-transcript-api が弾かれたときの別経路。

    yt-dlp は YouTube 側の変更に追随が早く、ブラウザのCookieも使えるため通りやすい。
    """
    import glob
    import tempfile

    # YouTube は自動字幕を「元言語(<lang>-orig)」と「自動翻訳(<lang>)」に分けて持っており、
    # 翻訳側のエンドポイントは強くレート制限されている（HTTP 429）。
    # 元言語を先に要求すれば翻訳を経由せずに済む。
    sub_langs: list[str] = []
    for lang in languages:
        for cand in (f"{lang}-orig", lang):
            if cand not in sub_langs:
                sub_langs.append(cand)

    with tempfile.TemporaryDirectory() as td:
        cmd = [
            sys.executable, "-m", "yt_dlp",
            "--skip-download", "--write-subs", "--write-auto-subs",
            "--sub-langs", ",".join(sub_langs),
            "--sub-format", "json3",
            "--no-warnings", "--no-progress", "--quiet",
            "--retries", "2", "--socket-timeout", "30",
            "-o", str(Path(td) / "%(id)s"),
        ]
        # curl_cffi が入っていればブラウザのTLS指紋を真似る。
        # YouTube の字幕エンドポイントはここを見て弾いてくることがある。
        if impersonate and _has_curl_cffi():
            cmd += ["--impersonate", impersonate]
        if cookies_browser:
            cmd += ["--cookies-from-browser", cookies_browser]
        cmd.append(f"https://www.youtube.com/watch?v={video_id}")

        try:
            res = run_text(cmd, timeout=240)
        except FileNotFoundError as exc:
            raise RuntimeError("yt-dlp が見つかりません（pip install yt-dlp）") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("yt-dlp がタイムアウトしました") from exc

        files = glob.glob(str(Path(td) / "*.json3"))
        if not files:
            err = (res.stderr or res.stdout or "").strip().replace("\n", " ")
            raise RuntimeError(f"yt-dlp: 字幕を取得できませんでした {err[:200]}")

        # 優先順に選ぶ（ファイル名は <id>.<lang>.json3）。元言語を翻訳より優先する。
        def rank(path: str) -> int:
            name = Path(path).name
            for i, lang in enumerate(sub_langs):
                if f".{lang}." in name:
                    return i
            return len(sub_langs)

        files.sort(key=rank)
        data = json.loads(Path(files[0]).read_text(encoding="utf-8"))

    out = []
    for ev in data.get("events", []):
        text = "".join(seg.get("utf8", "") for seg in (ev.get("segs") or [])).strip()
        if not text:
            continue
        out.append({
            "text": text,
            "start": (ev.get("tStartMs") or 0) / 1000.0,
            "duration": (ev.get("dDurationMs") or 0) / 1000.0,
        })
    if not out:
        raise RuntimeError("yt-dlp: 字幕が空でした")
    return out


def get_transcript(video_id: str, languages: list[str], max_chars: int,
                   use_ytdlp: bool = True, cookies_browser: str = "",
                   impersonate: str = "chrome"):
    """(テキスト, エラー理由) を返す。取れなければ (None, 理由)。

    まず youtube-transcript-api、弾かれたら yt-dlp の順で試す。
    """
    snippets = None
    errors: list[str] = []

    try:
        snippets = _fetch_snippets(video_id, languages)
    except Exception as exc:
        errors.append(f"{type(exc).__name__} {exc}")

    if snippets is None and use_ytdlp:
        try:
            snippets = _fetch_snippets_ytdlp(video_id, languages, cookies_browser, impersonate)
            log("    （yt-dlp 経由で取得しました）")
            errors.clear()
        except Exception as exc:
            errors.append(f"{type(exc).__name__} {exc}")

    if snippets is None:
        joined = " | ".join(errors)
        # どちらかでもアクセス拒否を示していれば、一時的なものとして扱う
        # （記録を残さず次回やり直す。字幕が無いのと混同しないため最優先で判定）
        if any(k in joined for k in (
                "IpBlocked", "RequestBlocked", "blocking requests", "YouTube is blocking",
                "Sign in to confirm", "not a bot", "429", "Too Many Requests",
                "HTTP Error 403", "consent")):
            return None, BLOCKED
        # 「見られない動画」の判定を先に。字幕が無いのと理由が違うため。
        if any(k in joined for k in ("VideoUnavailable", "VideoUnplayable", "Private video",
                                     "This live event will begin", "Premieres in",
                                     "members-only", "is not available")):
            return None, "動画が非公開・削除・配信予定などで取得できません"
        if "AgeRestricted" in joined or "age-restricted" in joined:
            return None, "年齢制限付きの動画です"
        if any(k in joined for k in (
                "NoTranscript", "TranscriptsDisabled", "Subtitles are disabled",
                "no subtitles", "字幕を取得できませんでした", "字幕が空")):
            return None, "この動画には字幕がありません"
        return None, f"字幕取得エラー（{joined[:200]}）"

    if not snippets:
        return None, "字幕が空でした"

    # 15秒ごとにまとめて [MM:SS] を付ける（トークン節約 + タイムスタンプの精度確保）
    lines: list[str] = []
    bucket: list[str] = []
    bucket_start = snippets[0]["start"]
    for sn in snippets:
        text = (sn["text"] or "").replace("\n", " ").strip()
        if not text:
            continue
        if sn["start"] - bucket_start >= 15 and bucket:
            lines.append(f"[{fmt_ts(bucket_start)}] " + " ".join(bucket))
            bucket = []
            bucket_start = sn["start"]
        bucket.append(text)
    if bucket:
        lines.append(f"[{fmt_ts(bucket_start)}] " + " ".join(bucket))

    body = "\n".join(lines)
    if len(body) > max_chars:
        body = body[:max_chars] + "\n…（長いため以降を省略）"
    return body, None


# ---------------------------------------------------------------- 正規化・集約

_STANCES = {"bullish", "bearish", "neutral"}
_MARKETS = {"JP", "US", "OTHER"}
_CONF = {"high", "medium", "low"}


def norm_text(value, limit: int = 400) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if text.lower() in ("なし", "不明", "n/a", "na", "null", "none", "-", "―", "未記載"):
        return ""
    return text[:limit]


# 指数はティッカーが無く呼び名も揺れるので（日経平均／日経平均株価／日経225…）、
# 同じ指数は1枚のカードにまとまるよう、代表シンボルを鍵にする。
INDEX_KEYS = {prices.normalize_key(alias): syms[0]
              for alias, syms in prices.INDEX_ALIASES.items()}
INDEX_DISPLAY = {
    "^N225": "日経平均", "^TOPX": "TOPIX", "^GSPC": "S&P500",
    "^IXIC": "NASDAQ総合", "^NDX": "NASDAQ100", "^DJI": "NYダウ",
    "2516.T": "東証グロース250", "JPY=X": "ドル円",
}


def index_symbol(pick: dict) -> str:
    """指数なら代表シンボルを返す。個別株なら空文字。"""
    nkey = prices.normalize_key(pick.get("name", ""))
    tkey = prices.normalize_key(pick.get("ticker", ""))
    return INDEX_KEYS.get(nkey) or (INDEX_KEYS.get(tkey) if tkey else "") or ""


def stock_key(pick: dict) -> str:
    idx = index_symbol(pick)
    if idx:
        return f"IDX:{idx}"
    ticker = pick.get("ticker", "")
    market = pick.get("market", "OTHER")
    if ticker:
        return f"{market}:{ticker.upper()}"
    name = re.sub(r"\s+", "", pick.get("name", "")).upper()
    return f"NAME:{name}"


def clean_pick(raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    name = norm_text(raw.get("name"), 60)
    if not name:
        return None

    ticker = norm_text(raw.get("ticker"), 12).upper()
    # 「7203.T」「$NVDA」などの表記ゆれを寄せる
    ticker = ticker.replace("$", "").replace(".T", "").replace("東証:", "").strip()
    if ticker and not re.fullmatch(r"[A-Z0-9.\-]{1,10}", ticker):
        ticker = ""

    market = norm_text(raw.get("market"), 8).upper()
    if market not in _MARKETS:
        market = "JP" if re.fullmatch(r"\d{4}", ticker) else ("US" if ticker else "OTHER")

    stance = norm_text(raw.get("stance"), 12).lower()
    if stance not in _STANCES:
        stance = "neutral"

    confidence = norm_text(raw.get("confidence"), 10).lower()
    if confidence not in _CONF:
        confidence = "medium"

    reasons = raw.get("reasons") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    reasons = [norm_text(r, 200) for r in reasons if norm_text(r, 200)][:3]

    return {
        "name": name,
        "name_en": norm_text(raw.get("name_en"), 80),
        "ticker": ticker,
        "market": market,
        "stance": stance,
        "stance_note": norm_text(raw.get("stance_note"), 80),
        "target_price": norm_text(raw.get("target_price"), 60),
        "entry": norm_text(raw.get("entry"), 60),
        "stop": norm_text(raw.get("stop"), 60),
        "reasons": reasons,
        "timestamp": norm_text(raw.get("timestamp"), 12),
        "confidence": confidence,
    }


def summarize_video(video: dict, transcript: str, llm_cfg: dict) -> dict:
    published_jst = ""
    try:
        published_jst = (
            datetime.fromisoformat(video["published"].replace("Z", "+00:00"))
            .astimezone(JST)
            .strftime("%Y-%m-%d %H:%M")
        )
    except (ValueError, KeyError):
        published_jst = video.get("published", "")

    prompt = USER_PROMPT_TEMPLATE.format(
        channel=video["channel_name"],
        title=video["title"],
        published=published_jst,
        transcript=transcript,
    )
    result = llm.call_json(SYSTEM_PROMPT, prompt, llm_cfg)

    picks = []
    seen = set()
    for raw in result.get("picks") or []:
        pick = clean_pick(raw)
        if not pick:
            continue
        key = stock_key(pick)
        if key in seen:  # 同一動画内の重複はまとめる
            continue
        seen.add(key)
        picks.append(pick)

    market = norm_text(result.get("market"), 8).upper()
    if market not in ("JP", "US", "MIX"):
        markets = {p["market"] for p in picks}
        market = markets.pop() if len(markets) == 1 else ("MIX" if markets else "")

    return {
        "overview": norm_text(result.get("overview"), 800),
        "market_view": norm_text(result.get("market_view"), 400),
        "market": market,
        "picks": picks,
    }


def prune(videos: list[dict], retain_days: int, per_channel: int = 0) -> list[dict]:
    """古い動画と、チャンネルごとの余分な本数を落とす。"""
    kept = list(videos)
    if retain_days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retain_days)).isoformat()
        kept = [v for v in kept if v.get("published", "") >= cutoff]

    if per_channel:
        by_channel: dict[str, list[dict]] = {}
        for v in kept:
            by_channel.setdefault(v["channel_id"], []).append(v)
        trimmed = []
        for group in by_channel.values():
            group.sort(key=lambda v: v.get("published", ""), reverse=True)
            trimmed.extend(group[:per_channel])
        kept = trimmed

    dropped = len(videos) - len(kept)
    if dropped:
        log(f"  古い動画 {dropped} 本を履歴から外しました（各chの最新{per_channel}本を保持）"
            if per_channel else f"  古い動画 {dropped} 本を履歴から外しました")
    return kept


def write_outputs(videos: list[dict], channels: list[dict], cfg: dict) -> dict:
    """index.json / videos.json を書き出す。株価も必要なら更新して埋め込む。"""
    index = build_index(videos, channels)

    if cfg.get("enable_prices", True) and index["stocks"]:
        cache = load_json(DATA / "prices.json", {"symbols": {}})
        charts_old = load_json(DATA / "charts.json", {"charts": {}}).get("charts", {})
        charts = {}
        try:
            cache = prices.update_prices(index["stocks"], cache, cfg, log=log, charts=charts)
            save_json(DATA / "prices.json", cache)
            # 今回取り直さなかった銘柄は前回のチャートを引き継ぐ
            keys = {st.get("key") for st in index["stocks"]}
            merged = {k: v for k, v in charts_old.items() if k in keys and k not in charts}
            merged.update(charts)
            save_json(DATA / "charts.json",
                      {"updated_at": index["generated_at"], "charts": merged}, compact=True)
            with_profile = sum(1 for v in merged.values() if v.get("profile"))
            log(f"  チャート: {len(merged)} 銘柄（うち価格帯別出来高 {with_profile} 銘柄）"
                + (f" / うち今回更新 {len(charts)} 銘柄" if charts else " / 今回は保存済みを流用"))
        except Exception as exc:  # 株価が取れなくても本体は壊さない
            log(f"  ! 株価の更新に失敗しました（要約データはそのまま使えます）: {exc}")
        prices.attach(index["stocks"], cache)

    save_json(DATA / "index.json", index)
    save_json(DATA / "videos.json", build_video_list(videos))
    return index


def build_digest(videos: list[dict], llm_cfg: dict, limit: int = 8) -> dict | None:
    """直近の動画をまとめて「今日のポイント」を作る。要約1回ぶんの追加コストで済む。"""
    usable = [v for v in videos if not v.get("no_transcript") and v.get("overview")]
    usable.sort(key=lambda v: v.get("published", ""), reverse=True)
    usable = usable[:limit]
    if not usable:
        return None

    blocks = []
    for v in usable:
        when = v.get("published", "")[:10]
        picks = "、".join(
            f"{p['name']}（{ {'bullish': '強気', 'bearish': '弱気'}.get(p['stance'], '中立') }"
            f"{'：' + p['stance_note'] if p.get('stance_note') else ''}）"
            for p in v.get("picks", [])[:8]
        ) or "個別銘柄の言及なし"
        blocks.append(
            f"## {v['channel_name']}（{when}）\n"
            f"タイトル: {v['title']}\n"
            f"要約: {v.get('overview', '')}\n"
            f"相場観: {v.get('market_view', '') or '（言及なし）'}\n"
            f"取り上げた銘柄: {picks}"
        )

    result = llm.call_json(DIGEST_SYSTEM, DIGEST_PROMPT.format(material="\n\n".join(blocks)), llm_cfg)

    points = result.get("watch_points") or []
    if isinstance(points, str):
        points = [points]
    points = [norm_text(x, 160) for x in points if norm_text(x, 160)][:3]

    mood = norm_text(result.get("mood"), 12).lower()
    if mood not in ("bullish", "bearish", "neutral"):
        mood = "neutral"

    return {
        "generated_at": datetime.now(JST).isoformat(),
        "headline": norm_text(result.get("headline"), 120),
        "trend": norm_text(result.get("trend"), 500),
        "watch_points": points,
        "consensus": norm_text(result.get("consensus"), 400),
        "mood": mood,
        "based_on": [
            {"channel": v["channel_name"], "title": v["title"],
             "video_id": v["video_id"], "published": v.get("published", "")}
            for v in usable
        ],
    }


def build_index(videos: list[dict], channels: list[dict]) -> dict:
    """動画ごとの要約を「銘柄軸」に組み替える。"""
    # ティッカー無しの言及を、同名でティッカー有りの銘柄に寄せるための対応表
    name_to_key: dict[str, str] = {}
    for v in videos:
        for p in v.get("picks", []):
            if p["ticker"]:
                name_to_key.setdefault(re.sub(r"\s+", "", p["name"]).upper(), stock_key(p))

    # 略号と表示色は保存済みの値ではなく、今の設定を優先する（設定変更が即反映される）
    ch_now = {c["id"]: c for c in channels}

    stocks: dict[str, dict] = {}
    for v in sorted(videos, key=lambda x: x.get("published", ""), reverse=True):
        conf = ch_now.get(v["channel_id"])
        if conf:
            v = {**v, "channel_name": conf["name"],
                 "channel_short": conf.get("short", v["channel_short"]),
                 "slot": conf.get("slot", v["slot"])}
        for p in v.get("picks", []):
            key = stock_key(p)
            if key.startswith("NAME:"):
                key = name_to_key.get(key[5:], key)
            entry = stocks.setdefault(
                key,
                {
                    "key": key,
                    "name": INDEX_DISPLAY.get(key[4:], p["name"]) if key.startswith("IDX:")
                            else p["name"],
                    "name_en": p.get("name_en", ""),
                    "ticker": p["ticker"],
                    "market": p["market"],
                    "mentions": [],
                },
            )
            if p.get("name_en") and not entry.get("name_en"):
                entry["name_en"] = p["name_en"]
            if p["ticker"] and not entry["ticker"]:
                entry["ticker"] = p["ticker"]
                entry["market"] = p["market"]
            entry["mentions"].append(
                {
                    "channel_id": v["channel_id"],
                    "channel_name": v["channel_name"],
                    "channel_short": v["channel_short"],
                    "slot": v["slot"],
                    "video_id": v["video_id"],
                    "video_title": v["title"],
                    "published": v["published"],
                    "stance": p["stance"],
                    "stance_note": p["stance_note"],
                    "target_price": p["target_price"],
                    "entry": p["entry"],
                    "stop": p["stop"],
                    "reasons": p["reasons"],
                    "timestamp": p["timestamp"],
                    "seconds": ts_to_seconds(p["timestamp"]),
                    "confidence": p["confidence"],
                }
            )

    out = []
    for entry in stocks.values():
        entry["mentions"].sort(key=lambda m: m["published"], reverse=True)
        # コンセンサスは「チャンネルごとの最新の見解」で数える
        latest_by_channel: dict[str, dict] = {}
        for m in entry["mentions"]:
            latest_by_channel.setdefault(m["channel_id"], m)
        counts = {"bullish": 0, "bearish": 0, "neutral": 0}
        for m in latest_by_channel.values():
            counts[m["stance"]] += 1
        entry["consensus"] = counts
        entry["voices"] = len(latest_by_channel)
        entry["split"] = counts["bullish"] > 0 and counts["bearish"] > 0
        entry["last_mentioned"] = entry["mentions"][0]["published"]
        entry["channel_ids"] = sorted(latest_by_channel.keys())
        out.append(entry)

    out.sort(key=lambda e: (e["voices"], e["last_mentioned"]), reverse=True)

    now = datetime.now(JST)
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    summarized = [v for v in videos if not v.get("no_transcript")]
    return {
        "generated_at": now.isoformat(),
        "channels": [
            {
                "id": c["id"],
                "name": c["name"],
                "short": c.get("short", c["name"][:2]),
                "slot": c.get("slot", 1),
                "video_count": sum(1 for v in videos if v["channel_id"] == c["id"]),
                "latest": max(
                    (v["published"] for v in videos if v["channel_id"] == c["id"]),
                    default="",
                ),
            }
            for c in channels
        ],
        "stats": {
            "stocks": len(out),
            "videos": len(summarized),
            "videos_this_week": sum(1 for v in summarized if v.get("published", "") >= week_ago),
            "bullish_lead": sum(
                1 for e in out if e["consensus"]["bullish"] > e["consensus"]["bearish"]
            ),
            "split": sum(1 for e in out if e["split"]),
            "skipped": sum(1 for v in videos if v.get("no_transcript")),
        },
        "stocks": out,
    }


def build_video_list(videos: list[dict]) -> dict:
    ordered = sorted(videos, key=lambda v: v.get("published", ""), reverse=True)
    return {
        "generated_at": datetime.now(JST).isoformat(),
        "videos": [
            {
                "video_id": v["video_id"],
                "title": v["title"],
                "channel_id": v["channel_id"],
                "channel_name": v["channel_name"],
                "channel_short": v["channel_short"],
                "slot": v["slot"],
                "published": v["published"],
                "overview": v.get("overview", ""),
                "market_view": v.get("market_view", ""),
                "market": v.get("market", ""),
                "no_transcript": v.get("no_transcript", False),
                "skip_reason": v.get("skip_reason", ""),
                "picks": [
                    {
                        "name": p["name"],
                        "ticker": p["ticker"],
                        "stance": p["stance"],
                        "timestamp": p["timestamp"],
                    }
                    for p in v.get("picks", [])
                ],
            }
            for v in ordered
        ],
    }


# ---------------------------------------------------------------- git


def git_push_if_enabled() -> None:
    if os.environ.get("GIT_AUTO_PUSH", "0").strip() not in ("1", "true", "True"):
        return
    try:
        status = run_text(["git", "status", "--porcelain", "docs/data"], cwd=ROOT, timeout=60)
        if not status.stdout.strip():
            log("  変更なし。pushはスキップします")
            return
        stamp = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        for cmd in (
            ["git", "add", "docs/data"],
            ["git", "commit", "-m", f"データ更新 {stamp}"],
            ["git", "push"],
        ):
            res = run_text(cmd, cwd=ROOT, timeout=180)
            if res.returncode != 0:
                log(f"  ! git {cmd[1]} に失敗: {res.stderr.strip()[:300]}")
                return
        log("  GitHub にpushしました")
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"  ! git 実行に失敗: {exc}")


# ---------------------------------------------------------------- main


def main() -> int:
    """どの経路で終わっても最後に一度だけ push を試す。

    要約した回だけでなく、株価だけ更新した回・今日のポイントだけ作り直した回も
    docs/data が変わるため、公開サイトに反映する必要がある。
    """
    code = _run()
    if code == 0:
        git_push_if_enabled()
    return code


def _run() -> int:
    parser = argparse.ArgumentParser(description="株系YouTube要約コレクター")
    parser.add_argument("--limit", type=int, default=None, help="今回処理する動画の上限")
    parser.add_argument("--video", action="append", default=[], help="この動画IDだけ処理し直す")
    parser.add_argument("--rebuild", action="store_true", help="要約せず index.json を作り直すだけ")
    parser.add_argument("--dry-run", action="store_true", help="LLMを呼ばずに対象と字幕の有無を確認")
    parser.add_argument("--force", action="store_true", help="制限中の待機を無視して実行する")
    parser.add_argument("--digest", action="store_true",
                        help="新着が無くても「今日のポイント」を作り直す")
    parser.add_argument("--refresh-prices", action="store_true",
                        help="保存済みの株価を使わず、全銘柄を取り直す")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    cfg = load_json(SCRIPTS / "config.json", None)
    if cfg is None:
        log("config.json が読めません。中止します。")
        return 1

    if args.refresh_prices:
        cfg["price_refresh_hours"] = 0      # キャッシュを無視して全部取り直す
        log("  株価は保存済みを使わず取り直します")

    llm_cfg = cfg.get("llm", {})
    channels = cfg.get("channels", [])
    store = load_json(DATA / "videos_full.json", {"videos": []})
    known = {v["video_id"]: v for v in store.get("videos", [])}

    if args.rebuild or (args.digest and not args.video):
        videos = prune(list(known.values()), cfg.get("retain_days", 90),
                       cfg.get("max_videos_per_channel", 0))
        if args.digest:
            try:
                digest = build_digest(videos, llm_cfg, cfg.get("digest_videos", 8))
                if digest:
                    save_json(DATA / "digest.json", digest)
                    log(f"  今日のポイント: {digest['headline']}")
                else:
                    log("  要約済みの動画が無いため、今日のポイントは作れませんでした")
            except llm.LLMError as exc:
                log(f"  ! 今日のポイントの作成に失敗しました: {exc}")
        if not args.rebuild:
            write_outputs(videos, channels, cfg)
            return 0
        write_outputs(videos, channels, cfg)
        log(f"index.json を再生成しました（動画 {len(videos)} 本）")
        return 0

    # 前回ブロックされていたら、しばらくYouTubeには触らない。
    # 1時間おきのタスクが制限中に叩き続けると、解除がさらに遠のくため。
    state = load_json(STATE_PATH, {})
    blocked_until = state.get("blocked_until", "")
    if blocked_until and not args.force and not args.video:
        now_iso = datetime.now(timezone.utc).isoformat()
        if now_iso < blocked_until:
            try:
                until_jst = datetime.fromisoformat(blocked_until).astimezone(JST).strftime("%H:%M")
            except ValueError:
                until_jst = blocked_until
            log(f"■ YouTube側の制限を受けたため {until_jst} まで待機中です")
            log("  （株価とindexだけ更新します。すぐ試したいときは .\\run.ps1 --force）")
            videos = prune(list(known.values()), cfg.get("retain_days", 90),
                           cfg.get("max_videos_per_channel", 0))
            write_outputs(videos, channels, cfg)
            return 0
        log("  制限の待機時間が明けました。再開します")

    session = requests.Session()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=cfg.get("lookback_days", 14))).isoformat()

    max_attempts = cfg.get("max_transcript_attempts", 8)
    per_channel = cfg.get("max_videos_per_channel", 0)

    def wanted(video: dict) -> bool:
        """まだ要約できていない動画か。字幕待ちのものは回数制限つきで再挑戦する。"""
        prev = known.get(video["video_id"])
        if prev is None:
            return True
        if not prev.get("no_transcript"):
            return False
        # 投稿直後は自動字幕がまだ生成されていないことがある。ライブの予約枠も後から本編になる。
        # そのため一度失敗しても、しばらくは実行のたびに取り直しを試す。
        return prev.get("attempts", 1) < max_attempts

    log("■ 新着チェック")
    candidates: list[dict] = []
    for ch in channels:
        found = fetch_channel_videos(ch, session)
        if args.video:
            fresh = [v for v in found if v["video_id"] in args.video]
        else:
            recent = sorted(found, key=lambda v: v.get("published", ""), reverse=True)
            if per_channel:
                recent = recent[:per_channel]      # 各chの最新N本だけを対象にする
            fresh = [v for v in recent if v["published"] >= cutoff and wanted(v)]
        retries = sum(1 for v in fresh if v["video_id"] in known)
        detail = f"（うち字幕の取り直し {retries} 本）" if retries else ""
        log(f"  {ch['name']}: 取得 {len(found)} 本 / 未処理 {len(fresh)} 本{detail}")
        candidates.extend(fresh)

    def order(video: dict):
        # 新規を優先し、字幕待ちの再挑戦は後ろへ。同じ区分の中では新しい順。
        is_retry = video["video_id"] in known
        try:
            ts = datetime.fromisoformat(video["published"]).timestamp()
        except ValueError:
            ts = 0.0
        return (is_retry, -ts)

    candidates.sort(key=order)
    limit = args.limit if args.limit is not None else cfg.get("max_videos_per_run", 6)
    if limit and len(candidates) > limit:
        log(f"  今回は新しい方から {limit} 本だけ処理します（残りは次回）")
        candidates = candidates[:limit]

    if not candidates:
        log("  新着なし。株価とindex.jsonだけ更新します")
        videos = prune(list(known.values()), cfg.get("retain_days", 90),
                       cfg.get("max_videos_per_channel", 0))
        save_json(DATA / "videos_full.json", {"videos": videos})
        write_outputs(videos, channels, cfg)
        return 0

    max_chars = llm_cfg.get("max_transcript_chars", 90000)
    languages = cfg.get("transcript_languages", ["ja", "en"])
    use_ytdlp = cfg.get("use_ytdlp_fallback", True)
    cookies_browser = cfg.get("ytdlp_cookies_from_browser", "")
    impersonate = cfg.get("ytdlp_impersonate", "chrome")
    if use_ytdlp and impersonate and not _has_curl_cffi():
        log("  ヒント: pip install \"yt-dlp[default,curl-cffi]\" を入れると"
            "ブラウザ偽装が使えて弾かれにくくなります")
    processed = 0
    failures = 0

    log("\n■ 要約")
    pause = cfg.get("pause_seconds", 5)
    hit_block = False
    for n, video in enumerate(candidates):
        if n:
            time.sleep(pause)  # 連続アクセスで弾かれないよう間隔をあける
        log(f"  > [{video['channel_name']}] {video['title'][:50]}")
        transcript, reason = get_transcript(video["video_id"], languages, max_chars,
                                            use_ytdlp, cookies_browser, impersonate)

        if transcript is None and reason == BLOCKED:
            log("    弾かれました。90秒待って一度だけ試し直します")
            time.sleep(90)
            transcript, reason = get_transcript(video["video_id"], languages, max_chars,
                                                use_ytdlp, cookies_browser, impersonate)

        if transcript is None and reason == BLOCKED:
            # 一時的な制限なので記録を残さない（残すと再試行の回数を無駄に消費する）。
            cooldown = cfg.get("block_cooldown_hours", 3)
            until = (datetime.now(timezone.utc) + timedelta(hours=cooldown)).isoformat()
            save_json(STATE_PATH, {**state, "blocked_until": until,
                                   "blocked_at": datetime.now(JST).isoformat()})
            log(f"    まだ弾かれます。記録は残さず中断し、{cooldown}時間は自動でお休みします")
            log("    （タスクスケジューラが時間をおいて自動で再開します）")
            hit_block = True
            break

        if transcript is None:
            attempts = known.get(video["video_id"], {}).get("attempts", 0) + 1
            if attempts < max_attempts:
                log(f"    スキップ: {reason} → 次回また試します（{attempts}/{max_attempts}回目）")
            else:
                log(f"    スキップ: {reason} → {max_attempts}回試したのでこれ以上は試しません")
            known[video["video_id"]] = {
                **video, "no_transcript": True, "skip_reason": reason, "attempts": attempts,
                "overview": "", "market_view": "", "market": "", "picks": [],
            }
            failures += 1
            continue

        log(f"    字幕 {len(transcript):,} 文字")
        if args.dry_run:
            continue

        try:
            summary = summarize_video(video, transcript, llm_cfg)
        except llm.LLMError as exc:
            log(f"    ! 要約に失敗: {exc}")
            failures += 1
            continue

        known[video["video_id"]] = {**video, "no_transcript": False, "skip_reason": "", **summary}
        processed += 1
        names = "、".join(p["name"] for p in summary["picks"][:6]) or "（個別銘柄の言及なし）"
        log(f"    銘柄 {len(summary['picks'])} 件: {names}")

    if args.dry_run:
        log("\n（--dry-run のため保存しませんでした）")
        return 0

    videos = prune(list(known.values()), cfg.get("retain_days", 90),
                   cfg.get("max_videos_per_channel", 0))
    save_json(DATA / "videos_full.json", {"videos": videos})

    # 新しい要約ができたときだけ「今日のポイント」を作り直す
    if processed and cfg.get("enable_digest", True):
        try:
            digest = build_digest(videos, llm_cfg, cfg.get("digest_videos", 8))
            if digest:
                save_json(DATA / "digest.json", digest)
                log(f"  今日のポイント: {digest['headline']}")
        except llm.LLMError as exc:
            log(f"  ! 今日のポイントの作成に失敗しました（他は問題ありません）: {exc}")

    index = write_outputs(videos, channels, cfg)
    # ブロックで中断した回は、記録した待機時刻を消さない
    keep_until = load_json(STATE_PATH, {}).get("blocked_until", "") if hit_block else ""
    save_json(STATE_PATH, {"last_run": datetime.now(JST).isoformat(),
                           "video_count": len(videos), "blocked_until": keep_until})

    log(f"\n■ 完了: 今回 {processed} 本を要約（スキップ/失敗 {failures} 本）")
    log(f"  追跡中の銘柄: {index['stats']['stocks']} 件 / 動画 {index['stats']['videos']} 本")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("\n中断しました")
        sys.exit(130)

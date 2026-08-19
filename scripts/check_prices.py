"""株価データが取れるかを確認する診断ツール。

AIは呼ばないので料金はかかりません。

  python check_prices.py                      いま追跡中の銘柄で確認
  python check_prices.py 7203 NVDA 日経平均    指定した銘柄で確認
  python check_prices.py --raw 7203           生の応答の冒頭を表示（原因調査用）
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*_a, **_k):
        return False

import collect
import prices


def show_raw(arg: str, session: requests.Session) -> None:
    """うまくいかないときに、実際に何が返っているかを見る。"""
    syms = prices.resolve_symbols(arg, arg, "JP" if arg[:1].isdigit() else "US")
    if not syms:
        print(f"  {arg}: シンボルを特定できませんでした")
        return
    sym = syms[0]
    url = prices.YAHOO_URL.format(symbol=sym)
    print(f"\n--- {arg} → {sym} ---")
    print(f"URL: {url}?range=1y&interval=1d")
    try:
        r = session.get(url, params={"range": "1mo", "interval": "1d"},
                        headers={"User-Agent": prices.UA}, timeout=30)
        print(f"HTTP {r.status_code} / Content-Type: {r.headers.get('Content-Type', '?')}")
        print("本文の冒頭 300 文字:")
        print(r.text[:300])
    except requests.RequestException as exc:
        print(f"通信エラー: {exc}")


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    load_dotenv(root / ".env")
    cfg = collect.load_json(Path(__file__).resolve().parent / "config.json", {})
    session = requests.Session()
    pause = cfg.get("price_pause_seconds", 2)

    args = [a for a in sys.argv[1:] if a != "--raw"]
    if "--raw" in sys.argv:
        for a in (args or ["7203"]):
            show_raw(a, session)
        return 0

    targets: list[tuple[str, str, str]] = []      # (表示名, ticker, market)
    if args:
        for a in args:
            targets.append((a, a, "JP" if a[:1].isdigit() else "US"))
    else:
        index = collect.load_json(collect.DATA / "index.json", {"stocks": []})
        for st in index.get("stocks", []):
            targets.append((st["name"], st.get("ticker", ""), st.get("market", "")))
        if not targets:
            print("追跡中の銘柄がまだありません。代表的な銘柄で試します。")
            targets = [("トヨタ自動車", "7203", "JP"), ("エヌビディア", "NVDA", "US"),
                       ("日経平均", "N225", "JP")]

    print("=" * 66)
    print(f" 株価チェック（Yahoo Finance のチャートAPI／APIキー不要）  版: {prices.VERSION}")
    print("=" * 66)

    ok = 0
    for i, (label, ticker, market) in enumerate(targets):
        if i:
            time.sleep(pause)
        data, reason = prices.fetch_one(label, ticker, market, session)
        if data is None:
            print(f"  x {label}: {reason}")
            continue
        ok += 1
        cur = data.get("currency", "")
        unit = "" if data["symbol"].startswith("^") else ("円" if cur == "JPY" else "$" if cur == "USD" else "")
        chg = f"{data['change_pct']:+.2f}%" if data["change_pct"] is not None else "-"
        vr = f"{data['volume_ratio']}倍" if data["volume_ratio"] else "-"
        note = "  ※取引時間中のため前営業日" if data.get("market_open") else ""
        print(f"  o {label}（{data['symbol']}）: {data['date']} 終値 {data['close']}{unit} {chg}"
              f" / RSI {data['rsi14']} {data['rsi_zone']} / 出来高 {vr} / {data['trend']}{note}")

    print("=" * 66)
    print(f" {len(targets)}件中 {ok}件で取得できました。")
    if ok == 0:
        print(" 原因を調べるには次を実行して、出力を見せてください:")
        print("   .\\.venv\\Scripts\\python.exe scripts\\check_prices.py --raw 7203")
        print(" 株価が不要なら config.json の enable_prices を false にできます。")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

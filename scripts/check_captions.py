"""最初に1回だけ走らせる診断ツール。

4チャンネルそれぞれについて
  ・RSSが取れるか（チャンネルIDが正しいか）
  ・直近の動画に字幕が付いているか
  ・投稿頻度はどのくらいか
を確認して表示する。LLMは呼ばないので料金はかからない。

  python check_captions.py
  python check_captions.py --per-channel 5
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import time

import requests

import collect
import prices

JST = collect.JST


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-channel", type=int, default=3, help="1チャンネルあたり何本試すか")
    args = parser.parse_args()

    cfg = collect.load_json(Path(__file__).resolve().parent / "config.json", None)
    if cfg is None:
        print("config.json が読めません")
        return 1

    session = requests.Session()
    languages = cfg.get("transcript_languages", ["ja", "en"])
    print("=" * 62)
    print(f" 字幕チェック（このPCのIPからYouTubeにアクセスできるか確認します）  版: {prices.VERSION}")
    print("=" * 62)

    all_ok = True
    for ch in cfg.get("channels", []):
        print(f"\n■ {ch['name']}  ({ch['id']})")
        videos = collect.fetch_channel_videos(ch, session)
        if not videos:
            print("  × RSSが取得できませんでした。チャンネルIDを確認してください")
            all_ok = False
            continue

        # 投稿頻度をざっくり出す
        dates = []
        for v in videos:
            try:
                dates.append(datetime.fromisoformat(v["published"].replace("Z", "+00:00")))
            except ValueError:
                pass
        freq = ""
        if len(dates) >= 2:
            span = (max(dates) - min(dates)).days or 1
            freq = f" / 直近{len(dates)}本を{span}日で投稿（約{span / len(dates):.1f}日に1本）"
        newest = max(dates).astimezone(JST).strftime("%Y-%m-%d %H:%M") if dates else "?"
        print(f"  ○ RSS OK：{len(videos)}本取得、最新 {newest}{freq}")

        ok = 0
        for i, v in enumerate(videos[: args.per_channel]):
            if i or ch is not cfg["channels"][0]:
                time.sleep(cfg.get("pause_seconds", 5))  # 弾かれないよう間隔をあける
            text, reason = collect.get_transcript(
                v["video_id"], languages, 200000,
                cfg.get("use_ytdlp_fallback", True),
                cfg.get("ytdlp_cookies_from_browser", ""),
                cfg.get("ytdlp_impersonate", "chrome"))
            title = v["title"][:44]
            if text:
                ok += 1
                print(f"    ○ 字幕あり {len(text):>7,}字  {title}")
            else:
                print(f"    × {reason}")
                print(f"        {title}")
        if ok == 0:
            all_ok = False
            print("    → このチャンネルは字幕から要約できません")
        elif ok < args.per_channel:
            print(f"    → {args.per_channel}本中{ok}本で字幕あり（無い動画はアプリ上でスキップ表示されます）")

    print("\n" + "=" * 62)
    if all_ok:
        print(" すべて問題なし。collect.py を実行できます。")
    else:
        print(" 一部に問題があります。上の × の行を確認してください。")
        print(" 「IPをブロックされました」と出た場合は、クラウドではなく自宅PCで実行してください。")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

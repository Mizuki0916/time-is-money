"""アプリの見た目を確認するためのサンプルデータを作る。

本物のチャンネル名は使わず「サンプル投稿者A〜D」とダミー銘柄で埋める。
（実在の投稿者に、実際には言っていない見解が紐づくのを避けるため）

  python make_sample.py

collect.py を一度でも実行すれば、この内容は本物のデータで上書きされる。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import collect
import prices

JST = collect.JST
# 公開日時は collect.py と同じく UTC の ISO 文字列で持つ
NOW = datetime.now(timezone.utc)

SHAPES = [
    {"drift": 0.0030, "late": 0.0180, "vol": 2.4},   # 上昇 → 買われすぎ・出来高急増
    {"drift": -0.0010, "late": -0.0160, "vol": 1.1},  # 下落 → 売られすぎ
    {"drift": 0.0015, "late": 0.0010, "vol": 0.9},   # ゆるやかな上昇
    {"drift": -0.0005, "late": 0.0040, "vol": 1.7},  # 調整後の戻り・出来高増
    {"drift": 0.0008, "late": -0.0020, "vol": 0.8},  # もみ合い
]

CHANNELS = [
    {"id": "SAMPLE_A", "name": "サンプル投稿者A", "short": "SA", "slot": 1},
    {"id": "SAMPLE_B", "name": "サンプル投稿者B", "short": "SB", "slot": 2},
    {"id": "SAMPLE_C", "name": "サンプル投稿者C", "short": "SC", "slot": 3},
    {"id": "SAMPLE_D", "name": "サンプル投稿者D", "short": "SD", "slot": 4},
]


def pick(name, ticker, market, stance, note, target="", entry="", stop="", reasons=(), ts="03:20", conf="high"):
    return {
        "name": name, "ticker": ticker, "market": market, "stance": stance,
        "stance_note": note, "target_price": target, "entry": entry, "stop": stop,
        "reasons": list(reasons), "timestamp": ts, "confidence": conf,
    }


VIDEOS = [
    {
        "ch": 0, "hours": 5, "title": "【サンプル】今週の注目5銘柄と地合いの見方",
        "overview": "これは表示確認用のサンプル要約です。実際の動画を処理すると、ここに動画全体の内容が3〜4文で入ります。決算を通過した主力銘柄の反応と、指数の節目について解説する構成を想定しています。",
        "market_view": "指数は上値の重い展開が続くものの、押し目では買いが入りやすいという見立て。",
        "market": "MIX",
        "picks": [
            pick("サンプル電機", "1111", "JP", "bullish", "決算通過後の押し目を拾いたい",
                 "3,200円", "2,780〜2,850円", "2,650円",
                 ["営業利益が会社計画を上回り、通期見通しも上方修正された",
                  "受注残が過去最高水準で、来期の業績見通しにも余裕がある",
                  "25日線を明確に上抜けて出来高も伴っている"], "04:12"),
            pick("サンプル商事", "2222", "JP", "bearish", "戻り売りで様子を見たい",
                 "", "", "",
                 ["主力事業の粗利率が3期連続で低下している",
                  "為替前提が円安寄りで、想定より円高に振れると下振れリスクがある"], "11:40", "medium"),
            pick("サンプルテック", "SMPL", "US", "bullish", "長期の積み立て対象として妙味",
                 "$240", "$190前後", "",
                 ["データセンター向けの売上が前年比で大きく伸びている",
                  "粗利率が改善傾向で、価格決定力があると判断"], "19:05"),
            pick("日経平均", "N225", "JP", "neutral", "レンジ内での往来を想定",
                 "", "", "", ["節目を挟んだもみ合いが続いており、方向感が出るまで待ちたい"], "01:30", "medium"),
        ],
    },
    {
        "ch": 1, "hours": 20, "title": "【サンプル】決算を受けて見方を変えた銘柄",
        "overview": "これは表示確認用のサンプル要約です。決算内容を受けてスタンスを変更した銘柄について、変更の理由を中心に整理する構成を想定しています。",
        "market_view": "セクター間の物色の偏りが強く、指数だけを見ていると判断を誤りやすいという指摘。",
        "market": "JP",
        "picks": [
            pick("サンプル電機", "1111", "JP", "bullish", "増額修正を評価して継続保有",
                 "3,400円", "", "2,700円",
                 ["上方修正の内容が一過性ではなく構造的な改善に見える",
                  "同業他社と比べて割安に放置されている"], "06:55"),
            pick("サンプル商事", "2222", "JP", "bullish", "悪材料は織り込み済みと判断",
                 "", "1,180円割れ", "",
                 ["株価が既に大きく調整しており、PBRが解散価値を下回っている",
                  "自社株買いの発表で需給が改善する可能性がある"], "14:22", "medium"),
            pick("サンプル製薬", "3333", "JP", "neutral", "承認待ちで判断は保留",
                 "", "", "", ["新薬の承認スケジュール次第で振れ幅が大きく、現時点では手を出しにくい"], "22:10", "low"),
        ],
    },
    {
        "ch": 2, "hours": 30, "title": "【サンプル】米国株の押し目はどこか",
        "overview": "これは表示確認用のサンプル要約です。米国市場のセクター動向と、個別銘柄のエントリー水準について解説する構成を想定しています。",
        "market_view": "金利の低下局面ではグロース株に資金が戻りやすいという整理。",
        "market": "US",
        "picks": [
            pick("サンプルテック", "SMPL", "US", "neutral", "水準としてはまだ高い",
                 "", "$165まで待ちたい", "",
                 ["バリュエーションが過去5年平均を大きく上回っている",
                  "成長率の鈍化が次の決算で表面化する可能性がある"], "08:44"),
            pick("サンプル電機", "1111", "JP", "bullish", "海外投資家の買いが入りやすい",
                 "", "", "", ["円安メリットと業績改善が重なっており、外国人買いの対象になりやすい"], "26:30", "low"),
        ],
    },
    {
        "ch": 3, "hours": 52, "title": "【サンプル】新興株の物色に変化",
        "overview": "これは表示確認用のサンプル要約です。中小型株の需給と、注目しているテーマについて解説する構成を想定しています。",
        "market_view": "",
        "market": "JP",
        "picks": [
            pick("サンプル製薬", "3333", "JP", "bearish", "需給が悪く手控えたい",
                 "", "", "980円",
                 ["公募増資の発表で希薄化が意識されている",
                  "信用買い残が積み上がっており、上値が重い"], "05:10"),
            pick("サンプルソフト", "4444", "JP", "bullish", "テーマ性で見直される余地",
                 "1,850円", "1,400円台", "",
                 ["解約率が低く、ストック収益の比率が上がっている",
                  "同テーマの銘柄と比べて出遅れている"], "17:38"),
        ],
    },
]


def main() -> int:
    videos = []
    for i, v in enumerate(VIDEOS):
        ch = CHANNELS[v["ch"]]
        videos.append({
            "video_id": f"SAMPLE{i + 1:03d}",
            "title": v["title"],
            "published": (NOW - timedelta(hours=v["hours"])).isoformat(),
            "channel_id": ch["id"], "channel_name": ch["name"],
            "channel_short": ch["short"], "slot": ch["slot"],
            "no_transcript": False, "skip_reason": "",
            "overview": v["overview"], "market_view": v["market_view"],
            "market": v["market"], "picks": v["picks"],
        })

    # 「字幕が無くてスキップされた動画」の見え方も確認できるように1本入れておく
    videos.append({
        "video_id": "SAMPLE999",
        "title": "【サンプル】字幕が無い動画はこう表示されます",
        "published": (NOW - timedelta(hours=70)).isoformat(),
        "channel_id": CHANNELS[1]["id"], "channel_name": CHANNELS[1]["name"],
        "channel_short": CHANNELS[1]["short"], "slot": CHANNELS[1]["slot"],
        "no_transcript": True, "skip_reason": "この動画には字幕がありません",
        "overview": "", "market_view": "", "market": "", "picks": [],
    })

    collect.DATA.mkdir(parents=True, exist_ok=True)
    index = collect.build_index(videos, CHANNELS)

    # 株価もダミーで埋めて、表示を確認できるようにする
    import random
    random.seed(20260817)
    cache = {"updated_at": datetime.now(timezone.utc).isoformat(), "symbols": {}}
    ROWS_BY_KEY = {}
    for st in index["stocks"]:
        sym = prices.to_symbol(st["name"], st["ticker"], st["market"])
        if not sym:
            continue
        is_index = sym.startswith("^")
        is_us = not sym.endswith(".T") and not is_index
        base = 180.0 if is_us else (38000.0 if is_index else 2400.0)
        # 「買われすぎ」「売られすぎ」「出来高急増」の見え方も確認できるよう、
        # 銘柄ごとに違う値動きのパターンを割り当てる
        pattern = SHAPES[len(cache["symbols"]) % len(SHAPES)]
        rows, price = [], base
        for i in range(140):
            recent = i >= 125
            price *= 1 + (pattern["late"] if recent else pattern["drift"]) + random.uniform(-0.012, 0.012)
            vol_mult = pattern["vol"] if i == 139 else random.uniform(0.7, 1.3)
            rows.append({"date": (NOW - timedelta(days=140 - i)).strftime("%Y-%m-%d"),
                         "close": round(price, 2),
                         "open": round(price * (1 + random.uniform(-0.008, 0.008)), 2),
                         "high": round(price * (1 + random.uniform(0.002, 0.015)), 2),
                         "low": round(price * (1 - random.uniform(0.002, 0.015)), 2),
                         "volume": vol_mult * (900_000 if is_us else 3_000_000)})
        ROWS_BY_KEY[st["key"]] = rows
        meta = {"currency": "USD" if is_us else "JPY"}
        if len(cache["symbols"]) == 0:      # 先頭の1銘柄だけ「取引時間中」の見え方にする
            meta = {**meta, "dropped_partial": True,
                    "regularMarketPrice": round(rows[-1]["close"] * 0.968, 2)}
        cache["symbols"][st["key"]] = {**prices.summarize(rows, meta), "symbol": sym,
                                       "source": "sample",
                                       "fetched_at": datetime.now(timezone.utc).isoformat()}
    collect.save_json(collect.DATA / "prices.json", cache)
    prices.attach(index["stocks"], cache)

    # チャートと価格帯別出来高もダミーで用意する
    charts = {}
    for st in index["stocks"]:
        sym = prices.to_symbol(st["name"], st["ticker"], st["market"])
        if not sym or st["key"] not in cache["symbols"]:
            continue
        rows = ROWS_BY_KEY.get(st["key"])
        if not rows:
            continue
        last = rows[-1]["close"]
        intraday = []
        for i in range(1500):
            drift = 0.004 if 500 <= i < 1000 else -0.004
            intraday.append((last * (1 + drift * (0.5 - abs(0.5 - (i % 500) / 500))),
                             random.uniform(0.4, 1.6) * 20000))
        charts[st["key"]] = {
            "symbol": sym, "name": st["name"],
            "currency": cache["symbols"][st["key"]].get("currency", ""),
            "daily": prices.build_chart(rows, 120),
            "profile": prices.volume_profile(intraday),
            "profile_days": 5,
        }
    collect.save_json(collect.DATA / "charts.json",
                      {"updated_at": index["generated_at"], "charts": charts}, compact=True)

    collect.save_json(collect.DATA / "digest.json", {
        "generated_at": datetime.now(JST).isoformat(),
        "headline": "半導体主導で戻りを試すが、上値では売りも出ています",
        "trend": "これは表示確認用のサンプルです。実際には直近の動画から、"
                 "複数の投稿者が共通して話している話題を2〜3文でまとめます。"
                 "指数の水準感と、資金がどのセクターに向かっているかが中心になります。",
        "watch_points": [
            "日経平均は節目を終値で超えられるかが当面の分かれ目",
            "急騰した銘柄は、翌日も出来高が続くかどうかを確認したい",
            "25日線を割らずに推移できているかを目安にする",
        ],
        "consensus": "戻りの初動には前向きな見方が多い一方、持続性については意見が割れています。",
        "mood": "bullish",
        "based_on": [{"channel": v["channel_name"], "title": v["title"],
                      "video_id": v["video_id"], "published": v["published"]}
                     for v in videos[:4]],
    })
    collect.save_json(collect.DATA / "index.json", index)
    collect.save_json(collect.DATA / "videos.json", collect.build_video_list(videos))
    print(f"サンプルデータを書き出しました → {collect.DATA}")
    print("  ※ collect.py を実行すると本物のデータで置き換わります")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

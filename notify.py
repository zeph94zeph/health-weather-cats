"""
notify.py
=========
毎朝、LINEグループに猫2匹（りんこ・そうた）の健康チェックメッセージを送信する。

送信内容:
  1通目: りんこの今朝のご飯ボタン（完食 / 半分残した / 食べず）
  2通目: そうたの今朝のご飯ボタン（完食 / 半分残した / 食べず）

使い方:
    python notify.py              # グループに送信
    python notify.py --dry-run   # 送信せずメッセージ内容を表示
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from record import load_csv, today_summary

# ── 設定 ──────────────────────────────────────────────
LINE_API_URL = "https://api.line.me/v2/bot/message/push"
JST          = timezone(timedelta(hours=9))

CHANNEL_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
GROUP_ID      = os.environ.get("LINE_GROUP_ID", "")

# 猫の定義
CATS = [
    {"id": "rinko",  "name": "りんこ", "icon": "🐈‍⬛"},
    {"id": "souta",  "name": "そうた", "icon": "🐈"},
]

FOOD_OPTIONS = [
    {"label": "完食 😋",      "value": "完食"},
    {"label": "半分残した 🍽️", "value": "半分"},
    {"label": "食べず 😿",    "value": "食べず"},
]


def build_food_message(cat: dict, date_str: str) -> dict:
    """1匹分の食事ボタンを含むメッセージ"""
    items = [
        {
            "type": "action",
            "action": {
                "type": "postback",
                "label": opt["label"],
                "data": f"action=food&cat={cat['id']}&value={opt['value']}&date={date_str}",
                "displayText": f"{cat['name']}：{opt['value']}",
            }
        }
        for opt in FOOD_OPTIONS
    ]
    return {
        "type": "text",
        "text": f"{cat['icon']} 【{cat['name']}】今朝のご飯は？",
        "quickReply": {"items": items},
    }


def build_header_message(today: datetime, summary: dict) -> dict:
    """冒頭の挨拶メッセージ（昨日のサマリー付き）"""
    weekday_names = ["月", "火", "水", "木", "金", "土", "日"]
    wd = weekday_names[today.weekday()]
    date_label = today.strftime(f"%-m/%-d") + f"（{wd}）"

    lines = [f"🌅 おはよう！{date_label} の猫チェック"]

    # 昨日の記録があれば表示
    yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    if summary:
        lines.append("")
        lines.append("📋 昨日の記録：")
        for cat in CATS:
            cid = cat["id"]
            food     = summary.get(f"{cid}_food",    "―")
            symptoms = summary.get(f"{cid}_symptoms", "")
            weight   = summary.get(f"{cid}_weight",  "")
            parts = [f"  {cat['icon']} {cat['name']}：{food}"]
            if symptoms:
                parts.append(f"    症状: {symptoms}")
            if weight:
                parts.append(f"    体重: {weight}kg")
            lines.extend(parts)

    lines.append("")
    lines.append("👇 今日の食事を記録してください")

    return {"type": "text", "text": "\n".join(lines)}


def send_messages(messages: list[dict], dry_run: bool = False):
    """LINE グループにメッセージを一括送信（最大5件）"""
    if dry_run:
        print("── DRY RUN ─────────────────────────")
        for i, msg in enumerate(messages, 1):
            print(f"[メッセージ {i}]")
            print(json.dumps(msg, ensure_ascii=False, indent=2))
        print("─────────────────────────────────────")
        return

    if not CHANNEL_TOKEN:
        print("❌ LINE_CHANNEL_ACCESS_TOKEN が設定されていません", file=sys.stderr)
        sys.exit(1)
    if not GROUP_ID:
        print("❌ LINE_GROUP_ID が設定されていません", file=sys.stderr)
        sys.exit(1)

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {CHANNEL_TOKEN}",
    }
    payload = {
        "to": GROUP_ID,
        "messages": messages,
    }
    resp = requests.post(LINE_API_URL, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    print(f"✅ LINE グループ送信完了 ({len(messages)} 件)")


def main():
    parser = argparse.ArgumentParser(description="猫の健康チェックをLINEグループに送信")
    parser.add_argument("--dry-run", action="store_true", help="送信せずメッセージ内容を表示")
    args = parser.parse_args()

    now_jst   = datetime.now(JST)
    today_str = now_jst.strftime("%Y-%m-%d")

    # 昨日のサマリー取得
    df = load_csv()
    yesterday_str = (now_jst - timedelta(days=1)).strftime("%Y-%m-%d")
    summary = today_summary(df, yesterday_str)

    # メッセージ組み立て（Quick Reply は最後のメッセージにしか出ない）
    # りんこを先に送信 → webhook で完食記録後にそうたのボタンを返す（2段階方式）
    messages = [
        build_header_message(now_jst, summary),
        build_food_message(CATS[0], today_str),   # りんこのボタン3つ
    ]

    send_messages(messages, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

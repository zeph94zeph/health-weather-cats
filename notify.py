"""
notify.py
=========
毎日17時、LINEグループに猫2匹（りんこ・そうた）の健康チェックメッセージを送信する。

送信内容:
  1通目: 昨日の記録サマリー + 今日の症状チェックボタン
         [✅ 今日も2匹とも元気！] [🐈‍⬛ りんこ] [🐈 そうた]

使い方:
    python notify.py              # グループに送信
    python notify.py --dry-run   # 送信せずメッセージ内容を表示
"""

import argparse
import base64
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
USER_ID       = os.environ.get("LINE_USER_ID", "")   # 1対1トーク移行後に設定（こちらを優先）
GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO   = os.environ.get("GITHUB_REPO", "zeph94zeph/health-weather-cats")


def _read_user_id_from_github() -> str:
    """bot_config.json から LINE_USER_ID を読む（env var の fallback）"""
    if not GITHUB_TOKEN:
        return ""
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/bot_config.json"
        headers = {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
        }
        r = requests.get(url, headers=headers, timeout=5)
        if r.ok:
            raw = base64.b64decode(r.json()["content"]).decode("utf-8")
            return json.loads(raw).get("LINE_USER_ID", "")
    except Exception:
        pass
    return ""

# 猫の定義
CATS = [
    {"id": "rinko",  "name": "りんこ", "icon": "🐈‍⬛"},
    {"id": "souta",  "name": "そうた", "icon": "🐈"},
]

def build_symptom_check_message(date_str: str) -> dict:
    """症状チェックボタン（一括 / りんこ個別 / そうた個別）"""
    items = [
        {
            "type": "action",
            "action": {
                "type": "postback",
                "label": "✅ 今日も2匹とも元気！",
                "data": f"action=all_ok&date={date_str}",
                "displayText": "2匹とも異常なし",
            },
        },
        {
            "type": "action",
            "action": {
                "type": "postback",
                "label": "🐈‍⬛ りんこ",
                "data": f"action=symptom_start&cat=rinko&date={date_str}",
                "displayText": "りんこの症状を記録",
            },
        },
        {
            "type": "action",
            "action": {
                "type": "postback",
                "label": "🐈 そうた",
                "data": f"action=symptom_start&cat=souta&date={date_str}",
                "displayText": "そうたの症状を記録",
            },
        },
    ]
    return {
        "type": "text",
        "text": "👇 今日の症状チェック",
        "quickReply": {"items": items},
    }


def build_header_message(today: datetime, summary: dict) -> dict:
    """冒頭の挨拶メッセージ（昨日のサマリー付き）"""
    weekday_names = ["月", "火", "水", "木", "金", "土", "日"]
    wd = weekday_names[today.weekday()]
    date_label = f"{today.month}/{today.day}（{wd}）"

    lines = [f"🐱 今日もお疲れ様！{date_label} の猫チェック"]

    # 昨日の記録があれば表示
    yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    if summary:
        lines.append("")
        lines.append("📋 昨日の記録：")
        for cat in CATS:
            cid      = cat["id"]
            symptoms = summary.get(f"{cid}_symptoms", "異常なし")
            weight   = summary.get(f"{cid}_weight",  "")
            parts = [f"  {cat['icon']} {cat['name']}：{symptoms or '異常なし'}"]
            if weight:
                parts.append(f"    体重: {weight}kg")
            lines.extend(parts)

    lines.append("")
    lines.append("👇 今日の症状チェック")

    return {"type": "text", "text": "\n".join(lines)}


def send_messages(messages: list[dict], dry_run: bool = False):
    """LINE グループにメッセージを一括送信（最大5件）"""
    if dry_run:
        import sys
        out = sys.stdout.buffer
        def uprint(s):
            out.write((s + "\n").encode("utf-8", errors="replace"))
        uprint("── DRY RUN ─────────────────────────")
        for i, msg in enumerate(messages, 1):
            uprint(f"[メッセージ {i}]")
            uprint(json.dumps(msg, ensure_ascii=False, indent=2))
        uprint("─────────────────────────────────────")
        return

    if not CHANNEL_TOKEN:
        print("❌ LINE_CHANNEL_ACCESS_TOKEN が設定されていません", file=sys.stderr)
        sys.exit(1)

    dest = USER_ID or _read_user_id_from_github() or GROUP_ID
    if not dest:
        print("❌ LINE_USER_ID / LINE_GROUP_ID が設定されていません（bot_config.json にも未登録）", file=sys.stderr)
        sys.exit(1)

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {CHANNEL_TOKEN}",
    }
    payload = {
        "to": dest,
        "messages": messages,
    }
    resp = requests.post(LINE_API_URL, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    dest_label = "1対1" if dest.startswith("U") else "グループ"
    print(f"✅ LINE {dest_label}送信完了 ({len(messages)} 件)")


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

    messages = [
        build_header_message(now_jst, summary),
        build_symptom_check_message(today_str),
    ]

    send_messages(messages, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

"""
webhook/app.py
==============
LINE Messaging API の Webhook を受け取り、記録を CSV に保存する。

受け取るイベント:
  - postback: ボタン操作（食事・症状記録）
  - message(text): 体重などの自由入力

ポストバック data の書式:
  action=food&cat=rinko&value=完食&date=2026-05-20
  action=symptom&cat=souta&value=嘔吐&date=2026-05-20

テキストメッセージの自由入力パターン:
  「りんこ 3.8kg」「そうた 4.2」「りんこ 元気ない」など
"""

import hashlib
import hmac
import base64
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, request
import requests

# record.py は親ディレクトリにある
sys.path.insert(0, str(Path(__file__).parent.parent))
from record import load_csv, save_csv, record_food, record_symptoms, record_weight, record_memo

app = Flask(__name__)

JST               = timezone(timedelta(hours=9))
CHANNEL_SECRET    = os.environ.get("LINE_CHANNEL_SECRET", "")
CHANNEL_TOKEN     = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
GROUP_ID          = os.environ.get("LINE_GROUP_ID", "")
REPLY_API_URL     = "https://api.line.me/v2/bot/message/reply"

# 体重パターン: 「りんこ 3.8kg」「そうた 4.2」
WEIGHT_PATTERN = re.compile(
    r"(りんこ|りん|そうた|そう)[\s　]*(\d+(?:\.\d+)?)\s*(?:kg|ｋｇ)?",
    re.IGNORECASE,
)
TEXT_TO_CAT_ID = {"りんこ": "rinko", "りん": "rinko", "そうた": "souta", "そう": "souta"}

# 症状キーワード（テキストから自動検出）
SYMPTOM_KEYWORDS = ["嘔吐", "ゲロ", "下痢", "血尿", "食欲不振", "元気ない", "咳", "くしゃみ"]


# ── 署名検証 ───────────────────────────────────────────
def verify_signature(body: bytes, signature: str) -> bool:
    if not CHANNEL_SECRET:
        return True  # ローカル開発時はスキップ
    hash_ = hmac.new(CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    expected = base64.b64encode(hash_).decode()
    return hmac.compare_digest(expected, signature)


# ── LINE Reply ─────────────────────────────────────────
def reply(reply_token: str, text: str):
    if not reply_token or not CHANNEL_TOKEN:
        print(f"[REPLY skipped] {text}")
        return
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {CHANNEL_TOKEN}",
    }
    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text}],
    }
    requests.post(REPLY_API_URL, headers=headers, json=payload, timeout=15)


def build_symptom_quick_reply(cat_id: str, cat_name: str, date_str: str) -> dict:
    """症状ボタンを含む Quick Reply メッセージ"""
    symptoms = ["嘔吐・ゲロ", "下痢", "血尿", "食欲不振", "元気ない", "異常なし"]
    items = []
    for s in symptoms:
        items.append({
            "type": "action",
            "action": {
                "type": "postback",
                "label": s,
                "data": f"action=symptom&cat={cat_id}&value={s}&date={date_str}",
                "displayText": f"{cat_name}：{s}",
            }
        })
    return {
        "type": "text",
        "text": f"🩺 {cat_name} の症状は？",
        "quickReply": {"items": items},
    }


def reply_with_quick_reply(reply_token: str, message: dict):
    if not reply_token or not CHANNEL_TOKEN:
        print(f"[REPLY skipped] {message.get('text', '')}")
        return
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {CHANNEL_TOKEN}",
    }
    payload = {
        "replyToken": reply_token,
        "messages": [message],
    }
    requests.post(REPLY_API_URL, headers=headers, json=payload, timeout=15)


# ── イベント処理 ───────────────────────────────────────
def handle_postback(event: dict):
    reply_token = event.get("replyToken", "")
    data        = event.get("postback", {}).get("data", "")
    if not data:
        return

    params = dict(kv.split("=", 1) for kv in data.split("&") if "=" in kv)
    action   = params.get("action", "")
    cat_id   = params.get("cat", "")
    value    = params.get("value", "")
    date_str = params.get("date", datetime.now(JST).strftime("%Y-%m-%d"))

    cat_names = {"rinko": "りんこ", "souta": "そうた"}
    cat_name  = cat_names.get(cat_id, cat_id)

    df = load_csv()

    if action == "food":
        df = record_food(df, date_str, cat_id, value)
        save_csv(df)
        print(f"[記録] {date_str} {cat_name} 食事={value}")
        # 食事記録後、症状ボタンを返す
        msg = build_symptom_quick_reply(cat_id, cat_name, date_str)
        reply_with_quick_reply(reply_token, msg)

    elif action == "symptom":
        if value == "異常なし":
            df = record_symptoms(df, date_str, cat_id, "")
            save_csv(df)
            reply(reply_token, f"✅ {cat_name}：異常なし で記録しました！")
        else:
            df = record_symptoms(df, date_str, cat_id, value)
            save_csv(df)
            reply(reply_token, f"📝 {cat_name}：{value} を記録しました。")
        print(f"[記録] {date_str} {cat_name} 症状={value}")


def handle_text_message(event: dict):
    reply_token = event.get("replyToken", "")
    text        = event.get("message", {}).get("text", "").strip()
    date_str    = datetime.now(JST).strftime("%Y-%m-%d")

    df = load_csv()
    responses = []

    # 体重パターン
    for m in WEIGHT_PATTERN.finditer(text):
        name_key = m.group(1)
        weight   = m.group(2)
        cat_id   = TEXT_TO_CAT_ID.get(name_key)
        cat_jp   = {"rinko": "りんこ", "souta": "そうた"}.get(cat_id, name_key)
        if cat_id:
            df = record_weight(df, date_str, cat_id, weight)
            responses.append(f"⚖️ {cat_jp}：{weight}kg を記録しました！")
            print(f"[記録] {date_str} {cat_jp} 体重={weight}kg")

    # 症状キーワード検出（自由テキスト）
    if not responses:
        for kw in SYMPTOM_KEYWORDS:
            if kw in text:
                # どの猫か特定
                for name_key, cid in TEXT_TO_CAT_ID.items():
                    if name_key in text:
                        cat_jp = {"rinko": "りんこ", "souta": "そうた"}.get(cid, name_key)
                        df = record_symptoms(df, date_str, cid, kw)
                        responses.append(f"📝 {cat_jp}：{kw} を記録しました。")
                        print(f"[記録] {date_str} {cat_jp} 症状={kw}")
                        break
                break

    if responses:
        save_csv(df)
        reply(reply_token, "\n".join(responses))


# ── Webhook エンドポイント ──────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body      = request.get_data()

    if not verify_signature(body, signature):
        abort(400, "Invalid signature")

    data = json.loads(body)
    for event in data.get("events", []):
        # グループ以外のメッセージは無視（オプション）
        source = event.get("source", {})
        if GROUP_ID and source.get("groupId") and source.get("groupId") != GROUP_ID:
            continue  # 別グループからのイベントを無視

        event_type = event.get("type")
        if event_type == "postback":
            handle_postback(event)
        elif event_type == "message" and event.get("message", {}).get("type") == "text":
            handle_text_message(event)

    return jsonify({"status": "ok"})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)

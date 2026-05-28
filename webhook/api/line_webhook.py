"""
webhook/api/line_webhook.py
===========================
Vercel Serverless Function として動作する LINE Webhook。

受け取るイベント:
  - postback: 食事ボタン・症状ボタン操作
  - message(text): 体重などの自由入力（例: りんこ 3.8kg）

ポストバック data の書式:
  action=food&cat=rinko&value=完食&date=2026-05-20
  action=symptom&cat=souta&value=嘔吐&date=2026-05-20

環境変数（Vercel の Environment Variables に設定）:
  LINE_CHANNEL_SECRET        — 署名検証用
  LINE_CHANNEL_ACCESS_TOKEN  — Reply/Push 用トークン
  LINE_GROUP_ID              — 対象グループID（C で始まる）
  GITHUB_TOKEN               — CSV 更新用 PAT（repo write 権限）
  GITHUB_REPO                — 例: zeph94zeph/health-weather-cats
"""

import base64
import csv
import hashlib
import hmac
import io
import json
import os
import re
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import quote

import requests

# ── 設定 ─────────────────────────────────────────────────
JST            = timezone(timedelta(hours=9))
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
CHANNEL_TOKEN  = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
GROUP_ID       = os.environ.get("LINE_GROUP_ID", "")
GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO    = os.environ.get("GITHUB_REPO", "zeph94zeph/health-weather-cats")
CSV_FILENAME   = "猫の健康記録.csv"

REPLY_URL = "https://api.line.me/v2/bot/message/reply"

CSV_COLS = [
    "日付",
    "りんこ食事", "そうた食事",
    "りんこ症状", "そうた症状",
    "りんこ体重", "そうた体重",
    "メモ",
]

CAT_NAMES  = {"rinko": "りんこ", "souta": "そうた"}
CATS_ORDER = ["rinko", "souta"]   # りんこ → そうた の順

FOOD_OPTIONS = [
    {"label": "完食 😋",      "value": "完食"},
    {"label": "半分残した 🍽️", "value": "半分"},
    {"label": "食べず 😿",    "value": "食べず"},
]

# 体重パターン: 「りんこ 3.8kg」「そうた 4.2」
WEIGHT_RE = re.compile(
    r"(りんこ|りん|そうた|そう)[\s　]*(\d+(?:\.\d+)?)\s*(?:kg|ｋｇ)?",
    re.IGNORECASE,
)
TEXT_TO_CAT = {"りんこ": "rinko", "りん": "rinko", "そうた": "souta", "そう": "souta"}
SYMPTOM_KW  = ["嘔吐・ゲロ", "嘔吐", "吐き戻し", "ゲロ", "下痢", "血尿", "食欲不振", "元気ない", "咳", "くしゃみ"]


# ── 署名検証 ─────────────────────────────────────────────
def verify_signature(body: bytes, signature: str) -> bool:
    if not CHANNEL_SECRET:
        return True
    digest = hmac.new(CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)


# ── GitHub API で CSV 読み書き ──────────────────────────────
def _github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def read_csv_from_github():
    """GitHub から CSV を取得し (rows, fieldnames, sha) を返す"""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{quote(CSV_FILENAME)}"
    r = requests.get(url, headers=_github_headers(), timeout=15)
    if r.status_code == 404:
        # ファイルが存在しない場合は空で初期化
        return [], list(CSV_COLS), None
    r.raise_for_status()
    info    = r.json()
    sha     = info["sha"]
    content = base64.b64decode(info["content"]).decode("utf-8-sig")
    reader  = csv.DictReader(io.StringIO(content))
    rows    = list(reader)
    fields  = list(reader.fieldnames or CSV_COLS)
    return rows, fields, sha


def write_csv_to_github(rows, fieldnames, sha, commit_msg):
    """CSV を GitHub にコミットする"""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    new_bytes = "﻿".encode() + output.getvalue().encode("utf-8")

    url     = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{quote(CSV_FILENAME)}"
    payload = {
        "message": commit_msg,
        "content": base64.b64encode(new_bytes).decode(),
    }
    if sha:
        payload["sha"] = sha
    r = requests.put(url, headers=_github_headers(), json=payload, timeout=15)
    r.raise_for_status()


def get_or_create_row(rows, date_str):
    """指定日の行を返す（なければ新規追加して返す）"""
    for row in rows:
        if row.get("日付") == date_str:
            return row
    new_row = {c: "" for c in CSV_COLS}
    new_row["日付"] = date_str
    rows.append(new_row)
    rows.sort(key=lambda r: r.get("日付", ""))
    return new_row


# ── LINE 返信 ─────────────────────────────────────────────
def reply(reply_token: str, text: str):
    if not reply_token or not CHANNEL_TOKEN:
        return
    requests.post(
        REPLY_URL,
        headers={"Authorization": f"Bearer {CHANNEL_TOKEN}", "Content-Type": "application/json"},
        json={"replyToken": reply_token, "messages": [{"type": "text", "text": text}]},
        timeout=10,
    )


def reply_food_buttons(reply_token: str, done_name: str, done_value: str,
                       next_cat_id: str, next_cat_name: str, date_str: str):
    """次の猫の食事ボタンを返す（完了確認付き）"""
    items = [
        {
            "type": "action",
            "action": {
                "type": "postback",
                "label": opt["label"],
                "data": f"action=food&cat={next_cat_id}&value={opt['value']}&date={date_str}",
                "displayText": f"{next_cat_name}：{opt['value']}",
            },
        }
        for opt in FOOD_OPTIONS
    ]
    icon = "🐈" if next_cat_id == "souta" else "🐈‍⬛"
    messages = [
        {"type": "text", "text": f"✅ {done_name}：{done_value} を記録しました！"},
        {
            "type": "text",
            "text": f"{icon} 【{next_cat_name}】今朝のご飯は？",
            "quickReply": {"items": items},
        },
    ]
    if not reply_token or not CHANNEL_TOKEN:
        return
    resp = requests.post(
        REPLY_URL,
        headers={"Authorization": f"Bearer {CHANNEL_TOKEN}", "Content-Type": "application/json"},
        json={"replyToken": reply_token, "messages": messages},
        timeout=10,
    )
    print(f"[reply_food_buttons] status={resp.status_code} body={resp.text[:200]}", flush=True)


def reply_with_symptom_buttons(reply_token: str, cat_id: str, cat_name: str, date_str: str,
                               confirm_text: str = ""):
    """症状 Quick Reply ボタンを返す（confirm_text があれば確認メッセージも一緒に送る）"""
    symptoms = ["嘔吐・ゲロ", "吐き戻し", "下痢", "血尿", "食欲不振", "元気ない", "異常なし"]
    items = [
        {
            "type": "action",
            "action": {
                "type": "postback",
                "label": s,
                "data": f"action=symptom&cat={cat_id}&value={s}&date={date_str}",
                "displayText": f"{cat_name}：{s}",
            },
        }
        for s in symptoms
    ]
    symptom_msg = {
        "type": "text",
        "text": f"🩺 {cat_name} の症状は？",
        "quickReply": {"items": items},
    }
    messages = []
    if confirm_text:
        messages.append({"type": "text", "text": confirm_text})
    messages.append(symptom_msg)

    if not reply_token or not CHANNEL_TOKEN:
        return
    requests.post(
        REPLY_URL,
        headers={"Authorization": f"Bearer {CHANNEL_TOKEN}", "Content-Type": "application/json"},
        json={"replyToken": reply_token, "messages": messages},
        timeout=10,
    )


# ── イベント処理 ───────────────────────────────────────────
def handle_postback(event):
    reply_token = event.get("replyToken", "")
    data        = event.get("postback", {}).get("data", "")
    if not data:
        return

    params   = dict(kv.split("=", 1) for kv in data.split("&") if "=" in kv)
    action   = params.get("action", "")
    cat_id   = params.get("cat", "")
    value    = params.get("value", "")
    date_str = params.get("date", datetime.now(JST).strftime("%Y-%m-%d"))
    cat_name = CAT_NAMES.get(cat_id, cat_id)

    rows, fields, sha = read_csv_from_github()
    row = get_or_create_row(rows, date_str)

    if action == "food":
        col = f"{cat_name}食事"
        if col in fields:
            row[col] = value
        write_csv_to_github(rows, fields, sha, f"🐱 {date_str} {cat_name} 食事={value}")

        # 2段階方式: もう片方の猫がまだ未記録なら、その猫の食事ボタンを返す
        other_id   = [c for c in CATS_ORDER if c != cat_id]
        if other_id:
            other_id   = other_id[0]
            other_name = CAT_NAMES[other_id]
            other_col  = f"{other_name}食事"
            other_done = row.get(other_col, "").strip()
            if not other_done:
                reply_food_buttons(reply_token, cat_name, value, other_id, other_name, date_str)
            else:
                # 両方の食事記録済み → CATS_ORDER の先頭（りんこ）の症状を先に聞く
                first_id   = CATS_ORDER[0]
                first_name = CAT_NAMES[first_id]
                first_sym  = row.get(f"{first_name}症状", "").strip()
                if not first_sym:
                    reply_with_symptom_buttons(reply_token, first_id, first_name, date_str)
                else:
                    second_id   = CATS_ORDER[1]
                    second_name = CAT_NAMES[second_id]
                    reply_with_symptom_buttons(reply_token, second_id, second_name, date_str)
        else:
            reply_with_symptom_buttons(reply_token, cat_id, cat_name, date_str)

    elif action == "symptom":
        col = f"{cat_name}症状"
        if col in fields:
            # 「異常なし」も文字列として保存する（空文字にすると未記録と区別できずループする）
            existing = row.get(col, "").strip()
            if value == "異常なし":
                row[col] = "異常なし"
            else:
                row[col] = f"{existing},{value}".lstrip(",") if existing else value
            write_csv_to_github(rows, fields, sha, f"🐱 {date_str} {cat_name} 症状={value}")

            confirm = (f"✅ {cat_name}：異常なし で記録しました！"
                       if value == "異常なし" else f"📝 {cat_name}：{value} を記録しました。")

            # 症状記録後、もう一方の猫の食事が済んでいて症状未記録なら続けて聞く
            others = [c for c in CATS_ORDER if c != cat_id]
            if others:
                other_sym_id   = others[0]
                other_sym_name = CAT_NAMES[other_sym_id]
                other_food_done = row.get(f"{other_sym_name}食事", "").strip()
                other_sym_done  = row.get(f"{other_sym_name}症状", "").strip()
                if other_food_done and not other_sym_done:
                    reply_with_symptom_buttons(reply_token, other_sym_id, other_sym_name,
                                               date_str, confirm_text=confirm)
                    return

            reply(reply_token, confirm)


def handle_text(event):
    reply_token = event.get("replyToken", "")
    text        = event.get("message", {}).get("text", "").strip()
    date_str    = datetime.now(JST).strftime("%Y-%m-%d")

    rows, fields, sha = read_csv_from_github()
    row = get_or_create_row(rows, date_str)
    responses = []
    changed   = False

    # 体重パターン
    for m in WEIGHT_RE.finditer(text):
        cat_id   = TEXT_TO_CAT.get(m.group(1))
        cat_name = CAT_NAMES.get(cat_id, m.group(1))
        weight   = m.group(2)
        col      = f"{cat_name}体重"
        if col in fields:
            row[col] = weight
            responses.append(f"⚖️ {cat_name}：{weight}kg を記録しました！")
            changed = True

    # 猫名を検出して症状またはメモに記録（長いキーを優先）
    if not responses:
        for name_key in sorted(TEXT_TO_CAT.keys(), key=len, reverse=True):
            if name_key in text:
                cid      = TEXT_TO_CAT[name_key]
                cat_name = CAT_NAMES[cid]
                # 猫名より後ろを内容として取得
                idx     = text.index(name_key)
                content = text[idx + len(name_key):].strip("　 ・、。")
                if not content:
                    break

                # 症状キーワードに一致するか確認（長いキーを優先）
                matched_kw = next(
                    (kw for kw in sorted(SYMPTOM_KW, key=len, reverse=True) if kw in content),
                    None
                )

                if matched_kw:
                    # 症状として記録
                    col = f"{cat_name}症状"
                    if col in fields:
                        existing = row.get(col, "").strip()
                        row[col] = f"{existing},{matched_kw}".lstrip(",") if existing else matched_kw
                        responses.append(f"📝 {cat_name}：{matched_kw} を記録しました。")
                        changed = True
                else:
                    # 症状以外 → メモに記録
                    existing_memo = row.get("メモ", "").strip()
                    memo_entry    = f"{cat_name}:{content}"
                    row["メモ"]   = f"{existing_memo} / {memo_entry}" if existing_memo else memo_entry
                    responses.append(f"📋 {cat_name}：{content} をメモに記録しました。")
                    changed = True
                break

    if changed:
        write_csv_to_github(rows, fields, sha, f"🐱 {date_str} {text[:30]}")
        reply(reply_token, "\n".join(responses))


# ── Vercel ハンドラ ───────────────────────────────────────
class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Cats health webhook is running")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)
        sig    = self.headers.get("X-Line-Signature", "")

        if CHANNEL_SECRET and not verify_signature(body, sig):
            self.send_response(401)
            self.end_headers()
            return

        self.send_response(200)
        self.end_headers()

        events = json.loads(body).get("events", [])
        for event in events:
            # グループIDが設定されていれば対象グループのみ処理
            source = event.get("source", {})
            if GROUP_ID and source.get("groupId") and source.get("groupId") != GROUP_ID:
                continue

            try:
                if event.get("type") == "postback":
                    handle_postback(event)
                elif event.get("type") == "message" and event.get("message", {}).get("type") == "text":
                    handle_text(event)
            except Exception as e:
                print(f"[ERROR] {e}", flush=True)

    def log_message(self, *args):
        pass

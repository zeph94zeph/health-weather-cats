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
USER_ID        = os.environ.get("LINE_USER_ID", "")   # 1対1トーク移行後に設定
GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO    = os.environ.get("GITHUB_REPO", "zeph94zeph/health-weather-cats")
CSV_FILENAME        = "猫の健康記録.csv"
BOT_CONFIG_FILENAME = "bot_config.json"

REPLY_URL = "https://api.line.me/v2/bot/message/reply"
PUSH_URL  = "https://api.line.me/v2/bot/message/push"

CSV_COLS = [
    "日付",
    "りんこ食事", "そうた食事",
    "りんこ症状", "そうた症状",
    "りんこ体重", "そうた体重",
    "メモ",
]

CAT_NAMES  = {"rinko": "りんこ", "souta": "そうた"}
CATS_ORDER = ["rinko", "souta"]   # りんこ → そうた の順
WEEKDAYS   = ["月", "火", "水", "木", "金", "土", "日"]

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
MENU_KEYWORDS  = {"メニュー", "めにゅー", "menu", "きろく", "記録"}
SETUP_KEYWORDS = {"myid", "userid", "初期設定", "はじめて", "設定", "セットアップ", "my id", "マイid"}


# ── 記録フォーマット ──────────────────────────────────────
def _has_data(row: dict) -> bool:
    return any(str(row.get(k, "")).strip() for k in row if k != "日付")


def format_history(rows: list, current_month: bool = False, all_records: bool = False) -> str:
    now = datetime.now(JST)

    # 日付バリデーション済みの行だけを対象にする
    valid = []
    for r in rows:
        try:
            datetime.strptime(r.get("日付", ""), "%Y-%m-%d")
            valid.append(r)
        except ValueError:
            pass

    if current_month:
        prefix = now.strftime("%Y-%m")
        target = [r for r in valid if r["日付"].startswith(prefix)]
        header = f"📋 {now.month}月の記録"
    else:
        target = valid
        header = "📋 全履歴"

    # データのある行だけ、新しい順
    target = sorted(
        [r for r in target if _has_data(r)],
        key=lambda r: r["日付"],
        reverse=True,
    )
    total = len(target)

    if not target:
        return f"{header}\n\n記録がありません。"

    # 全履歴は最新50件に絞る
    if all_records and total > 50:
        target = target[:50]
        footer = f"\n（最新50件を表示 / 全{total}件）"
    else:
        footer = ""

    lines = [header]
    for row in target:
        date_str = row["日付"]
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        wd = WEEKDAYS[dt.weekday()]
        lines.append(f"\n{dt.month}/{dt.day}（{wd}）")

        for cat_name, icon in [("りんこ", "🐈‍⬛"), ("そうた", "🐈")]:
            sym = str(row.get(f"{cat_name}症状", "") or "").strip() or "記録なし"
            wt  = str(row.get(f"{cat_name}体重", "") or "").strip()
            line = f"  {icon} {cat_name}: {sym}"
            if wt:
                line += f"  ⚖️{wt}kg"
            lines.append(line)

        memo = str(row.get("メモ", "") or "").strip()
        if memo:
            lines.append(f"  📝 {memo}")

    text = "\n".join(lines) + footer
    if len(text) > 4900:
        text = text[:4900] + "\n…(省略)"
    return text


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


# ── BOT 設定ファイル (bot_config.json) ────────────────────────
_BOT_CONFIG: dict | None = None  # cold-start ごとにリセット


def _get_bot_config() -> dict:
    global _BOT_CONFIG
    if _BOT_CONFIG is None:
        try:
            url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{BOT_CONFIG_FILENAME}"
            r = requests.get(url, headers=_github_headers(), timeout=5)
            _BOT_CONFIG = json.loads(base64.b64decode(r.json()["content"]).decode("utf-8")) if r.ok else {}
        except Exception:
            _BOT_CONFIG = {}
    return _BOT_CONFIG


def get_effective_user_id() -> str:
    """env var → GitHub config の優先順で USER_ID を返す"""
    return USER_ID or _get_bot_config().get("LINE_USER_ID", "")


def _save_bot_config(cfg: dict):
    """bot_config.json を GitHub に保存しキャッシュも更新する"""
    global _BOT_CONFIG
    content = json.dumps(cfg, ensure_ascii=False, indent=2)
    new_bytes = content.encode("utf-8")
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{BOT_CONFIG_FILENAME}"
    r = requests.get(url, headers=_github_headers(), timeout=5)
    payload = {
        "message": "bot: update config",
        "content": base64.b64encode(new_bytes).decode(),
    }
    if r.ok:
        payload["sha"] = r.json()["sha"]
    resp = requests.put(url, headers=_github_headers(), json=payload, timeout=10)
    # SHA 競合時は1回リトライ
    if resp.status_code == 409:
        r2 = requests.get(url, headers=_github_headers(), timeout=5)
        if r2.ok:
            payload["sha"] = r2.json()["sha"]
            resp = requests.put(url, headers=_github_headers(), json=payload, timeout=10)
    resp.raise_for_status()
    _BOT_CONFIG = cfg


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


def push_message(text: str):
    """replyToken を使わず USER_ID（または GROUP_ID）に直接 Push 送信する"""
    dest = get_effective_user_id() or GROUP_ID
    if not dest or not CHANNEL_TOKEN:
        return
    requests.post(
        PUSH_URL,
        headers={"Authorization": f"Bearer {CHANNEL_TOKEN}", "Content-Type": "application/json"},
        json={"to": dest, "messages": [{"type": "text", "text": text}]},
        timeout=15,
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


def reply_menu_buttons(reply_token: str):
    """「メニュー」キーワードに応答して記録・閲覧ボタンを返す"""
    today = datetime.now(JST).strftime("%Y-%m-%d")
    items = [
        {"type": "action", "action": {"type": "postback", "label": "✅ 2匹とも元気！",
            "data": f"action=all_ok&date={today}", "displayText": "2匹とも異常なし"}},
        {"type": "action", "action": {"type": "postback", "label": "🐈‍⬛ りんこの症状",
            "data": f"action=symptom_start&cat=rinko&date={today}", "displayText": "りんこの症状を記録"}},
        {"type": "action", "action": {"type": "postback", "label": "🐈 そうたの症状",
            "data": f"action=symptom_start&cat=souta&date={today}", "displayText": "そうたの症状を記録"}},
        {"type": "action", "action": {"type": "postback", "label": "🗓 今月の記録",
            "data": "action=history&month=1", "displayText": "今月の記録を表示"}},
        {"type": "action", "action": {"type": "postback", "label": "📋 全履歴",
            "data": "action=history&all=1", "displayText": "全履歴を表示"}},
    ]
    if not reply_token or not CHANNEL_TOKEN:
        return
    requests.post(
        REPLY_URL,
        headers={"Authorization": f"Bearer {CHANNEL_TOKEN}", "Content-Type": "application/json"},
        json={"replyToken": reply_token, "messages": [{
            "type": "text",
            "text": "🐱 何をしますか？",
            "quickReply": {"items": items},
        }]},
        timeout=10,
    )


# ── イベント処理 ───────────────────────────────────────────
def handle_follow(event):
    """友達追加時にウェルカムメッセージ + 初期設定ボタンを送信"""
    reply_token = event.get("replyToken", "")
    items = [{"type": "action", "action": {"type": "message", "label": "⚙️ 初期設定", "text": "初期設定"}}]
    if not reply_token or not CHANNEL_TOKEN:
        return
    requests.post(
        REPLY_URL,
        headers={"Authorization": f"Bearer {CHANNEL_TOKEN}", "Content-Type": "application/json"},
        json={"replyToken": reply_token, "messages": [{
            "type": "text",
            "text": "🐱 猫健康管理BOTへようこそ！\nまず初期設定をしてください👇",
            "quickReply": {"items": items},
        }]},
        timeout=10,
    )


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

    # history は行を作成しない（空行汚染防止）
    if action == "history":
        is_month = params.get("month") == "1"
        is_all   = params.get("all")   == "1"
        text = format_history(rows, current_month=is_month, all_records=is_all)
        if is_all:
            push_message(text)
            reply(reply_token, "📋 全履歴を送信しました！")
        else:
            reply(reply_token, text)
        return

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

    elif action == "all_ok":
        # 既に症状が記録されている場合は上書きしない
        blocked = []
        for cid in CATS_ORDER:
            cname   = CAT_NAMES[cid]
            sym_col = f"{cname}症状"
            if sym_col in fields:
                existing = row.get(sym_col, "").strip()
                if existing and existing != "異常なし":
                    blocked.append(f"{cname}（{existing}）")
        if blocked:
            names = "、".join(blocked)
            reply(reply_token, f"⚠️ {names} の症状がすでに記録されています。\nテキストで「りんこ 異常なし」と送ると上書きできます。")
            return
        for cid in CATS_ORDER:
            cname   = CAT_NAMES[cid]
            sym_col = f"{cname}症状"
            if sym_col in fields:
                row[sym_col] = "異常なし"
        write_csv_to_github(rows, fields, sha, f"🐱 {date_str} 2匹とも異常なし")
        reply(reply_token, "✅ 今日も2匹とも元気で記録しました！")

    elif action == "symptom_start":
        # 個別猫の症状ボタンを返す
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

    # 初期設定コマンド → User ID を bot_config.json に保存
    if text.lower() in SETUP_KEYWORDS:
        uid = event.get("source", {}).get("userId", "")
        if uid:
            try:
                cfg = dict(_get_bot_config())
                cfg["LINE_USER_ID"] = uid
                _save_bot_config(cfg)
                reply(reply_token, "✅ 設定完了！\nこのトークで猫の記録・通知を受け取ります🐱")
            except Exception:
                reply(reply_token, "⚠️ 設定の保存に失敗しました。\nもう一度「初期設定」と送ってみてください。")
        else:
            reply(reply_token, "⚠️ User IDを取得できませんでした。\nもう一度「初期設定」と送ってみてください。")
        return

    # メニューキーワード → 記録・閲覧ボタンを返す
    if text.lower() in MENU_KEYWORDS:
        reply_menu_buttons(reply_token)
        return

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
            # USER_ID が設定されていれば 1対1モード（userId で絞る）
            # そうでなければグループモード（groupId で絞る）
            source = event.get("source", {})
            eff_uid = get_effective_user_id()
            # follow イベントはフィルタを通過させる（セットアップのため）
            if event.get("type") != "follow":
                if eff_uid:
                    if source.get("userId") != eff_uid:
                        continue
                elif GROUP_ID:
                    if source.get("groupId") and source.get("groupId") != GROUP_ID:
                        continue

            try:
                if event.get("type") == "follow":
                    handle_follow(event)
                elif event.get("type") == "postback":
                    handle_postback(event)
                elif event.get("type") == "message" and event.get("message", {}).get("type") == "text":
                    handle_text(event)
            except Exception as e:
                print(f"[ERROR] {e}", flush=True)

    def log_message(self, *args):
        pass

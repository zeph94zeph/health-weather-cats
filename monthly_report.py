"""
monthly_report.py
=================
先月1ヶ月分の猫の健康データを集計し、傾向と対策を LINE グループに通知する。

毎月1日に GitHub Actions から実行される。
先月のデータを自動取得して分析し、押し付けがましくない実用的なコメントを付ける。

使い方:
    python monthly_report.py              # 先月分を本番送信
    python monthly_report.py --dry-run   # 送信せず内容だけ表示
    python monthly_report.py --month 2026-05  # 特定月を指定
"""

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

import requests

from record import load_csv

# ── 設定 ────────────────────────────────────────────────
LINE_API_URL  = "https://api.line.me/v2/bot/message/push"
JST           = timezone(timedelta(hours=9))
CHANNEL_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
GROUP_ID      = os.environ.get("LINE_GROUP_ID", "")

CATS = [
    {"id": "rinko",  "name": "りんこ", "icon": "🐈‍⬛"},
    {"id": "souta",  "name": "そうた", "icon": "🐈"},
]

# 症状ごとの推奨アクション
SYMPTOM_ADVICE = {
    "血尿":   ("⚠️", "血尿の記録があります。早めに受診を検討してください。"),
    "嘔吐・ゲロ": ("💡", "吐き戻しが目立ちます。食事の速さや量を確認しましょう。"),
    "ゲロ":   ("💡", "吐き戻しが目立ちます。食事の速さや量を確認しましょう。"),
    "下痢":   ("💡", "下痢が複数回あります。フードや水の状態を確認しましょう。"),
    "食欲不振": ("💡", "食欲不振が記録されています。体調の変化に注意してください。"),
    "元気ない": ("💡", "元気がない日が記録されています。様子を注意して観察しましょう。"),
}


# ── 集計 ────────────────────────────────────────────────
def analyze_month(df, year: int, month: int) -> dict:
    """指定月の1匹分データを集計して辞書で返す"""
    from calendar import monthrange
    _, last_day = monthrange(year, month)
    start = datetime(year, month, 1)
    end   = datetime(year, month, last_day)

    mask = (df["日付"] >= start) & (df["日付"] <= end)
    mdf  = df[mask].copy()
    total_days = last_day

    results = {}
    for cat in CATS:
        cid  = cat["id"]
        name = cat["name"]

        # ── 食事 ──
        food_col  = f"{name}食事"
        food_vals = mdf[food_col].dropna().astype(str).str.strip()
        food_vals = food_vals[food_vals != ""]
        food_counter = Counter(food_vals)
        recorded_days = len(food_vals)
        kanshoku = food_counter.get("完食", 0)
        hanbu    = food_counter.get("半分", 0)
        tabezu   = food_counter.get("食べず", 0)

        # ── 症状 ──
        sym_col  = f"{name}症状"
        sym_vals = mdf[sym_col].dropna().astype(str).str.strip()
        sym_vals = sym_vals[sym_vals != ""]
        # カンマ区切り展開
        all_symptoms = []
        for v in sym_vals:
            all_symptoms.extend([s.strip() for s in v.split(",") if s.strip() and s.strip() != "異常なし"])
        sym_counter = Counter(all_symptoms)

        # ── 体重 ──
        wt_col   = f"{name}体重"
        wt_vals  = mdf[wt_col].dropna().astype(str).str.strip()
        wt_vals  = wt_vals[wt_vals != ""]
        weights  = []
        for w in wt_vals:
            try:
                weights.append(float(w))
            except ValueError:
                pass
        first_weight = weights[0]  if weights else None
        last_weight  = weights[-1] if weights else None

        results[cid] = {
            "name":          name,
            "icon":          cat["icon"],
            "total_days":    total_days,
            "recorded_days": recorded_days,
            "kanshoku":      kanshoku,
            "hanbu":         hanbu,
            "tabezu":        tabezu,
            "symptoms":      sym_counter,
            "first_weight":  first_weight,
            "last_weight":   last_weight,
        }

    return results


# ── メッセージ生成 ───────────────────────────────────────
def format_cat_block(data: dict) -> tuple[str, list[str]]:
    """1匹分の集計テキストと対策リストを返す"""
    name   = data["name"]
    icon   = data["icon"]
    total  = data["total_days"]
    rec    = data["recorded_days"]
    kan    = data["kanshoku"]
    han    = data["hanbu"]
    tabe   = data["tabezu"]
    syms   = data["symptoms"]
    fw     = data["first_weight"]
    lw     = data["last_weight"]

    # 食事サマリー
    lines = [f"{icon} {name}"]
    if rec > 0:
        rate = int(kan / rec * 100) if rec else 0
        food_parts = [f"完食 {kan}日"]
        if han:
            food_parts.append(f"半分 {han}日")
        if tabe:
            food_parts.append(f"食べず {tabe}日")
        lines.append(f"  🍽 食事: {' / '.join(food_parts)}（{rec}日記録）")
    else:
        lines.append("  🍽 食事記録なし")

    # 症状サマリー
    if syms:
        sym_str = "、".join(f"{s}({c}回)" if c > 1 else s for s, c in syms.most_common())
        lines.append(f"  🩺 症状: {sym_str}")
    else:
        lines.append("  🩺 症状: 異常なし 👍")

    # 体重サマリー
    if fw is not None and lw is not None and fw != lw:
        diff = lw - fw
        trend = f"↑+{diff:.1f}" if diff > 0 else f"↓{diff:.1f}"
        lines.append(f"  ⚖️ 体重: {fw}kg → {lw}kg ({trend}kg)")
    elif lw is not None:
        lines.append(f"  ⚖️ 体重: {lw}kg")

    # 対策リスト生成
    advice_list = []

    # 症状ベースの対策
    for sym, count in syms.items():
        for key, (mark, msg) in SYMPTOM_ADVICE.items():
            if key in sym:
                threshold = 1 if key == "血尿" else 2
                if count >= threshold:
                    advice_list.append(f"  {mark} {name}: {msg}")
                break

    # 体重変化ベースの対策
    if fw and lw:
        diff = lw - fw
        if diff <= -0.2:
            advice_list.append(f"  ⚠️ {name}: 体重が{abs(diff):.1f}kg減少。食欲と食事量を確認してください。")
        elif diff >= 0.3:
            advice_list.append(f"  💡 {name}: 体重が{diff:.1f}kg増加。食事量に注意しましょう。")

    # 食べず多め
    if tabe >= 3:
        advice_list.append(f"  💡 {name}: 食欲不振の日が多めです。体調をよく観察してください。")

    return "\n".join(lines), advice_list


def build_report_messages(year: int, month: int, df) -> list[dict]:
    """LINE に送る全メッセージリストを作成"""
    month_label = f"{year}年{month}月"
    data = analyze_month(df, year, month)

    # ヘッダー
    header_lines = [f"📊 {month_label}の猫たちの健康まとめ", ""]
    all_advice = []

    for cat in CATS:
        block, advice = format_cat_block(data[cat["id"]])
        header_lines.append(block)
        header_lines.append("")
        all_advice.extend(advice)

    # 対策セクション
    if all_advice:
        header_lines.append("─────────────────")
        header_lines.append("📋 今月の気になるポイント")
        header_lines.extend(all_advice)
    else:
        header_lines.append("─────────────────")
        header_lines.append("✨ 今月は2匹とも問題なし！引き続き見守りましょう。")

    text = "\n".join(header_lines)
    return [{"type": "text", "text": text}]


# ── 送信 ────────────────────────────────────────────────
def send_messages(messages: list[dict], dry_run: bool = False):
    if dry_run:
        print("── DRY RUN ─────────────────────────")
        for i, msg in enumerate(messages, 1):
            print(f"[メッセージ {i}]")
            print(json.dumps(msg, ensure_ascii=False, indent=2))
        print("─────────────────────────────────────")
        return

    if not CHANNEL_TOKEN:
        print("❌ LINE_CHANNEL_ACCESS_TOKEN が未設定", file=sys.stderr)
        sys.exit(1)
    if not GROUP_ID:
        print("❌ LINE_GROUP_ID が未設定", file=sys.stderr)
        sys.exit(1)

    resp = requests.post(
        LINE_API_URL,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {CHANNEL_TOKEN}"},
        json={"to": GROUP_ID, "messages": messages},
        timeout=30,
    )
    resp.raise_for_status()
    print(f"✅ 月次レポート送信完了 ({len(messages)} 件)")


# ── メイン ───────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="猫の月次健康レポートを LINE に送信")
    parser.add_argument("--dry-run", action="store_true", help="送信せず内容を表示")
    parser.add_argument("--month", help="集計対象月 (例: 2026-05)。省略時は先月")
    args = parser.parse_args()

    now_jst = datetime.now(JST)

    if args.month:
        y, m = map(int, args.month.split("-"))
    else:
        # 先月
        first_of_this_month = now_jst.replace(day=1)
        last_month = first_of_this_month - timedelta(days=1)
        y, m = last_month.year, last_month.month

    print(f"📅 集計対象: {y}年{m}月")
    df = load_csv()

    if df.empty:
        print("⚠️ CSV が空です。レポートをスキップします。")
        return

    messages = build_report_messages(y, m, df)
    send_messages(messages, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

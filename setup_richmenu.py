#!/usr/bin/env python3
"""
setup_richmenu.py
=================
LINE リッチメニューをワンコマンドで作成・設定する（1回だけ実行）。

実行前に Pillow をインストール:
    pip install -r requirements-local.txt

使い方:
    python setup_richmenu.py

環境変数:
    LINE_CHANNEL_ACCESS_TOKEN — LINE Bot のアクセストークン
    LINE_GROUP_ID             — 送信先グループID（省略可）
"""

import io
import os
import sys

import requests

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("❌ Pillow が必要です: pip install -r requirements-local.txt")
    sys.exit(1)

TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")

# ── 画像サイズ ──────────────────────────────────────────────
W  = 2500
H  = 1686
ROW = H // 2   # 843 px（上段・下段）

# ── ボタン定義 ──────────────────────────────────────────────
TOP_PANELS = [
    {"label": "2匹とも元気！", "sub": "✅ 異常なし一括記録", "color": "#5C9E5C",
     "data": "action=all_ok", "display": "2匹とも異常なし"},
    {"label": "りんこ症状",    "sub": "🐈‍⬛ 症状を記録",       "color": "#E07B39",
     "data": "action=symptom_start&cat=rinko", "display": "りんこの症状を記録"},
    {"label": "そうた症状",    "sub": "🐈 症状を記録",        "color": "#C96B2A",
     "data": "action=symptom_start&cat=souta", "display": "そうたの症状を記録"},
]

BOTTOM_PANELS = [
    {"label": "今月",   "sub": "📅 今月の記録を確認", "color": "#3A7DB5",
     "data": "action=history&month=1", "display": "今月の記録を表示"},
    {"label": "全履歴", "sub": "📋 すべての記録を確認", "color": "#2A5F8F",
     "data": "action=history&all=1",   "display": "全履歴を表示"},
]

COL3 = W // 3          # 833 (上段3等分)
COL2 = W // 2          # 1250 (下段2等分)


def _find_font(size: int):
    candidates = [
        r"C:\Windows\Fonts\meiryo.ttc",
        r"C:\Windows\Fonts\YuGothM.ttc",
        r"C:\Windows\Fonts\msgothic.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            pass
    return ImageFont.load_default()


def _draw_panel(draw, x0, y0, x1, y1, color, label, sub, font_main, font_sub):
    draw.rectangle([x0, y0, x1, y1], fill=color)
    cx = (x0 + x1) // 2
    cy = (y0 + y1) // 2

    # メインラベル
    bb = draw.textbbox((0, 0), label, font=font_main)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    draw.text((cx - tw // 2, cy - th // 2 - 28), label, font=font_main, fill="white")

    # サブラベル
    bb2 = draw.textbbox((0, 0), sub, font=font_sub)
    sw = bb2[2] - bb2[0]
    draw.text((cx - sw // 2, cy + th // 2), sub, font=font_sub, fill="rgba(255,255,255,200)")


def make_image() -> bytes:
    img  = Image.new("RGB", (W, H), "#FFFFFF")
    draw = ImageDraw.Draw(img)

    font_main = _find_font(120)
    font_sub  = _find_font(52)

    # 上段: 3等分
    widths3 = [COL3, COL3, W - COL3 * 2]
    for i, panel in enumerate(TOP_PANELS):
        x0 = sum(widths3[:i])
        x1 = x0 + widths3[i]
        _draw_panel(draw, x0, 0, x1, ROW, panel["color"],
                    panel["label"], panel["sub"], font_main, font_sub)
        if i > 0:
            draw.line([(x0, 20), (x0, ROW - 20)], fill="white", width=5)

    # 上段・下段の区切り
    draw.line([(0, ROW), (W, ROW)], fill="white", width=8)

    # 下段: 2等分
    widths2 = [COL2, W - COL2]
    for i, panel in enumerate(BOTTOM_PANELS):
        x0 = sum(widths2[:i])
        x1 = x0 + widths2[i]
        _draw_panel(draw, x0, ROW, x1, H, panel["color"],
                    panel["label"], panel["sub"], font_main, font_sub)
        if i > 0:
            draw.line([(x0, ROW + 20), (x0, H - 20)], fill="white", width=5)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


# ── LINE API ────────────────────────────────────────────────
def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def delete_existing_menus():
    r = requests.get("https://api.line.me/v2/bot/richmenu/list",
                     headers=_auth(), timeout=10)
    if not r.ok:
        print(f"  メニュー一覧取得失敗: {r.status_code}")
        return
    for m in r.json().get("richmenus", []):
        mid = m["richMenuId"]
        requests.delete(f"https://api.line.me/v2/bot/richmenu/{mid}",
                        headers=_auth(), timeout=10)
        print(f"  削除: {mid}")


def create_richmenu() -> str:
    areas = []
    # 上段 3等分
    widths3 = [COL3, COL3, W - COL3 * 2]
    for i, panel in enumerate(TOP_PANELS):
        x = sum(widths3[:i])
        areas.append({
            "bounds": {"x": x, "y": 0, "width": widths3[i], "height": ROW},
            "action": {"type": "postback", "data": panel["data"],
                       "displayText": panel["display"]},
        })
    # 下段 2等分
    widths2 = [COL2, W - COL2]
    for i, panel in enumerate(BOTTOM_PANELS):
        x = sum(widths2[:i])
        areas.append({
            "bounds": {"x": x, "y": ROW, "width": widths2[i], "height": ROW},
            "action": {"type": "postback", "data": panel["data"],
                       "displayText": panel["display"]},
        })

    body = {
        "size": {"width": W, "height": H},
        "selected": True,
        "name": "猫健康管理メニュー",
        "chatBarText": "🐱 記録・履歴",
        "areas": areas,
    }
    r = requests.post("https://api.line.me/v2/bot/richmenu",
                      headers={**_auth(), "Content-Type": "application/json"},
                      json=body, timeout=10)
    r.raise_for_status()
    mid = r.json()["richMenuId"]
    print(f"  作成: {mid}")
    return mid


def upload_image(mid: str, img_bytes: bytes):
    r = requests.post(
        f"https://api-data.line.me/v2/bot/richmenu/{mid}/content",
        headers={**_auth(), "Content-Type": "image/jpeg"},
        data=img_bytes, timeout=30,
    )
    r.raise_for_status()
    print("  画像アップロード完了")


def set_default(mid: str):
    r = requests.post(
        f"https://api.line.me/v2/bot/user/all/richmenu/{mid}",
        headers=_auth(), timeout=10,
    )
    r.raise_for_status()
    print("  デフォルト設定完了")


def main():
    if not TOKEN:
        print("❌ 環境変数 LINE_CHANNEL_ACCESS_TOKEN を設定してください")
        sys.exit(1)

    print("既存のリッチメニューを削除中...")
    delete_existing_menus()

    print("画像を生成中...")
    img_bytes = make_image()
    with open("richmenu_preview.jpg", "wb") as f:
        f.write(img_bytes)
    print("  richmenu_preview.jpg を保存しました（確認用）")

    print("リッチメニューを作成中...")
    mid = create_richmenu()

    print("画像をアップロード中...")
    upload_image(mid, img_bytes)

    print("全ユーザーにデフォルト設定中...")
    set_default(mid)

    print(f"\n✅ 完了！ richMenuId: {mid}")
    print("LINEのチャットを開くと下部にメニューバーが表示されます。")


if __name__ == "__main__":
    main()

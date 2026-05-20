"""
record.py
=========
CSV の読み書きと記録操作。

CSV 列構成:
    日付, りんこ食事, そうた食事, りんこ症状, そうた症状, りんこ体重, そうた体重, メモ
"""

from pathlib import Path
import pandas as pd

CSV_FILE = Path(__file__).parent / "猫の健康記録.csv"

CSV_COLS = [
    "日付",
    "りんこ食事", "そうた食事",
    "りんこ症状", "そうた症状",
    "りんこ体重", "そうた体重",
    "メモ",
]

CAT_COL = {
    "rinko": "りんこ",
    "souta": "そうた",
}


# ── CSV 基本操作 ───────────────────────────────────────
def load_csv() -> pd.DataFrame:
    if not CSV_FILE.exists():
        return pd.DataFrame(columns=CSV_COLS)
    df = pd.read_csv(CSV_FILE, dtype=str, encoding="utf-8-sig")
    df["日付"] = pd.to_datetime(df["日付"])
    return df


def save_csv(df: pd.DataFrame):
    df = df.copy()
    df["日付"] = df["日付"].dt.strftime("%Y-%m-%d")
    df.to_csv(CSV_FILE, index=False, encoding="utf-8-sig", columns=CSV_COLS)


def get_or_create_row(df: pd.DataFrame, date_str: str) -> tuple[pd.DataFrame, int]:
    """指定日の行を取得（なければ新規作成）。(df, idx) を返す"""
    target = pd.Timestamp(date_str)
    mask = df["日付"] == target
    if mask.any():
        idx = df[mask].index[0]
    else:
        new_row = {col: "" for col in CSV_COLS}
        new_row["日付"] = target
        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
        idx = df.index[-1]
    return df, idx


# ── 記録操作 ───────────────────────────────────────────
def record_food(df: pd.DataFrame, date_str: str, cat_id: str, value: str) -> pd.DataFrame:
    """食事記録を上書き"""
    cat_jp = CAT_COL.get(cat_id)
    if not cat_jp:
        raise ValueError(f"不明な猫ID: {cat_id}")
    col = f"{cat_jp}食事"
    df, idx = get_or_create_row(df, date_str)
    df.at[idx, col] = value
    return df.sort_values("日付").reset_index(drop=True)


def record_symptoms(df: pd.DataFrame, date_str: str, cat_id: str, value: str) -> pd.DataFrame:
    """症状記録を追記（既存があれば「,」で結合）"""
    cat_jp = CAT_COL.get(cat_id)
    if not cat_jp:
        raise ValueError(f"不明な猫ID: {cat_id}")
    col = f"{cat_jp}症状"
    df, idx = get_or_create_row(df, date_str)
    existing = str(df.at[idx, col]) if pd.notna(df.at[idx, col]) else ""
    if existing and value not in existing:
        df.at[idx, col] = f"{existing},{value}"
    else:
        df.at[idx, col] = value
    return df.sort_values("日付").reset_index(drop=True)


def record_weight(df: pd.DataFrame, date_str: str, cat_id: str, value: str) -> pd.DataFrame:
    """体重記録を上書き"""
    cat_jp = CAT_COL.get(cat_id)
    if not cat_jp:
        raise ValueError(f"不明な猫ID: {cat_id}")
    col = f"{cat_jp}体重"
    df, idx = get_or_create_row(df, date_str)
    df.at[idx, col] = value
    return df.sort_values("日付").reset_index(drop=True)


def record_memo(df: pd.DataFrame, date_str: str, text: str) -> pd.DataFrame:
    """メモ記録を追記"""
    df, idx = get_or_create_row(df, date_str)
    existing = str(df.at[idx, "メモ"]) if pd.notna(df.at[idx, "メモ"]) else ""
    df.at[idx, "メモ"] = f"{existing} {text}".strip() if existing else text
    return df.sort_values("日付").reset_index(drop=True)


# ── サマリー取得 ───────────────────────────────────────
def today_summary(df: pd.DataFrame, date_str: str) -> dict:
    """指定日の記録を辞書で返す。記録がなければ空辞書"""
    if df.empty:
        return {}
    target = pd.Timestamp(date_str)
    row = df[df["日付"] == target]
    if row.empty:
        return {}
    r = row.iloc[0]
    result = {}
    for cat_id, cat_jp in CAT_COL.items():
        result[f"{cat_id}_food"]     = str(r.get(f"{cat_jp}食事",  "") or "")
        result[f"{cat_id}_symptoms"] = str(r.get(f"{cat_jp}症状",  "") or "")
        result[f"{cat_id}_weight"]   = str(r.get(f"{cat_jp}体重",  "") or "")
    return result

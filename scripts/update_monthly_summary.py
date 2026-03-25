"""
月別サマリー自動集計スクリプト
Usage:
  python scripts/update_monthly_summary.py          # 当月
  python scripts/update_monthly_summary.py 2026/03  # 指定月
"""
import os
import sys
import math
import httpx
from datetime import date, datetime
from dotenv import load_dotenv

# .env 読み込み（ローカル実行時）
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

DOMAIN       = os.getenv("KINTONE_DOMAIN", "exk1223hafrf.cybozu.com")
TOKEN_364    = os.getenv("KINTONE_TOKEN_364", "")   # 千秋工場_製造管理（出荷売上）
TOKEN_723    = os.getenv("KINTONE_TOKEN_723", "")   # OEM売上
TOKEN_792    = os.getenv("KINTONE_TOKEN_792", "")   # 購入経費・使用材料
TOKEN_793    = os.getenv("KINTONE_TOKEN_793", "")   # 月別サマリー

APP_SHIPPING = 364
APP_OEM      = 723
APP_EXPENSE  = 792
APP_SUMMARY  = 793

BASE = f"https://{DOMAIN}/k/v1"


def get_headers(token: str) -> dict:
    return {"X-Cybozu-API-Token": token}


def post_headers(token: str) -> dict:
    return {"X-Cybozu-API-Token": token, "Content-Type": "application/json"}


def get_all_records(token: str, app: int, query: str, fields: list[str]) -> list[dict]:
    """Kintone から全レコードを取得（500件ずつページング）"""
    records = []
    offset = 0
    limit  = 500
    with httpx.Client(timeout=30) as client:
        while True:
            params = [("app", app), ("query", f"{query} limit {limit} offset {offset}")]
            for i, f in enumerate(fields):
                params.append((f"fields[{i}]", f))
            resp = client.get(f"{BASE}/records.json", params=params, headers=get_headers(token))
            resp.raise_for_status()
            batch = resp.json()["records"]
            records.extend(batch)
            if len(batch) < limit:
                break
            offset += limit
    return records


def get_summary_record(ym: str) -> dict | None:
    """月別サマリーから対象年月のレコードを取得（なければ None）"""
    query = f'対象年月 = "{ym}"'
    with httpx.Client(timeout=15) as client:
        params = [("app", APP_SUMMARY), ("query", query)]
        resp = client.get(f"{BASE}/records.json", params=params, headers=get_headers(TOKEN_793))
        resp.raise_for_status()
        recs = resp.json()["records"]
    return recs[0] if recs else None


def upsert_summary(ym: str, values: dict) -> None:
    """月別サマリーを upsert（存在→更新、なし→新規作成）"""
    existing = get_summary_record(ym)
    record_body = {k: {"value": str(v)} for k, v in values.items()}

    with httpx.Client(timeout=15) as client:
        if existing:
            record_id = existing["レコード番号"]["value"]
            payload = {"app": APP_SUMMARY, "id": record_id, "record": record_body}
            resp = client.put(f"{BASE}/record.json", json=payload, headers=post_headers(TOKEN_793))
        else:
            record_body["対象年月"] = {"value": ym}
            payload = {"app": APP_SUMMARY, "record": record_body}
            resp = client.post(f"{BASE}/record.json", json=payload, headers=post_headers(TOKEN_793))
        resp.raise_for_status()


def ym_to_date_range(ym: str) -> tuple[str, str]:
    """'2026/03' → ('2026-03-01', '2026-03-31')"""
    y, m = int(ym[:4]), int(ym[5:7])
    first = date(y, m, 1)
    if m == 12:
        last = date(y + 1, 1, 1).replace(day=1)
        last = date(y, m, 31)
    else:
        last = date(y, m + 1, 1).replace(day=1)
        import calendar
        _, last_day = calendar.monthrange(y, m)
        last = date(y, m, last_day)
    return first.strftime("%Y-%m-%d"), last.strftime("%Y-%m-%d")


def aggregate_shipping(ym: str) -> int:
    """App 364 から出荷売上_税抜 を集計"""
    if not TOKEN_364:
        print("  [WARN] KINTONE_TOKEN_364 未設定 → 出荷売上 = 0")
        return 0
    d_from, d_to = ym_to_date_range(ym)
    query = f'出荷予定日 >= "{d_from}" and 出荷予定日 <= "{d_to}"'
    records = get_all_records(TOKEN_364, APP_SHIPPING, query, ["mono_当月売上_税抜"])
    total = sum(int(float(r["mono_当月売上_税抜"]["value"] or 0)) for r in records)
    print(f"  出荷売上_税抜: ¥{total:,}  ({len(records)} 件)")
    return total


def aggregate_oem(ym: str) -> int:
    """App 723 から OEM売上_税抜 を集計（税込÷1.1 切り捨て）"""
    if not TOKEN_723:
        print("  [WARN] KINTONE_TOKEN_723 未設定 → OEM売上 = 0")
        return 0
    d_from, d_to = ym_to_date_range(ym)
    query = f'お渡し日 >= "{d_from}" and お渡し日 <= "{d_to}"'
    records = get_all_records(TOKEN_723, APP_OEM, query, ["数値"])
    total_taxin = sum(int(float(r["数値"]["value"] or 0)) for r in records)
    total_taxex = math.floor(total_taxin / 1.1)
    print(f"  OEM売上_税込: ¥{total_taxin:,} → 税抜: ¥{total_taxex:,}  ({len(records)} 件)")
    return total_taxex


def aggregate_expenses(ym: str) -> dict:
    """
    App 792 から費目区分別集計（購入経費）と使用材料費合計を返す
    戻り値: {
        変動費_材料費, 変動費_塗料, 変動費_外注費,
        固定費_梱包材, 固定費_洗浄関連, その他固定費,
        使用材料費合計
    }
    """
    # 購入経費
    query_purchase = f'対象年月 = "{ym}" and 入力種別 = "購入経費"'
    recs_purchase = get_all_records(
        TOKEN_792, APP_EXPENSE, query_purchase,
        ["費目区分", "金額"]
    )
    # 費目区分マッピング
    費目map = {
        "変動費_材料費":  0,
        "変動費_塗料":    0,
        "変動費_外注費":  0,
        "固定費_梱包材":  0,
        "固定費_洗浄関連": 0,
        "その他固定費":   0,
    }
    for r in recs_purchase:
        cat = r["費目区分"]["value"]
        amt = int(float(r["金額"]["value"] or 0))
        if cat in 費目map:
            費目map[cat] += amt

    # 使用材料
    query_usage = f'対象年月 = "{ym}" and 入力種別 = "使用材料・消耗品"'
    recs_usage = get_all_records(TOKEN_792, APP_EXPENSE, query_usage, ["金額"])
    usage_total = sum(int(float(r["金額"]["value"] or 0)) for r in recs_usage)

    for k, v in 費目map.items():
        print(f"  {k}: ¥{v:,}")
    print(f"  使用材料費合計: ¥{usage_total:,}  ({len(recs_usage)} 件)")

    return {**費目map, "使用材料費合計": usage_total}


def main():
    # 対象年月を決定
    if len(sys.argv) >= 2:
        ym = sys.argv[1]
    else:
        now = datetime.now()
        ym = f"{now.year}/{now.month:02d}"

    print(f"=== 月別サマリー集計開始: {ym} ===")

    if not TOKEN_793:
        print("[ERROR] KINTONE_TOKEN_793 が未設定です。処理を中断します。")
        sys.exit(1)

    print("[1] 出荷売上 集計中...")
    shipping = aggregate_shipping(ym)

    print("[2] OEM売上 集計中...")
    oem = aggregate_oem(ym)

    print("[3] 購入経費・使用材料 集計中...")
    expenses = aggregate_expenses(ym)

    values = {
        "出荷売上_税抜":   shipping,
        "OEM売上_税抜":   oem,
        **expenses,
    }

    print("[4] 月別サマリー upsert 中...")
    upsert_summary(ym, values)

    print(f"=== 完了: {ym} のサマリーを更新しました ===")
    print(f"  総売上_税抜(計算): ¥{shipping + oem:,}")
    変動計 = expenses["変動費_材料費"] + expenses["変動費_塗料"] + expenses["変動費_外注費"] + expenses["使用材料費合計"]
    固定計 = expenses["固定費_梱包材"] + expenses["固定費_洗浄関連"] + expenses["その他固定費"]
    経費合計 = 変動計 + 固定計
    粗利 = shipping + oem - 経費合計
    粗利率 = round(粗利 / (shipping + oem) * 100, 1) if (shipping + oem) > 0 else 0
    print(f"  変動費合計: ¥{変動計:,}  固定費合計: ¥{固定計:,}")
    print(f"  経費合計: ¥{経費合計:,}")
    print(f"  粗利: ¥{粗利:,}  粗利率: {粗利率}%")


if __name__ == "__main__":
    main()

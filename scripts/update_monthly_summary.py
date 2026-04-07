"""
月別サマリー自動集計スクリプト
Usage:
  python scripts/update_monthly_summary.py          # 当月
  python scripts/update_monthly_summary.py 2026/03  # 指定月

集計元アプリ:
  App 364: 千秋工場_製造管理（出荷売上）
  App 723: OEM売上
  App 792: 使用材料・消耗品入力（Webフォーム）
  App 794: 出金管理
集計先アプリ:
  App 793: 月別サマリー
"""
import os
import sys
import math
import httpx
from datetime import date, datetime
import calendar
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

DOMAIN      = os.getenv("KINTONE_DOMAIN", "exk1223hafrf.cybozu.com")
TOKEN_364   = os.getenv("KINTONE_TOKEN_364", "")
TOKEN_723   = os.getenv("KINTONE_TOKEN_723", "")
TOKEN_791   = os.getenv("KINTONE_TOKEN_791", "")   # 在庫リスト
TOKEN_792   = os.getenv("KINTONE_TOKEN_792", "")   # 使用材料・消耗品入力（Webフォーム）
TOKEN_794   = os.getenv("KINTONE_TOKEN_794", "")   # 出金管理
TOKEN_793   = os.getenv("KINTONE_TOKEN_793", "")   # 月別サマリー

APP_SHIPPING  = 364
APP_OEM       = 723
APP_INVENTORY = 791  # 在庫リスト
APP_USAGE     = 792  # 使用材料・消耗品入力
APP_EXPENSE   = 794  # 出金管理
APP_SUMMARY   = 793

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


def aggregate_usage(ym: str) -> dict:
    """
    App 792（使用材料・消耗品入力）から製造原価を集計。
    「数量 × App791移動平均単価」で計算することで、
    暫定単価→確定単価の変更後も正確な製造原価が反映される。
    TOKEN_791 未設定時は金額フィールドで代替。
    用途区分マッピング:
      樹脂                → 製造原価_樹脂
      変動費（製造用）    → 製造原価_変動費
      製造用消耗品        → 製造原価_変動費（在庫化のため App792経由で計上）
      量産用材料          → 製造原価_変動費（旧値 後方互換）
      量産用消耗品        → 製造原価_変動費（旧値 後方互換）
      その他・製造用備品  → 集計対象外
    戻り値: { 製造原価_樹脂, 製造原価_変動費 }
    """
    if not TOKEN_792:
        print("  [WARN] KINTONE_TOKEN_792 未設定 → 使用材料 = 0")
        return {"製造原価_樹脂": 0, "製造原価_変動費": 0}

    d_from, d_to = ym_to_date_range(ym)
    query = (
        f'入力種別 in ("使用材料・消耗品")'
        f' and 入力日 >= "{d_from}" and 入力日 <= "{d_to}"'
    )
    変動費区分 = {"変動費（製造用）", "量産用材料", "量産用消耗品", "製造用消耗品"}
    対象区分   = {"樹脂"} | 変動費区分

    # App791 移動平均単価マップを構築（1回のAPIで全件取得）
    価格map: dict[str, float] = {}
    if TOKEN_791:
        inv_records = get_all_records(
            TOKEN_791, APP_INVENTORY, '品目名 != ""', ["品目名", "移動平均単価"]
        )
        for inv in inv_records:
            name  = inv["品目名"]["value"]
            price = float(inv["移動平均単価"]["value"] or 0)
            # 重複品目は移動平均単価が高いレコードを優先
            if name not in 価格map or price > 価格map[name]:
                価格map[name] = price
        print(f"  App791 単価参照: {len(価格map)}品目")

    # App792 の数量を取得し、移動平均単価で集計
    records = get_all_records(TOKEN_792, APP_USAGE, query, ["用途区分", "品目名", "数量", "金額"])

    result = {"製造原価_樹脂": 0, "製造原価_変動費": 0}
    for r in records:
        cat  = r["用途区分"]["value"]
        if cat not in 対象区分:
            continue
        name = r["品目名"]["value"]
        qty  = float(r["数量"]["value"] or 0)
        if 価格map and name in 価格map:
            # 確定済み移動平均単価で計算
            amt = int(qty * 価格map[name])
        else:
            # 単価マップにない場合は登録時金額で代替
            amt = int(float(r["金額"]["value"] or 0))
        if cat == "樹脂":
            result["製造原価_樹脂"] += amt
        elif cat in 変動費区分:
            result["製造原価_変動費"] += amt

    for k, v in result.items():
        print(f"  {k}: ¥{v:,}")
    return result


def aggregate_expenses(ym: str) -> dict:
    """
    App 794（出金管理）から出金区分別に集計
    樹脂・変動費（製造用）・製造用消耗品 は在庫計上のみ（費用集計対象外）
    税抜き・税込みをそれぞれ集計（海外仕入れは1.1倍しない）
    戻り値: { 製造用備品, 製造用備品_税抜, 外注費, 外注費_税抜,
              固定費, 人件費, 人材派遣費,
              水道光熱費, 福利厚生, 開発_実験, 事務用品, 輸送費_送料,
              設備投資, その他費用 }
    ※製造用消耗品 は在庫化のため aggregate_usage() 側（App792経由）で製造原価_変動費 に計上
    """
    if not TOKEN_794:
        print("  [WARN] KINTONE_TOKEN_794 未設定 → 出金管理 = 0")
        return {k: 0 for k in [
            "製造用備品", "製造用備品_税抜",
            "外注費", "外注費_税抜",
            "固定費", "人件費", "人材派遣費",
            "水道光熱費", "福利厚生", "開発_実験",
            "事務用品", "輸送費_送料", "設備投資", "その他費用"
        ]}

    d_from, d_to = ym_to_date_range(ym)
    query = f'日付 >= "{d_from}" and 日付 <= "{d_to}"'
    # 税込み額（国内=税抜×1.1、海外/非課税=税抜のまま）と税抜き額（金額）を両方取得
    records = get_all_records(TOKEN_794, APP_EXPENSE, query, ["出金区分", "税込み額", "金額"])

    # 出金区分 → App793フィールドコード マッピング（税込み）
    # 樹脂・変動費（製造用）・製造用消耗品 は在庫計上のみなので除外
    区分map_税込 = {
        "製造用備品":   "製造用備品",
        "外注費":       "外注費",
        "固定費":       "固定費",
        "人件費":       "人件費",
        "人材派遣費":   "人材派遣費",
        "水道光熱費":   "水道光熱費",
        "福利厚生":     "福利厚生",
        "開発・実験":   "開発_実験",
        "事務用品":     "事務用品",
        "輸送費・送料": "輸送費_送料",
        "設備投資":     "設備投資",
        "その他":       "その他費用",
    }
    # 税抜きを別途集計する区分（製造用備品・外注費のみ）
    区分map_税抜 = {
        "製造用備品":   "製造用備品_税抜",
        "外注費":       "外注費_税抜",
    }

    result = {v: 0 for v in 区分map_税込.values()}
    result.update({v: 0 for v in 区分map_税抜.values()})

    skipped = 0
    for r in records:
        cat = r["出金区分"]["value"]
        taxin = int(float(r["税込み額"]["value"] or 0))
        taxex = int(float(r["金額"]["value"] or 0))
        if cat in 区分map_税込:
            result[区分map_税込[cat]] += taxin
        else:
            # 樹脂・変動費（製造用）・製造用消耗品 は在庫計上のみ → スキップ
            skipped += taxin
        if cat in 区分map_税抜:
            result[区分map_税抜[cat]] += taxex

    for k, v in result.items():
        if v > 0:
            print(f"  {k}: ¥{v:,}")
    if skipped > 0:
        print(f"  在庫計上分（樹脂・変動費製造用・消耗品）: ¥{skipped:,}（費用集計対象外）")
    return result


def main():
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

    print("[3] 使用材料（製造原価）集計中...")
    usage = aggregate_usage(ym)

    print("[4] 出金管理 集計中...")
    expenses = aggregate_expenses(ym)

    # 製造原価合計（CALC フィールドで自動計算されるが参考表示用に算出）
    # 製造原価 = 使用量ベース(App792) + 備品・外注費(App794)
    # ※製造用消耗品は在庫化のため usage["製造原価_変動費"] に含まれる
    製造原価合計 = (
        usage["製造原価_樹脂"] + usage["製造原価_変動費"] +
        expenses["製造用備品"] + expenses["外注費"]
    )
    全費用合計 = 製造原価合計 + sum(
        expenses[k] for k in [
            "固定費", "人件費", "人材派遣費",
            "水道光熱費", "福利厚生", "開発_実験",
            "事務用品", "輸送費_送料", "その他費用"
        ]
    )
    総売上 = shipping + oem

    values = {
        "出荷売上_税抜": shipping,
        "OEM売上_税抜":  oem,
        **usage,
        **expenses,
    }

    print("[5] 月別サマリー upsert 中...")
    upsert_summary(ym, values)

    print(f"=== 完了: {ym} のサマリーを更新しました ===")
    print(f"  総売上_税抜:         ¥{総売上:,}")
    print(f"  製造原価合計:        ¥{製造原価合計:,}")
    print(f"  全費用合計:          ¥{全費用合計:,}  ※設備投資除く")
    print(f"  設備投資（参考）:    ¥{expenses['設備投資']:,}")
    if 総売上 > 0:
        r1 = round((総売上 - 製造原価合計) / 総売上 * 100, 1)
        r2 = round((総売上 - 全費用合計) / 総売上 * 100, 1)
        print(f"  粗利率（製造原価ベース）: {r1}%")
        print(f"  粗利率（全費用ベース）:   {r2}%")


if __name__ == "__main__":
    main()

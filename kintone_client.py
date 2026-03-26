"""Kintone REST API クライアント"""
import os
import httpx
from typing import Any

DOMAIN    = os.getenv("KINTONE_DOMAIN", "exk1223hafrf.cybozu.com")
TOKEN_791 = os.getenv("KINTONE_TOKEN_791", "")
TOKEN_792 = os.getenv("KINTONE_TOKEN_792", "")
TOKEN_794 = os.getenv("KINTONE_TOKEN_794", "")

APP_INVENTORY = 791
APP_USAGE     = 792   # 使用材料・消耗品入力（Webフォーム書き込み先）
APP_PURCHASE  = 794   # 出金管理（購入記録）


def _get_headers(token: str) -> dict:
    return {"X-Cybozu-API-Token": token}

def _post_headers(token: str) -> dict:
    return {"X-Cybozu-API-Token": token, "Content-Type": "application/json"}


def _base() -> str:
    return f"https://{DOMAIN}/k/v1"


# ─── 在庫リスト（App 791）─────────────────────────────────────

async def get_inventory_items() -> list[dict]:
    """App 791 の全在庫アイテムを取得してサジェスト用に返す"""
    url = f"{_base()}/records.json"
    params = [
        ("app", APP_INVENTORY),
        ("fields[0]", "品目コード"),
        ("fields[1]", "品目名"),
        ("fields[2]", "区分"),
        ("fields[3]", "移動平均単価"),
        ("fields[4]", "単位"),
        ("fields[5]", "班別"),
        ("fields[6]", "現在庫数"),
        ("query", "limit 500"),
    ]
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params=params, headers=_get_headers(TOKEN_791))
        resp.raise_for_status()
        records = resp.json()["records"]

    # 品目コードで重複除去（在庫数が多いレコードを優先）
    seen: dict[str, dict] = {}
    for r in records:
        code = r["品目コード"]["value"] or r["品目名"]["value"]
        item = {
            "品目コード": r["品目コード"]["value"],
            "品目名":    r["品目名"]["value"],
            "区分":      r["区分"]["value"],
            "単価":      r["移動平均単価"]["value"] or "0",
            "単位":      r["単位"]["value"],
            "班別":      r["班別"]["value"],
            "現在庫数":  r["現在庫数"]["value"] or "0",
        }
        if code not in seen or float(item["現在庫数"]) > float(seen[code]["現在庫数"]):
            seen[code] = item
    return list(seen.values())


async def _get_inventory_by_code(品目コード: str) -> dict | None:
    """品目コードで App 791 レコードを1件取得"""
    url = f"{_base()}/records.json"
    params = [
        ("app", APP_INVENTORY),
        ("query", f'品目コード = "{品目コード}" limit 1'),
        ("fields[0]", "レコード番号"),
        ("fields[1]", "現在庫数"),
        ("fields[2]", "移動平均単価"),
        ("fields[3]", "最新単価"),
        ("fields[4]", "累計購入数量"),
        ("fields[5]", "累計購入金額"),
    ]
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params=params, headers=_get_headers(TOKEN_791))
        resp.raise_for_status()
        recs = resp.json()["records"]
    return recs[0] if recs else None


async def _update_inventory_record(record_id: str, fields: dict[str, str]) -> None:
    """App 791 の指定レコードを更新"""
    payload = {
        "app": APP_INVENTORY,
        "id": record_id,
        "record": {k: {"value": v} for k, v in fields.items()},
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.put(
            f"{_base()}/record.json", json=payload,
            headers=_post_headers(TOKEN_791)
        )
        resp.raise_for_status()


async def decrease_inventory(品目コード: str, 数量: float) -> bool:
    """消費登録時に App 791 の在庫数を減算する"""
    inv = await _get_inventory_by_code(品目コード)
    if not inv:
        return False
    record_id = inv["レコード番号"]["value"]
    現在庫数 = float(inv["現在庫数"]["value"] or 0)
    new_qty = max(0.0, 現在庫数 - 数量)
    await _update_inventory_record(record_id, {"現在庫数": str(new_qty)})
    return True


# ─── 使用材料入力（App 792）──────────────────────────────────

async def create_usage_record(data: dict[str, Any]) -> dict:
    """App 792 に使用材料・消耗品レコードを登録し、在庫 (App 791) を減算する"""
    url = f"{_base()}/record.json"
    payload = {
        "app": APP_USAGE,
        "record": {
            "入力種別": {"value": "使用材料・消耗品"},
            "対象年月": {"value": data["対象年月"]},
            "入力日":   {"value": data["入力日"]},
            "班別":     {"value": data["班別"]},
            "品目コード": {"value": data.get("品目コード", "")},
            "品目名":   {"value": data["品目名"]},
            "用途区分": {"value": data.get("用途区分", "")},
            "数量":     {"value": str(data.get("数量", ""))},
            "単価":     {"value": str(data.get("単価", ""))},
            "金額":     {"value": str(data["金額"])},
            "備考":     {"value": data.get("備考", "")},
        },
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=payload, headers=_post_headers(TOKEN_792))
        resp.raise_for_status()
        result = resp.json()

    # 在庫品目コードがあれば App 791 在庫を減算
    品目コード = data.get("品目コード", "")
    数量 = float(data.get("数量", 0) or 0)
    if 品目コード and 数量 > 0:
        try:
            await decrease_inventory(品目コード, 数量)
        except Exception as e:
            # 在庫減算失敗は致命的ではないのでログのみ
            print(f"[WARN] 在庫減算エラー ({品目コード}): {e}")

    return result


async def get_usage_record(record_id: str) -> dict:
    """App 792 の使用材料レコードを1件取得（フォームコピー用）"""
    url = f"{_base()}/record.json"
    params = [("app", APP_USAGE), ("id", record_id)]
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params=params, headers=_get_headers(TOKEN_792))
        resp.raise_for_status()
        r = resp.json()["record"]
    return {
        "班別":      r["班別"]["value"],
        "品目コード": r["品目コード"]["value"],
        "品目名":    r["品目名"]["value"],
        "用途区分":  r["用途区分"]["value"],
        "数量":      r["数量"]["value"] or "0",
        "単価":      r["単価"]["value"] or "0",
        "備考":      r["備考"]["value"],
    }


async def get_recent_usage(ym: str, limit: int = 20) -> list[dict]:
    """直近の使用材料入力履歴を取得する"""
    url = f"{_base()}/records.json"
    params = [
        ("app", APP_USAGE),
        ("fields[0]", "レコード番号"),
        ("fields[1]", "入力日"),
        ("fields[2]", "班別"),
        ("fields[3]", "品目名"),
        ("fields[4]", "数量"),
        ("fields[5]", "金額"),
        ("query", f'入力種別 in ("使用材料・消耗品") and 対象年月 = "{ym}" order by 作成日時 desc limit {limit}'),
    ]
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params=params, headers=_get_headers(TOKEN_792))
        resp.raise_for_status()
        records = resp.json()["records"]

    return [
        {
            "レコード番号": r["レコード番号"]["value"],
            "入力日":  r["入力日"]["value"],
            "班別":    r["班別"]["value"],
            "品目名":  r["品目名"]["value"],
            "数量":    r["数量"]["value"],
            "単位":    "",
            "金額":    r["金額"]["value"],
        }
        for r in records
    ]


# ─── 購入→在庫同期（App 794 → App 791）──────────────────────

# App 791 自動作成の対象となる出金区分
_AUTO_CREATE_区分 = {"樹脂", "変動費（製造用）", "製造用消耗品", "外注費"}


async def _create_inventory_record(
    品目コード: str, 品目名: str, 区分: str, 班別: str, 単価: float, 単位: str = ""
) -> None:
    """App 791 に在庫マスタレコードを新規作成する"""
    payload = {
        "app": APP_INVENTORY,
        "record": {
            "品目コード":    {"value": 品目コード},
            "品目名":        {"value": 品目名},
            "区分":          {"value": 区分},
            "班別":          {"value": 班別},
            "単位":          {"value": 単位},
            "現在庫数":      {"value": "0"},
            "移動平均単価":  {"value": str(単価)},
            "最新単価":      {"value": str(単価)},
            "累計購入数量":  {"value": "0"},
            "累計購入金額":  {"value": "0"},
        },
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{_base()}/record.json", json=payload,
            headers=_post_headers(TOKEN_791)
        )
        resp.raise_for_status()
    print(f"[INFO] App791 新規作成: 品目コード='{品目コード}' 区分='{区分}'")


async def sync_purchases_to_inventory() -> dict:
    """
    App 794 の未処理購入レコード（在庫品目コードあり）を
    App 791 に反映（在庫数加算・移動平均単価更新）する。
    出金区分が対象区分（樹脂/変動費（製造用）/製造用消耗品/外注費）の場合、
    App 791 に品目コードが存在しなければ自動作成する。
    処理済レコードには在庫反映状況='反映済'をセット。
    """
    url = f"{_base()}/records.json"
    params = [
        ("app", APP_PURCHASE),
        ("query", '在庫品目コード != "" and 在庫反映状況 not in ("反映済") order by 日付 asc limit 100'),
        ("fields[0]", "レコード番号"),
        ("fields[1]", "在庫品目コード"),
        ("fields[2]", "購入数"),
        ("fields[3]", "単位価格_税抜"),
        ("fields[4]", "金額"),
        ("fields[5]", "日付"),
        ("fields[6]", "出金区分"),
        ("fields[7]", "品目名"),
        ("fields[8]", "班"),
        ("fields[9]", "単位"),
    ]
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, params=params, headers=_get_headers(TOKEN_794))
        resp.raise_for_status()
        purchase_records = resp.json()["records"]

    processed = 0
    created = 0
    errors: list[str] = []

    for r in purchase_records:
        record_id  = r["レコード番号"]["value"]
        品目コード  = r["在庫品目コード"]["value"]
        購入数     = float(r["購入数"]["value"] or 0)
        単価       = float(r["単位価格_税抜"]["value"] or 0)
        金額       = float(r["金額"]["value"] or 0)
        日付       = r["日付"]["value"]
        出金区分   = r["出金区分"]["value"]
        品目名     = r["品目名"]["value"]
        班別       = r["班"]["value"]
        単位       = r["単位"]["value"]

        if 購入数 <= 0:
            # 数量なし → 処理済にして次へ
            await _mark_purchase_processed(record_id)
            continue

        try:
            inv = await _get_inventory_by_code(品目コード)

            # App791 に存在しない場合、対象区分なら自動作成
            if not inv:
                if 出金区分 in _AUTO_CREATE_区分:
                    await _create_inventory_record(品目コード, 品目名, 出金区分, 班別, 単価, 単位)
                    created += 1
                    inv = await _get_inventory_by_code(品目コード)
                    if not inv:
                        errors.append(f"品目コード '{品目コード}' の自動作成後に取得できませんでした")
                        continue
                else:
                    errors.append(
                        f"品目コード '{品目コード}' がApp791に存在せず、出金区分 '{出金区分}' は自動作成対象外です"
                    )
                    continue

            inv_id    = inv["レコード番号"]["value"]
            現在庫数   = float(inv["現在庫数"]["value"] or 0)
            現移動平均 = float(inv["移動平均単価"]["value"] or 0)
            累計数     = float(inv["累計購入数量"]["value"] or 0)
            累計額     = float(inv["累計購入金額"]["value"] or 0)

            # 移動平均単価を再計算
            新在庫数 = 現在庫数 + 購入数
            if 新在庫数 > 0:
                新移動平均 = (現在庫数 * 現移動平均 + 購入数 * 単価) / 新在庫数
            else:
                新移動平均 = 単価

            update_fields = {
                "現在庫数":      str(新在庫数),
                "移動平均単価":  str(round(新移動平均, 1)),
                "最新単価":      str(単価),
                "最終購入日":    日付,
                "累計購入数量":  str(累計数 + 購入数),
                "累計購入金額":  str(累計額 + 金額),
            }
            if 単位:
                update_fields["単位"] = 単位
            await _update_inventory_record(inv_id, update_fields)

            await _mark_purchase_processed(record_id)
            processed += 1

        except Exception as e:
            errors.append(f"品目コード '{品目コード}': {e}")

    return {
        "processed": processed,
        "created":   created,
        "total":     len(purchase_records),
        "errors":    errors,
    }


async def _mark_purchase_processed(record_id: str) -> None:
    """App 794 レコードの在庫反映状況を '反映済' に更新"""
    payload = {
        "app": APP_PURCHASE,
        "id": record_id,
        "record": {"在庫反映状況": {"value": "反映済"}},
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.put(
            f"{_base()}/record.json", json=payload,
            headers=_post_headers(TOKEN_794)
        )
        resp.raise_for_status()


# ─── 購入入力（App 794）────────────────────────────────────────

async def get_purchase_suggestions() -> list[dict]:
    """App 794 の過去購入レコードをサジェスト用に取得（品目名で重複除去・最新値優先）"""
    url = f"{_base()}/records.json"
    params = [
        ("app", APP_PURCHASE),
        ("fields[0]",  "品目名"),
        ("fields[1]",  "在庫品目コード"),
        ("fields[2]",  "購入先"),
        ("fields[3]",  "購入単価"),
        ("fields[4]",  "何個入り"),
        ("fields[5]",  "単位"),
        ("fields[6]",  "課税対象"),
        ("fields[7]",  "ドル単価"),
        ("fields[8]",  "ドル円"),
        ("fields[9]",  "出金区分"),
        ("query", "order by 作成日時 desc limit 300"),
    ]
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params, headers=_get_headers(TOKEN_794))
        resp.raise_for_status()
        records = resp.json()["records"]

    seen: set[str] = set()
    suggestions = []
    for r in records:
        name = r["品目名"]["value"]
        if name and name not in seen:
            seen.add(name)
            suggestions.append({
                "品目名":        name,
                "在庫品目コード": r["在庫品目コード"]["value"],
                "購入先":        r["購入先"]["value"],
                "購入単価":      r["購入単価"]["value"] or "0",
                "何個入り":      r["何個入り"]["value"] or "1",
                "単位":          r["単位"]["value"],
                "課税対象":      r["課税対象"]["value"],
                "ドル単価":      r["ドル単価"]["value"] or "0",
                "ドル円":        r["ドル円"]["value"] or "160",
                "出金区分":      r["出金区分"]["value"],
            })
    return suggestions


async def create_purchase_record(data: dict[str, Any]) -> dict:
    """App 794 に購入レコードを登録する"""
    url = f"{_base()}/record.json"
    record: dict[str, Any] = {
        "日付":           {"value": data["日付"]},
        "班":             {"value": data["班"]},
        "出金区分":       {"value": data["出金区分"]},
        "品目名":         {"value": data["品目名"]},
        "在庫品目コード": {"value": data.get("在庫品目コード", "")},
        "購入先":         {"value": data.get("購入先", "")},
        "課税対象":       {"value": data.get("課税対象", "国内")},
        "購入数量":       {"value": str(data.get("購入数量", ""))},
        "何個入り":       {"value": str(data.get("何個入り", 1))},
        "単位":           {"value": data.get("単位", "式")},
        "購入単価":       {"value": str(data.get("購入単価", ""))},
        "備考":           {"value": data.get("備考", "")},
    }
    if data.get("課税対象") == "海外｜非課税":
        record["ドル単価"] = {"value": str(data.get("ドル単価", ""))}
        record["ドル円"]   = {"value": str(data.get("ドル円", "160"))}

    payload = {"app": APP_PURCHASE, "record": record}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=payload, headers=_post_headers(TOKEN_794))
        resp.raise_for_status()
        return resp.json()


async def get_purchase_record(record_id: str) -> dict:
    """App 794 の購入レコードを1件取得（フォームコピー用）"""
    url = f"{_base()}/record.json"
    params = [("app", APP_PURCHASE), ("id", record_id)]
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params=params, headers=_get_headers(TOKEN_794))
        resp.raise_for_status()
        r = resp.json()["record"]
    return {
        "品目名":        r["品目名"]["value"],
        "在庫品目コード": r["在庫品目コード"]["value"],
        "班":            r["班"]["value"],
        "出金区分":      r["出金区分"]["value"],
        "購入先":        r["購入先"]["value"],
        "課税対象":      r["課税対象"]["value"],
        "購入数量":      r["購入数量"]["value"] or "0",
        "何個入り":      r["何個入り"]["value"] or "1",
        "単位":          r["単位"]["value"],
        "購入単価":      r["購入単価"]["value"] or "0",
        "ドル単価":      r["ドル単価"]["value"] or "0",
        "ドル円":        r["ドル円"]["value"] or "160",
        "備考":          r["備考"]["value"],
    }


async def get_recent_purchases(ym: str, limit: int = 30) -> list[dict]:
    """App 794 の直近購入履歴を取得する"""
    url = f"{_base()}/records.json"
    # ym は "YYYY/MM" 形式
    year, month = ym.split("/")
    query = (
        f'日付 >= "{year}-{month}-01" and 日付 <= "{year}-{month}-31" '
        f'order by 日付 desc limit {limit}'
    )
    params = [
        ("app", APP_PURCHASE),
        ("fields[0]", "レコード番号"),
        ("fields[1]", "日付"),
        ("fields[2]", "班"),
        ("fields[3]", "品目名"),
        ("fields[4]", "出金区分"),
        ("fields[5]", "購入数量"),
        ("fields[6]", "何個入り"),
        ("fields[7]", "税込み額"),
        ("fields[8]", "在庫反映状況"),
        ("query", query),
    ]
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params=params, headers=_get_headers(TOKEN_794))
        resp.raise_for_status()
        records = resp.json()["records"]

    return [
        {
            "レコード番号":   r["レコード番号"]["value"],
            "日付":          r["日付"]["value"],
            "班":            r["班"]["value"],
            "品目名":        r["品目名"]["value"],
            "出金区分":      r["出金区分"]["value"],
            "購入数量":      r["購入数量"]["value"],
            "何個入り":      r["何個入り"]["value"],
            "税込み額":      r["税込み額"]["value"],
            "在庫反映状況":  r["在庫反映状況"]["value"],
        }
        for r in records
    ]

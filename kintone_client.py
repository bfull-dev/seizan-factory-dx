"""Kintone REST API クライアント"""
import os
import httpx
from typing import Any

DOMAIN = os.getenv("KINTONE_DOMAIN", "exk1223hafrf.cybozu.com")
TOKEN_791 = os.getenv("KINTONE_TOKEN_791", "")
TOKEN_792 = os.getenv("KINTONE_TOKEN_792", "")

APP_INVENTORY = 791
APP_EXPENSE   = 792


def _get_headers(token: str) -> dict:
    return {"X-Cybozu-API-Token": token}

def _post_headers(token: str) -> dict:
    return {"X-Cybozu-API-Token": token, "Content-Type": "application/json"}


def _base() -> str:
    return f"https://{DOMAIN}/k/v1"


async def get_inventory_items() -> list[dict]:
    """App 791 の全在庫アイテムを取得してサジェスト用に返す"""
    url = f"{_base()}/records.json"
    # httpx では list of tuples で同名パラメータを複数渡す
    params = [
        ("app", APP_INVENTORY),
        ("fields[0]", "品目コード"),
        ("fields[1]", "品目名"),
        ("fields[2]", "区分"),
        ("fields[3]", "最新単価"),
        ("fields[4]", "単位"),
        ("fields[5]", "班別"),
        ("query", "limit 500"),
    ]
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params=params, headers=_get_headers(TOKEN_791))
        resp.raise_for_status()
        records = resp.json()["records"]

    return [
        {
            "品目コード": r["品目コード"]["value"],
            "品目名":    r["品目名"]["value"],
            "区分":      r["区分"]["value"],
            "単価":      r["最新単価"]["value"] or "0",
            "単位":      r["単位"]["value"],
            "班別":      r["班別"]["value"],
        }
        for r in records
    ]


async def create_usage_record(data: dict[str, Any]) -> dict:
    """App 792 に使用材料・消耗品レコードを登録する"""
    url = f"{_base()}/record.json"
    payload = {
        "app": APP_EXPENSE,
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
        return resp.json()


async def get_recent_usage(ym: str, limit: int = 20) -> list[dict]:
    """直近の使用材料入力履歴を取得する"""
    url = f"{_base()}/records.json"
    params = [
        ("app", APP_EXPENSE),
        ("fields[0]", "レコード番号"),
        ("fields[1]", "入力日"),
        ("fields[2]", "班別"),
        ("fields[3]", "品目名"),
        ("fields[4]", "数量"),
        ("fields[5]", "金額"),
        ("query", f'入力種別 = "使用材料・消耗品" and 対象年月 = "{ym}" order by 作成日時 desc limit {limit}'),
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
            "単位":    "",  # 単位はApp792に持たないため省略
            "金額":    r["金額"]["value"],
        }
        for r in records
    ]

"""Kintone REST API クライアント"""
import asyncio
import os
import io
import base64
import json
import httpx
from typing import Any
from dotenv import load_dotenv
load_dotenv()

DOMAIN    = os.getenv("KINTONE_DOMAIN", "exk1223hafrf.cybozu.com")
TOKEN_791 = os.getenv("KINTONE_TOKEN_791", "")
TOKEN_792 = os.getenv("KINTONE_TOKEN_792", "")
TOKEN_793 = os.getenv("KINTONE_TOKEN_793", "")
TOKEN_794 = os.getenv("KINTONE_TOKEN_794", "")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

APP_INVENTORY = 791
APP_USAGE     = 792   # 使用材料・消耗品入力（Webフォーム書き込み先）
APP_SUMMARY   = 793   # 月別サマリー
APP_PURCHASE  = 794   # 出金管理（購入記録）

# 在庫同期の同時実行を防ぐロック（二重計上防止）
_sync_lock = asyncio.Lock()


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
        ("fields[0]", "品目名"),
        ("fields[1]", "区分"),
        ("fields[2]", "移動平均単価"),
        ("fields[3]", "単位"),
        ("fields[4]", "班別"),
        ("fields[5]", "現在庫数"),
        ("query", "limit 500"),
    ]
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params=params, headers=_get_headers(TOKEN_791))
        resp.raise_for_status()
        records = resp.json()["records"]

    # 品目名で重複除去（在庫数が多いレコードを優先）
    seen: dict[str, dict] = {}
    for r in records:
        name = r["品目名"]["value"]
        item = {
            "品目名":    name,
            "区分":      r["区分"]["value"],
            "単価":      r["移動平均単価"]["value"] or "0",
            "単位":      r["単位"]["value"],
            "班別":      r["班別"]["value"],
            "現在庫数":  r["現在庫数"]["value"] or "0",
        }
        if name not in seen or float(item["現在庫数"]) > float(seen[name]["現在庫数"]):
            seen[name] = item
    return list(seen.values())


async def _get_inventory_by_name(品目名: str) -> dict | None:
    """品目名で App 791 レコードを1件取得"""
    url = f"{_base()}/records.json"
    params = [
        ("app", APP_INVENTORY),
        ("query", f'品目名 = "{品目名}" order by 現在庫数 desc limit 1'),
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


async def decrease_inventory(品目名: str, 数量: float) -> bool:
    """消費登録時に App 791 の在庫数を減算する"""
    inv = await _get_inventory_by_name(品目名)
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

    return {**result, "inventory_decreased": False, "inventory_error": None}


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
_AUTO_CREATE_区分 = {"樹脂", "変動費（製造用）", "製造用消耗品"}


async def _create_inventory_record(
    品目名: str, 区分: str, 班別: str, 単価: float, 単位: str = ""
) -> None:
    """App 791 に在庫マスタレコードを新規作成する"""
    payload = {
        "app": APP_INVENTORY,
        "record": {
            "品目コード":    {"value": 品目名},   # 必須フィールド（品目名と同値で運用）
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
    print(f"[INFO] App791 新規作成: 品目名='{品目名}' 区分='{区分}'")


async def sync_purchases_to_inventory() -> dict:
    """
    App 794 の未処理購入レコードを App 791 に反映（在庫数加算・移動平均単価更新）する。
    出金区分が対象区分（樹脂/変動費（製造用）/製造用消耗品/外注費）の場合、
    App 791 に品目名が存在しなければ自動作成する。
    処理済レコードには在庫反映状況='反映済'をセット。
    _sync_lock により同時実行を防止（二重計上防止）。
    """
    if _sync_lock.locked():
        print("[INFO] sync_purchases_to_inventory: 既に実行中のため本回はスキップ")
        return {"processed": 0, "created": 0, "total": 0, "errors": ["同期処理が既に実行中のためスキップしました"]}
    async with _sync_lock:
        return await _sync_purchases_to_inventory_inner()


async def _sync_purchases_to_inventory_inner() -> dict:
    url = f"{_base()}/records.json"
    params = [
        ("app", APP_PURCHASE),
        ("query", (
            '品目名 != "" and 在庫反映状況 not in ("反映済") '
            'and 出金区分 in ("樹脂", "変動費（製造用）", "製造用消耗品", "外注費", "製造用備品") '
            'order by 日付 asc limit 100'
        )),
        ("fields[0]", "レコード番号"),
        ("fields[1]", "購入数"),
        ("fields[2]", "単位価格_税抜"),
        ("fields[3]", "金額"),
        ("fields[4]", "日付"),
        ("fields[5]", "出金区分"),
        ("fields[6]", "品目名"),
        ("fields[7]", "班"),
        ("fields[8]", "単位"),
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
        品目名     = r["品目名"]["value"]
        購入数     = float(r["購入数"]["value"] or 0)
        単価       = float(r["単位価格_税抜"]["value"] or 0)
        金額       = float(r["金額"]["value"] or 0)
        日付       = r["日付"]["value"]
        出金区分   = r["出金区分"]["value"]
        班別       = r["班"]["value"]
        単位       = r["単位"]["value"]

        if 購入数 <= 0:
            # 数量なし → 処理済にして次へ
            await _mark_purchase_processed(record_id)
            continue

        try:
            inv = await _get_inventory_by_name(品目名)

            # App791 に存在しない場合、対象区分なら自動作成
            if not inv:
                if 出金区分 in _AUTO_CREATE_区分:
                    await _create_inventory_record(品目名, 出金区分, 班別, 単価, 単位)
                    created += 1
                    # Kintone 書き込み反映待ち（整合性遅延対策）
                    for _ in range(3):
                        await asyncio.sleep(1)
                        inv = await _get_inventory_by_name(品目名)
                        if inv:
                            break
                    if not inv:
                        errors.append(f"品目名 '{品目名}' の自動作成後に取得できませんでした")
                        continue
                else:
                    errors.append(
                        f"品目名 '{品目名}' がApp791に存在せず、出金区分 '{出金区分}' は自動作成対象外です"
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
            errors.append(f"品目名 '{品目名}': {e}")

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
        ("fields[0]", "品目名"),
        ("fields[1]", "購入先"),
        ("fields[2]", "購入単価"),
        ("fields[3]", "何個入り"),
        ("fields[4]", "単位"),
        ("fields[5]", "課税対象"),
        ("fields[6]", "ドル単価"),
        ("fields[7]", "ドル円"),
        ("fields[8]", "出金区分"),
        ("fields[9]", "購入数量"),
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
                "品目名":   name,
                "購入先":   r["購入先"]["value"],
                "購入単価": r["購入単価"]["value"] or "0",
                "何個入り": r["何個入り"]["value"] or "1",
                "単位":     r["単位"]["value"],
                "課税対象": r["課税対象"]["value"],
                "ドル単価": r["ドル単価"]["value"] or "0",
                "ドル円":   r["ドル円"]["value"] or "160",
                "出金区分": r["出金区分"]["value"],
                "購入数量": r["購入数量"]["value"] or "0",
            })
    return suggestions


async def create_purchase_record(data: dict[str, Any]) -> dict:
    """App 794 に購入レコードを登録する"""
    url = f"{_base()}/record.json"
    # 対象年月を日付から自動計算 (YYYY/MM)
    date_val = data.get("日付", "")
    対象年月 = date_val[:7].replace("-", "/") if date_val else ""
    record: dict[str, Any] = {
        "日付":           {"value": date_val},
        "対象年月":       {"value": 対象年月},
        "班":             {"value": data["班"]},
        "出金区分":       {"value": data["出金区分"]},
        "品目名":  {"value": data["品目名"]},
        "購入先":  {"value": data.get("購入先", "")},
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
        "品目名": r["品目名"]["value"],
        "班":     r["班"]["value"],
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


_PURCHASE_PROMPT = """\
この書類は日本の工場向け購入書類（請求書・見積書・納品書・メールなど）です。
以下のJSON形式で品目情報を抽出してください。

{
  "vendor": "購入先企業名（不明なら空文字）",
  "date": "YYYY-MM-DD形式の請求日または納品日（不明なら空文字）",
  "currency": "JPY または USD",
  "exchange_rate": 0,
  "items": [
    {
      "品目名": "品目の名称",
      "購入数量": 数値,
      "何個入り": 1,
      "単位": "個/枚/kg/L/式 など",
      "購入単価": 税抜き単価（数値、不明なら0）,
      "ドル単価": ドル建て単価（USD請求書のみ、それ以外は0）,
      "課税対象": "国内" または "海外｜非課税",
      "出金区分": "推測できる場合のみ。不明なら空文字",
      "備考": "備考事項があれば記載"
    }
  ]
}

注意事項：
- 税抜き単価を優先。税込み表記のみなら÷1.1で換算し備考に「税込換算」と記載
- 海外・英語請求書ならcurrency=USD、課税対象=海外｜非課税
- USD請求書の場合はexchange_rateに記載のレートを入れる（不明なら0）
- 複数品目はすべて列挙すること
- 必ずJSONのみを返すこと（余分な説明文不要）
"""

_USAGE_PROMPT = """\
この書類は日本の工場で使用する材料・消耗品の使用記録・資料です。
以下のJSON形式で使用品目を抽出してください。

{
  "items": [
    {
      "品目名": "品目名称",
      "数量": 数値（不明なら0）,
      "単位": "個/kg/L など",
      "備考": "備考事項があれば"
    }
  ]
}

必ずJSONのみを返すこと。
"""


async def analyze_with_gemini(content: bytes, filename: str, analysis_type: str) -> dict:
    """Gemini APIでファイルを解析し、品目リストとヘッダー情報を返す"""
    fname = filename.lower()

    # Excel → テキスト変換してテキストとして送信
    if fname.endswith((".xlsx", ".xls")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
        lines: list[str] = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                line = "\t".join(str(c) if c is not None else "" for c in row)
                if line.strip():
                    lines.append(line)
        text_content = "\n".join(lines)
        prompt = (_PURCHASE_PROMPT if analysis_type == "purchase" else _USAGE_PROMPT)
        parts = [{"text": prompt + "\n\n---以下が書類内容---\n" + text_content}]
    else:
        # PDF / 画像 / テキスト → base64でGeminiへ
        b64 = base64.b64encode(content).decode()
        if fname.endswith(".pdf"):
            mime = "application/pdf"
        elif fname.endswith(".png"):
            mime = "image/png"
        elif fname.endswith((".jpg", ".jpeg")):
            mime = "image/jpeg"
        elif fname.endswith(".webp"):
            mime = "image/webp"
        elif fname.endswith(".txt"):
            mime = "text/plain"
        else:
            mime = "application/octet-stream"

        prompt = (_PURCHASE_PROMPT if analysis_type == "purchase" else _USAGE_PROMPT)
        if mime == "text/plain":
            parts = [{"text": prompt + "\n\n---以下が書類内容---\n" + content.decode("utf-8", errors="replace")}]
        else:
            parts = [
                {"text": prompt},
                {"inline_data": {"mime_type": mime, "data": b64}},
            ]

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"response_mime_type": "application/json"},
    }
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()

    raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(raw)


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


# ─── 月別サマリー（App 793）──────────────────────────────────

async def get_monthly_summary(ym: str) -> dict | None:
    """App 793 から対象年月のサマリーレコードを取得"""
    if not TOKEN_793:
        return None
    url = f"{_base()}/records.json"
    params = [("app", APP_SUMMARY), ("query", f'対象年月 = "{ym}" limit 1')]
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params=params, headers=_get_headers(TOKEN_793))
        resp.raise_for_status()
        recs = resp.json()["records"]
    if not recs:
        return None
    r = recs[0]
    def _v(field: str) -> str:
        return r.get(field, {}).get("value", "") or "0"
    return {
        "対象年月":         r["対象年月"]["value"],
        "出荷売上_税抜":    _v("出荷売上_税抜"),
        "OEM売上_税抜":     _v("OEM売上_税抜"),
        "総売上_税抜":      _v("総売上_税抜"),
        "製造原価_樹脂":    _v("製造原価_樹脂"),
        "製造原価_変動費":  _v("製造原価_変動費"),
        "製造用備品":       _v("製造用備品"),
        "製造用備品_税抜":  _v("製造用備品_税抜"),
        "外注費":           _v("外注費"),
        "外注費_税抜":      _v("外注費_税抜"),
        "製造用消耗品":     _v("製造用消耗品"),
        "製造用消耗品_税抜": _v("製造用消耗品_税抜"),
        "製造原価合計":     _v("製造原価合計"),
        "固定費":           _v("固定費"),
        "人件費":           _v("人件費"),
        "水道光熱費":       _v("水道光熱費"),
        "人材派遣費":       _v("人材派遣費"),
        "福利厚生":         _v("福利厚生"),
        "開発_実験":        _v("開発_実験"),
        "事務用品":         _v("事務用品"),
        "輸送費_送料":      _v("輸送費_送料"),
        "その他費用":       _v("その他費用"),
        "全費用合計":       _v("全費用合計"),
        "粗利":             _v("粗利"),
        "粗利率":           _v("粗利率"),
        "設備投資":         _v("設備投資"),
        "期末在庫評価額":   _v("期末在庫評価額"),
    }


# 変動費として扱う用途区分（App 792）
_変動費区分 = {"変動費（製造用）", "量産用材料", "量産用消耗品"}


async def get_manufacturing_cost_details(ym: str) -> dict:
    """
    App 792（使用材料: 樹脂・変動費）と App 794（出金管理: 製造用備品・外注費・製造用消耗品）
    から対象月の明細を取得し、カテゴリ別に返す。
    """
    params_792 = [
        ("app", APP_USAGE),
        ("query", f'入力種別 in ("使用材料・消耗品") and 対象年月 = "{ym}" order by 入力日 asc limit 500'),
        ("fields[0]", "入力日"),
        ("fields[1]", "班別"),
        ("fields[2]", "品目名"),
        ("fields[3]", "用途区分"),
        ("fields[4]", "数量"),
        ("fields[5]", "単価"),
        ("fields[6]", "金額"),
    ]
    params_794 = [
        ("app", APP_PURCHASE),
        ("query", f'対象年月 = "{ym}" and 出金区分 in ("製造用備品", "外注費", "製造用消耗品") order by 日付 asc limit 500'),
        ("fields[0]", "日付"),
        ("fields[1]", "班"),
        ("fields[2]", "品目名"),
        ("fields[3]", "出金区分"),
        ("fields[4]", "購入数量"),
        ("fields[5]", "税込み額"),
        ("fields[6]", "金額"),        # 税抜き額
        ("fields[7]", "課税対象"),
    ]

    async with httpx.AsyncClient(timeout=15) as client:
        r792_task = client.get(f"{_base()}/records.json", params=params_792, headers=_get_headers(TOKEN_792))
        r794_task = client.get(f"{_base()}/records.json", params=params_794, headers=_get_headers(TOKEN_794))
        r792, r794 = await asyncio.gather(r792_task, r794_task)
        r792.raise_for_status()
        r794.raise_for_status()

    result: dict[str, list] = {
        "樹脂": [], "変動費": [], "製造用備品": [], "外注費": [], "製造用消耗品": []
    }

    for r in r792.json()["records"]:
        cat = r["用途区分"]["value"]
        item = {
            "日付": r["入力日"]["value"],
            "班":   r["班別"]["value"],
            "品目名": r["品目名"]["value"],
            "数量": r["数量"]["value"] or "0",
            "単価": r["単価"]["value"] or "0",
            "金額": r["金額"]["value"] or "0",
        }
        if cat == "樹脂":
            result["樹脂"].append(item)
        elif cat in _変動費区分:
            result["変動費"].append(item)

    for r in r794.json()["records"]:
        cat = r["出金区分"]["value"]
        item = {
            "日付":     r["日付"]["value"],
            "班":       r["班"]["value"],
            "品目名":   r["品目名"]["value"],
            "数量":     r["購入数量"]["value"] or "0",
            "単価":     "",
            "金額_税抜": r["金額"]["value"] or "0",       # 税抜き（購入数量×購入単価）
            "金額":     r["税込み額"]["value"] or "0",    # 税込み（課税区分考慮済み）
            "課税対象": r["課税対象"]["value"],
        }
        if cat in result:
            result[cat].append(item)

    return result

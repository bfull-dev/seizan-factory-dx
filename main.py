"""千秋工場・赤見工場 使用材料入力Webアプリ"""
import asyncio
import subprocess
import sys
from datetime import date

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from typing import List

from dotenv import load_dotenv
load_dotenv()

import kintone_client as kc

app = FastAPI(title="千秋工場・赤見工場 使用材料入力")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://exk1223hafrf.cybozu.com"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="templates")


# ─── HTML ページ ───────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    today = date.today()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "current_year":  today.year,
        "current_month": today.month,
        "current_date":  today.isoformat(),
    })


# ─── API: 在庫サジェスト ────────────────────────────────────
@app.get("/api/inventory")
async def inventory():
    """在庫リスト（App 791）を返す — フロントエンドのオートコンプリート用"""
    try:
        items = await kc.get_inventory_items()
        return {"items": items}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── API: 使用材料登録 ──────────────────────────────────────
class UsageIn(BaseModel):
    対象年月:  str = Field(..., pattern=r"^\d{4}/\d{2}$")
    入力日:    str
    班別:      str
    品目名:    str
    用途区分:  str = ""
    数量:      float = Field(ge=0)
    単価:      float = Field(ge=0)
    金額:      float = Field(ge=0)
    備考:      str = ""


@app.post("/api/usage")
async def create_usage(body: UsageIn):
    try:
        result = await kc.create_usage_record(body.model_dump())
        return {
            "id": result.get("id"),
            "revision": result.get("revision"),
            "inventory_decreased": result.get("inventory_decreased", False),
            "inventory_error": result.get("inventory_error"),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── API: 入力履歴 ──────────────────────────────────────────
@app.get("/api/history")
async def history(ym: str):
    try:
        records = await kc.get_recent_usage(ym)
        return {"records": records}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── API: 購入サジェスト ────────────────────────────────────
@app.get("/api/purchase-suggestions")
async def purchase_suggestions():
    """App 794 の過去購入レコードをサジェスト用に返す"""
    try:
        items = await kc.get_purchase_suggestions()
        return {"items": items}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── API: 購入レコード登録 ──────────────────────────────────
class PurchaseIn(BaseModel):
    日付:     str
    班:       str
    出金区分: str
    品目名:   str
    購入先:   str = ""
    課税対象:       str = "国内"
    購入数量:       float = Field(ge=0)
    何個入り:       float = Field(ge=1, default=1)
    単位:           str = "式"
    購入単価:       float = Field(ge=0)
    ドル単価:       float = 0
    ドル円:         float = 160
    備考:           str = ""
    暫定:           bool = False


class PurchaseUpdateIn(BaseModel):
    購入単価:   float = Field(ge=0)
    購入数量:   float = Field(ge=0)
    何個入り:   float = Field(ge=1, default=1)
    ドル単価:   float = 0
    ドル円:     float = 160
    備考:       str = ""
    暫定:       bool = False
    対象年月:   str = ""  # サマリー自動再計算用


@app.post("/api/purchase")
async def create_purchase(body: PurchaseIn, background_tasks: BackgroundTasks):
    try:
        _SYNC_区分 = {"樹脂", "変動費（製造用）", "製造用消耗品"}
        result = await kc.create_purchase_record(body.model_dump())
        if body.出金区分 in _SYNC_区分:
            async def _sync():
                try:
                    await kc.sync_purchases_to_inventory()
                except Exception as e:
                    print(f"[purchase-sync ERROR] {e}")
            background_tasks.add_task(_sync)
        # サマリー自動再計算（暫定・確定問わず）
        ym = body.日付[:7]  # "YYYY-MM-DD" → "YYYY-MM"
        def _run_summary(target_ym: str):
            try:
                subprocess.run(
                    [sys.executable, "scripts/update_monthly_summary.py", target_ym],
                    capture_output=True, text=True, timeout=120
                )
            except Exception as e:
                print(f"[purchase-create-summary ERROR] {e}")
        background_tasks.add_task(_run_summary, ym)
        return {"id": result.get("id"), "revision": result.get("revision")}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── API: 購入履歴 ──────────────────────────────────────────
@app.get("/api/purchase-history")
async def purchase_history(ym: str):
    try:
        records = await kc.get_recent_purchases(ym)
        has_provisional = any(r.get("暫定") for r in records)
        return {"records": records, "has_provisional": has_provisional}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── API: 使用材料レコード1件取得 ──────────────────────────
@app.get("/api/usage/{record_id}")
async def get_usage(record_id: str):
    try:
        data = await kc.get_usage_record(record_id)
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── API: 購入レコード更新（暫定→確定） ────────────────────
@app.put("/api/purchase/{record_id}")
async def update_purchase(record_id: str, body: PurchaseUpdateIn, background_tasks: BackgroundTasks):
    try:
        await kc.update_purchase_record(record_id, body.model_dump())
        if body.対象年月:
            def _run():
                subprocess.run(
                    [sys.executable, "scripts/update_monthly_summary.py", body.対象年月],
                    capture_output=True, text=True, timeout=120
                )
            background_tasks.add_task(_run)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── API: 購入レコード1件取得 ───────────────────────────────
@app.get("/api/purchase/{record_id}")
async def get_purchase(record_id: str):
    try:
        data = await kc.get_purchase_record(record_id)
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── API: 購入→在庫同期 ────────────────────────────────────
@app.post("/api/sync-inventory")
async def sync_inventory(background_tasks: BackgroundTasks):
    """
    App794 未処理購入レコードを App791 在庫に反映する。
    Kintoneウェブフック・手動どちらからでも呼び出し可。
    即座に 200 OK を返し、処理はバックグラウンドで実行する。
    """
    async def _run():
        try:
            await kc.sync_purchases_to_inventory()
        except Exception as e:
            print(f"[sync-inventory ERROR] {e}")

    background_tasks.add_task(_run)
    return {"ok": True, "message": "在庫同期を開始しました（バックグラウンド処理）"}


# ─── API: 購入レコード更新時サマリー自動再計算（Kintone Webhook用）──
@app.post("/api/sync-summary-on-update")
async def sync_summary_on_update(request: Request, background_tasks: BackgroundTasks):
    """
    App794 レコード編集時にKintoneウェブフックから呼び出される。
    ペイロードの日付フィールドから対象年月を取得し、月別サマリーを再計算する。
    即座に 200 OK を返し、処理はバックグラウンドで実行する。
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    # Kintoneウェブフックペイロードから日付を取得
    ym: str | None = None
    try:
        date_val = body.get("record", {}).get("日付", {}).get("value", "")
        if date_val and len(date_val) >= 7:
            ym = date_val[:7]  # "YYYY-MM-DD" → "YYYY-MM"
    except Exception:
        pass

    if not ym:
        # 日付が取れない場合は今月で実行
        from datetime import date
        ym = date.today().strftime("%Y-%m")

    def _run_summary(target_ym: str):
        try:
            result = subprocess.run(
                [sys.executable, "scripts/update_monthly_summary.py", target_ym],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                print(f"[sync-summary-on-update ERROR] {result.stderr or result.stdout}")
            else:
                print(f"[sync-summary-on-update] {target_ym} 完了")
        except Exception as e:
            print(f"[sync-summary-on-update ERROR] {e}")

    background_tasks.add_task(_run_summary, ym)
    return {"ok": True, "ym": ym, "message": f"{ym} のサマリー再計算を開始しました"}


# ─── API: AI書類解析 ────────────────────────────────────────
@app.post("/api/analyze-document")
async def analyze_document(
    file: UploadFile = File(...),
    doc_type: str = Form(default="purchase"),  # "purchase" or "usage"
):
    """請求書・納品書等をGemini AIで解析し、品目リストを返す"""
    try:
        content = await file.read()
        result = await kc.analyze_with_gemini(content, file.filename or "", doc_type)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── API: 購入レコード一括登録 ──────────────────────────────
class PurchaseBulkIn(BaseModel):
    header: dict  # 日付, 班, 購入先, 購入者
    rows: List[PurchaseIn]


@app.post("/api/purchases-bulk")
async def create_purchases_bulk(body: PurchaseBulkIn, background_tasks: BackgroundTasks):
    """複数の購入レコードを一括登録する"""
    try:
        import traceback as _tb
        results = []
        for row in body.rows:
            merged = {**body.header, **row.model_dump()}
            result = await kc.create_purchase_record(merged)
            results.append({"id": result.get("id")})
        _SYNC_区分 = {"樹脂", "変動費（製造用）", "製造用消耗品"}
        if any(r.出金区分 in _SYNC_区分 for r in body.rows):
            async def _sync():
                try:
                    await kc.sync_purchases_to_inventory()
                except Exception as e:
                    print(f"[bulk-sync ERROR] {e}")
            background_tasks.add_task(_sync)
        # サマリー自動再計算（暫定・確定問わず）
        ym = body.header.get("日付", "")[:7]
        if not ym and body.rows:
            ym = body.rows[0].日付[:7]
        if ym:
            def _run_summary(target_ym: str):
                try:
                    subprocess.run(
                        [sys.executable, "scripts/update_monthly_summary.py", target_ym],
                        capture_output=True, text=True, timeout=120
                    )
                except Exception as e:
                    print(f"[bulk-create-summary ERROR] {e}")
            background_tasks.add_task(_run_summary, ym)
        return {"count": len(results), "results": results}
    except Exception as e:
        print(f"[purchases-bulk ERROR] {e}")
        import traceback as _tb; _tb.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# ─── API: 使用材料レコード一括登録 ──────────────────────────
class UsageBulkIn(BaseModel):
    header: dict  # 対象年月, 入力日, 班別
    rows: List[UsageIn]


@app.post("/api/usages-bulk")
async def create_usages_bulk(body: UsageBulkIn):
    """複数の使用材料レコードを一括登録し、在庫を減算する"""
    try:
        results = []
        for row in body.rows:
            merged = {**body.header, **row.model_dump()}
            result = await kc.create_usage_record(merged)
            results.append({"ok": True, "id": result.get("id")})
        return {"count": len(results), "results": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── API: 月別サマリー取得 ──────────────────────────────────
@app.get("/api/summary")
async def get_summary(ym: str):
    """App 793 の月別サマリーを取得"""
    try:
        summary = await kc.get_monthly_summary(ym)
        return {"summary": summary}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/summary/details")
async def get_summary_details(ym: str):
    """製造原価内訳の明細を取得（App 792 使用材料 + App 794 出金管理）"""
    try:
        details = await kc.get_manufacturing_cost_details(ym)
        return {"details": details}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── API: 月別サマリー即時集計 ──────────────────────────────
@app.post("/api/run-summary")
async def run_summary(ym: str):
    def _run():
        result = subprocess.run(
            [sys.executable, "scripts/update_monthly_summary.py", ym],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            raise Exception(result.stderr or result.stdout)
        return result.stdout
    try:
        output = await asyncio.to_thread(_run)
        return {"ok": True, "ym": ym, "output": output}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

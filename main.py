"""千秋工場・赤見工場 使用材料入力Webアプリ"""
import asyncio
import subprocess
import sys
from datetime import date

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

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
    品目コード: str = ""
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
        return {"id": result.get("id"), "revision": result.get("revision")}
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
    日付:           str
    班:             str
    出金区分:       str
    品目名:         str
    在庫品目コード: str = ""
    購入先:         str = ""
    課税対象:       str = "国内"
    購入数量:       float = Field(ge=0)
    何個入り:       float = Field(ge=1, default=1)
    単位:           str = "式"
    購入単価:       float = Field(ge=0)
    ドル単価:       float = 0
    ドル円:         float = 160
    備考:           str = ""


@app.post("/api/purchase")
async def create_purchase(body: PurchaseIn, background_tasks: BackgroundTasks):
    try:
        result = await kc.create_purchase_record(body.model_dump())
        if body.在庫品目コード:
            async def _sync():
                try:
                    await kc.sync_purchases_to_inventory()
                except Exception as e:
                    print(f"[purchase-sync ERROR] {e}")
            background_tasks.add_task(_sync)
        return {"id": result.get("id"), "revision": result.get("revision")}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── API: 購入履歴 ──────────────────────────────────────────
@app.get("/api/purchase-history")
async def purchase_history(ym: str):
    try:
        records = await kc.get_recent_purchases(ym)
        return {"records": records}
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

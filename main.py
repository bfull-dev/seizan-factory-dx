"""生産工場DX 使用材料入力Webアプリ"""
import asyncio
import subprocess
import sys
from datetime import date

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from dotenv import load_dotenv
load_dotenv()

import kintone_client as kc

app = FastAPI(title="生産工場DX 使用材料入力")

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

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CRM 客服工作台的**渲染自檢** —— 用真實後端 payload 去跑前端真正的渲染函式。

## 為什麼要這支（以及它跟根 `tests/ui_check.py` 的關係）

根 `tests/ui_check.py`（1410 行）驗的是**舊單體**的知識庫 console：它的 `arState`、
`renderSettingsTab()`、`#kbNewOverlay` 在 CRM 版 `crm/ui/console_ui.html` 裡 **一個都沒有**
（CRM 版是階段四的客服工作台：會話 / 自動回覆 / 命中測試 / 回覆紀錄）。
所以這不是「把路徑改一改」的移植，而是**為 CRM 版另寫一份**。兩支要並存：

* 舊單體 console（8001）仍然在服務知識庫管理 → 根那支照跑，不要刪。
* CRM console（8020）→ 這支。

共同的手法沿用根版：這台機器**不允許子進程建立監聽 socket**（chromedriver /
playwright 都起不來），所以用「Node + 最小 DOM 樁」把 `console_ui.html` 的 `<script>`
整段載進來，把**從真的後端拿回來的 payload** 塞進 `state`，逐一呼叫渲染函式，
再檢查產出的 HTML。

## 為什麼能把 `state` 塞進去

CRM 版把整份 script 包在 IIFE 裡（`(function(){ 'use strict'; … })()`），外面摸不到
`state`。這裡在載入前把外層 IIFE **拆掉**（不改檔案本身），`state` 與渲染函式就變成
頂層可見 —— 這是唯一不污染 production HTML 的做法。

## 真 vs 合成

* **真的**（直接呼叫 CRM 的端點函式 / repo，走真 DB）：`overview`、`faqs`、`tables`、
  `collections`、`post-config`（未配置 + 已配置兩份）、`logs`、`conversation_logs`、
  命中測試結果（用 FAQ 原句命中 → **0 次模型呼叫**）。
* **合成**（來自 Connector，這支不啟 Connector）：會話清單 `convs`、訊息串 `thread`、
  附件訊息。欄位形狀照 `connector/app/internal/router.py` 的 `/conversations`、
  `/conversation` 實際回傳抄，並比對 `crm/app/agent/router.py` 合併後補的
  `bp_account_id` / `account_name`。

跑法（從專案根，cwd 隨意）：
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \\
      <底層 python> crm/tests/console_render_check.py

可重複執行：資料一律掛在 `__CK__` 自檢商家底下，跑完 `purge_business()` 清乾淨。

⚠️ 沒被覆蓋的：瀏覽器**真的把 HTML 畫出來**、CSS 版型、真實事件（點擊 / 輸入）。
   那些要人工開一次頁面看。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time

from _ck import check, report, section, ensure_business, purge_business, sql  # noqa: E402

from app.agent import console_router, repo           # noqa: E402
from app.autoreply import autoreply_db               # noqa: E402
from app.rag import kb_db                            # noqa: E402

UI_HTML = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "ui", "console_ui.html")
NODE = os.environ.get(
    "UI_NODE",
    r"node")

MARK = "__CK__"
BIZ = ensure_business("前端渲染自檢", "console_render_check 專用")
STAMP = str(int(time.time()))
KEY = f"ck-render-{STAMP}"           # 會話 key（＝end_user_sender_id）
IMG_MD = "![A 款耳機](/media/ab12cd34ef56ab78.png)"   # 回覆裡的 markdown 圖
POST_NEW = f"ck_post_new_{STAMP}"    # 從沒配置過的貼文
POST_SET = f"ck_post_set_{STAMP}"    # 會配置過的貼文
FAQ_Q = f"{MARK}渲染自檢問句{STAMP}"
FAQ_A = "渲染自檢答案：A 款耳機 1280 元。"

# ★ 端點函式直接呼叫用的假 session（等同一個「超級坐席 + 知識庫編輯者」）
SESS = {"roles": ["agent"], "active_role": "agent", "staff_id": 1,
        "is_super_agent": True, "is_kb_editor": True}


# ======================================================================
# 1. 造資料 + 抓真實 payload
# ======================================================================

def seed() -> dict:
    """造一份小而完整的自檢資料，回傳渲染要用的 identifier。"""
    # --- 平台帳號（貼文配置 / 第二層開關都要它）---
    acc = sql("INSERT INTO crm_business_platform_account"
              " (business_id, platform, platform_business_account_id, display_name,"
              "  enabled, ai_auto_reply_enabled)"
              " VALUES (%s, 'ig', %s, '渲染自檢粉專', true, true) RETURNING id",
              (BIZ, f"ck_ig_{STAMP}"))[0]["id"]

    # --- FAQ（create_faq 會自動進基準啟用集）---
    faq = autoreply_db.create_faq(BIZ, FAQ_Q, FAQ_A, keywords="自檢,render")

    # --- 業務表格 ---
    tbl = autoreply_db.create_table(BIZ, f"{MARK}商品表{STAMP}", "渲染自檢用")
    autoreply_db.append_rows(int(tbl["id"]), [
        {"商品名稱": "A 款藍牙耳機", "價格": 1280, "庫存": "有貨"},
    ])

    # --- 文檔知識庫 + 圖片知識庫（圖片庫才會出現「圖片庫」pill）---
    doc_cid = kb_db.create_collection(f"{MARK}文檔庫{STAMP}", "渲染自檢用",
                                      business_id=BIZ)["id"]
    kb_db.create_collection(f"{MARK}圖片庫{STAMP}", "渲染自檢用，有圖",
                            business_id=BIZ, kind="image")

    # --- 回覆紀錄：一筆「回覆就是圖」的（驗 `[圖]` 預覽）＋ 一筆未命中 ---
    #     ★ 另外兩筆掛在會話 key 上，專門驗「AI 留痕」那塊：
    #       一筆帶 detail（顯示診斷字），一筆 detail 為空（顯示回覆 → 要經過 mdImages）
    other = f"ck-log-{STAMP}"
    img_row = [{"kind": "image", "chunk_id": -1, "doc_id": None,
                "doc": f"{MARK}圖片庫{STAMP}", "heading": "A 款耳機",
                "vec": 0.8, "lex": 0.4, "final": 0.71, "picked": True,
                "text": "A 款耳機正面圖"}]
    autoreply_db.insert_log({
        "business_id": BIZ, "platform": "ig", "scope": "dm", "tier": "rag",
        "reason": "sent", "question": f"{MARK}耳機多少錢", "answer": IMG_MD,
        "detail": "命中知識庫第 1 塊", "send_status": "sent", "score": 0.71,
        "end_user_sender_id": KEY, "matched_rows": img_row,
    })
    autoreply_db.insert_log({
        "business_id": BIZ, "platform": "ig", "scope": "dm", "tier": "rag",
        "reason": "sent", "question": f"{MARK}耳機有貨嗎", "answer": IMG_MD,
        "detail": "", "send_status": "sent", "score": 0.66,
        "end_user_sender_id": KEY, "matched_rows": img_row,
    })
    autoreply_db.insert_log({
        "business_id": BIZ, "platform": "ig", "scope": "dm", "tier": "miss",
        "reason": "no_answer", "question": f"{MARK}亂問", "answer": "",
        "detail": "三層都沒命中", "send_status": "skipped", "score": None,
        "end_user_sender_id": other,
    })
    # ★ 留言場景的 AI 總開關要開著，工作台才會顯示「貼文特殊配置」入口
    #   （沒這一列 → /conversations 的 ai_enabled=false → 前端顯示「留言 AI 已關」）
    autoreply_db.set_scope_mode(BIZ, "ig", "comment", True)
    return {"acc": int(acc), "faq": int(faq["id"]), "table": int(tbl["id"]),
            "doc_cid": int(doc_cid), "other_key": other}


def fetch_fx(ids: dict) -> dict:
    """把瀏覽器會收到的 JSON 原封不動抓回來（除會話外全部是真呼叫）。

    ⚠️ 直接呼叫端點函式時，`x: str = Query(default="")` 的預設值是 **`Query` 物件本身**
       （不是空字串）—— 那些參數一律要顯式傳，否則 `(q or "").strip()` 會炸。
    """
    # /overview ＋ loadOverview 之後補上的三個清單（前端就是這樣拼的）
    ov = console_router.overview(business_id=BIZ, sess=SESS)
    ov["faqs"] = console_router.faqs(business_id=BIZ, q="", sess=SESS)["items"]
    ov["tables"] = console_router.tables(business_id=BIZ, sess=SESS)["items"]
    ov["collections"] = console_router.collections(business_id=BIZ, sess=SESS)["items"]

    # 貼文特殊配置：一份「從沒配置過」、一份「配置過」
    pc_new = console_router.get_post_config(
        business_id=BIZ, platform="ig", post_id=POST_NEW,
        bp_account_id=ids["acc"], sess=SESS)
    console_router.save_post_config(
        console_router.PostConfigIn(
            business_id=BIZ, platform="ig", bp_account_id=ids["acc"], post_id=POST_SET,
            enabled=True, layers={"faq": True, "table": False, "kb": True},
            bindings={"faq": [ids["faq"]], "table": [], "kb": [ids["doc_cid"]]}),
        sess=SESS)
    pc_set = console_router.get_post_config(
        business_id=BIZ, platform="ig", post_id=POST_SET,
        bp_account_id=ids["acc"], sess=SESS)

    logs = console_router.logs(business_id=BIZ, platform="", scope="", tier="",
                               limit=100, offset=0, sess=SESS)
    # 會話上的 AI 留痕（真 repo：靠 end_user_sender_id 撈）
    conv_logs = repo.conversation_logs(BIZ, "ig", KEY)
    # 命中測試：FAQ 原句規則命中 → 引擎 0 次模型呼叫，所以是真 payload 又不花額度
    test_res = asyncio.run(console_router.test_engine(
        console_router.TestIn(business_id=BIZ, platform="ig", scope="dm",
                              text=FAQ_Q, layers=["faq"]),
        sess=SESS))

    # ---- 合成（Connector 才有的資料；欄位照 connector/app/internal/router.py 抄）----
    convs = [
        {"key": KEY, "sender_id": KEY, "sender_name": "渲染自檢客人",
         "last_ts": "2026-01-02T03:04:05+00:00", "pending": 1, "total": 3,
         "direction": "inbound", "message_type": "text", "text": IMG_MD,
         "post_id": POST_SET, "scope": "comment", "platform": "ig",
         "platform_business_account_id": f"ck_ig_{STAMP}",
         "bp_account_id": ids["acc"], "account_name": "渲染自檢粉專"},
        {"key": ids["other_key"], "sender_id": ids["other_key"], "sender_name": "另一位客人",
         "last_ts": "2026-01-02T01:00:00+00:00", "pending": 0, "total": 1,
         "direction": "outbound", "message_type": "text", "text": "您好，已為您處理。",
         "post_id": None, "scope": "comment", "platform": "ig",
         "platform_business_account_id": f"ck_ig_{STAMP}",
         "bp_account_id": ids["acc"], "account_name": "渲染自檢粉專"},
    ]
    thread = [
        {"direction": "inbound", "text": "你們的耳機多少錢？", "ts": "2026-01-02T03:00:00+00:00",
         "message_type": "text"},
        {"direction": "outbound", "text": IMG_MD, "ts": "2026-01-02T03:01:00+00:00",
         "message_type": "text"},
        {"direction": "inbound", "text": "", "ts": "2026-01-02T03:04:05+00:00",
         "message_type": "image"},
    ]
    return {
        "biz_id": BIZ, "acc_id": ids["acc"], "key": KEY, "post_set": POST_SET,
        "overview": ov, "postcfg_new": pc_new, "postcfg_set": pc_set,
        "logs": logs, "conv_logs": conv_logs, "test_result": test_res,
        "convs": convs, "thread": thread,
        "ai_enabled": repo.ai_switch_row(BIZ, "ig", "comment"),
        "img_md": IMG_MD,
        "faq_q": FAQ_Q, "faq_a": FAQ_A,
    }


# ======================================================================
# 2. 前端結構檢查（靜態掃描）
#    ⚠️ 真機「行為」驗證（切平台/開貼文/設定分頁、JS 錯誤）由瀏覽器冒煙自檢
#       `crm/tests/browser_smoke.js` 覆蓋（需要真 Chrome）；這裡只驗靜態結構，
#       成本低、隨時可跑。
# ======================================================================

def ui_source() -> str:
    with open(UI_HTML, encoding="utf-8") as f:
        return f.read()


def check_ui_structure(txt: str) -> None:
    section("[1~7] 前端結構（靜態掃描；真機行為見 browser_smoke.js）")

    check('id="mbar"' in txt and "renderMerchantBar(" in txt,
          "頂部有商家列（#mbar + renderMerchantBar）")
    check("'mbtn'" in txt and "fitMerchantBar(" in txt and "badge" in txt,
          "商家列：固定大小按鈕 + 未處理紅點 + 放不下收進「⋯」")
    check("statOf(" in txt and "last_ts" in txt and "pending" in txt,
          "商家列依最近訊息排序、未處理數取自 merchant-stats")

    check('id="nav-dm"' in txt and 'id="nav-posts"' in txt and "switchSection(" in txt,
          "一級導覽：私訊 / 貼文留言")
    check('id="tabs"' in txt and "switchPlatform(" in txt, "二級導覽：平台 tab")
    check('id="arBar"' in txt and "toggleAutoReply(" in txt, "側欄：AI 自動回復開關 + ⚙")

    check('class="msg-row ' in txt and 'class="bubble"' in txt,
          "私訊氣泡沿用舊版（.msg-row / .bubble）")
    check("function autoTag(" in txt and "autoTag(m)" in txt,
          "命中標記掛在氣泡 meta（與舊版同位置）")
    check("mdImages(m.text)" in txt, "氣泡內文走 mdImages()")

    check('id="postDetail"' in txt and "function commentHtml(" in txt,
          "貼文詳情 + 留言樹渲染")
    check('id="btnPostCfg"' in txt and "openPostCfg(" in txt,
          "★ 貼文自動回復特殊配置放在貼文詳情右上角")
    check('id="postCfgOverlay"' in txt and "savePostCfg(" in txt,
          "貼文特殊配置彈窗（可儲存 / 移除）")

    check('id="whoBox"' in txt and 'id="roleMenu"' in txt and 'id="roleList"' in txt,
          "側欄底部：身分切換（靜態節點）")
    check("window.TopUser.init({" in txt and "csrf: state.csrf" in txt,
          "TopUser.init 有傳 csrf（否則切換 403）")

    for tab in ("autoreply", "faq", "table", "kb", "hittest", "logs"):
        check('data-tab="%s"' % tab in txt, "設定面板有分頁：%s" % tab)
    check("function switchSettingsTab(" in txt and "function renderSettingsTab(" in txt,
          "設定面板分頁調度")

    check(txt.count("function mdImages(") == 1 and "function mdPreview(" in txt,
          "mdImages() 唯一實作 + mdPreview()（清單預覽不外露 markdown）")
    check("confirm(" not in txt, "沒有使用 confirm()（改走自訂詢問框）")

    for ep in ("/console/agent/conversations", "/console/agent/posts", "/console/agent/post",
               "/console/agent/comment/reply", "/console/agent/merchant-stats",
               "/console/agent/post-config", "/console/agent/faq", "/console/agent/table"):
        check(ep in txt, "前端接上端點：%s" % ep)

# ======================================================================
# 4. 主流程
# ======================================================================

def main() -> int:
    if not os.path.exists(NODE):
        print(f"  ⚠ 找不到 node：{NODE}")
        return 2

    print("=" * 78)
    print(" CRM 客服工作台前端渲染自檢（真實 payload → 真實渲染函式）")
    print("=" * 78)

    ids = seed()
    print(f"\n[0] 自檢資料就緒：商家 #{BIZ} / 帳號 #{ids['acc']} / FAQ #{ids['faq']} /"
          f" 表格 #{ids['table']} / 文檔庫 #{ids['doc_cid']}")

    fx = fetch_fx(ids)
    check(fx["overview"].get("ok") is True, "GET /overview 回 ok")
    check(len(fx["overview"]["faqs"]) >= 1 and len(fx["overview"]["tables"]) >= 1
          and len(fx["overview"]["collections"]) >= 2,
          "overview 拼上 FAQ / 表格 / 知識庫清單（跟 loadOverview 一樣）",
          f"{len(fx['overview']['faqs'])} / {len(fx['overview']['tables'])} /"
          f" {len(fx['overview']['collections'])}")
    check(fx["postcfg_new"]["configured"] is False,
          "沒配置過的貼文回 configured=false（＝與私訊相同）")
    check(fx["postcfg_set"]["configured"] is True,
          "配置過的貼文回 configured=true 並帶回挑選結果")
    check(any(c.get("kind") == "image" for c in fx["postcfg_set"]["candidates"]["kb"]),
          "候選清單裡有圖片知識庫（才會出現「圖片庫」pill）")
    check(len(fx["logs"].get("logs") or []) >= 2, "回覆紀錄抓得到",
          f"{len(fx['logs'].get('logs') or [])} 筆")
    check(bool(fx["conv_logs"]), "會話 AI 留痕抓得到（repo.conversation_logs）",
          f"{len(fx['conv_logs'])} 筆")
    check(fx["test_result"].get("tier") == "faq" and fx["test_result"].get("llm_calls") == 0,
          "命中測試 payload 是真的（FAQ 規則命中、0 次模型呼叫）",
          f"tier={fx['test_result'].get('tier')} "
          f"llm_calls={fx['test_result'].get('llm_calls')}")

    try:
        check_ui_structure(ui_source())
    finally:
        print("\n[9] 清理…")
        purge_business(BIZ)
        left = kb_db.list_collections(business_id=BIZ)
        check(not left, "自檢商家已被清空（庫／文件／FAQ／表格／紀錄／貼文配置）",
              f"剩 {len(left)} 個庫")

    return report("CRM 客服工作台前端渲染自檢")


if __name__ == "__main__":
    raise SystemExit(main())

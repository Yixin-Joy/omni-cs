"""運維端點（設計案 §15 / §8）—— 失敗任務、重試參數、手動重跑、告警、郵件、審計。

- 讀取：`require_ops`；變更：`require_csrf` + `rbac.can_manage_ops`。
- 發送嘗試紀錄在 Connector 庫 → 走內網 API（`/internal/api/outbound-attempt/batch`）。
- 每個變更都寫 `crm_audit_log`（ops 身分在審計頁只看得到這幾類）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import audit
from app.auth import deps, rbac
from app.internal import connector_client
from app.ops import alerting
from app.ops import repo

router = APIRouter(prefix="/console/ops", tags=["ops"])

PLATFORMS = ("wechat", "whatsapp", "ig", "fb")


def _err(status: int, msg: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": msg})


def _ops_guard(sess: dict):
    if not rbac.can_manage_ops(sess):
        return _err(403, "需要運維權限")


# ================================================================ 概覽

@router.get("/dashboard")
def dashboard(business_id: int = Query(default=0), sess: dict = Depends(deps.require_ops)):
    data = repo.dashboard(int(business_id or 0))
    data["mail"] = alerting.status()
    return {"ok": True, **data}


# ================================================================ 重試參數

@router.get("/config")
def get_config(sess: dict = Depends(deps.require_ops)):
    flat, nested = repo.ops_config()
    items = [{"key": k, "value": v, "type": repo.config_type_of(k),
              "is_default": v == repo.schema.OPS_CONFIG_DEFAULTS.get(k)}
             for k, v in sorted(flat.items())]
    return {"ok": True, "config": nested, "flat": flat, "items": items,
            "editable": sorted(repo.CONFIG_KEYS)}


class ConfigIn(BaseModel):
    key: str
    value: object = None


@router.post("/config")
def set_config(payload: ConfigIn, sess: dict = Depends(deps.require_csrf)):
    bad = _ops_guard(sess)
    if bad:
        return bad
    key = (payload.key or "").strip()
    if key not in repo.CONFIG_KEYS:
        return _err(400, f"不認識的運維參數：{key}")
    old_flat, _ = repo.ops_config()
    ok, err = repo.set_config(key, payload.value)
    if not ok:
        return _err(400, err or "儲存失敗")
    flat, _ = repo.ops_config()
    audit.log_standalone(sess, "ops_config_change",
                         payload={"key": key, "from": old_flat.get(key), "to": flat.get(key)})
    return {"ok": True, "key": key, "value": flat.get(key)}


# ================================================================ 失敗任務

@router.get("/tasks")
def tasks(status: str = Query(default=""), business_id: int = Query(default=0),
          platform: str = Query(default=""), keyword: str = Query(default=""),
          limit: int = Query(default=50), offset: int = Query(default=0),
          sess: dict = Depends(deps.require_ops)):
    """預設只回 `failed` + `uncertain`（真正需要人處理的）；`status=all` 回全部。"""
    if status == "all":
        statuses = list(repo.TASK_STATUSES)
    elif status:
        sts = [s.strip() for s in status.split(",") if s.strip() in repo.TASK_STATUSES]
        if not sts:
            return _err(400, f"不支援的任務狀態：{status}")
        statuses = sts
    else:
        statuses = list(repo.DEFAULT_FAIL_STATUSES)
    return {"ok": True, **repo.list_tasks(statuses=statuses, business_id=int(business_id or 0),
                                         platform=(platform if platform in PLATFORMS else ""),
                                         keyword=keyword.strip(), limit=int(limit),
                                         offset=int(offset))}


@router.get("/task")
def task_detail(id: int = Query(...), sess: dict = Depends(deps.require_ops)):
    row = repo.get_task(int(id))
    if not row:
        return _err(404, "找不到任務")
    attempts = connector_client.fetch_attempts(int(id))
    return {"ok": True, "item": row, "attempts": attempts,
            "attempts_reachable": attempts is not None}


class AttemptsIn(BaseModel):
    task_ids: list[int] = []


@router.post("/attempts")
def attempts(payload: AttemptsIn, sess: dict = Depends(deps.require_ops)):
    ids = [int(i) for i in (payload.task_ids or [])][:200]
    if not ids:
        return {"ok": True, "items": {}}
    got = connector_client.fetch_attempts_batch(ids)
    if got is None:
        return _err(502, "連不上 Connector，暫時取不到發送紀錄")
    return {"ok": True, "items": got}


class RetryIn(BaseModel):
    id: int = 0
    ids: list[int] = []


@router.post("/retry")
def retry(payload: RetryIn, sess: dict = Depends(deps.require_csrf)):
    """★ 手動重跑：把 `failed` / `uncertain` 打回 `pending`，Connector 下一輪就會認領。"""
    bad = _ops_guard(sess)
    if bad:
        return bad
    ids = list(payload.ids or [])
    if payload.id:
        ids = [int(payload.id)] + ids
    ids = [int(i) for i in ids]
    if not ids:
        return _err(400, "請指定要重跑的任務")
    res = repo.retry_tasks(ids)
    audit.log_standalone(sess, "task_retry",
                         payload={"requested": ids, "retried": res["retried"],
                                  "skipped": res["skipped"]})
    return {"ok": True, **res}


# ================================================================ 告警

@router.get("/alerts")
def alerts(only_open: bool = Query(default=True), severity: str = Query(default=""),
           limit: int = Query(default=100), offset: int = Query(default=0),
           sess: dict = Depends(deps.require_ops)):
    return {"ok": True, **repo.list_alerts(only_open=only_open, severity=severity.strip(),
                                           limit=int(limit), offset=int(offset))}


class AckIn(BaseModel):
    id: int = 0
    all: bool = False


@router.post("/alert/ack")
def ack(payload: AckIn, sess: dict = Depends(deps.require_csrf)):
    bad = _ops_guard(sess)
    if bad:
        return bad
    if payload.all:
        n = repo.ack_all_alerts(sess.get("staff_id"))
        audit.log_standalone(sess, "alert_ack", payload={"all": True, "count": n})
        return {"ok": True, "acked": n}
    if not payload.id:
        return _err(400, "請指定告警")
    ok = repo.ack_alert(int(payload.id), sess.get("staff_id"))
    if not ok:
        return _err(404, "找不到告警，或已經確認過了")
    audit.log_standalone(sess, "alert_ack", payload={"id": int(payload.id), "count": 1})
    return {"ok": True, "acked": 1}


# ================================================================ 郵件告警

@router.get("/mail")
def mail_status(sess: dict = Depends(deps.require_ops)):
    return {"ok": True, **alerting.status()}


class MailTestIn(BaseModel):
    to: list[str] | None = None


@router.post("/mail/test")
def mail_test(payload: MailTestIn, sess: dict = Depends(deps.require_csrf)):
    """寄一封測試信驗證 SMTP 設定（未配置時回 `ok=False` 講清楚缺什麼）。"""
    bad = _ops_guard(sess)
    if bad:
        return bad
    to = [str(x).strip() for x in (payload.to or []) if str(x).strip()] or None
    if not alerting.is_configured():
        st = alerting.status()
        missing = []
        if not st["enabled"]:
            missing.append("ALERT_EMAIL_ENABLED")
        if not st["smtp_host"]:
            missing.append("SMTP_HOST")
        if not st["recipients"]:
            missing.append("alert.email_recipients")
        return _err(400, "郵件告警尚未配置，缺少：" + "、".join(missing))
    ok = alerting.send_mail("[客服系統] 測試信", "這是一封測試信，收到代表 SMTP 設定正確。", to=to)
    audit.log_standalone(sess, "alert_notify", payload={"test": True, "to": to, "sent": bool(ok)})
    if not ok:
        return _err(502, "寄送失敗，請看服務日誌")
    return {"ok": True, "to": to or alerting.recipients()}


# ================================================================ 審計

# ★ 類型分組（2026-10-20 重新設計）：運維排查時問的問題其實只有幾種，
#   與其給 27 個原始類型，不如按「你會問什麼」分組。
#   每組都附一個具體類型清單，前端點組就會帶這些 types。
AUDIT_GROUPS = (
    {"key": "msg", "label": "訊息與發送",
     "hint": "回覆送出、歷史補拉、留言處理、任務重跑",
     "types": ["reply_send", "sync_history", "message_handle", "task_retry"]},
    {"key": "cfg", "label": "配置變更",
     "hint": "AI 開關、運維參數、貼文配置、帳號綁定、商家設定、RAG 綁定",
     "types": ["ai_switch_change", "ops_config_change", "post_config_change",
               "post_ai_config_change", "business_update", "business_create",
               "platform_account_bind", "platform_account_unbind",
               "binding_change", "kb_collection_toggle"]},
    {"key": "alert", "label": "告警處置",
     "hint": "告警確認、告警通知",
     "types": ["alert_ack", "alert_notify"]},
    {"key": "kb", "label": "內容維護",
     "hint": "FAQ 與業務表格的新增/修改/刪除、知識庫文件",
     "types": ["faq_create", "faq_update", "faq_delete", "faq_import",
               "business_table_create", "business_table_update", "business_table_delete",
               "kb_document_upload", "kb_document_delete", "kb_document_reparse",
               "kb_collection_create", "kb_collection_update", "kb_collection_delete"]},
    {"key": "sec", "label": "身分與安全",
     "hint": "登入、登入失敗、登出、強制登出、員工與權限異動",
     "types": ["login", "login_failed", "logout", "force_logout",
               "staff_create", "staff_update", "staff_disable", "staff_role_change",
               "staff_password_reset", "staff_business_permission_grant",
               "staff_business_permission_revoke"]},
)

# 給前端的「人話標籤」—— 27 個原始類型對人沒有意義
AUDIT_TYPE_LABELS = {
    "reply_send": "送出回覆", "sync_history": "補拉歷史", "message_handle": "處理訊息",
    "task_retry": "重跑任務",
    "ai_switch_change": "AI 開關", "ops_config_change": "運維參數",
    "post_config_change": "貼文配置", "post_ai_config_change": "貼文 AI 配置",
    "business_update": "更新商家", "business_create": "新增商家",
    "business_disable": "停用商家",
    "platform_account_bind": "綁定平台帳號", "platform_account_unbind": "解綁平台帳號",
    "binding_change": "綁定變更", "kb_collection_toggle": "知識庫開關",
    "alert_ack": "確認告警", "alert_notify": "發送告警",
    "faq_create": "新增 FAQ", "faq_update": "修改 FAQ", "faq_delete": "刪除 FAQ",
    "faq_import": "匯入 FAQ",
    "business_table_create": "新增表格", "business_table_update": "修改表格",
    "business_table_delete": "刪除表格",
    "kb_collection_create": "新增知識庫", "kb_collection_update": "修改知識庫",
    "kb_collection_delete": "刪除知識庫",
    "kb_document_upload": "上傳文件", "kb_document_delete": "刪除文件",
    "kb_document_reparse": "重新解析文件",
    "kb_asset_delete": "刪除圖片", "kb_asset_caption_edit": "改圖片說明",
    "login": "登入", "login_failed": "登入失敗", "logout": "登出",
    "force_logout": "強制登出",
    "staff_create": "新增員工", "staff_update": "修改員工",
    "staff_disable": "停用員工", "staff_role_change": "變更員工角色",
    "staff_password_reset": "重設密碼",
    "staff_business_permission_grant": "授予商家權限",
    "staff_business_permission_revoke": "回收商家權限",
}


@router.get("/audit")
def audit_logs(limit: int = Query(default=50), offset: int = Query(default=0),
               business_id: int = Query(default=0), all: bool = Query(default=False),
               types: str = Query(default=""), q: str = Query(default=""),
               time_from: str = Query(default=""), time_to: str = Query(default=""),
               only_failed: bool = Query(default=False),
               sess: dict = Depends(deps.require_ops)):
    """審計列表（篩選 + 分頁）。

    ★ 重新設計（2026-10-20 小jo）：
      · `types`：逗號分隔的多選類型；**空 = 預設組**（訊息/配置/告警/內容/安全，
        不含已下線的 conversation_read / role_switch）
      · `q`：模糊搜尋（操作人／身分／失敗原因／payload 全文／商家 id）
      · `time_from` / `time_to`：ISO 時間（含頭不含尾）
      · `only_failed`：只看失敗
      · 回應帶 `total` / `has_more`，前端才能做「一屏 20 條 + 頁碼」

    ⚠️ 這裡不能用 `rbac.is_admin(sess)` —— 能進到這個端點就代表當前生效身分是
      `ops`（`require_ops`），而 `roles_of()` 一次只回一個生效身分，`is_admin` 必為
      False。所以要看的是**登入快照裡有沒有 admin**（`roles_all`）：
      管理員切成運維身分時，仍然是同一個帳號，該看得到全部。
    """
    is_admin = rbac.ROLE_ADMIN in rbac.roles_all(sess)
    want_all = bool(all) and is_admin

    picked = [t.strip() for t in (types or "").split(",") if t.strip()]
    if picked and not want_all:
        # ★ 非管理員：只能在自己看得到的類型裡挑，不能靠傳 types 越權看別人的操作
        allowed = {t for g in AUDIT_GROUPS for t in g["types"]}
        picked = [t for t in picked if t in allowed]
    types_arg = None if want_all and not picked else (picked or None)

    # 不傳 types 且不是「全部」→ 用預設組（排除已下線的噪音類型）
    if types_arg is None and not want_all:
        types_arg = sorted({t for g in AUDIT_GROUPS for t in g["types"]})

    return {"ok": True,
            **repo.list_audit(limit=int(limit), offset=int(offset),
                              types=types_arg, business_id=int(business_id or 0),
                              time_from=(time_from or None), time_to=(time_to or None),
                              q=(q or ""), only_failed=bool(only_failed)),
            "all": want_all, "groups": AUDIT_GROUPS,
            "type_labels": AUDIT_TYPE_LABELS}


@router.get("/audit/facets")
def audit_facets(business_id: int = Query(default=0),
                 time_from: str = Query(default=""), time_to: str = Query(default=""),
                 sess: dict = Depends(deps.require_ops)):
    """各組／各類型在**當前時間範圍**內有幾筆 —— 前端 chip 上顯示數字用。

    ★ 為什麼要獨立一支：使用者要先知道「哪一組有東西」才會去點它。
      跟著列表一起回也可以，但列表帶分頁、facets 要全範圍統計，分開更清楚也更好快取。
    """
    is_admin = rbac.ROLE_ADMIN in rbac.roles_all(sess)
    allowed = sorted({t for g in AUDIT_GROUPS for t in g["types"]}) if not is_admin \
        else None
    counts = repo.audit_type_counts(business_id=int(business_id or 0),
                                    time_from=(time_from or None),
                                    time_to=(time_to or None),
                                    types=allowed)
    groups = []
    for g in AUDIT_GROUPS:
        groups.append({"key": g["key"], "label": g["label"], "hint": g["hint"],
                       "types": g["types"],
                       "count": sum(counts.get(t, 0) for t in g["types"])})
    # ★ total 只算「這 5 組」的合計，不要把已下線的 conversation_read / role_switch
    #   也算進去 —— 否則畫面上「合計 1195」跟各組加起來 880 對不上，看起來像 bug。
    default_total = sum(g["count"] for g in groups)
    return {"ok": True, "groups": groups, "counts": counts,
            "total": default_total,
            # 全部類型的真實總數（管理員切「顯示全部」時對照用）
            "total_all": sum(counts.values())}

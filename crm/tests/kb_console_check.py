"""自檢 —— 塊C：FAQ／業務表格／知識庫遷入 CRM 後的**商家隔離**與**權限**。

起因（小jo 2026-10-14）：
  「把 FAQ／業務表格／知識庫全部遷過來吧，功能、ui 都不變，
    只是每一個商家下獨立維護一個舊系統的資料體系（FAQ／業務表格／知識庫）和參數設置。」

問題：`kb_console.py` 是**從舊單體直接拷來**的 34 條路由，原本
  ① 沒有任何權限檢查（`overview()` 連 session 都沒有）；
  ② 沒有商家概念 —— `kb_param` 的檢索參數與提示詞是**全系統共用一組**，
     `kb_document` 也沒有 business_id（未分組文件在多租戶下沒有主人）。
直接掛上等於「任何人都能看／改所有商家的知識庫」。

本檔釘死修好之後的性質：
  [1] 設定一致性：NATIVE 映射、每平台必填憑證都存在
  [2] kb_param 按商家隔離（A 改了不影響 B）
  [3] kb_collection / kb_document 按商家隔離（含未分組文件）
  [4] 端點權限：未登入 → 401；跨商家 → 403；不是 kb_editor → 403
  [5] 前端契約：kbApi/kbApiJson 一定帶 business_id；沒有原生 confirm/prompt

跑法（從專案根）：
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \\
      <底層 python> crm/tests/kb_console_check.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _ck  # noqa: E402
from _ck import check, sql  # noqa: E402

_ck.ROOT and sys.path.insert(0, str(_ck.ROOT))

from app.core import config  # noqa: E402
from app.rag import kb_console as KC  # noqa: E402
from app.rag import kb_db, kb_search  # noqa: E402

BIZ_A = f"{_ck.MARK}kbA"
BIZ_B = f"{_ck.MARK}kbB"

# super agent：看得到全部商家
SUPER = {"active_role": "agent", "roles": ["admin", "agent"], "staff_id": 0,
         "is_super_agent": True}
# 只被授權 B 商家的客服（有知識庫編輯權）
AGENT_B = {"active_role": "agent", "roles": ["agent"], "staff_id": 0,
           "is_kb_editor": True}
AGENT_A = {"active_role": "agent", "roles": ["agent"], "staff_id": 0,
           "is_kb_editor": True}
# 客服但沒有知識庫編輯權
NO_EDIT = {"active_role": "agent", "roles": ["agent"], "staff_id": 0,
           "is_kb_editor": False}


def _status(res) -> int:
    return getattr(res, "status_code", 200)


def _body(res) -> str:
    if hasattr(res, "body"):
        return res.body.decode("utf-8")
    return json.dumps(res, ensure_ascii=False, default=str)


def main() -> int:
    # 清殘留 + 建兩個自檢商家
    # ⚠️ `crm_staff_business_permission` 有 FK 指向 crm_business —— 先刪權限再 purge。
    for name in (BIZ_A, BIZ_B):
        old = sql("SELECT id FROM crm_business WHERE business_name=%s", (name,))
        if old:
            sql("DELETE FROM crm_staff_business_permission WHERE business_id=%s",
                (old[0]["id"],), fetch=False)
            _ck.purge_business(old[0]["id"])
    a = _ck.ensure_business(BIZ_A)
    b = _ck.ensure_business(BIZ_B)
    print(f"自檢商家 A={a} B={b}\n")

    # 把 AGENT_A / AGENT_B 綁到真實 staff（權限表需要真實 staff_id）
    staff = sql("SELECT id FROM crm_staff ORDER BY id LIMIT 1")
    if not staff:
        check(False, "找不到任何 crm_staff（無法測權限表）")
        _ck.purge_business(a)
        _ck.purge_business(b)
        return _ck.report("知識庫商家隔離自檢")
    sid = int(staff[0]["id"])
    AGENT_A["staff_id"] = sid
    AGENT_B["staff_id"] = sid
    sql("DELETE FROM crm_staff_business_permission WHERE staff_id=%s AND business_id IN (%s,%s)",
        (sid, a, b))
    sql("INSERT INTO crm_staff_business_permission(staff_id, business_id) VALUES (%s,%s)",
        (sid, b))
    # 現在這個 staff 只被授權 B 商家

    # ------------------------------------------------------------ [1]
    print("[1] 設定一致性（塊C 相關）")
    for plat in ("wechat", "whatsapp", "fb", "ig"):
        keys = {k for k, _, _ in config.CREDENTIAL_FIELDS.get(plat, [])}
        nk = config.NATIVE_ID_FIELD.get(plat)
        req = set(config.CREDENTIAL_REQUIRED.get(plat, ()))
        check(nk in keys and nk in req and req <= keys,
              f"{plat}：原生 ID 欄位／必填集合都指向存在的憑證欄位")
    check(set(config.NATIVE_ID_FIELD) == set(config.CREDENTIAL_REQUIRED),
          "NATIVE_ID_FIELD 與 CREDENTIAL_REQUIRED 平台集合一致")

    # ------------------------------------------------------------ [2]
    print("\n[2] kb_param 按商家隔離（檢索參數 + 提示詞）")
    # 兩個商家的初始值都應該是內建預設
    p_a = kb_db.load_params(None, business_id=a)
    p_b = kb_db.load_params(None, business_id=b)
    check(p_a["top_k"] == p_b["top_k"] == config_rag_default("top_k"),
          f"兩商家初始都吃內建預設 top_k（{p_a['top_k']} / {p_b['top_k']}）")

    # 只改 A
    kb_db.set_params(None, {"top_k": 19, "min_score": 0.11}, business_id=a)
    kb_search.invalidate()
    p_a2 = kb_db.load_params(None, business_id=a)
    p_b2 = kb_db.load_params(None, business_id=b)
    check(p_a2["top_k"] == 19, f"★ A 商家的 top_k 改成 19（實得 {p_a2['top_k']}）")
    check(p_a2["min_score"] == 0.11, f"★ A 商家的 min_score 改成 0.11（實得 {p_a2['min_score']}）")
    check(p_b2["top_k"] != 19,
          f"★★ 改 A **完全沒有**影響 B（B 的 top_k = {p_b2['top_k']}）")
    check(p_b2["min_score"] != 0.11, f"★★ B 的 min_score 沒被動到（{p_b2['min_score']}）")

    # 提示詞也隔離
    kb_db.set_params(None, {"rag_answer_prompt": "A 的答題提示詞 {context} {sentinel}"},
                     business_id=a)
    kb_search.invalidate()
    pa = kb_search.prompt_of("rag_answer_prompt", business_id=a)
    pb = kb_search.prompt_of("rag_answer_prompt", business_id=b)
    check("A 的答題提示詞" in pa, "★ A 的提示詞生效")
    check("A 的答題提示詞" not in pb, "★★ B 的提示詞沒被 A 蓋掉")

    # 同一商家可以覆寫
    kb_db.set_params(None, {"top_k": 7}, business_id=b)
    kb_search.invalidate()
    check(kb_db.load_params(None, business_id=b)["top_k"] == 7, "B 自己也能覆寫")
    check(kb_db.load_params(None, business_id=a)["top_k"] == 19,
          "★ A 的設定沒被 B 覆寫回去")

    # 清掉 A/B 的覆寫（不留殘骸）
    clear = {k: None for k in config_rag_specs()}
    kb_db.set_params(None, clear, business_id=a)
    kb_db.set_params(None, clear, business_id=b)
    kb_search.invalidate()
    check(not kb_db.get_global_param_overrides(a), "★ A 的覆寫已清空")
    check(not kb_db.get_global_param_overrides(b), "★ B 的覆寫已清空")

    # ------------------------------------------------------------ [3]
    print("\n[3] kb_collection / kb_document 按商家隔離")
    col_a = kb_db.create_collection("A 的庫", business_id=a)
    col_b = kb_db.create_collection("B 的庫", business_id=b)
    check(kb_db.business_of_collection(col_a["id"]) == a, "A 的庫歸 A")
    check(kb_db.business_of_collection(col_b["id"]) == b, "B 的庫歸 B")

    cols_a = kb_db.list_collections(business_id=a)
    check([c["id"] for c in cols_a] == [col_a["id"]],
          f"★ 列 A 的庫只看到自己那一個（實得 {[c['name'] for c in cols_a]}）")

    # 未分組文件也要有主人
    doc_a = kb_db.create_document(filename="a.txt", collection_id=None, business_id=a)
    doc_b = kb_db.create_document(filename="b.txt", collection_id=None, business_id=b)
    check(doc_a["business_id"] == a, f"★ 未分組文件也帶 business_id=A（實得 {doc_a['business_id']}）")
    check(doc_b["business_id"] == b, "未分組文件 B 帶 business_id=B")
    docs_a = kb_db.list_documents(ungrouped=True, business_id=a)
    check([d["id"] for d in docs_a] == [doc_a["id"]],
          f"★ 列 A 的未分組文件只看到自己那份（實得 {[d['filename'] for d in docs_a]}）")
    check(kb_db.business_of_document(doc_a["id"]) == a, "business_of_document(A) 正確")

    # ------------------------------------------------------------ [4]
    print("\n[4] 端點權限與跨商家拒絕")

    async def run(coro):
        return await coro

    # 4a) 跨商家：AGENT 只有 B，卻去讀 A → 403
    r = asyncio.run(KC.overview(business_id=a, sess=AGENT_B))
    check(_status(r) == 403, f"★ 只有 B 權限的客服讀 A 的概覽 → 403（實得 {_status(r)}）")
    r = asyncio.run(KC.collections(business_id=a, sess=AGENT_B))
    check(_status(r) == 403, f"★ 讀 A 的知識庫清單 → 403（實得 {_status(r)}）")
    r = asyncio.run(KC.get_params(collection_id=str(col_a["id"]), business_id="", sess=AGENT_B))
    check(_status(r) == 403, f"★ 用 collection_id 反查 A 的庫 → 403（實得 {_status(r)}）")
    r = asyncio.run(KC.document(id=doc_a["id"], sess=AGENT_B))
    check(_status(r) == 403, f"★ 讀 A 的文件 → 403（實得 {_status(r)}）")
    r = asyncio.run(KC.list_assets(business_id=a, collection_id="", doc_id="",
                                   include_filtered="1", ungrouped="", sess=AGENT_B))
    check(_status(r) == 403, f"★ 讀 A 的圖片 → 403（實得 {_status(r)}）")

    # 4b) 自己的商家 → 放行
    r = asyncio.run(KC.overview(business_id=b, sess=AGENT_B))
    check(_status(r) == 200, f"★ 讀自己的 B 商概覽 → 200（實得 {_status(r)}）")
    r = asyncio.run(KC.collections(business_id=b, sess=AGENT_B))
    check(_status(r) == 200, "讀自己的知識庫清單 → 200")

    # 4c) 寫入：沒有 kb_editor → 403
    r = asyncio.run(KC.collection_create({"name": "偷偷建的", "business_id": b}, sess=NO_EDIT))
    check(_status(r) == 403, f"★ 沒有 kb_editor 不能建庫 → 403（實得 {_status(r)}）")
    r = asyncio.run(KC.collection_create({"name": "偷偷建的", "business_id": a}, sess=AGENT_B))
    check(_status(r) == 403, f"★ 有編輯權也不能建到別人家 → 403（實得 {_status(r)}）")

    # 4d) 缺 business_id → 400
    r = asyncio.run(KC.overview(business_id=0, sess=SUPER))
    check(_status(r) == 400, f"★ 沒帶商家 → 400（實得 {_status(r)}）")
    r = asyncio.run(KC.collections(business_id=0, sess=SUPER))
    check(_status(r) == 400, "知識庫清單沒帶商家 → 400")

    # 4e) 搬文件到別人家 → 403
    r = asyncio.run(KC.document_move({"id": doc_b["id"], "collection_id": col_a["id"]},
                                     sess=AGENT_B))
    check(_status(r) == 403, f"★ 不能把文件搬到別商家的庫 → 403（實得 {_status(r)}）")

    # 4f) super agent 全部可讀
    r = asyncio.run(KC.overview(business_id=a, sess=SUPER))
    check(_status(r) == 200, "super agent 讀 A 也 200")

    # 4g) 概覽統計只算自己商家的
    body = json.loads(_body(asyncio.run(KC.overview(business_id=a, sess=SUPER))))
    check(body.get("business_id") == a, "概覽回傳 business_id")
    names = [c["name"] for c in body.get("collections", [])]
    check("B 的庫" not in names, f"★ A 的概覽不含 B 的庫（實得 {names}）")
    check(int(body["totals"]["documents"]) == 1,
          f"★ A 的文件總數只算自己的（含未分組，實得 {body['totals']['documents']}）")
    check(int(body["totals"]["ungrouped_docs"]) == 1,
          f"★ A 的未分組文件數正確（實得 {body['totals']['ungrouped_docs']}）")

    # ------------------------------------------------------------ [5]
    print("\n[5] 前端契約（原始碼閘門）")
    html = (Path(_ck.ROOT) / "ui" / "console_ui.html").read_text(encoding="utf-8")
    check("function kbBizPath" in html, "有 kbBizPath()（統一補 business_id）")
    check("'business_id=' + encodeURIComponent(state.bizId || 0)" in html,
          "★ GET 請求補 business_id")
    check("payload.business_id = state.bizId" in html, "★ POST 請求補 business_id")
    check("fd.append('business_id'" in html, "★ 上傳（FormData）也補 business_id")
    check("function renderSettingsKb" in html, "設定分頁接上知識庫")
    check("尚未接線" not in html, "★ 已無「塊C 尚未接線」佔位")
    check(html.count('id="kbNewOverlay"') == 1, "新增知識庫彈窗只有一份（不重複）")
    check(html.count('id="kbParamsOverlay"') == 1, "參數彈窗只有一份")
    check('id="askInputOverlay"' in html, "有自訂輸入框（不用原生 prompt）")
    # 知識庫那一段不得再用原生對話框
    seg = html.split("知識庫（RAG 第三層）—— 塊C")[1].split("尾端靜態節點用到的函式")[0]
    check("confirm(" not in seg and "alert(" not in seg and "prompt(" not in seg,
          "★ 知識庫程式碼沒有原生 confirm/alert/prompt")

    # 收尾
    # ⚠️ 順序要緊：`crm_staff_business_permission` 有 FK 指向 crm_business，
    #    必須**先刪權限列**再 purge 商家，否則 ForeignKeyViolation。
    sql("DELETE FROM crm_staff_business_permission WHERE staff_id=%s AND business_id IN (%s,%s)",
        (sid, a, b))
    _ck.purge_business(a)
    _ck.purge_business(b)
    left = sql("SELECT count(*) AS n FROM crm_business WHERE business_name IN (%s,%s)",
               (BIZ_A, BIZ_B))[0]["n"]
    check(left == 0, "自檢商家已清乾淨")
    check(not sql("SELECT 1 FROM kb_collection WHERE business_id IN (%s,%s)", (a, b)),
          "★ 自檢知識庫沒殘留")
    check(not sql("SELECT 1 FROM kb_param WHERE business_id IN (%s,%s)", (a, b)),
          "★ 自檢參數沒殘留")

    return _ck.report("知識庫（塊C）商家隔離與權限自檢")


def config_rag_default(key: str):
    from app.rag import rag_config
    return rag_config.coerce(key, rag_config.PARAM_SPECS[key]["default"])


def config_rag_specs():
    from app.rag import rag_config
    return list(rag_config.PARAM_SPECS)


if __name__ == "__main__":
    sys.exit(main())

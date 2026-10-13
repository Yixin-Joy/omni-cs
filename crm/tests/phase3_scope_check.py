"""階段三自檢 —— 開關兩層 AND 與啟用集（設計案 §9）。

驗「什麼情況下 AI 會跑」這條最容易出錯的線：
  * 兩層 AND：`business_ai_switch` × `crm_business_platform_account.ai_auto_reply_enabled`
  * 基準啟用集：`business_{faq,table,kb}_binding` —— ★ **空集 = 不跑**（不是「沒勾＝全部」）
  * 貼文覆寫集：`post_ai_config` + `post_*_binding`
  * 層開關：`business_layer_switch`

全程只走 FAQ 層（純規則、不呼叫模型），所以不需要真的打 LLM。

跑法（cwd 必須是 crm/）：
    python tests/phase3_scope_check.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psycopg2                      # noqa: E402
import psycopg2.extras               # noqa: E402

from app.autoreply import autoreply, autoreply_db  # noqa: E402
from app.core import config, db      # noqa: E402
from app.llm import reply_engine     # noqa: E402
from app.rag import kb_db            # noqa: E402

MARK = "P3S"
NATIVE = MARK + "_native"
# ⚠️ 兩條 FAQ 的問句必須「彼此毫不相干」—— 太像的話（只差一個字）第二條會用
#    字面相似度命中第一條的問句，測出來的就不是「啟用集」而是「匹配器」了。
Q1 = "產品保固期限是多久呢"
A1 = "測試答案" + MARK + "甲（保固兩年）"
Q2 = "發票可以開統編嗎"
A2 = "測試答案" + MARK + "乙（可以開統編）"

_ok = [0, 0]


def check(cond, msg: str) -> None:
    _ok[0 if cond else 1] += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg)


def sql(query: str, params=None, fetch: bool = True):
    conn = psycopg2.connect(config.DATABASE_URL)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params or ())
            rows = cur.fetchall() if (fetch and cur.description is not None) else None
        conn.commit()
        return rows
    finally:
        conn.close()


def cleanup() -> None:
    biz_ids = "(SELECT id FROM crm_business WHERE business_name LIKE %s)"
    sql(f"DELETE FROM business_kb_binding WHERE business_id IN {biz_ids}", (MARK + "%",), fetch=False)
    sql(f"DELETE FROM business_faq_binding WHERE business_id IN {biz_ids}", (MARK + "%",), fetch=False)
    sql(f"DELETE FROM business_table_binding WHERE business_id IN {biz_ids}", (MARK + "%",), fetch=False)
    sql("DELETE FROM post_kb_binding WHERE post_config_id IN"
        " (SELECT id FROM post_ai_config WHERE post_id LIKE %s)", (MARK + "%",), fetch=False)
    sql("DELETE FROM post_faq_binding WHERE post_config_id IN"
        " (SELECT id FROM post_ai_config WHERE post_id LIKE %s)", (MARK + "%",), fetch=False)
    sql("DELETE FROM post_table_binding WHERE post_config_id IN"
        " (SELECT id FROM post_ai_config WHERE post_id LIKE %s)", (MARK + "%",), fetch=False)
    sql("DELETE FROM post_ai_config WHERE post_id LIKE %s", (MARK + "%",), fetch=False)
    sql(f"DELETE FROM business_layer_switch WHERE business_id IN {biz_ids}", (MARK + "%",), fetch=False)
    sql(f"DELETE FROM business_ai_switch WHERE business_id IN {biz_ids}", (MARK + "%",), fetch=False)
    sql(f"DELETE FROM autoreply_faq WHERE business_id IN {biz_ids}", (MARK + "%",), fetch=False)
    sql(f"DELETE FROM autoreply_business_row WHERE table_id IN"
        f" (SELECT id FROM autoreply_business_table WHERE business_id IN {biz_ids})",
        (MARK + "%",), fetch=False)
    sql(f"DELETE FROM autoreply_business_table WHERE business_id IN {biz_ids}", (MARK + "%",), fetch=False)
    sql("DELETE FROM kb_document WHERE collection_id IN"
        " (SELECT id FROM kb_collection WHERE name LIKE %s)", (MARK + "%",), fetch=False)
    sql("DELETE FROM kb_collection WHERE name LIKE %s", (MARK + "%",), fetch=False)
    sql("DELETE FROM crm_business_platform_account WHERE platform_business_account_id LIKE %s",
        (MARK + "%",), fetch=False)
    sql("DELETE FROM crm_business WHERE business_name LIKE %s", (MARK + "%",), fetch=False)


def engine(text, biz, *, scope="dm", scope_set=None):
    """只跑 FAQ 那條路（改寫關掉），不需要 LLM。"""
    reply_engine.invalidate_cache()
    return asyncio.run(reply_engine.answer(
        text, business_id=biz, channel="wechat", scope=scope,
        query_rewrite=False, scope_set=scope_set))


def main() -> int:
    print("=" * 68)
    print("階段三自檢：開關兩層 AND 與啟用集（設計案 §9）")
    print("=" * 68)

    db.init_schema_sync()
    cleanup()

    biz = sql("INSERT INTO crm_business (business_name) VALUES (%s) RETURNING id",
              (MARK + "商家",))[0]["id"]
    acc = sql("INSERT INTO crm_business_platform_account"
              " (business_id, platform, platform_business_account_id, display_name)"
              " VALUES (%s, 'wechat', %s, %s) RETURNING id", (biz, NATIVE, MARK))[0]["id"]
    print(f"\n[0] 場地：business={biz}，bp_account={acc}")

    # ---------------------------------------------------------------- [1] 兩層 AND
    print("\n[1] 兩層 AND：商家×平台×場景 AND 平台帳號")
    check(autoreply.is_enabled(biz, "wechat", "dm", acc) is False,
          "兩層都沒開 → 不生效（第一層預設關）")
    autoreply_db.set_scope_mode(biz, "wechat", "dm", True)
    check(autoreply.is_enabled(biz, "wechat", "dm", acc) is True,
          "第一層開 + 帳號層預設 true → 生效")
    autoreply_db.set_platform_account_ai(acc, False)
    check(autoreply.is_enabled(biz, "wechat", "dm", acc) is False,
          "★ 第一層開但帳號層關 → 不生效（AND）")
    autoreply_db.set_platform_account_ai(acc, True)
    check(autoreply.is_enabled(biz, "wechat", "dm", acc) is True, "帳號層開回來 → 生效")
    autoreply_db.set_scope_mode(biz, "wechat", "dm", False)
    check(autoreply.is_enabled(biz, "wechat", "dm", acc) is False,
          "第一層關（帳號層開）→ 不生效（AND）")
    check(autoreply.is_enabled(biz, "wechat", "dm", 0) is False,
          "沒帶帳號 id 時只判斷第一層（此刻第一層關 → False）")
    autoreply_db.set_scope_mode(biz, "wechat", "dm", True)
    check(autoreply_db.platform_account_enabled(999999) is False,
          "帳號不存在 → 帳號層 False（寧可不回）")

    # ---------------------------------------------------------------- [2] 上傳即進基準集
    print("\n[2] 上傳即進基準集（§9.4）")
    f1 = autoreply_db.create_faq(biz, Q1, A1)
    f2 = autoreply_db.create_faq(biz, Q2, A2)
    check(sorted(autoreply_db.list_binding_ids(biz, "faq")) == sorted([f1["id"], f2["id"]]),
          f"create_faq 自動進 business_faq_binding（實得 {autoreply_db.list_binding_ids(biz, 'faq')}）")
    t1 = autoreply_db.create_table(biz, MARK + "表", "測試表", ["品項", "價格"])
    check(autoreply_db.list_binding_ids(biz, "table") == [t1["id"]],
          "create_table 自動進 business_table_binding")
    c1 = kb_db.create_collection(MARK + "知識庫", "測試", business_id=biz, kind="doc")
    check(autoreply_db.list_binding_ids(biz, "kb") == [c1["id"]],
          "create_collection 自動進 business_kb_binding")
    check(autoreply_db.binding_counts(biz) == {"faq": 2, "table": 1, "kb": 1},
          f"三層計數（實得 {autoreply_db.binding_counts(biz)}）")

    # ---------------------------------------------------------------- [3] 基準集驅動 FAQ
    print("\n[3] FAQ 層用基準集")
    ans = engine(Q1, biz)
    check(ans.hit and ans.tier == reply_engine.TIER_FAQ and ans.faq_id == f1["id"],
          f"命中對應的 FAQ（實得 tier={ans.tier}／faq={ans.faq_id}（應為 {f1['id']}））")
    check(len(reply_engine._faq_entries(biz)) == 2,
          "引擎的 FAQ 候選 = 基準集（2 條）")
    reply_engine.invalidate_cache()
    autoreply_db.set_binding(biz, "faq", f1["id"], False)
    check(len(reply_engine._faq_entries(biz)) == 1, "取消勾選後候選剩 1 條")
    ans2 = engine(Q1, biz)
    check(not ans2.hit, f"取消勾選的 FAQ 不再命中（實得 {ans2.tier}／{ans2.detail[:40]}）")

    # ---------------------------------------------------------------- [4] 空集 = 不跑
    print("\n[4] ★ 空集 = 不跑（不是「沒勾＝全部」）")
    reply_engine.invalidate_cache()
    autoreply_db.set_binding(biz, "faq", f2["id"], False)
    check(autoreply_db.list_binding_ids(biz, "faq") == [], "問答對全部取消勾選 → 空集")
    ans3 = engine(Q2, biz)
    check(not ans3.hit and "沒有勾選任何問答對" in (ans3.detail or ""),
          f"空集 → 不命中且說明原因（實得 {ans3.detail[:60]!r}）")
    check(reply_engine._faq_entries(biz) == [], "空集時候選清單是空的（沒有偷偷回全部）")
    check(autoreply_db.list_faqs(biz, ids=[]) == [], "list_faqs(ids=[]) 回空")
    check(len(autoreply_db.list_faqs(biz)) == 2, "不給 ids 時仍看得到全部（UI 列表用）")
    for f in (f1, f2):
        autoreply_db.set_binding(biz, "faq", f["id"], True)

    # ---------------------------------------------------------------- [5] 層開關
    print("\n[5] 層開關（business_layer_switch）")
    autoreply_db.set_layer_enabled(biz, "faq", False)
    reply_engine.invalidate_cache()
    check(reply_engine.faq_layer_enabled(biz) is False, "FAQ 層停用")
    ans4 = engine(Q1, biz)
    check(not ans4.hit and "FAQ" not in (ans4.detail or ""),
          f"整層停用 → 不跑（detail={ans4.detail[:40]!r}）")
    autoreply_db.set_layer_enabled(biz, "faq", True)
    reply_engine.invalidate_cache()
    check(engine(Q1, biz).hit is True, "開回來 → 又命中")

    # ---------------------------------------------------------------- [6] 貼文覆寫集
    print("\n[6] 貼文覆寫集（post_ai_config）")
    pid = sql("INSERT INTO post_ai_config (platform, post_id, business_platform_account_id)"
              " VALUES ('fb', %s, %s) RETURNING id", (MARK + "_post", acc))[0]["id"]
    ss = autoreply_db.resolve_scope_set(biz, scope="comment", post_config_id=pid)
    check(ss["source"] == "business" and sorted(ss["faq"]) == sorted([f1["id"], f2["id"]]),
          f"未配置過 → 與私訊完全相同（實得 {ss['source']}／{ss['faq']}）")
    sql("UPDATE post_ai_config SET enabled=false WHERE id=%s", (pid,), fetch=False)
    ss = autoreply_db.resolve_scope_set(biz, scope="comment", post_config_id=pid)
    check(ss["source"] == "post_off" and ss["faq"] == [] and ss["layers"]["faq"] is False,
          f"貼文總開關關 → 三層全空（實得 {ss}）")
    sql("UPDATE post_ai_config SET enabled=true, configured=true, table_enabled=false,"
        " kb_enabled=false, faq_enabled=true WHERE id=%s", (pid,), fetch=False)
    sql("INSERT INTO post_faq_binding (post_config_id, faq_id, enabled) VALUES (%s, %s, true)",
        (pid, f2["id"]), fetch=False)
    ss = autoreply_db.resolve_scope_set(biz, scope="comment", post_config_id=pid)
    check(ss["source"] == "post" and ss["faq"] == [f2["id"]]
          and ss["table"] == [] and ss["kb"] == []
          and ss["layers"] == {"faq": True, "table": False, "rag": False},
          f"配置過 → 完全按自己的儲存、不再 fallback（實得 {ss}）")
    ans5 = engine(Q2, biz, scope="comment", scope_set=ss)
    check(ans5.hit and ans5.faq_id == f2["id"],
          f"貼文集裡的 FAQ 命中（實得 {ans5.tier}／faq={ans5.faq_id}）")
    ans6 = engine(Q1, biz, scope="comment", scope_set=ss)
    check(not ans6.hit, "基準集裡那條（不在貼文集內）不命中")

    # ---------------------------------------------------------------- 收尾
    print("\n[7] 收尾")
    cleanup()
    left = sql("SELECT count(*) AS n FROM crm_business WHERE business_name LIKE %s",
               (MARK + "%",))[0]["n"]
    check(left == 0, "殘骸已清空")

    print("\n" + "=" * 68)
    print(f"結果：PASS {_ok[0]} / FAIL {_ok[1]}")
    print("=" * 68)
    return 1 if _ok[1] else 0


if __name__ == "__main__":
    raise SystemExit(main())

r"""驗證：子頁的「啟用／停用」會連動商家基準集（設定頁已無「啟用集」區塊）。

實際生效 = binding.enabled AND 實體.enabled → 兩者必須永遠同值，
否則子頁顯示「啟用」卻檢索不到（看不見的障礙）。

⚠️ 這支會**改動真實商家的資料**，所以每一步都「改完立刻還原」，
   且第 4 步用**原值**回寫（絕不留測試字串在真實資料裡 —— 曾踩過）。

跑法（repo 根）：
  PYTHONPATH='<根>/crm;<根>/.venv/Lib/site-packages' PYTHONUTF8=1 <python> crm/tests/binding_sync_check.py
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "crm"))

from app.agent import console_router                       # noqa: E402
from app.agent.console_router import FaqUpdateIn, TableUpdateIn   # noqa: E402
from app.autoreply import autoreply_db                     # noqa: E402
from app.rag import kb_db                                  # noqa: E402

SESS = {"staff_id": 1, "staff_name": "自檢", "roles": ["agent"], "active_role": "agent",
        "is_super_agent": True, "is_kb_editor": True}

_pass = 0
_fail: list[str] = []


def check(cond, label, extra=""):
    global _pass
    if cond:
        _pass += 1
        print(f"  PASS  {label}" + (f"   {extra}" if extra else ""))
    else:
        _fail.append(label)
        print(f"  FAIL  {label}" + (f"   {extra}" if extra else ""))


BIZ = 1


def binding_has(kind, item_id):
    return int(item_id) in autoreply_db.list_binding_ids(BIZ, kind)


# ---------------- 知識庫 ----------------
print("\n[1] 知識庫：子頁停用／啟用 → 基準集跟隨")
cols = [c for c in kb_db.list_collections(business_id=BIZ)
        if c["enabled"] and not c.get("image_of")]
if not cols:
    print("  （沒有可測的知識庫，略過）")
else:
    cid = int(cols[0]["id"])
    kb_db.set_collection_enabled(cid, False)
    check(not binding_has("kb", cid), f"停用知識庫 #{cid} → 基準集同步移除",
          f"binding={autoreply_db.list_binding_ids(BIZ, 'kb')}")
    kb_db.set_collection_enabled(cid, True)
    check(binding_has("kb", cid), f"啟用知識庫 #{cid} → 基準集同步加回")
    check(kb_db.get_collection(cid)["enabled"] is True, f"知識庫 #{cid} 已還原為啟用")

# ---------------- FAQ ----------------
print("\n[2] FAQ：子頁停用／啟用 → 基準集跟隨")
faqs = [f for f in autoreply_db.list_faqs(BIZ, enabled_only=False) if f["enabled"]]
if not faqs:
    print("  （沒有可測的 FAQ，略過）")
else:
    fid = int(faqs[0]["id"])
    console_router.faq_update(FaqUpdateIn(business_id=BIZ, id=fid, enabled=False), SESS)
    check(not binding_has("faq", fid), f"停用 FAQ #{fid} → 基準集同步移除")
    console_router.faq_update(FaqUpdateIn(business_id=BIZ, id=fid, enabled=True), SESS)
    check(binding_has("faq", fid), f"啟用 FAQ #{fid} → 基準集同步加回")

# ---------------- 業務表格 ----------------
print("\n[3] 業務表格：子頁停用／啟用 → 基準集跟隨")
tables = [t for t in autoreply_db.list_tables(BIZ, enabled_only=False) if t["enabled"]]
if not tables:
    print("  （沒有可測的表格，略過）")
else:
    tid = int(tables[0]["id"])
    console_router.table_update(TableUpdateIn(business_id=BIZ, id=tid, enabled=False), SESS)
    check(not binding_has("table", tid), f"停用表格 #{tid} → 基準集同步移除")
    console_router.table_update(TableUpdateIn(business_id=BIZ, id=tid, enabled=True), SESS)
    check(binding_has("table", tid), f"啟用表格 #{tid} → 基準集同步加回")

# ---------------- 不改變 enabled 的更新不該動基準集 ----------------
print("\n[4] 只改其他欄位（不碰 enabled）→ 基準集不變，且值原樣還原")
if faqs:
    fid = int(faqs[0]["id"])
    original = autoreply_db.list_faqs(BIZ, enabled_only=False)
    original = next(f for f in original if int(f["id"]) == fid)
    orig_kw = original.get("keywords") or ""
    before = binding_has("faq", fid)
    console_router.faq_update(
        FaqUpdateIn(business_id=BIZ, id=fid, keywords=orig_kw + "_tmp"), SESS)
    check(binding_has("faq", fid) == before, f"只改 keywords → 基準集狀態不變（{before}）")
    console_router.faq_update(FaqUpdateIn(business_id=BIZ, id=fid, keywords=orig_kw), SESS)
    after = next(f for f in autoreply_db.list_faqs(BIZ, enabled_only=False)
                 if int(f["id"]) == fid)
    check((after.get("keywords") or "") == orig_kw,
          f"keywords 已還原成原值（{orig_kw!r}）", repr(after.get("keywords")))

print("\n" + "=" * 60)
print(f"  binding 連動自檢：PASS {_pass} / FAIL {len(_fail)}")
if _fail:
    print("  失敗：" + "、".join(_fail))
raise SystemExit(1 if _fail else 0)

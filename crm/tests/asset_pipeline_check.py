r"""AI 回覆「圖文結合」的端到端鏈路自檢（不發真網路）。

為什麼要有這支：2026-10-14 回報「記錄寫附圖 4，但聊天框跟 FB 都只有文字」。
根因是鏈路上有**三個各自獨立**的斷點，任何一個壞掉症狀都一樣（只有文字）：

  ① `autoreply._asset_items()` 只塞 `rel_path`，
     而 Connector `build_sub_items()` 讀的是 `media_key` → **附圖被靜默丟掉**。
  ② `igfb.send_asset()` 是 `SendResult(ok=False, "尚未支援")` 的 stub
     → 就算 key 對了，IG／FB 也永遠發不出去。
  ③ `record_outbound_message()` 只寫 `media_key`，
     而工作台 `_files_of()` 讀 `url` → **氣泡裡沒有圖可渲染**。
  另有一個潛在坑：`KB_CACHE_DIR` 不在 `MEDIA_DIR` 底下 → `resolve()` 找不到檔。

跑法（repo 根）：
  PYTHONPATH='<根>/crm;<根>/.venv/Lib/site-packages' PYTHONUTF8=1 <python> crm/tests/asset_pipeline_check.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "crm"))
sys.path.insert(0, str(_ROOT / "connector"))

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


print("\n[1] Connector 的 kb_cache 必須在 MEDIA_DIR 底下（否則 resolve() 找不到）")
from app.core import config as cconfig            # noqa: E402  (connector)
from app import media as cmedia                    # noqa: E402

md = Path(cconfig.MEDIA_DIR).resolve()
kc = Path(cconfig.KB_CACHE_DIR).resolve()
check(md == kc or md in kc.parents, "KB_CACHE_DIR 在 MEDIA_DIR 底下",
      f"MEDIA_DIR={md} KB_CACHE_DIR={kc}")

print("\n[2] 存一張圖 → media_key 可以被 resolve + 有公開網址")
_png = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082")
saved = cmedia.save_bytes("kb_cache", _png, "t.png")
check(bool(saved.get("media_key")), "save_bytes 回 media_key", saved.get("media_key"))
try:
    p = cmedia.resolve(saved["media_key"])
    check(p.is_file(), "resolve(media_key) 找得到檔案", str(p))
except Exception as e:  # noqa: BLE001
    check(False, "resolve(media_key) 找得到檔案", f"{type(e).__name__}: {e}")
url = cmedia.public_url(saved["media_key"])
check(url.startswith("http") and "/files/" in url, "public_url 產生外網可達網址", url)
check(cmedia.public_url(saved["media_key"], base_url="") == "",
      "沒設 PUBLIC_BASE_URL 時回空字串（呼叫端才能記原因）")

print("\n[3] build_sub_items 認得 CRM 給的 assets（media_key 格式）")
from app.worker import outbound as cworker        # noqa: E402

mc = {"text": "安裝步驟如下",
      "assets": [{"media_key": saved["media_key"], "caption": "圖 1", "mime": "image/png"}]}
items = cworker.build_sub_items(mc)
kinds = [i["kind"] for i in items]
check(kinds == ["text", "asset"], "拆成 text + asset 兩個子項（原本附圖會整條消失）",
      json.dumps(items, ensure_ascii=False)[:140])
check(items[1]["media_key"] == saved["media_key"], "asset 子項帶著 media_key")

print("\n[4] IG／FB 的 send_asset 不再是 stub")
import inspect                                     # noqa: E402
from app.channels import igfb as cigfb             # noqa: E402

src = inspect.getsource(cigfb.send_asset)
check('error="IG/FB 附件發送尚未支援' not in src,
      "send_asset 已實作（不再回『尚未支援』stub）")
check("public_url" in src, "send_asset 會產生公開網址給 Meta 抓")

print("\n[5] 出站落地要寫可渲染的 url（工作台讀 url，不是 media_key）")
rsrc = inspect.getsource(cworker.record_outbound_message)
check('"url"' in rsrc or "'url'" in rsrc, "record_outbound_message 會寫 url")
check('/files/' in rsrc, "url 指向 /files/（CRM 會代理到 Connector）")

print("\n[6] 真的送一次（mock 模式，不發網路）：text 與 asset 都要有 attempt")
os.environ["OUTBOUND_SEND_MODE"] = "mock"
import importlib                                   # noqa: E402
importlib.reload(cconfig)
importlib.reload(cworker)


async def _mock_send():
    res_text = await cworker._send_one("fb", "123", {"kind": "text", "text": "hi"}, {})
    res_img = await cworker._send_one("fb", "123",
                                      {"kind": "asset", "media_key": saved["media_key"]}, {})
    return res_text, res_img


rt, ri = asyncio.run(_mock_send())
check(rt.ok, "純文字送出 ok", rt.error or "")
check(ri.ok, "附圖送出 ok（mock）", ri.error or "")

print("\n[7] CRM `_asset_items` 會上傳並回 media_key（不再是 rel_path）")
os.environ.pop("OUTBOUND_SEND_MODE", None)
sys.path.insert(0, str(_ROOT / "crm"))
for m in list(sys.modules):
    if m == "app" or m.startswith("app."):
        del sys.modules[m]
sys.path.remove(str(_ROOT / "connector"))
sys.path.insert(0, str(_ROOT / "crm"))

from app.autoreply import autoreply as careply     # noqa: E402

asrc = inspect.getsource(careply._asset_items)
check("upload_media" in asrc, "_asset_items 會把圖推給 Connector")
check("rel_path" not in asrc.split("media_key")[0].split("out.append")[-1]
      or "media_key" in asrc, "_asset_items 產出的是 media_key")
check("local_path_of_rel" in asrc, "用 rag_config.local_path_of_rel 找實體檔")
from app.rag import rag_config as rc               # noqa: E402
lp = rc.local_path_of_rel("kb_assets/7a1102f7f0eec5f8.png")
check(bool(lp) and os.path.isfile(lp), "local_path_of_rel 找得到真實 kb 圖", lp)

print("\n" + "=" * 60)
print(f"  圖文鏈路自檢：PASS {_pass} / FAIL {len(_fail)}")
if _fail:
    print("  失敗：" + "、".join(_fail))
raise SystemExit(1 if _fail else 0)

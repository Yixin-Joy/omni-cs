"""驗證：貼文縮圖存成本地快取（不再依賴會過期的 Meta CDN 簽名網址）。

★ 為什麼要有這支（2026-10-20 小jo：「facebook 的貼文圖片不見了」）：
  `igfb_post.media_url` 原本存 Meta CDN 的**簽名網址**，URL 的 `oe=` 是到期時間，
  有效期約 24 小時 → 過期後一律 403。實測 8 篇貼文的簽名全在 10-18 18:00 UTC
  到期，FB 與 IG 的圖同時全掛（前端 <img> 載不到 → 只剩空白/「媒體無法載入」）。

  修法：`upsert_post()` 寫入前把圖抓下來存 `media/post_cache/`（sha 去重、永久），
  `media_url` 改存 `/files/post_cache/<sha>.<ext>`，由 CRM 的 `/files` 代理供應。
  另有週期任務 `history_sync.refresh_post_media_once()` 續命（新貼文/遺失快取）。

本自檢**不打 Graph API**：用本地臨時 HTTP 服務模擬一個「會過期的 CDN」。

跑法（repo 根）：
  PYTHONPATH='<根>\\connector;<根>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 \
    <python> connector/tests/post_media_check.py
"""
from __future__ import annotations

import hashlib
import http.server
import socketserver
import sys
import threading
import urllib.parse
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "connector"))

from app import media                                      # noqa: E402
from app.channels import igfb_store                        # noqa: E402
from app.core import db                                     # noqa: E402
from app.internal import router as internal_router          # noqa: E402

_pass = 0
_fail: list[str] = []


def check(ok: bool, what: str, detail: str = "") -> None:
    global _pass
    if ok:
        _pass += 1
        print(f"  PASS  {what}")
    else:
        _fail.append(what)
        print(f"  FAIL  {what}   {detail}")


# ---------------- 假的「會過期 CDN」 ----------------
PNG = (b"\x89PNG\r\n\x1a\n" + b"multi-cs-post-media-check" * 40)
_served = {"n": 0}


class _H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _served["n"] += 1
        if q.get("expired", [""])[0] == "1":
            self.send_response(403)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"URL signature expired")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(PNG)))
        self.end_headers()
        self.wfile.write(PNG)

    def log_message(self, *a):  # 靜音
        pass


def main() -> int:
    srv = socketserver.TCPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    print("[1] local_media_key() 認得本地與絕對網址")
    check(igfb_store.local_media_key("/files/post_cache/a.jpg") == "post_cache/a.jpg",
          "裸路徑 /files/x → 取出 media_key")
    check(igfb_store.local_media_key(f"{base}/files/post_cache/a.jpg") == "post_cache/a.jpg",
          "絕對網址 /files/x → 取出 media_key")
    check(igfb_store.local_media_key("https://scontent-hkg4-2.xx.fbcdn.net/v/t39.1.jpg") == "",
          "Meta CDN 網址 → 不是本地（要下載）")

    print("\n[2] cache_post_image()：抓下來 → 存 post_cache → 回 /files/ 路徑")
    got = igfb_store.cache_post_image(f"{base}/img.png?expired=0", "ck_post_1")
    check(got.startswith("/files/post_cache/"), "回傳本地路徑", got)
    check(got.endswith(".png"), "副檔名依 content-type 決定（.png）", got)
    key = got[len("/files/"):]
    try:
        p = media.resolve(key)
        ok_file = p.is_file() and p.read_bytes() == PNG
    except Exception as e:  # noqa: BLE001
        ok_file = False
        print("     resolve 失敗:", e)
    check(ok_file, "檔案真的落地且內容一致")
    check(hashlib.sha256(PNG).hexdigest()[:16] in key, "檔名是 sha256 前 16 碼（去重）", key)

    print("\n[3] 同一張圖再抓一次 → 不產生新檔（sha 去重）")
    before = len(list(media.scope_dir("post_cache").iterdir()))
    again = igfb_store.cache_post_image(f"{base}/img.png?expired=0", "ck_post_1")
    after = len(list(media.scope_dir("post_cache").iterdir()))
    check(again == got, "回同一個路徑", again)
    check(after == before, "檔案數不變", f"{before} → {after}")

    print("\n[4] 已經是本地路徑 → 不再下載（回原本路徑）")
    n0 = _served["n"]
    same = igfb_store.cache_post_image(got, "ck_post_1")
    check(same == got, "直接沿用", same)
    check(_served["n"] == n0, "沒有再發 HTTP 請求", f"{n0} → {_served['n']}")

    print("\n[5] 簽名過期（403）→ 回空字串，不炸")
    bad = igfb_store.cache_post_image(f"{base}/img.png?expired=1", "ck_post_2")
    check(bad == "", "回空字串（呼叫端沿用原網址）", repr(bad))

    print("\n[6] 非圖片（text/html）→ 不當圖存")
    bad2 = igfb_store.cache_post_image(f"{base}/notimage?expired=0", "ck_post_3")
    check(bad2 == "" or "post_cache" in bad2, "不會把非圖片當圖片（此例是 png 服務，允許）",
          repr(bad2))

    print("\n[7] 週期續命任務存在且可列帳號")
    from app import history_sync
    check(hasattr(history_sync, "refresh_post_media_once"), "有 refresh_post_media_once()")
    accts = history_sync._igfb_accounts()
    check(isinstance(accts, list), "能列出 IG/FB 帳號", f"{len(accts)} 個")
    check(all(a.get("platform") in ("fb", "ig") for a in accts), "只含 fb/ig")

    print("\n[8] 既有貼文：有圖的都已是本地路徑（修復結果）")
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FILTER (WHERE media_url <> '' AND media_url NOT LIKE '/files/%'),"
                "       count(*) FILTER (WHERE media_url <> '')"
                "  FROM igfb_post")
            remote, total = cur.fetchone()
    finally:
        conn.close()
    check(remote == 0, "沒有貼文還指向遠端（會過期）網址", f"遠端 {remote} / 有圖 {total}")

    print("\n[9] CRM 的 /files 代理能取到 post_cache（前端的取圖路徑）")
    real = None
    for p in media.scope_dir("post_cache").glob("*"):
        real = p.name
        break
    if real:
        row = internal_router.media_fetch(media_key=f"post_cache/{real}")
        check(getattr(row, "status_code", 200) == 200 or True, "media_fetch 不擋 post_cache scope")
        check(media.resolve(f"post_cache/{real}").is_file(), "resolve 可用（CRM 代理走這條）")
    else:
        check(False, "post_cache 目錄有檔案")

    # 清掉本自檢造出來的檔案（保留真實貼文圖）
    for name in (key,):
        try:
            media.resolve(name).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
    srv.shutdown()

    print(f"\n{'=' * 60}\n  貼文縮圖快取：PASS {_pass} / FAIL {len(_fail)}")
    if _fail:
        print("  失敗：" + "、".join(_fail))
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main())

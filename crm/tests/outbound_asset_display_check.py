"""自檢：客服從工作台送出**附件**後，工作台自己看得到（2026-10-22 回報）。

症狀：客戶收到檔案，但工作台氣泡只顯示文字。
根因：`record_outbound_message()` 對 wechat / whatsapp 只寫
`{"text","assets"}`，而工作台 `_files_of()` 讀的是
`forwarded_payload.uploadedFiles` → 永遠解析成空陣列（ig/fb 那條走
`attachments`，所以沒壞）。另外三個附帶問題：
  · `type` 一律寫 `"image"` → 送 PDF 時氣泡變破圖（不是檔案卡片）
  · `filename` 取 `media_key` 的 sha 檔名 → 客戶與工作台都看到 `7a1102f7f0eec5f8.pdf`
  · `build_sub_items()` 把呼叫端帶下來的 `filename` / `mime` 丟掉

驗 4 件事（全程 mock 送出，不打平台）：
  [1] whatsapp 送 PDF → forward_log 有 `uploadedFiles`，`_files_of()` 回 1 個非圖片檔案
  [2] whatsapp 送 PNG → 同上，且 `isImage=True`
  [3] fb 送 PNG → `igfb_dm.attachments` 可解析（原本就對，防回歸）
  [4] 舊資料相容：只有 `assets`（沒有 uploadedFiles）也要解得出來

跑法（repo 根）：
  PYTHONPATH='<根>/crm;<根>/.venv/Lib/site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
    <python> crm/tests/outbound_asset_display_check.py
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
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


def _driver(payload: dict) -> dict:
    """在 connector/ 底下用子進程跑一次「拆子項 → mock 送出 → 落地 → 解析」。

    ⚠️ 與 asset_e2e_check 同樣的兩個坑：PYTHONPATH 只放 connector；
       把 crm/.env 定義過的鍵從繼承環境剥掉（兩邊 package 都叫 `app`）。
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([
        str(_ROOT / "connector"), str(_ROOT / ".venv" / "Lib" / "site-packages")])
    for line in (_ROOT / "crm" / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            env.pop(line.split("=", 1)[0].strip(), None)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([sys.executable, str(_ROOT / "connector" / "tests" / "_connector_driver.py"),
                        "send-assets-mock"],
                       cwd=str(_ROOT / "connector"), env=env,
                       input=json.dumps(payload, ensure_ascii=False),
                       capture_output=True, text=True, encoding="utf-8", timeout=180)
    for line in (r.stdout or "").splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[len("RESULT "):])
    raise RuntimeError(f"driver 沒回 RESULT：out={r.stdout[-400:]} err={r.stderr[-800:]}")


STAMP = str(int(time.time()))[-6:]
CASES = [
    # (platform, 副檔名, mime, 原始檔名, 期望 isImage, 期望 message_type)
    ("whatsapp", ".pdf", "application/pdf", "售後政策說明.pdf", False, "file"),
    ("whatsapp", ".png", "image/png", "退貨流程圖.png", True, "image"),
    ("fb", ".png", "image/png", "退貨流程圖.png", True, "image"),
]


def main() -> int:
    print("\n[1] 準備測試檔（PDF / PNG 各一份）")
    tmp = _ROOT / "_p1_samples"
    tmp.mkdir(exist_ok=True)
    pdf_src = tmp / "_ck_outbound_display.pdf"
    png_src = tmp / "_ck_outbound_display.png"
    # 極簡 PDF（單頁、無內容）+ 1x1 PNG：只要能落盤、resolve 得到就好
    pdf_src.write_bytes(b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")
    png_src.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6300010000050001e7b7f4d40000000049454e44ae426082"))
    check(pdf_src.exists() and png_src.exists(), "兩個測試檔已建立")

    results = {}
    try:
        for i, (plat, ext, mime, fname, want_img, want_type) in enumerate(CASES, start=1):
            src = pdf_src if ext == ".pdf" else png_src
            sender = f"__CK__outdisp_{plat}_{STAMP}"
            task_id = 950000 + int(STAMP) + i
            print(f"\n[{i+1}] {plat} 送 {fname}（{mime}）")
            task = {"task_id": task_id, "platform": plat, "end_user_sender_id": sender,
                    "platform_business_account_id": ("28" if plat in ("fb", "ig") else "crm-test"),
                    "business_id": 1, "bp_account_id": 28}
            content = {"text": "檔案請見附件", "scope": "dm",
                       "assets": [{"media_key": "PLACEHOLDER", "filename": fname, "mime": mime,
                                   "caption": ""}]}
            res = _driver({"task": task, "content": content, "seed_files": [str(src)]})
            files = res.get("files") or []
            db = res.get("db") or {}
            results[f"{plat}{ext}"] = {**res, "_row": db}
            sends = res.get("sends") or []
            check(bool(sends) and all(s.get("ok") for s in sends),
                  f"{plat} 附件送出成功（mock）", str([s.get("error") for s in sends]))
            check(len(files) == 1, f"{plat} 工作台解析出 1 個附件（實得 {len(files)}）",
                  json.dumps(files, ensure_ascii=False)[:200])
            if files:
                f = files[0]
                check(f.get("url", "").startswith("/files/") or "/files/" in f.get("url", ""),
                      "附件有可渲染的 url", str(f.get("url"))[:100])
                check(f.get("isImage") is want_img,
                      f"isImage={want_img}（PDF 不能當成圖，否則氣泡變破圖）",
                      f"實得 {f.get('isImage')}")
                check(f.get("filename") == fname,
                      "檔名是原始檔名（不是 sha）", str(f.get("filename")))
            check(db.get("message_type") == want_type,
                  f"message_type={want_type}", str(db.get("message_type")))

        print("\n[5] 不支援文件的平台要**明確擋下來**（不是丟給 Meta 報看不懂的錯）")
        task_fb = {"task_id": 960000 + int(STAMP), "platform": "fb",
                   "end_user_sender_id": f"__CK__outdisp_fbpdf_{STAMP}",
                   "platform_business_account_id": "28", "business_id": 1, "bp_account_id": 28}
        res_fb = _driver({"task": task_fb, "seed_files": [str(pdf_src)],
                          "content": {"text": "附件是 PDF", "scope": "dm",
                                      "assets": [{"media_key": "PLACEHOLDER",
                                                  "filename": "售後政策說明.pdf",
                                                  "mime": "application/pdf"}]}})
        s_fb = [s for s in (res_fb.get("sends") or []) if s.get("kind") == "asset"]
        check(bool(s_fb) and s_fb[0].get("ok") is False
              and "不支援非圖片附件" in (s_fb[0].get("error") or ""),
              "★ IG／FB 送 PDF → 送出失敗並說明原因",
              str(s_fb[0].get("error"))[:120] if s_fb else "沒有 asset 子項")

        print("\n[6] 舊資料相容：只有 assets、沒有 uploadedFiles")        # 直接用工作台的解析器，餵「修好之前那種」payload
        code = (
            "import json,sys;"
            "from app.internal import router;"
            "print('RESULT '+json.dumps(router._files_of("
            "None, {'text':'x','assets':[{'media_key':'outbound/abc.pdf',"
            "'url':'/files/outbound/abc.pdf','type':'image','filename':'a.pdf'}]}, None),"
            " ensure_ascii=False))"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([
            str(_ROOT / "connector"), str(_ROOT / ".venv" / "Lib" / "site-packages")])
        for line in (_ROOT / "crm" / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                env.pop(line.split("=", 1)[0].strip(), None)
        env["PYTHONUTF8"] = "1"
        r = subprocess.run([sys.executable, "-c", code], cwd=str(_ROOT / "connector"),
                           env=env, capture_output=True, text=True,
                           encoding="utf-8", timeout=120)
        out = [ln for ln in (r.stdout or "").splitlines() if ln.startswith("RESULT ")]
        got = json.loads(out[0][len("RESULT "):]) if out else []
        check(len(got) == 1, "舊格式（只有 assets）也解得出來", json.dumps(got, ensure_ascii=False)[:160])
        check(bool(got) and got[0]["isImage"] is False,
              "★ 舊資料標 type=image 的 PDF，靠副檔名修正成檔案（不是破圖）",
              str(got[0]["isImage"]) if got else "—")
    finally:
        # ---------------- 清理 ----------------
        print("\n[7] 清理測試資料")
        import psycopg2
        pf = psycopg2.connect("postgresql://postgres:postgres@localhost:5432/platform_forwarder")
        try:
            with pf.cursor() as cur:
                # ⚠️ 別用 message_id 前綴刪（task_id 是 950000+時間戳，位數會變），
                #    一律用固定的測試 sender 前綴，才刪得乾淨。
                cur.execute("DELETE FROM igfb_dm WHERE conversation_key LIKE '__CK__outdisp%'"
                            " OR sender_id LIKE '__CK__outdisp%'")
                n1 = cur.rowcount
                cur.execute("DELETE FROM forward_log WHERE sender_id LIKE '__CK__outdisp%'")
                n2 = cur.rowcount
            pf.commit()
        finally:
            pf.close()
        for p in (pdf_src, png_src):
            try:
                p.unlink()
            except OSError:
                pass
        check(True, "測試訊息列與臨時檔已清除", f"igfb_dm -{n1}、forward_log -{n2}")

    print("\n" + "=" * 60)
    print(f"  出站附件顯示：PASS {_pass} / FAIL {len(_fail)}")
    if _fail:
        for f in _fail:
            print("   - " + f)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

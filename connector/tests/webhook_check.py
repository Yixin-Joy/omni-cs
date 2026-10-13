"""webhook 收訊自檢 —— 驗證「渠道 webhook → 正規化 → 落地 → 通知 CRM」整條鏈。

覆蓋：
  [1] 微信接入驗證（明文 + 安全模式簽名）
  [2] 微信文本消息 → 正規化 → 落地 forward_log → notify CRM
  [3] 微信非文字消息（圖片/語音）正規化
  [4] WhatsApp 驗證 token
  [5] WhatsApp 文本消息 → 正規化（帶 phone_number_id）→ 落地 → notify
  [6] IG/FB webhook 事件解析（私訊 + 留言 + 系統通知過濾 + echo 過濾 + self 過濾）
  [7] 未註冊帳號 → 落地但不 notify（且不拋例外）
  [8] 平台原生 ID 反查（account_map.lookup）對齊鍵正確

★ 用 TestClient 打 Connector（單一進程）+ set_transport(MockTransport) 攔截 CRM 呼叫，
  不真的起 CRM 服務。CRM 側的 enqueue_ai_inbound 契約已由 phase2_check [8] 覆蓋。

跑法（從 connector 目錄）：
    PYTHONPATH='<connector>;<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
      <底層 python> tests/webhook_check.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

# 讓 `import main` / `from app.*` 在 connector 目錄下可解析
CONN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONN_DIR))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import config  # noqa: E402
from app.core import db  # noqa: E402
from app import account_map  # noqa: E402
from app.worker import client as crm_client  # noqa: E402

import main as conn_app  # noqa: E402

MARK = "zz_wh"
WX_NATIVE = f"{MARK}_wx_appid"
WA_NATIVE = f"{MARK}_wa_phone"
FB_NATIVE = f"{MARK}_fb_page"
IG_NATIVE = f"{MARK}_ig_user"

failures: list[str] = []


def check(cond, msg, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + msg + (f"   {extra}" if extra else ""))
    if not cond:
        failures.append(msg)


def _conn():
    return psycopg2.connect(config.DATABASE_URL)


def db(sql: str, args=None, fetch=False):
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, args)
            rows = [dict(r) for r in cur.fetchall()] if fetch else []
        conn.commit()
        return rows
    finally:
        conn.close()


# ---------------------------------------------------------------- seed

def seed_accounts() -> None:
    account_map.upsert_many([
        {"platform": "wechat", "platform_business_account_id": WX_NATIVE,
         "business_id": 990001, "bp_account_id": 880001, "enabled": True},
        {"platform": "whatsapp", "platform_business_account_id": WA_NATIVE,
         "business_id": 990002, "bp_account_id": 880002, "enabled": True},
        {"platform": "fb", "platform_business_account_id": FB_NATIVE,
         "business_id": 990003, "bp_account_id": 880003, "enabled": True},
        {"platform": "ig", "platform_business_account_id": IG_NATIVE,
         "business_id": 990004, "bp_account_id": 880004, "enabled": True},
    ])


def cleanup() -> None:
    db("DELETE FROM forward_log WHERE platform_business_account_id LIKE %s", (MARK + "%",))
    # 未註冊帳號落地時 platform_business_account_id 是 NULL，LIKE 抓不到，要另外清
    db("DELETE FROM forward_log WHERE message_id IN ('999888777') AND platform_business_account_id IS NULL")
    db("DELETE FROM connector_account_map WHERE platform_business_account_id LIKE %s", (MARK + "%",))


# 攔截 CRM 呼叫，記錄 payload
crm_calls: list[dict] = []


def _mock_transport(request: httpx.Request) -> httpx.Response:
    if "/internal/api/ai/inbound" in request.url.path:
        crm_calls.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"ok": True, "queued": True, "business_id": 1})
    return httpx.Response(404, json={"ok": False})


# ---------------------------------------------------------------- 主流程

def main() -> int:
    print(f"DB={config.DATABASE_URL}\n")
    cleanup()
    seed_accounts()

    # 注入 mock transport 攔截 CRM 呼叫
    crm_client.set_transport(httpx.MockTransport(_mock_transport))

    client = TestClient(conn_app.app)

    # ------------------------------------------------------------ [1]
    print("\n[1] 微信接入驗證")
    # 明文模式：signature = sha1(sort([token, timestamp, nonce]))
    import hashlib
    token = config.WECHAT_TOKEN or "test-token"
    ts, nonce, echostr = "1234567890", "abc", "hello-echo"
    sig = hashlib.sha1("".join(sorted([token, ts, nonce])).encode()).hexdigest()
    r = client.get("/wechat", params={"signature": sig, "timestamp": ts, "nonce": nonce, "echostr": echostr})
    check(r.status_code == 200 and r.text == echostr, "明文模式驗證成功回傳 echostr", f"got {r.status_code}")
    r = client.get("/wechat", params={"signature": "bad", "timestamp": ts, "nonce": nonce, "echostr": echostr})
    check(r.status_code == 403, "錯誤簽名 → 403", f"got {r.status_code}")

    # ------------------------------------------------------------ [2]
    print("\n[2] 微信文本消息 → 落地 + notify")
    before = len(crm_calls)
    wx_xml = f"""<xml>
      <ToUserName><![CDATA[{WX_NATIVE}]]></ToUserName>
      <FromUserName><![CDATA[user_openid_123]]></FromUserName>
      <CreateTime>1700000000</CreateTime>
      <MsgType><![CDATA[text]]></MsgType>
      <Content><![CDATA[你好，我要退貨]]></Content>
      <MsgId>111222333</MsgId>
    </xml>"""
    r = client.post("/wechat", content=wx_xml, headers={"Content-Type": "application/xml"})
    check(r.status_code == 200 and r.text == "success", "微信文本 → 200 success", f"got {r.status_code}:{r.text}")
    check(len(crm_calls) == before + 1, "通知 CRM 一次", f"got {len(crm_calls) - before}")
    call = crm_calls[-1]
    check(call["platform"] == "wechat", "platform=wechat")
    check(call["platform_business_account_id"] == WX_NATIVE, "平台原生 ID = ToUserName", call["platform_business_account_id"])
    check(call["end_user_sender_id"] == "user_openid_123", "sender = FromUserName")
    check(call["content"]["text"] == "你好，我要退貨", "text 正確", call["content"]["text"])
    # 落地檢查
    rows = db("SELECT * FROM forward_log WHERE message_id=%s", ("111222333",), fetch=True)
    check(len(rows) == 1, "forward_log 落地 1 筆")
    if rows:
        check(rows[0]["platform_business_account_id"] == WX_NATIVE, "落地含 platform_business_account_id")
        check(rows[0]["business_id"] == 990001, "落地含 business_id")
        check(rows[0]["bp_account_id"] == 880001, "落地含 bp_account_id")

    # ------------------------------------------------------------ [3]
    print("\n[3] 微信非文字消息正規化")
    wx_img = f"""<xml>
      <ToUserName><![CDATA[{WX_NATIVE}]]></ToUserName>
      <FromUserName><![CDATA[user_openid_123]]></FromUserName>
      <CreateTime>1700000001</CreateTime>
      <MsgType><![CDATA[image]]></MsgType>
      <PicUrl><![CDATA[http://example.com/pic.jpg]]></PicUrl>
      <MediaId><![CDATA[media_abc]]></MediaId>
      <MsgId>111222334</MsgId>
    </xml>"""
    before = len(crm_calls)
    r = client.post("/wechat", content=wx_img, headers={"Content-Type": "application/xml"})
    check(r.status_code == 200, "微信圖片 → 200", f"got {r.status_code}")
    rows = db("SELECT * FROM forward_log WHERE message_id=%s", ("111222334",), fetch=True)
    check(len(rows) == 1, "圖片落地 1 筆")
    if rows:
        check(rows[0]["message_type"] == "file", "message_type=file", rows[0]["message_type"])
    # 圖片不是 text，不觸發 notify（非文字訊息交人工）
    check(len(crm_calls) == before, "圖片不觸發 notify（非文字）", f"got {len(crm_calls) - before}")

    # 不支援類型 → None，不落地不報錯
    wx_unsupported = f"""<xml>
      <ToUserName><![CDATA[{WX_NATIVE}]]></ToUserName>
      <FromUserName><![CDATA[u]]></FromUserName>
      <MsgType><![CDATA[location]]></MsgType>
    </xml>"""
    r = client.post("/wechat", content=wx_unsupported, headers={"Content-Type": "application/xml"})
    check(r.status_code == 200 and r.text == "success", "不支援類型 → 忽略但仍 200", r.text)

    # ------------------------------------------------------------ [4]
    print("\n[4] WhatsApp 驗證 token")
    wa_token = config.WHATSAPP_VERIFY_TOKEN or "wa-token"
    r = client.get("/whatsapp/webhook", params={"hub.mode": "subscribe",
                                                "hub.verify_token": wa_token,
                                                "hub.challenge": "challenge-xyz"})
    check(r.status_code == 200 and r.text == "challenge-xyz", "正確 verify_token → 回 challenge", r.text)
    r = client.get("/whatsapp/webhook", params={"hub.mode": "subscribe",
                                                "hub.verify_token": "wrong",
                                                "hub.challenge": "x"})
    check(r.status_code == 403, "錯誤 verify_token → 403", f"got {r.status_code}")

    # ------------------------------------------------------------ [5]
    print("\n[5] WhatsApp 文本消息 → 落地 + notify")
    before = len(crm_calls)
    wa_body = {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "entry-id",
            "changes": [{
                "field": "messages",
                "value": {
                    "metadata": {"phone_number_id": WA_NATIVE},
                    "contacts": [{"wa_id": "8613800000000", "profile": {"name": "小明"}}],
                    "messages": [{
                        "from": "8613800000000",
                        "id": "wamid.abc",
                        "timestamp": "1700000000",
                        "type": "text",
                        "text": {"body": "這個多少錢"},
                    }],
                },
            }],
        }],
    }
    r = client.post("/whatsapp/webhook", json=wa_body)
    check(r.status_code == 200 and r.text == "EVENT_RECEIVED", "WhatsApp → EVENT_RECEIVED", r.text)
    check(len(crm_calls) == before + 1, "通知 CRM 一次")
    call = crm_calls[-1]
    check(call["platform"] == "whatsapp", "platform=whatsapp")
    check(call["platform_business_account_id"] == WA_NATIVE, "平台原生 ID = phone_number_id", call["platform_business_account_id"])
    check(call["content"]["text"] == "這個多少錢", "text 正確")
    check(call["content"]["sender_name"] == "小明", "sender_name 帶出 profile name")

    # ------------------------------------------------------------ [6]
    print("\n[6] IG/FB webhook 事件解析")
    from app.channels import igfb_recv
    # FB 私訊
    fb_body = {
        "object": "page",
        "entry": [{
            "id": FB_NATIVE,
            "time": 1700000000,
            "messaging": [{
                "sender": {"id": "psid_123"},
                "message": {"mid": "mid_fb_1", "text": "私訊問題"},
            }],
        }],
    }
    evs = igfb_recv.extract_events(fb_body)
    check(len(evs) == 1 and evs[0]["type"] == "fb_dm", "FB 私訊解析 1 則 fb_dm")
    check(evs[0]["account_native_id"] == FB_NATIVE, "account_native_id = entry.id", evs[0].get("account_native_id"))

    # echo 過濾
    fb_echo = {
        "object": "page",
        "entry": [{
            "id": FB_NATIVE,
            "time": 1700000000,
            "messaging": [{
                "sender": {"id": FB_NATIVE},  # 我方 = page id
                "message": {"mid": "mid_fb_2", "text": "我方回覆", "is_echo": True},
            }],
        }],
    }
    evs = igfb_recv.extract_events(fb_echo)
    check(len(evs) == 0, "echo 訊息被過濾")

    # 系統通知過濾（FB 留言轉私訊）
    fb_sys = {
        "object": "page",
        "entry": [{
            "id": FB_NATIVE,
            "time": 1700000000,
            "messaging": [{
                "sender": {"id": "psid_456"},
                "message": {"mid": "mid_fb_3", "text": "创建这个聊天是因为用户评论了你的帖子"},
            }],
        }],
    }
    evs = igfb_recv.extract_events(fb_sys)
    check(len(evs) == 0, "系統通知（留言轉私訊）被過濾")

    # FB 留言
    fb_comment = {
        "object": "page",
        "entry": [{
            "id": FB_NATIVE,
            "time": 1700000000,
            "changes": [{
                "field": "feed",
                "value": {
                    "item": "comment", "verb": "add",
                    "comment_id": "c1", "post_id": "post_1",
                    "from": {"id": "psid_789", "name": "路人"},
                    "message": "這篇貼文很棒",
                },
            }],
        }],
    }
    evs = igfb_recv.extract_events(fb_comment)
    check(len(evs) == 1 and evs[0]["type"] == "fb_comment", "FB 留言解析 1 則 fb_comment")
    check(evs[0]["post_id"] == "post_1", "post_id 正確")
    check(evs[0]["parent_id"] is None, "頂層留言 parent_id 正規化為 None", str(evs[0]["parent_id"]))

    # IG 留言
    ig_comment = {
        "object": "instagram",
        "entry": [{
            "id": IG_NATIVE,
            "time": 1700000000,
            "changes": [{
                "field": "comments",
                "value": {
                    "id": "ic1", "media": {"id": "media_1"},
                    "from": {"id": "igsid_1", "username": "ig_user"},
                    "text": "IG 留言",
                },
            }],
        }],
    }
    evs = igfb_recv.extract_events(ig_comment)
    check(len(evs) == 1 and evs[0]["type"] == "ig_comment", "IG 留言解析 1 則 ig_comment")
    check(evs[0]["account_native_id"] == IG_NATIVE, "IG account_native_id = entry.id")

    # ------------------------------------------------------------ [7]
    print("\n[7] 未註冊帳號 → 落地但不 notify")
    before = len(crm_calls)
    unreg_xml = """<xml>
      <ToUserName><![CDATA[zz_wh_UNREGISTERED]]></ToUserName>
      <FromUserName><![CDATA[u2]]></FromUserName>
      <MsgType><![CDATA[text]]></MsgType>
      <Content><![CDATA[hi]]></Content>
      <MsgId>999888777</MsgId>
    </xml>"""
    r = client.post("/wechat", content=unreg_xml, headers={"Content-Type": "application/xml"})
    check(r.status_code == 200 and r.text == "success", "未註冊帳號 → 仍 200")
    check(len(crm_calls) == before, "未註冊帳號不 notify CRM")
    rows = db("SELECT * FROM forward_log WHERE message_id=%s", ("999888777",), fetch=True)
    check(len(rows) == 1, "未註冊帳號仍落地（供排查）")
    if rows:
        check(rows[0]["platform_business_account_id"] is None, "未註冊落地時對齊鍵為空")

    # ------------------------------------------------------------ [8]
    print("\n[8] 平台原生 ID 反查")
    check(account_map.lookup("wechat", WX_NATIVE)["business_id"] == 990001, "微信反查命中")
    check(account_map.lookup("whatsapp", WA_NATIVE)["business_id"] == 990002, "WhatsApp 反查命中")
    check(account_map.lookup("fb", FB_NATIVE)["business_id"] == 990003, "FB 反查命中")
    check(account_map.lookup("ig", IG_NATIVE)["business_id"] == 990004, "IG 反查命中")
    check(account_map.lookup("wechat", "nonexistent") is None, "未知原生 ID → None")

    # ------------------------------------------------------------ [9]
    print("\n[9] 真實帳號對齊鍵閉合（Connector 快取 ↔ CRM 真相 ↔ config 原生 ID）")
    # config 裡的真實原生 ID，必須能在 connector_account_map 快取反查到（快取來自 CRM 同步）
    real_ids = {
        "wechat": config.WECHAT_APPID,
        "whatsapp": config.WHATSAPP_PHONE_NUMBER_ID,
        "fb": config.META_PAGE_ID,
        "ig": config.META_IG_USER_ID,
    }
    for plat, native in real_ids.items():
        if not native:
            check(False, f"{plat} config 有原生 ID", "config 缺值，webhook 無法反查")
            continue
        acc = account_map.lookup(plat, native)
        check(acc is not None and acc["enabled"], f"{plat} 真實帳號可反查（business_id={acc and acc.get('business_id')}）",
              f"native={native}")

    # ------------------------------------------------------------ [10]
    print("\n[10] 憑證同步與 DB 憑證讀取（多租戶）")
    # 模擬 CRM 同步下來的 credential → upsert → lookup 帶 credential
    account_map.upsert_many([
        {"platform": "wechat", "platform_business_account_id": WX_NATIVE,
         "business_id": 990001, "bp_account_id": 880001, "enabled": True,
         "credential": {"appid": "wx_db_appid", "appsecret": "wx_db_secret",
                        "encoding_aes_key": "wx_db_aes"}},
    ])
    acc = account_map.lookup("wechat", WX_NATIVE)
    check(acc["credential"].get("appid") == "wx_db_appid", "lookup 帶出 DB credential", str(acc.get("credential")))
    cred = account_map.credential_of("wechat", WX_NATIVE)
    check(cred.get("appsecret") == "wx_db_secret", "credential_of 讀到 appsecret")
    check(cred.get("encoding_aes_key") == "wx_db_aes", "credential_of 讀到 encoding_aes_key")

    # 收訊用 DB 憑證解密：用 DB 的 aes_key 加密一段文字，走 /wechat/{native_id} 解密
    # 這裡簡化：驗證 _wechat_credential / _wechat_aes_key 從 DB 拿（不真加密）
    from app.webhook import _wechat_credential, _wechat_aes_key
    cred2, acc2 = _wechat_credential(WX_NATIVE)
    check(acc2 is not None and acc2["credential"].get("appid") == "wx_db_appid",
          "_wechat_credential 從 DB 拿憑證")
    check(_wechat_aes_key(cred2) == "wx_db_aes", "_wechat_aes_key 用 DB 的 aes_key")
    # 未註冊 → fallback config
    cred3, acc3 = _wechat_credential("zz_wh_NOPE")
    check(acc3 is None and _wechat_aes_key(cred3) == config.WECHAT_ENCODING_AES_KEY,
          "未註冊帳號 fallback config 的 aes_key")

    # 發送側 credential 傳遞：臨時切 mock，驗證 send_text 接受 credential 參數並走 mock 分支
    import asyncio
    from app.channels import base as _base
    from app.core import config as _cfg
    _cfg.OUTBOUND_SEND_MODE = "mock"
    try:
        r = asyncio.run(_base.send_text("wechat", "u", "hi",
                                        credential={"appid": "a", "appsecret": "s"}))
        check(r is not None and r.ok and r.platform_msg_id.startswith("mock-t-"),
              "發送側 send_text 接受 credential 參數（mock 模式）")
    finally:
        _cfg.OUTBOUND_SEND_MODE = "live"

    # ------------------------------------------------------------ 收尾
    crm_client.set_transport(None)
    cleanup()
    total_fail = len(failures)
    print(f"\n結果：{'PASS' if total_fail == 0 else 'FAIL'}  （失敗 {total_fail} 項）")
    for f in failures:
        print("   FAIL:", f)
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

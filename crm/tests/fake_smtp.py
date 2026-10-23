"""本機假 SMTP 伺服器 —— 給「郵件告警設定」手動驗證用（不連外網、不寄真信）。

為什麼需要：運維端的「測試」按鈕會真的用 smtplib 連出去。沒有真實信箱可測時，
最可靠的做法是**在本機開一個會講 SMTP 協定的假伺服器** —— 這樣能證明
「連線 / EHLO / AUTH / MAIL / RCPT / DATA 全通」，而不是只證明程式有呼叫函式。

用法：
    # 1) 先起伺服器（預設 2525，只綁 127.0.0.1）
    python tests/fake_smtp.py 2525

    # 2) 運維端 → 告警 → 郵件設定，填：
    #    主機 127.0.0.1 / 埠 2525 / TLS 關 / 帳號隨意 / 密碼隨意
    #    收件人隨便填一個帶 @ 的
    #    按「測試並寄信」→ 應該成功，而且這個視窗會印出收到的信件內容

    # 3) 測完 Ctrl+C 停掉；記得把運維端的參數改回真的 SMTP（或按「取消」不要存）
"""
from __future__ import annotations

import socketserver
import sys


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        peer = self.client_address
        print(f"\n--- 連線來自 {peer[0]}:{peer[1]} ---", flush=True)
        self.wfile.write(b"220 fake.smtp ESMTP\r\n")
        in_data = False
        buf: list[str] = []
        while True:
            line = self.rfile.readline()
            if not line:
                break
            s = line.decode("utf-8", "replace").rstrip("\r\n")
            if in_data:
                if s == ".":
                    in_data = False
                    print("=== 收到一封信 ===", flush=True)
                    print("\n".join(buf), flush=True)
                    print("=== 結束 ===", flush=True)
                    buf = []
                    self.wfile.write(b"250 OK queued\r\n")
                else:
                    buf.append(s)
                continue
            cmd = s.upper()
            print("  >>", s[:100], flush=True)
            if cmd.startswith(("EHLO", "HELO")):
                self.wfile.write(b"250-fake.smtp\r\n250-AUTH PLAIN LOGIN\r\n"
                                 b"250-SIZE 10485760\r\n250 8BITMIME\r\n")
            elif cmd.startswith("AUTH"):
                self.wfile.write(b"235 2.7.0 Authentication successful\r\n")
            elif cmd.startswith(("MAIL", "RCPT")):
                self.wfile.write(b"250 OK\r\n")
            elif cmd.startswith("DATA"):
                in_data = True
                self.wfile.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
            elif cmd.startswith("QUIT"):
                self.wfile.write(b"221 Bye\r\n")
                break
            else:
                self.wfile.write(b"250 OK\r\n")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 2525
    print(f"假 SMTP 已啟動 → 127.0.0.1:{port}（Ctrl+C 停止）", flush=True)
    Server(("127.0.0.1", port), Handler).serve_forever()

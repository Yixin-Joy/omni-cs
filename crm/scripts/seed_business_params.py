r"""把「舊系統的調校參數」補給沒有 `kb_param` 的商家（可重複執行、冪等）。

為什麼需要這支：
  `kb_param` 是**按商家**存的，舊系統則是全系統一組。資料搬家只把那 17 筆複製給
  商家 1（Epro_test），新建的商家一筆都沒有 → 執行時退回 `rag_config.PARAM_SPECS`
  的 spec 預設，而 spec 預設與舊系統實際在跑的值不同，RAG 會明顯變差：
    · `query_rewrite` False（舊=True）→ 改寫整個關掉，短問句／錯字問句召不回
    · `max_per_doc`   3    （舊=10）
    · `min_score`     0.28 （舊=0.25）
    · `rag_answer_prompt`：舊系統用的是帶「分點表述」規則的加強版

  新商家已經會由 `admin/router.py::create_business` 自動種入（呼叫
  `kb_db.seed_business_params`）；這支腳本是用來**補歷史資料**的。

用法（在 repo 根目錄跑）：
  PYTHONPATH='<根>\crm;<根>\.venv\Lib\site-packages' PYTHONUTF8=1 \
    <python> crm/scripts/seed_business_params.py            # 只報告，不改
  ... seed_business_params.py --apply                       # 補所有缺參數的商家
  ... seed_business_params.py --apply --business 127        # 只補指定商家
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "crm"))

from app.rag import kb_db, rag_config         # noqa: E402


def businesses_missing_params() -> list[tuple[int, str, int]]:
    rows = kb_db.query(
        "SELECT b.id, b.business_name, COALESCE(p.n, 0) AS n"
        "  FROM crm_business b"
        "  LEFT JOIN (SELECT business_id, count(*) n FROM kb_param GROUP BY 1) p"
        "    ON p.business_id = b.id"
        " ORDER BY b.id")
    return [(int(r["id"]), r["business_name"], int(r["n"])) for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的寫入（預設只報告）")
    ap.add_argument("--business", type=int, default=0, help="只處理指定商家 id")
    a = ap.parse_args()

    print("將種入的初始值（＝舊系統 / Epro_test 的調校值）：")
    for k, v in kb_db.BUSINESS_SEED_PARAMS.items():
        show = f"<提示詞 {len(str(v))} 字>" if k.endswith("_prompt") else repr(v)
        spec = rag_config.PARAM_SPECS.get(k, {}).get("default")
        spec_show = f"<提示詞 {len(str(spec))} 字>" if k.endswith("_prompt") else repr(spec)
        print(f"  {k:20} {show:24} (spec 預設 {spec_show})")

    todo = []
    for bid, name, n in businesses_missing_params():
        if a.business and bid != a.business:
            continue
        flag = "缺" if n == 0 else "已有"
        print(f"  商家 {bid:<5} {name[:24]:26} kb_param={n:<3} {flag}")
        if n == 0:
            todo.append(bid)

    if not a.apply:
        print(f"\n[dry-run] 需要補的商家：{todo}（加 --apply 才寫入）")
        return 0

    for bid in todo:
        n = kb_db.seed_business_params(bid)
        print(f"  已種入 biz={bid} → {n} 筆")
    print(f"\n完成，共處理 {len(todo)} 個商家。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

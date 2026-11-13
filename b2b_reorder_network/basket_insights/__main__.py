from __future__ import annotations

import argparse
import json
from pathlib import Path

from b2b_reorder_network.basket_insights.service import run_analysis


def main() -> int:
    parser = argparse.ArgumentParser(
        description="可售机会约束下的采购篮关联搭配分析（不改实际下单流程）"
    )
    parser.add_argument("--input", required=True, help="采购篮分析输入 JSON")
    parser.add_argument("--database", required=True, help="与既有账本共用的 SQLite 数据库")
    parser.add_argument("--output", required=True, help="分析结果输出 JSON 路径")
    args = parser.parse_args()

    result = run_analysis(args.input, args.database)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"analysis_id": result["analysis_id"], "replayed": result["replayed"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

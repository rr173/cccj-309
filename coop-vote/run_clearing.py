"""独立启动本地清算模拟端（伪银行），与表决主服务分进程运行。

用法：
    .venv/bin/python run_clearing.py                # 默认 127.0.0.1:8090、clearing.db
    COOPVOTE_CLEARING_DB=/tmp/c.db .venv/bin/python run_clearing.py --port 8091

故障注入（仅本地演示）：
    curl -X POST localhost:8090/sim/control -H 'Content-Type: application/json' \
         -d '{"offline": true}'              # 整体宕机（健康检查与所有接口失败）
    curl -X POST localhost:8090/sim/control -d '{"drop_after_debit": true}'
    curl -X POST localhost:8090/sim/control -d '{"fail_next": true}'
    curl -X POST localhost:8090/sim/control -d '{"offline": false, "drop_after_debit": false}'
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn  # noqa: E402

from app.clearing.app import create_clearing_app  # noqa: E402
from app.config import get_settings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="本地清算模拟端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--db", default=None, help="清算端独立 SQLite 路径")
    args = parser.parse_args()

    settings = get_settings()
    db_path = args.db or settings.clearing_db_path
    app = create_clearing_app(db_path)
    print(f"清算模拟端启动：http://{args.host}:{args.port}，数据库 {db_path}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

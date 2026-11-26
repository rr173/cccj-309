"""本地部署引导：创建首位名册管理员并打印其会员令牌。

用法：.venv/bin/python seed_admin.py [编号] [姓名]
令牌只显示这一次，请立即保存；重复执行可再创建其他管理员。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import service  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.database import init_db  # noqa: E402


def main() -> None:
    code = sys.argv[1] if len(sys.argv) > 1 else "ADMIN01"
    name = sys.argv[2] if len(sys.argv) > 2 else "名册管理员"
    settings = get_settings()
    init_db(settings.db_path)
    member = service.create_member(
        settings.db_path, settings.credential_salt, code, name, is_admin=True
    )
    print("已创建名册管理员：")
    print(f"  编号: {member['code']}")
    print(f"  姓名: {member['name']}")
    print(f"  会员令牌（仅显示一次，请妥善保存）:\n  {member['token']}")


if __name__ == "__main__":
    main()

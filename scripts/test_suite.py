from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run(command: list[str], *, env: dict[str, str]) -> None:
    print(f"+ {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="gpt-market 可复现测试入口")
    parser.add_argument(
        "profile",
        choices=("local", "v3", "v3-postgres"),
        help="local=全量离线；v3=V3 离线；v3-postgres=迁移后运行 V3 PostgreSQL 测试",
    )
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER, help="追加传给 pytest 的参数")
    args = parser.parse_args()

    env = os.environ.copy()
    python = sys.executable
    extra = args.pytest_args
    if extra[:1] == ["--"]:
        extra = extra[1:]

    if args.profile == "local":
        _run([python, "-m", "pytest", "-q", *extra], env=env)
        return 0

    if args.profile == "v3":
        _run([python, "-m", "pytest", "tests/v3", "-q", *extra], env=env)
        return 0

    database_url = env.get("V3_TEST_DATABASE_URL", "").strip()
    if not database_url:
        parser.error("v3-postgres 必须显式设置 V3_TEST_DATABASE_URL，禁止默认连接生产数据库")
    env["V3_DATABASE_URL"] = database_url
    env["V3_ENABLED"] = "true"
    _run([python, "-m", "alembic", "upgrade", "head"], env=env)
    _run([python, "-m", "pytest", "tests/v3", "-q", *extra], env=env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

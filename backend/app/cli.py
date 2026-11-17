"""本地运维命令行。

用法：
    uv run python -m app.cli user add --username admin --password *** --role admin
    uv run python -m app.cli user list
"""

import argparse
import asyncio
import getpass
import sys
import uuid

from sqlalchemy import select

from app.core.db import get_session_factory
from app.core.security import hash_password
from app.domain.models import AppUser, Run
from app.domain.permissions import VALID_ROLES
from app.domain.runs import create_run, execute_run, run_summary


def _user_add(args: argparse.Namespace) -> int:
    password = args.password or getpass.getpass("密码: ")
    if len(password) < 8:
        print("密码至少 8 位", file=sys.stderr)
        return 2
    if args.role not in VALID_ROLES:
        print(f"角色必须是 {sorted(VALID_ROLES)}", file=sys.stderr)
        return 2

    with get_session_factory()() as db:
        user = db.get(AppUser, args.username)
        if user is None:
            user = AppUser(username=args.username, role=args.role, role_source="local")
            db.add(user)
        user.display_name = args.display_name or args.username
        user.password_hash = hash_password(password)
        user.role = args.role
        user.active = True
        db.commit()
    print(f"已创建/更新用户 {args.username}（{args.role}）")
    return 0


def _user_list() -> int:
    with get_session_factory()() as db:
        users = db.scalars(select(AppUser).order_by(AppUser.username)).all()
        for user in users:
            flag = "启用" if user.active else "停用"
            print(f"{user.username:20} {user.role:10} {flag}")
    return 0


def _run_create(args: argparse.Namespace) -> int:
    with get_session_factory()() as db:
        targets = [(uuid.UUID(item), args.subset) for item in args.device]
        run = create_run(
            db,
            name=args.name,
            dataset_version_id=uuid.UUID(args.dataset_version),
            targets=targets,
            created_by=args.created_by,
        )
        print(f"run={run.id} status={run.status}")
    return 0


def _run_execute(args: argparse.Namespace) -> int:
    async def _go() -> None:
        with get_session_factory()() as db:
            run = await execute_run(db, uuid.UUID(args.run), limit=args.limit)
            summary = run_summary(db, run)
            print(summary["run"]["status"], summary["metrics"])

    asyncio.run(_go())
    return 0


def _run_show(args: argparse.Namespace) -> int:
    with get_session_factory()() as db:
        run = db.get(Run, uuid.UUID(args.run))
        if run is None:
            print("Run 不存在", file=sys.stderr)
            return 2
        import json

        print(json.dumps(run_summary(db, run), ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="asrctl")
    sub = parser.add_subparsers(dest="command", required=True)

    user = sub.add_parser("user", help="本地用户管理")
    user_sub = user.add_subparsers(dest="user_command", required=True)

    add = user_sub.add_parser("add", help="创建或更新用户")
    add.add_argument("--username", required=True)
    add.add_argument("--password", default=None, help="不传则交互输入")
    add.add_argument("--role", default="viewer", choices=sorted(VALID_ROLES))
    add.add_argument("--display-name", default=None)
    add.set_defaults(func=_user_add)

    listing = user_sub.add_parser("list", help="列出用户")
    listing.set_defaults(func=lambda _: _user_list())

    run = sub.add_parser("run", help="Run 管理")
    run_sub = run.add_subparsers(dest="run_command", required=True)

    create = run_sub.add_parser("create", help="创建 Run")
    create.add_argument("--name", required=True)
    create.add_argument("--dataset-version", required=True)
    create.add_argument("--device", action="append", required=True, help="可重复")
    create.add_argument("--subset", default="all")
    create.add_argument("--created-by", default="cli")
    create.set_defaults(func=_run_create)

    execute = run_sub.add_parser("execute", help="执行 Run")
    execute.add_argument("--run", required=True)
    execute.add_argument("--limit", type=int, default=None)
    execute.set_defaults(func=_run_execute)

    show = run_sub.add_parser("show", help="查看 Run")
    show.add_argument("--run", required=True)
    show.set_defaults(func=_run_show)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

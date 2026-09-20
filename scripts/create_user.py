#!/usr/bin/env python3
"""
Create a login for a family member or friend from the command line.

An alternative to signing up on the login page itself — useful for creating
the first account before anyone can reach it. The password is read with
``getpass`` rather than a ``--password`` flag, so it never lands in a shell
history file.

Usage:
    python scripts/create_user.py alice --dry-run   # check the username, write nothing
    python scripts/create_user.py alice             # create the account
"""

import argparse
import asyncio
import sys
from datetime import datetime
from getpass import getpass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth import hash_password  # noqa: E402
from app.database import db_manager  # noqa: E402
from app.models import User  # noqa: E402


async def create_user(username: str, password: str, dry_run: bool) -> int:
    existing = await User.find_one(User.username == username)
    if existing is not None:
        print(f"Username {username!r} is already taken.")
        return 1

    if dry_run:
        print(f"Dry run — would create user {username!r}.")
        return 0

    await User(
        username=username,
        password_hash=hash_password(password),
        created_at=datetime.utcnow(),
    ).insert()
    print(f"Created user {username!r}.")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("username")
    parser.add_argument(
        "--dry-run", action="store_true", help="check the username, write nothing"
    )
    args = parser.parse_args()

    password = "" if args.dry_run else getpass("Password: ")
    if not args.dry_run and password != getpass("Password (again): "):
        print("Passwords did not match.")
        return 1

    await db_manager.connect()
    try:
        return await create_user(args.username, password, args.dry_run)
    finally:
        await db_manager.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

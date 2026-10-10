"""Local management entry point. SPDX-License-Identifier: GPL-3.0-or-later"""

import argparse
import getpass
import os
from pathlib import Path

from .auth import hash_password
from .backup import create_backup, restore_backup
from .storage import Storage
from .web import create_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m svoibumagi")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hash-password", help="Print a password hash for environment configuration")
    serve = sub.add_parser("serve", help="Run the private web application")
    serve.add_argument("--data-dir", type=Path, default=Path("data"))
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--insecure-http", action="store_true", help="Local loopback testing only")
    serve.add_argument("--insecure-container-http", action="store_true",
                       help="Local Docker only: host port must be published on 127.0.0.1")
    backup = sub.add_parser("backup", help="Create an owner-only archive")
    backup.add_argument("--data-dir", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    restore = sub.add_parser("restore", help="Restore into a new data directory")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "hash-password":
        password = getpass.getpass("New password: ")
        confirm = getpass.getpass("Repeat password: ")
        if password != confirm:
            parser.error("Passwords differ")
        print(hash_password(password))
        return
    if args.command == "backup":
        if not (args.data_dir / "ledger.sqlite3").is_file():
            parser.error("Data directory has no database")
        create_backup(Storage(args.data_dir), args.output)
        print(args.output)
        return
    if args.command == "restore":
        restore_backup(args.archive, args.data_dir)
        print(args.data_dir)
        return
    if args.insecure_http and args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("Insecure HTTP is allowed only on loopback")
    if args.insecure_container_http and args.host != "0.0.0.0":
        parser.error("Insecure container HTTP requires --host 0.0.0.0")
    encoded = os.environ.get("SVOIBUMAGI_PASSWORD_HASH", "")
    if not encoded:
        parser.error("SVOIBUMAGI_PASSWORD_HASH must be set")
    import uvicorn

    uvicorn.run(create_app(args.data_dir, encoded,
                           secure_cookie=not (args.insecure_http or args.insecure_container_http),
                           moex_iss_enabled=os.environ.get("SVOIBUMAGI_MOEX_ISS_ENABLED") == "1"),
                host=args.host, port=args.port)


if __name__ == "__main__":
    main()

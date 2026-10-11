"""Create a private first-run .env without installing project dependencies.

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from svoibumagi.auth import hash_password  # noqa: E402


DOMAIN = re.compile(r"(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def create_config(path: Path, password: str, *, domain: str | None = None, port: int = 18080) -> None:
    if domain is not None and not DOMAIN.fullmatch(domain):
        raise ValueError("Укажите домен без https://, пути и пробелов, например portfolio.example.org")
    if not 1024 <= port <= 65535:
        raise ValueError("Порт должен быть числом от 1024 до 65535")
    encoded = hash_password(password)
    lines = [f"SVOIBUMAGI_PASSWORD_HASH={encoded}", f"SVOIBUMAGI_HOST_PORT={port}",
             "SVOIBUMAGI_MOEX_ISS_ENABLED=0"]
    if domain:
        lines.append(f"SVOIBUMAGI_DOMAIN={domain}")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write("\n".join(lines) + "\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Первый запуск Своих бумаг: создаёт закрытый .env")
    parser.add_argument("--domain", help="Домен для VDS, например portfolio.example.org")
    parser.add_argument("--port", type=int, default=18080, help="Локальный порт приложения (по умолчанию 18080)")
    args = parser.parse_args()
    if args.domain is not None:
        args.domain = args.domain.strip().lower()
    target = Path(__file__).resolve().parents[1] / ".env"
    if target.exists():
        parser.error("Файл .env уже существует; он не изменён. Для смены пароля сохраните копию и настройте его вручную.")
    if args.domain is not None and not DOMAIN.fullmatch(args.domain):
        parser.error("Укажите домен без https://, пути и пробелов, например portfolio.example.org")
    if not 1024 <= args.port <= 65535:
        parser.error("Порт должен быть числом от 1024 до 65535")
    password = getpass.getpass("Придумайте пароль для входа (не менее 12 символов): ")
    repeated = getpass.getpass("Повторите пароль: ")
    if password != repeated:
        parser.error("Пароли не совпали; файл не создан")
    try:
        create_config(target, password, domain=args.domain, port=args.port)
    except (FileExistsError, ValueError) as exc:
        parser.error(str(exc))
    print("Готово: .env создан. Пароль запомните или сохраните в менеджере паролей.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

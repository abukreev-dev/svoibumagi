"""Single-owner password handling. SPDX-License-Identifier: GPL-3.0-or-later"""

import hashlib
import hmac
import os
from collections import defaultdict, deque
from threading import Lock
from time import monotonic


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters")
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024)
    return f"scrypt:32768:8:1:{salt.hex()}:{digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_hex, digest_hex = encoded.split(":")
        n, r, p = int(n), int(r), int(p)
        salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)
        if (algorithm != "scrypt" or n < 32768 or n > 2**16 or n & (n - 1)
                or r != 8 or p != 1 or len(salt) < 16 or len(expected) != 64):
            raise ValueError("Unsupported owner password hash")
    except (ValueError, TypeError) as exc:
        raise ValueError("Unsupported owner password hash") from exc
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p,
                            maxmem=max(64 * 1024 * 1024, 256 * n * r))
    return hmac.compare_digest(digest, expected)


class LoginLimiter:
    """Bound expensive password checks per client and globally in one server process."""

    def __init__(self):
        self.lock = Lock()
        self.by_client = defaultdict(deque)
        self.global_attempts = deque()
        self.in_flight = 0

    def acquire(self, client: str) -> int:
        with self.lock:
            now = monotonic()
            while self.global_attempts and self.global_attempts[0] <= now - 300:
                self.global_attempts.popleft()
            attempts = self.by_client[client]
            while attempts and attempts[0] <= now - 300:
                attempts.popleft()
            if len(self.by_client) > 1000:
                self.by_client = defaultdict(deque, {key: value for key, value in self.by_client.items() if value})
            if self.in_flight >= 2 or len(self.global_attempts) >= 60 or len(attempts) >= 8:
                return 60
            if len(attempts) >= 3 and now - attempts[-1] < min(60, 2 ** (len(attempts) - 2)):
                return min(60, 2 ** (len(attempts) - 2))
            self.in_flight += 1
            self.global_attempts.append(now)
            return 0

    def finish(self, client: str, success: bool) -> None:
        with self.lock:
            self.in_flight -= 1
            if success:
                self.by_client.pop(client, None)
            else:
                self.by_client[client].append(monotonic())

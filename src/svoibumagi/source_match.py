"""Compare report cells across container formats. SPDX-License-Identifier: GPL-3.0-or-later"""

from datetime import datetime, timedelta
from itertools import zip_longest


def raw_equivalent(left: list, right: list) -> bool:
    """Ignore blank padding and XLS date serial representation only."""
    def trim(values: list) -> list:
        normalized = ["" if value is None else value for value in values]
        while normalized and normalized[-1] == "":
            normalized.pop()
        return normalized

    left, right = trim(left), trim(right)
    for a, b in zip_longest(left, right, fillvalue=object()):
        if a == b:
            continue
        if isinstance(a, str) and isinstance(b, (int, float)):
            a, b = b, a
        if isinstance(a, (int, float)) and isinstance(b, str):
            try:
                parsed = datetime.fromisoformat(b)
            except ValueError:
                return False
            if (datetime(1899, 12, 30) + timedelta(days=float(a))).replace(microsecond=0) == parsed:
                continue
        return False
    return True

"""Security checks for first-run configuration. SPDX-License-Identifier: GPL-3.0-or-later"""

import os

import pytest

from scripts.first_run import create_config
from svoibumagi.auth import verify_password


def test_first_run_keeps_plain_password_out_of_owner_only_config(tmp_path):
    target = tmp_path / ".env"
    secret = "a-long-synthetic-password"
    create_config(target, secret, domain="portfolio.example.org", port=18123)

    values = dict(line.split("=", 1) for line in target.read_text().splitlines())
    assert secret not in target.read_text()
    assert verify_password(secret, values["SVOIBUMAGI_PASSWORD_HASH"])
    assert values["SVOIBUMAGI_DOMAIN"] == "portfolio.example.org"
    assert values["SVOIBUMAGI_HOST_PORT"] == "18123"
    if os.name != "nt":
        assert target.stat().st_mode & 0o077 == 0

    with pytest.raises(FileExistsError):
        create_config(target, "another-synthetic-password")
    assert verify_password(secret, values["SVOIBUMAGI_PASSWORD_HASH"])


@pytest.mark.parametrize("domain", ["https://portfolio.example.org", "portfolio.example.org/path", "bad domain", "localhost"])
def test_first_run_rejects_non_public_domain(tmp_path, domain):
    with pytest.raises(ValueError):
        create_config(tmp_path / ".env", "a-long-synthetic-password", domain=domain)
    assert not (tmp_path / ".env").exists()

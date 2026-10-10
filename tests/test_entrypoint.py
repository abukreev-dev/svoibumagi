"""Local HTTP mode must stay explicit. SPDX-License-Identifier: GPL-3.0-or-later"""

import sys

import pytest

from svoibumagi import __main__ as entrypoint
from svoibumagi.auth import hash_password


def test_container_http_mode_requires_explicit_flag_and_container_host(monkeypatch, tmp_path):
    settings = []
    monkeypatch.setenv("SVOIBUMAGI_PASSWORD_HASH", hash_password("synthetic-password-123"))
    monkeypatch.setattr(entrypoint, "create_app", lambda *args, **kwargs: settings.append(kwargs) or object())
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)

    monkeypatch.setattr(sys, "argv", ["svoibumagi", "serve", "--host", "0.0.0.0",
                                      "--data-dir", str(tmp_path)])
    entrypoint.main()
    assert settings[-1]["secure_cookie"] is True

    monkeypatch.setattr(sys, "argv", ["svoibumagi", "serve", "--host", "0.0.0.0",
                                      "--insecure-container-http", "--data-dir", str(tmp_path)])
    entrypoint.main()
    assert settings[-1]["secure_cookie"] is False

    monkeypatch.setattr(sys, "argv", ["svoibumagi", "serve", "--host", "127.0.0.1",
                                      "--insecure-container-http", "--data-dir", str(tmp_path)])
    with pytest.raises(SystemExit) as error:
        entrypoint.main()
    assert error.value.code == 2

    monkeypatch.setattr(sys, "argv", ["svoibumagi", "serve", "--host", "0.0.0.0",
                                      "--insecure-http", "--data-dir", str(tmp_path)])
    with pytest.raises(SystemExit) as error:
        entrypoint.main()
    assert error.value.code == 2

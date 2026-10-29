from __future__ import annotations

import pytest

from insto.telegram_bot import _read_dotenv_token, extract_instagram_post_url


def test_read_dotenv_token(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        'OTHER_SETTING=value\nTELEGRAM_BOT_TOKEN="123456:secret-token"\n',
        encoding="utf-8",
    )

    assert _read_dotenv_token() == "123456:secret-token"


def test_read_dotenv_token_when_file_is_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    assert _read_dotenv_token() is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "https://www.instagram.com/p/ABC_123/",
            "https://www.instagram.com/p/ABC_123/",
        ),
        (
            "Please check https://instagram.com/reel/C0de-123/?igsh=abc",
            "https://www.instagram.com/reel/C0de-123/",
        ),
        (
            "https://www.instagram.com/tv/ShortCode",
            "https://www.instagram.com/tv/ShortCode/",
        ),
    ],
)
def test_extract_instagram_post_url(text: str, expected: str) -> None:
    assert extract_instagram_post_url(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "https://instagram.com/",
        "http://www.instagram.com/p/abc/",
        "https://instagram.com.evil.example/p/abc/",
        "https://instagram.com/accounts/login/",
        "https://user@instagram.com/p/abc/",
        "https://instagram.com:443/p/abc/",
        "not a link",
    ],
)
def test_reject_unsupported_instagram_urls(text: str) -> None:
    assert extract_instagram_post_url(text) is None

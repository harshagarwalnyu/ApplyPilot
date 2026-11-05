"""Unit tests for applypilot.mailcode (Gmail service mocked, no network)."""

import base64
import time
from unittest.mock import MagicMock

from applypilot import mailcode


def _enc(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _msg(sender="no-reply@acme.com", subject="Verify", plain=None, html_=None, age_min=1):
    parts = []
    if plain is not None:
        parts.append({"mimeType": "text/plain", "body": {"data": _enc(plain)}})
    if html_ is not None:
        parts.append({"mimeType": "text/html", "body": {"data": _enc(html_)}})
    return {
        "internalDate": str(int((time.time() - age_min * 60) * 1000)),
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}],
            "parts": parts,
        },
    }


def _service(msgs):
    svc = MagicMock()
    svc.users().messages().list().execute.return_value = {
        "messages": [{"id": str(i)} for i in range(len(msgs))]
    }
    svc.users().messages().get.side_effect = lambda userId, id, format: MagicMock(
        execute=lambda: msgs[int(id)]
    )
    return svc


def test_codes_and_links_plain():
    text = "Your code is 482913. Go https://acme.com/verify?token=abc. Or https://acme.com/home"
    assert mailcode.extract_codes(text) == ["482913"]
    assert mailcode.extract_links(text) == ["https://acme.com/verify?token=abc"]


def test_code_length_bounds():
    assert mailcode.extract_codes("123 1234 12345678 123456789") == ["1234", "12345678"]


def test_html_part_base64():
    h = '<p>Code <b>7391</b></p><a href="https://x.io/confirm/ab12">Confirm</a><style>.a{}</style>'
    body = mailcode.extract_body(_msg(html_=h)["payload"])
    assert "7391" in mailcode.extract_codes(body)
    assert mailcode.extract_links(body) == ["https://x.io/confirm/ab12"]


def test_fetch_filters_and_formats():
    msgs = [
        _msg(plain="code 112233 https://acme.com/activate/z"),
        _msg(sender="other@evil.com", plain="code 999999"),
        _msg(subject="Newsletter", plain="code 555555"),
        _msg(plain="old 777777", age_min=120),
    ]
    out = mailcode.fetch(_service(msgs), "acme.com", 30, "verify")
    assert len(out) == 1
    assert out[0]["codes"] == ["112233"]
    assert out[0]["links"] == ["https://acme.com/activate/z"]
    text = mailcode.format_results(out)
    assert "112233" in text and "no-reply@acme.com" in text


def test_max_three():
    msgs = [_msg(plain=f"code {100000 + i}") for i in range(5)]
    assert len(mailcode.fetch(_service(msgs), "acme.com")) == 3


def test_scope_exact():
    assert mailcode.SCOPES == ["https://www.googleapis.com/auth/gmail.readonly"]


def test_missing_setup_exit_2(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(mailcode, "CLIENT_FILE", tmp_path / "c.json")
    monkeypatch.setattr(mailcode, "TOKEN_FILE", tmp_path / "t.json")
    assert mailcode.main(["--from", "acme"]) == 2
    assert "Desktop app" in capsys.readouterr().out
    assert mailcode.main(["auth"]) == 2


def test_missing_token_only_exit_2(monkeypatch, tmp_path):
    c = tmp_path / "c.json"
    c.write_text("{}")
    monkeypatch.setattr(mailcode, "CLIENT_FILE", c)
    monkeypatch.setattr(mailcode, "TOKEN_FILE", tmp_path / "t.json")
    assert mailcode.main(["--from", "acme"]) == 2

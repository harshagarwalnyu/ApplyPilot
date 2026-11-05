"""Read-only Gmail reader: fetch verification codes/links for unattended apply.

Usage:
    python -m applypilot.mailcode auth
    python -m applypilot.mailcode --from <substr> [--since-minutes 30] [--subject <substr>]

Only the gmail.readonly scope is ever requested. Token contents are never printed.
"""

from __future__ import annotations

import argparse
import base64
import html
import re
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
APP_DIR = Path.home() / ".applypilot"
CLIENT_FILE = APP_DIR / "gmail-oauth-client.json"
TOKEN_FILE = APP_DIR / "gmail-readonly-token.json"
MAX_RESULTS = 3

_CODE_RE = re.compile(r"(?<![\w.])\d{4,8}(?![\w])")
_LINK_RE = re.compile(r"https://[^\s<>\"')\]]+", re.I)
_LINK_KEYS = ("verify", "confirm", "activate", "reset", "token")


def setup_message() -> str:
    return (
        "Gmail reader not set up. One-time steps:\n"
        "  1. Google Cloud Console: create a project, enable the Gmail API, and create an\n"
        "     OAuth client of type 'Desktop app'.\n"
        f"  2. Save the client JSON to {CLIENT_FILE}\n"
        "  3. Run: python -m applypilot.mailcode auth"
    )


def _b64(data: str) -> str:
    data += "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")


def _strip_html(s: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    # keep href targets so links survive tag stripping
    s = re.sub(r"(?is)<a\s[^>]*href=[\"']([^\"']+)[\"'][^>]*>", r" \1 ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return html.unescape(s)


def _walk(part: dict, out: list[str]) -> None:
    mime = part.get("mimeType", "")
    data = (part.get("body") or {}).get("data")
    if data and mime == "text/plain":
        out.append(_b64(data))
    elif data and mime == "text/html":
        out.append(_strip_html(_b64(data)))
    for sub in part.get("parts") or []:
        _walk(sub, out)


def extract_body(payload: dict) -> str:
    out: list[str] = []
    _walk(payload, out)
    return "\n".join(out)


def extract_codes(text: str) -> list[str]:
    seen: list[str] = []
    for m in _CODE_RE.findall(text):
        if m not in seen:
            seen.append(m)
    return seen


def extract_links(text: str) -> list[str]:
    seen: list[str] = []
    for m in _LINK_RE.findall(text):
        m = m.rstrip(".,;")
        if any(k in m.lower() for k in _LINK_KEYS) and m not in seen:
            seen.append(m)
    return seen


def _header(payload: dict, name: str) -> str:
    for h in payload.get("headers") or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def fetch(service, sender: str, since_minutes: int = 30, subject: str | None = None) -> list[dict]:
    """Return up to 3 newest matching messages as dicts."""
    cutoff = int(datetime.now(timezone.utc).timestamp()) - since_minutes * 60
    q = f'from:"{sender}"'
    if subject:
        q += f' subject:"{subject}"'
    q += f" after:{cutoff}"
    resp = service.users().messages().list(userId="me", q=q, maxResults=MAX_RESULTS).execute()
    results = []
    for ref in (resp.get("messages") or [])[:MAX_RESULTS]:
        msg = service.users().messages().get(userId="me", id=ref["id"], format="full").execute()
        payload = msg.get("payload") or {}
        sender_h = _header(payload, "From")
        subject_h = _header(payload, "Subject")
        # client-side re-check of substring filters (Gmail search is token-based)
        if sender.lower() not in sender_h.lower():
            continue
        if subject and subject.lower() not in subject_h.lower():
            continue
        ts = msg.get("internalDate")
        if ts:
            when = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
        else:
            try:
                when = parsedate_to_datetime(_header(payload, "Date"))
            except Exception:
                when = None
        if when is not None and when.timestamp() < cutoff:
            continue
        body = extract_body(payload)
        results.append(
            {
                "received": when.isoformat() if when else "?",
                "from": sender_h,
                "subject": subject_h,
                "codes": extract_codes(body),
                "links": extract_links(body),
            }
        )
    return results


def format_results(results: list[dict]) -> str:
    if not results:
        return "No matching messages."
    blocks = []
    for r in results:
        blocks.append(
            "\n".join(
                [
                    f"Received: {r['received']}",
                    f"From: {r['from']}",
                    f"Subject: {r['subject']}",
                    f"Codes: {', '.join(r['codes']) or '(none)'}",
                    "Links:" + ("" if r["links"] else " (none)"),
                    *[f"  {u}" for u in r["links"]],
                ]
            )
        )
    return "\n\n".join(blocks)


def load_credentials():
    """Return valid Credentials, refreshing if needed; None if unusable."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not TOKEN_FILE.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds.valid:
        if creds.refresh_token:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
        else:
            return None
    return creds


def build_service(creds):
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def run_auth() -> int:
    if not CLIENT_FILE.exists():
        print(setup_message())
        return 2
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
    creds = flow.run_local_server(port=0)
    APP_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    print(f"Authorized (gmail.readonly). Token saved to {TOKEN_FILE}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "auth":
        return run_auth()
    p = argparse.ArgumentParser(prog="python -m applypilot.mailcode")
    p.add_argument("--from", dest="sender", required=True, help="sender substring")
    p.add_argument("--since-minutes", type=int, default=30)
    p.add_argument("--subject", default=None, help="subject substring")
    args = p.parse_args(argv)

    if not CLIENT_FILE.exists() or not TOKEN_FILE.exists():
        print(setup_message())
        return 2
    creds = load_credentials()
    if creds is None:
        print("Gmail token unusable (no refresh token). Re-run: python -m applypilot.mailcode auth")
        return 2
    service = build_service(creds)
    print(format_results(fetch(service, args.sender, args.since_minutes, args.subject)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

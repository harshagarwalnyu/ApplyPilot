"""Set the default ATS password the apply agent uses to sign in and create candidate accounts.

  python -m applypilot.set_password            # type it (hidden), twice
  python -m applypilot.set_password --generate # make a strong random one and show it once
  python -m applypilot.set_password --domain idmsa.apple.com [--email you@x]  # one site's own password

The value goes only into profile.json (personal.password, or site_credentials[domain]). It is never echoed while typing.
"""
from __future__ import annotations

import argparse
import getpass
import json
import secrets
import string
import sys

from applypilot import config

# ATS password rules usually want upper, lower, digit and a symbol; avoid quote/backslash for JSON and forms.
SYMBOLS = "!@#$%^*-_+="


def generate(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits + SYMBOLS
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in SYMBOLS for c in pw)):
            return pw


def save(password: str, domain: str | None = None, email: str | None = None) -> None:
    profile = json.loads(config.PROFILE_PATH.read_text(encoding="utf-8"))
    if domain:
        email = email or profile.get("personal", {}).get("email", "")
        profile.setdefault("site_credentials", {})[domain.strip().lower()] = {
            "email": email, "password": password, "login_method": "email"}
    else:
        profile.setdefault("personal", {})["password"] = password
    tmp = config.PROFILE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(config.PROFILE_PATH)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--generate", action="store_true", help="generate a strong random password")
    parser.add_argument("--domain", help="sign-in host this password is for, e.g. idmsa.apple.com")
    parser.add_argument("--email", help="login email for --domain (default: profile email)")
    args = parser.parse_args(argv)
    if not config.PROFILE_PATH.is_file():
        print(f"No profile at {config.PROFILE_PATH}; run `applypilot init` first.")
        return 2
    if args.generate:
        password = generate()
        save(password, args.domain, args.email)
        print("Saved. Store this in your password manager now; it will not be shown again:")
        print(password)
        return 0
    first = getpass.getpass(f"{'Password for ' + args.domain if args.domain else 'Default ATS password'} (hidden): ")
    if len(first) < 8:
        print("Too short (ATS sites usually need 8+ characters). Nothing saved.")
        return 1
    if getpass.getpass("Again: ") != first:
        print("Did not match. Nothing saved.")
        return 1
    save(first, args.domain, args.email)
    print(f"Saved to profile.json ({'site_credentials[' + args.domain + ']' if args.domain else 'personal.password'}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""tier 2 - SEND an email for real (SMTP; Gmail by default). Not a draft.

Usage:
  & $env:JARVIS_PY lib\\py\\send_mail.py --to a@x.com[,b@y.com] --subject "..." (--body "..." | --body-file <file>)
        [--cc ...] [--bcc ...] [--attach <file>]... [--html]

Account: stored once with `python -m jarvis set-mail` (Windows Credential Manager,
service "jarvis-mail"). For Gmail use an App Password (Google Account > Security >
2-Step Verification > App passwords). Prints one JSON object.
"""

import argparse
import json
import mimetypes
import re
import smtplib
import ssl
import sys
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_ATTACH_MB = 24


def load_account() -> dict:
    import keyring

    raw = keyring.get_password("jarvis-mail", "account")
    if not raw:
        raise RuntimeError("No email account configured. Ask the user to run: python -m jarvis set-mail")
    return json.loads(raw)


def addresses(value: str) -> list[str]:
    out = [a.strip() for a in re.split(r"[,;]", value or "") if a.strip()]
    bad = [a for a in out if not EMAIL.match(a)]
    if bad:
        raise ValueError(f"invalid email address(es): {', '.join(bad)}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True)
    ap.add_argument("--cc", default="")
    ap.add_argument("--bcc", default="")
    ap.add_argument("--subject", required=True)
    ap.add_argument("--body", default="")
    ap.add_argument("--body-file", default="")
    ap.add_argument("--attach", action="append", default=[])
    ap.add_argument("--html", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    try:
        acct = load_account()
        to, cc, bcc = addresses(a.to), addresses(a.cc), addresses(a.bcc)
        if not to:
            raise ValueError("no recipient")
        body = Path(a.body_file).read_text(encoding="utf-8") if a.body_file else a.body
        msg = EmailMessage()
        msg["From"] = acct.get("from_name", "") and f"{acct['from_name']} <{acct['address']}>" or acct["address"]
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = a.subject
        msg["Message-ID"] = make_msgid()
        if a.html:
            msg.set_content(re.sub(r"<[^>]+>", "", body))
            msg.add_alternative(body, subtype="html")
        else:
            msg.set_content(body)
        total = 0
        for f in a.attach:
            p = Path(f).expanduser().resolve()
            data = p.read_bytes()
            total += len(data)
            if total > MAX_ATTACH_MB * 2**20:
                raise ValueError(f"attachments exceed {MAX_ATTACH_MB} MB")
            ctype, _ = mimetypes.guess_type(p.name)
            main_t, sub_t = (ctype or "application/octet-stream").split("/", 1)
            msg.add_attachment(data, maintype=main_t, subtype=sub_t, filename=p.name)
        if a.dry_run:
            print(json.dumps({"ok": True, "dry_run": True, "to": to, "cc": cc, "subject": a.subject,
                              "attachments": a.attach}))
            return 0
        host, port = acct.get("smtp_host", "smtp.gmail.com"), int(acct.get("smtp_port", 587))
        ctx = ssl.create_default_context()
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, context=ctx, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            server.starttls(context=ctx)
        with server:
            server.login(acct["address"], acct["app_password"])
            refused = server.send_message(msg, to_addrs=to + cc + bcc)
        print(json.dumps({"ok": True, "sent": True, "from": acct["address"], "to": to, "cc": cc,
                          "subject": a.subject, "attachments": [Path(f).name for f in a.attach],
                          "refused": list(refused)}))
        return 0
    except smtplib.SMTPAuthenticationError:
        print(json.dumps({"ok": False, "error": "SMTP login failed - re-run 'python -m jarvis set-mail' "
                                                "(Gmail needs an App Password, not the normal password)"}))
        return 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())

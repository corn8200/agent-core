#!/usr/bin/env python3
"""Home ops brief renderer + sender.

Takes plain-text brief from home_ops.prompts synthesizer, renders HTML +
plaintext fallback, sends via VPS SMTP (smtplib from /srv/apps/friday-email
on the VPS). NEVER embeds images — masthead/footer are pure styled HTML text.
"""

import asyncio
import html as html_lib
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.constants import PERSONAL_EMAIL, VPS_SSH


FONT_STACK = '-apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif'
TEXT_COLOR = "#1a1a1a"
FOOTER_COLOR = "#666"
RULE_COLOR = "#ddd"


def _escape(s: str) -> str:
    return html_lib.escape(s, quote=False)


def _brief_to_html_body(brief_text: str) -> str:
    escaped = _escape(brief_text.strip())
    return (
        f'<pre style="font-family:{FONT_STACK};font-size:15px;line-height:1.55;'
        f'white-space:pre-wrap;word-wrap:break-word;margin:0;color:{TEXT_COLOR};">'
        f'{escaped}</pre>'
    )


def build_html(brief_text: str, mode: str) -> str:
    body = _brief_to_html_body(brief_text)
    ts = datetime.now().strftime("%a %b %d, %I:%M %p").lstrip("0")
    mode_label = mode.capitalize()

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cornelius Home Ops</title>
</head>
<body style="margin:0;padding:0;background:#ffffff;">
<div style="max-width:600px;margin:0 auto;padding:28px 22px;font-family:{FONT_STACK};background:#ffffff;">
<div style="font-family:{FONT_STACK};font-size:12px;letter-spacing:3px;font-weight:600;color:{TEXT_COLOR};text-align:center;text-transform:uppercase;">
CORNELIUS HOME OPS
</div>
<hr style="border:none;border-top:1px solid {RULE_COLOR};margin:16px 0 22px 0;">
{body}
<hr style="border:none;border-top:1px solid {RULE_COLOR};margin:22px 0 14px 0;">
<div style="font-family:{FONT_STACK};font-size:11px;color:{FOOTER_COLOR};line-height:1.5;">
{mode_label} brief &middot; {ts}<br>
Reply to this email with feedback.
</div>
</div>
</body>
</html>"""


def build_plaintext(brief_text: str) -> str:
    header = "CORNELIUS HOME OPS\n" + ("=" * 20) + "\n\n"
    return header + brief_text.strip() + "\n\n--\nReply with feedback."


def _auto_subject(mode: str) -> str:
    date_str = datetime.now().strftime("%a %b %d")
    return f"Home ops — {mode} brief, {date_str}"


def _build_send_script(
    subject: str,
    recipients: list[str],
    html: str,
    plaintext: str,
) -> str:
    import json
    return f'''import smtplib, os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

load_dotenv("/srv/apps/friday-email/.env")

SUBJECT = {json.dumps(subject)}
RECIPIENTS = {json.dumps(recipients)}
HTML = {json.dumps(html)}
PLAIN = {json.dumps(plaintext)}

msg = MIMEMultipart("alternative")
msg["Subject"] = SUBJECT
msg["From"] = "Cornelius Family <notify@jcornelius.net>"
msg["To"] = ", ".join(RECIPIENTS)
msg.attach(MIMEText(PLAIN, "plain"))
msg.attach(MIMEText(HTML, "html"))

with smtplib.SMTP("smtp.gmail.com", 587) as s:
    s.starttls()
    s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
    s.sendmail(msg["From"], RECIPIENTS, msg.as_string())
print("sent")
'''


async def send_brief(
    brief_text: str,
    mode: str,
    recipients: list[str],
    subject: str | None = None,
) -> tuple[bool, str]:
    if mode not in ("morning", "evening"):
        return False, f"invalid mode: {mode}"
    if not recipients:
        return False, "no recipients"
    if not brief_text or not brief_text.strip():
        return False, "empty brief"

    subject = subject or _auto_subject(mode)
    html = build_html(brief_text, mode)
    plaintext = build_plaintext(brief_text)

    script = _build_send_script(subject, recipients, html, plaintext)

    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    remote_path = f"/tmp/home-ops-send-{ts}.py"

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", prefix="home-ops-send-", delete=False
    ) as f:
        f.write(script)
        local_path = f.name

    try:
        scp = subprocess.run(
            ["scp", local_path, f"{VPS_SSH}:{remote_path}"],
            capture_output=True, text=True, timeout=20,
        )
        if scp.returncode != 0:
            return False, f"scp failed: {scp.stderr.strip()[:200]}"

        run = subprocess.run(
            ["ssh", VPS_SSH,
             f"cd /srv/apps/friday-email && .venv/bin/python {remote_path}"],
            capture_output=True, text=True, timeout=45,
        )

        subprocess.run(
            ["ssh", VPS_SSH, f"rm -f {remote_path}"],
            capture_output=True, timeout=10,
        )

        if run.returncode != 0:
            err = (run.stderr or run.stdout or "").strip()[:300]
            return False, f"smtp failed: {err}"

        return True, f"sent to {len(recipients)} recipient{'s' if len(recipients) != 1 else ''}"
    finally:
        try:
            Path(local_path).unlink(missing_ok=True)
        except Exception:
            pass


async def _test_send():
    fake_brief = """Good evening. Here's the rundown for tonight and tomorrow morning.

Weather is holding clear through the night — low around forty-two, light wind. Tomorrow looks like partly cloudy with a high near sixty-eight.

Tomorrow's schedule:
- 8:30 AM standup call
- 10:00 AM drone inspection prep
- 1:00 PM lunch with Mike
- 4:00 PM school pickup

Overdue items to handle tonight:
- Submit expense report
- Pay water bill

Top priority for tomorrow is closing the Martinsburg quote. Everything else can slide if needed."""

    subject = "[TEST] " + _auto_subject("evening")
    ok, msg = await send_brief(
        fake_brief, "evening", [PERSONAL_EMAIL], subject=subject,
    )
    print(f"result: ok={ok} msg={msg}")


if __name__ == "__main__":
    if "--test" in sys.argv:
        asyncio.run(_test_send())
    else:
        print("usage: python3 -m home_ops.render --test")

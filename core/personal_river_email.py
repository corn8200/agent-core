"""Personal River HTML email wrapper.

Default brand for ALL outbound from John personally (anything signed,
sent, or shipped under his name — not Sentry LLC client work, not ATS
resumes). Pine ink on bone-050 background, Instrument Serif display +
Geist body, sentence case, quiet voice. Logo + signature embedded as
base64 data URIs so the email renders fully offline.

Dark-mode locked. Email clients (Apple Mail, Gmail iOS, Outlook mobile)
auto-invert colors when the OS is in dark mode — the wrapper sets
`color-scheme: light only`, the corresponding meta tags, and re-asserts
every brand color inside `@media (prefers-color-scheme: dark)` with
`!important` so the palette never flips.

Usage:
    from core.personal_river_email import wrap_personal_river

    html = wrap_personal_river(
        body="Hey Mike — quick note on the inspection...",
        title="A note",            # optional H1 above the body
        signoff=True,              # appends "— John" + signature image
    )

    # body can be plain text (newlines preserved, paragraphs auto-split
    # on blank lines) or already-HTML (passed through, but still wrapped
    # in the brand frame).
"""
from __future__ import annotations

import base64
import html as html_lib
import re
from datetime import datetime
from pathlib import Path

# Embedded brand assets — read once on import, base64-encoded.
_ASSETS_DIR = Path(
    "/Users/johncornelius/Projects/brand-kit/personal-river/project/assets/email"
)


def _data_uri(path: Path, mime: str = "image/png") -> str:
    if not path.exists():
        return ""
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


_LOGO_DATA_URI = _data_uri(_ASSETS_DIR / "logo-240.png")
_SIGNATURE_DATA_URI = _data_uri(_ASSETS_DIR / "signature-320.png")


# Palette (mirrors colors_and_type.css)
PINE_900 = "#0E2A25"      # ink, body text
PINE_700 = "#1E4D44"      # secondary text
SLATE_500 = "#5A6770"     # muted / footer
WOOD_400 = "#8C7355"      # rule accent
BONE_050 = "#FBF8F2"      # page background
BONE_100 = "#F5EFE3"      # surface
RIVER_500 = "#3F6E78"     # link

# Font stacks — Google Fonts loaded via @import in the head; system
# fallbacks chosen to match the brand feel as closely as possible if
# webfonts get blocked (Outlook desktop, paranoid corporate Gmail).
DISPLAY_FONT = (
    '"Instrument Serif", Cambria, Georgia, "Times New Roman", serif'
)
BODY_FONT = (
    '"Geist", -apple-system, BlinkMacSystemFont, "Segoe UI", '
    'Helvetica, Arial, sans-serif'
)
MONO_FONT = (
    '"JetBrains Mono", "SF Mono", Menlo, Consolas, monospace'
)


def _looks_like_html(s: str) -> bool:
    return bool(re.search(r"<[a-zA-Z][^>]*>", s))


def _text_to_paragraphs(text: str) -> str:
    """Convert plain text to <p> blocks, preserving single newlines as <br>."""
    text = text.strip()
    if not text:
        return ""
    blocks = re.split(r"\n\s*\n", text)
    out = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        escaped = html_lib.escape(block, quote=False)
        # Single newlines inside a block become <br> for line breaks
        escaped = escaped.replace("\n", "<br>")
        out.append(
            f'<p style="margin:0 0 18px 0;font-family:{BODY_FONT};'
            f'font-size:16px;line-height:1.65;color:{PINE_900} !important;">'
            f"{escaped}</p>"
        )
    return "\n".join(out)


def wrap_personal_river(
    body: str,
    *,
    title: str | None = None,
    signoff: bool = True,
    signoff_name: str = "John",
    show_signature: bool = True,
    show_logo: bool = True,
) -> str:
    """Wrap a body (plain text or HTML) in the Personal River email frame.

    Returns a complete HTML document ready to send as `--html`.
    """
    body = body or ""
    if _looks_like_html(body):
        body_html = body
    else:
        body_html = _text_to_paragraphs(body)

    title_html = ""
    if title:
        title_html = (
            f'<h1 style="margin:0 0 24px 0;font-family:{DISPLAY_FONT};'
            f"font-weight:400;font-size:34px;line-height:1.15;"
            f'color:{PINE_900} !important;letter-spacing:-0.01em;">'
            f"{html_lib.escape(title)}</h1>"
        )

    signoff_html = ""
    if signoff:
        sig_img = ""
        if show_signature and _SIGNATURE_DATA_URI:
            sig_img = (
                f'<img src="{_SIGNATURE_DATA_URI}" alt="{signoff_name}" '
                f'width="160" style="display:block;width:160px;height:auto;'
                f'margin:8px 0 0 0;opacity:0.92;">'
            )
        else:
            sig_img = (
                f'<div style="font-family:{DISPLAY_FONT};font-style:italic;'
                f'font-size:22px;color:{PINE_900} !important;margin-top:8px;">'
                f"— {html_lib.escape(signoff_name)}</div>"
            )
        signoff_html = (
            f'<div style="margin:36px 0 0 0;">'
            f'<div style="font-family:{BODY_FONT};font-size:16px;'
            f'color:{PINE_900} !important;margin-bottom:6px;">— {html_lib.escape(signoff_name)}</div>'
            f"{sig_img}"
            f"</div>"
        )

    logo_html = ""
    if show_logo and _LOGO_DATA_URI:
        logo_html = (
            f'<div style="text-align:center;padding:28px 0 8px 0;">'
            f'<img src="{_LOGO_DATA_URI}" alt="John Cornelius" '
            f'width="88" height="88" style="display:inline-block;'
            f'width:88px;height:88px;object-fit:contain;">'
            f"</div>"
        )

    ts = datetime.now().strftime("%a %b %d, %Y").lstrip("0")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>From John</title>
<style>
:root {{ color-scheme: light; }}
body, table, td, p, h1, h2, h3, div {{ -webkit-text-size-adjust:100%; -ms-text-size-adjust:100%; }}
@import url('https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Geist:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

/* ------------------------------------------------------------
   Dark-mode lockout. Apple Mail respects color-scheme: light;
   Gmail/Outlook do not, so re-assert every brand color with
   !important inside the dark-mode media query.
   ------------------------------------------------------------ */
@media (prefers-color-scheme: dark) {{
  body, .pr-page, .pr-card {{
    background:{BONE_050} !important;
    color:{PINE_900} !important;
  }}
  .pr-text, .pr-text * {{ color:{PINE_900} !important; }}
  .pr-muted, .pr-muted * {{ color:{SLATE_500} !important; }}
  .pr-rule {{ border-color:{WOOD_400} !important; }}
  a {{ color:{RIVER_500} !important; }}
}}
/* Outlook.com dark-mode overrides (uses [data-ogsc] / [data-ogsb] attrs) */
[data-ogsc] body, [data-ogsc] .pr-page, [data-ogsc] .pr-card,
[data-ogsb] body, [data-ogsb] .pr-page, [data-ogsb] .pr-card {{
  background:{BONE_050} !important;
  color:{PINE_900} !important;
}}
[data-ogsc] .pr-text, [data-ogsc] .pr-text * {{ color:{PINE_900} !important; }}
[data-ogsc] .pr-muted, [data-ogsc] .pr-muted * {{ color:{SLATE_500} !important; }}

a {{ color:{RIVER_500}; text-decoration:underline; }}
</style>
<!--[if mso]>
<style>
body, table, td {{ font-family: Cambria, Georgia, serif !important; }}
</style>
<![endif]-->
</head>
<body class="pr-page" style="margin:0;padding:0;background:{BONE_050};color:{PINE_900};">
<div class="pr-page" style="background:{BONE_050};padding:24px 12px;">
  <div class="pr-card" style="max-width:600px;margin:0 auto;background:{BONE_050};padding:8px 28px 36px 28px;">
    {logo_html}
    <div class="pr-text" style="font-family:{BODY_FONT};color:{PINE_900};">
      {title_html}
      {body_html}
      {signoff_html}
    </div>
    <hr class="pr-rule" style="border:none;border-top:1px solid {WOOD_400};opacity:0.35;margin:36px 0 14px 0;">
    <div class="pr-muted" style="font-family:{MONO_FONT};font-size:11px;color:{SLATE_500};letter-spacing:0.04em;text-transform:uppercase;">
      Built slow. Built to stay. &middot; {ts}
    </div>
  </div>
</div>
</body>
</html>"""


# Convenience: build the corresponding plain-text fallback for
# multipart messages (some clients prefer text/plain). Currently the
# VPS send-email wrapper always sends both parts when --html is set,
# but if a caller wants the matching plain side they can use this.
def to_plaintext(body: str, *, title: str | None = None,
                 signoff: bool = True, signoff_name: str = "John") -> str:
    parts: list[str] = []
    if title:
        parts.append(title)
        parts.append("=" * len(title))
        parts.append("")
    if _looks_like_html(body):
        # crude strip — the HTML branch is rare for personal mail
        text = re.sub(r"<[^>]+>", "", body)
        text = html_lib.unescape(text)
    else:
        text = body
    parts.append(text.strip())
    if signoff:
        parts.append("")
        parts.append(f"— {signoff_name}")
    parts.append("")
    parts.append("Built slow. Built to stay.")
    return "\n".join(parts)

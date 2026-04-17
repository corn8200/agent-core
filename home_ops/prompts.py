"""home-ops synthesizer prompts."""
from __future__ import annotations
import json
import sys
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

SYSTEM_PROMPT = """You are the home-ops assistant for John Cornelius. You write a short, question-driven brief twice a day (evening prep at 8 PM, morning anchor at 6:30 AM). This is John's ONE daily brief — it covers household AND business AND infrastructure. Do not skip any domain that has signal.

VOICE — follow these rules exactly:

1. Dry, competent, terse. No fluff. No emojis. No exclamation marks. No "Good morning", "Hope you're well", "Just a heads up", "I noticed", "I see that", "It looks like", "FYI". State the thing.
2. Questions beat assertions. If there are two things happening at once, ask "who's driving?" — don't declare "CONFLICT DETECTED". If a kid has a game, ask "you or Ashley on this one?" — don't assume.
3. Never claim certainty about family intent you can't verify. If a text said "baseball saturday", the brief says "jude's game saturday — you or ashley?", not "Jude has baseball Saturday and you are driving".
4. Normal sentence capitalization throughout. Capitalize proper nouns (names, places, brands), sentence starts, day labels ("Tomorrow", "Wednesday", "Next Monday"), and section labels ("Loose ends:", "Weather:", "Business:", "Infra:", "Urgent:"). NO all-lowercase output. NO CAPS headers. No markdown bold. No bullets except in loose ends / business / urgent. Plain text. Short lines.
5. Max ~450 words total across all sections. Shorter is better. If there's nothing to say in a section, drop the section entirely — don't pad.
6. John is a 20-year Army retiree and senior engineer. He cusses, he doesn't need hand-holding, he hates being over-explained to. Write like a sharp XO who's been paying attention all week.
7. Family: wife Ashley, kids Jude and James. Discover others (parents, in-laws, siblings) dynamically from the contacts relations in the gather data — never hardcode. If a name shows up in messages/mail/calendar and matches a contact relation, you can use it.
8. 7-day horizon for schedule. Focus depends on mode (see below). Mention week-ahead items ONLY if they need action now (gear to pack, reservation to make, someone to call, a driver to pick).
9. Loose ends = unresolved threads from messages/mail — someone waiting on a reply, a quote not returned, a return window closing, a birthday this week, a bill pending. Pull these from imessages_7d, mail_7d, and loose_ends. Max 4 bullets. Skip if nothing real.
10. Weather only if it changes a plan (outdoor event, drive time, kid activity, yard work mentioned). Skip otherwise.
11. NEVER mention TAMKO or John's day-job employer. Sentry AI Thermal is his side business — that IS in scope. Business section covers Sentry only.
12. Business section pulls from mail_7d (categorized as 'personal'/'action') and gather.vps.raw if it has sentry-mailqueue stats. Call out hot leads (clicks), new quote requests, bounces, SAM.gov matches. Skip if the pipeline is quiet.
13. Infra section only shows red. If Mac/VPS/Pi/Docker/services are all green, drop the section entirely. If vps_auth.status is 'fail', put it in Urgent too.
14. Urgent section = things that must happen today or bad things happen. Overdue reminders, VPS auth broken, unanswered hot client email > 24h, bill due today. Skip if nothing urgent.

MODE:
- evening (8 PM): focus is tomorrow prep and the day after. "What am I forgetting for tomorrow morning?" Gear, departure times, who's driving, what's the weather, anything due.
- morning (6:30 AM): focus is today's anchor points and timing. "What does today look like and what's the first thing?" First event, drive time, weather for it, the big thing to not drop.

OUTPUT SHAPE — STRICT. Day-bucketed schedule is the core. Optional business/infra/urgent/weather/loose-ends sections wrap it. Drop any section that has nothing real.

<1-line situation line that frames the next 24h>

Urgent:
- <only things that must move today>

Tomorrow (Tue Apr 14):
  7:30am — Auld drop off
  8:00am — ZR2 service at CMA Martinsburg (conflicts with drop-off — who's handling what?)
  6:00pm — Jude baseball practice, South Jefferson Park (you or Ashley driving?)

Thursday (Apr 16):
  6:00pm — Jude baseball practice

Next Monday (Apr 20):
  12:37pm — Fly IAD→ATL→XNA (leave house by 10am)
  Four nights in Joplin, back Fri 4/24
  Ashley solo with the boys all week — sync tonight on Thu practice

Next Tuesday (Apr 21):
  6:00pm — Jude baseball practice (Ashley covering — you're in Joplin)

Business:
- Three clicks on the Frederick lead since Monday — worth a direct call
- Two new SAM.gov matches tagged thermal inspection
- One bounce on yesterday's batch — address dead, needs a clean

Infra: <only if something's broken — e.g. "VPS auth flapping, currently green; mailtriage timer last ran 3h ago">

Loose ends:
- <short bullet, real thing someone's waiting on>
- <short bullet>

Weather: <one line, only if it changes a plan>

RULES for the output shape:
- Day labels are Title Case: "Today", "Tomorrow (Tue Apr 14):", "Wednesday (Apr 15):", "Next Monday (Apr 20):". Day-of-week always capitalized.
- Section labels Title Case: "Urgent:", "Loose ends:", "Weather:", "Business:", "Infra:".
- Section order (drop any that has nothing): situation line → Urgent → day buckets → Business → Infra → Loose ends → Weather.
- Two-space indent before each time entry. Time in lowercase (7:30am, 6:00pm). Em-dash between time and item.
- Event descriptions use normal sentence capitalization — proper nouns capitalized.
- Times chronological within each day.
- Day with no scheduled event but a standing note (trash day, bill due, birthday): one line under the day label with no time.
- Skip days that have nothing to say entirely — do NOT pad.
- Include EVERY day in the 10-day horizon that has an event. Missing a recurring event (e.g. next week's practice) is a failure.
- Questions for ambiguity (who's driving?) go inline in parentheses on the event line, not as a separate paragraph.
- The situation line at the top is ONE line. The day buckets ARE the schedule content, not a prose dump.
- Business/Infra/Urgent are bullets, max 4 bullets each. Actionable phrasing — "lead clicked three times, call" not "3 click events logged".
- No "Sent from home-ops". No closing. No preamble. End on the last useful line."""


FEW_SHOT_EXAMPLES = """Here are four example briefs showing the voice AND the day-bucketed layout. Match both. Proper sentence capitalization on every line — day labels, section labels, event descriptions. Time entries indented two spaces.

--- EVENING EXAMPLE 1 ---
Tomorrow's light until the afternoon — Jude's 5pm practice is the only hard anchor.

Tomorrow (Wed Apr 15):
  10:00am — Truck inspection at the dealer (paperwork's on the kitchen counter)
  5:00pm — Jude baseball practice, Field 3 (glove + cleats in the garage bin last you checked)

Sunday (Apr 19):
  7:10pm — Ashley's flight lands at Dulles (leave house by 5:30, ~90min each way)

Next Wednesday (Apr 22):
  5:00pm — Jude practice (weekly recurring — still on your calendar)

Loose ends:
- Plumber hasn't replied on the kitchen quote (5 days out)
- Monitor return window closes Tuesday
- James's library books due Thursday

Weather: Clear tomorrow morning, rain moves in around 3pm — 5pm practice might get pushed.

--- EVENING EXAMPLE 2 ---
Quiet tomorrow. One anchor — James's dentist at 2.

Tomorrow (Thu Apr 16):
  2:00pm — James dentist, Dr. Harper (insurance card — Ashley had it last, check her purse or the glove box)

Saturday (Apr 18):
  9:00am — Jude baseball tournament, Concord (registration packet not in the forwarded email — ask coach tonight)

Next Thursday (Apr 23):
  Ashley's mom's birthday — card not in the mail pile yet

Loose ends:
- Jude's field trip permission slip sitting in the mail pile since Monday
- Amazon refund for the grill cover — still no credit

Weather: Cold snap Friday night, low 28. Faucet drip on the outside spigot.

--- MORNING EXAMPLE 1 ---
Two anchors today — Jude's ortho at 11 and dinner with Ashley's parents at 6.

Today (Fri Apr 17):
  11:00am — Jude ortho at SmileWright (math quiz after — back on time; you or Ashley on pickup?)
  6:00pm — Dinner at Osteria, reservation under Cornelius (Ashley booked Thursday)

Saturday (Apr 18):
  9:00am — Jude tournament, Concord

Loose ends:
- James's coach texted yesterday about the rain-out makeup, still unanswered
- Car inspection sticker expires Friday

Weather: 62 and clear all day, nothing to dodge.

--- MORNING EXAMPLE 2 ---
Heavy day. Three anchors and a drive.

Today (Mon Apr 20):
  8:45am — James pediatrician (arrive 8:30 for paperwork)
  10:00am–12:00pm — Focused block (you flagged it yesterday as don't-miss)
  5:00pm — Jude baseball, Field 2 (Ashley said she could cover if you're still tied up at noon — confirm now)

Tuesday (Apr 21):
  Trash out

Thursday (Apr 23):
  Ashley's mom's birthday — card not in the mail pile yet

Next Monday (Apr 27):
  6:00am — Flight to Denver (packing list untouched)

Loose ends:
- DMV renewal notice sitting since Saturday
- Coach's rain-out makeup text from Sunday, still unanswered

Weather: Thunderstorms 4-6pm, 5pm practice is probably getting canceled — check the league text thread before you head out."""


def _filter_contacts(contacts, cap: int = 60):
    if not contacts:
        return []
    with_rel = [c for c in contacts if c.get("relations")]
    without_rel = [c for c in contacts if not c.get("relations")]
    without_rel.sort(
        key=lambda c: (
            1 if (c.get("email") or c.get("phone") or c.get("phones") or c.get("emails")) else 0,
            (c.get("name") or "").lower(),
        ),
        reverse=True,
    )
    merged = list(with_rel)
    seen = {id(c) for c in merged}
    for c in without_rel:
        if len(merged) >= cap:
            break
        if id(c) in seen:
            continue
        merged.append(c)
    return merged[:cap]


def _filter_imessages(msgs):
    if not msgs:
        return []
    cleaned = []
    for m in msgs:
        text = (m.get("text") or "").strip()
        if len(text) <= 1:
            continue
        if text.lower() in {"+1", "ok", "k", "y", "n", "lol", "haha", "yes", "no"}:
            continue
        cleaned.append(m)
    cleaned.sort(key=lambda m: m.get("ts") or "", reverse=True)
    return cleaned[:80]


def _filter_mail(mails):
    if not mails:
        return []
    def urg_val(m):
        u = m.get("urgency") or m.get("urgency_score") or 0
        try:
            return float(u)
        except (TypeError, ValueError):
            return 0.0
    def score(m):
        return (urg_val(m), m.get("ts") or "")
    prioritized = [m for m in mails if urg_val(m) >= 0.3]
    pool = prioritized if prioritized else sorted(mails, key=score, reverse=True)[:40]
    pool = sorted(pool, key=score, reverse=True)
    return pool[:40]


def _truncate_text(val, limit):
    if not isinstance(val, str):
        return val
    if len(val) <= limit:
        return val
    return val[: limit - 1] + "…"


def _slim_messages(msgs):
    slim = []
    for m in msgs:
        slim.append({
            "ts": m.get("ts"),
            "from_me": m.get("from_me"),
            "sender": m.get("sender"),
            "chat_name": m.get("chat_name") or m.get("chat"),
            "text": _truncate_text(m.get("text"), 240),
        })
    return slim


def _slim_mail(mails):
    slim = []
    for m in mails:
        slim.append({
            "ts": m.get("ts"),
            "from": m.get("from") or m.get("from_name"),
            "from_addr": m.get("from_addr"),
            "subject": _truncate_text(m.get("subject"), 160),
            "category": m.get("category"),
            "urgency": m.get("urgency") or m.get("urgency_score"),
            "preview": _truncate_text(m.get("preview"), 220),
        })
    return slim


def _slim_contacts(contacts):
    slim = []
    for c in contacts:
        slim.append({
            "name": c.get("name"),
            "org": c.get("org"),
            "relations": c.get("relations"),
            "birthday": c.get("birthday"),
        })
    return slim


def _slim_weather(w):
    if not isinstance(w, dict):
        return w
    hours = w.get("hours") or []
    return {
        "currently": w.get("currently"),
        "temp": w.get("temp"),
        "next_24h": w.get("next_24h"),
        "alerts": w.get("alerts"),
        "hours": hours[:24],
    }


def _build_day_labels() -> str:
    tz = ZoneInfo("America/New_York")
    today = datetime.now(tz).date()
    lines = ["DAY LABELS (use exactly these for bucketing):"]

    def fmt(d: date) -> str:
        return f"{d:%a %b %d} ({d.isoformat()})"

    lines.append(f"  Today = {fmt(today)}")
    lines.append(f"  Tomorrow = {fmt(today + timedelta(days=1))}")
    weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    for i in range(2, 7):
        d = today + timedelta(days=i)
        lines.append(f"  {weekday_names[d.weekday()]} = {fmt(d)}")
    for i in range(7, 11):
        d = today + timedelta(days=i)
        lines.append(f"  Next {weekday_names[d.weekday()]} = {fmt(d)}")
    return "\n".join(lines)


def _shrink_payload(payload: dict, limit: int = 24000) -> tuple[str, dict]:
    """Shrink payload to fit within limit. Returns (blob, info).

    info: {
      "fired": bool — True if any shrink step ran
      "cleared": bool — True if final fallback (clearing imessages+mail) was used
      "original_size": int — blob size before shrink
      "final_size": int — blob size after shrink
      "limit": int — threshold that triggered shrinking
    }
    """
    blob = json.dumps(payload, indent=2, default=str, ensure_ascii=False)
    original_size = len(blob)
    info = {
        "fired": False,
        "cleared": False,
        "original_size": original_size,
        "final_size": original_size,
        "limit": limit,
    }
    if len(blob) <= limit:
        return blob, info

    info["fired"] = True

    w = payload.get("weather")
    if isinstance(w, dict) and isinstance(w.get("hours"), list) and len(w["hours"]) > 12:
        w["hours"] = w["hours"][:12]
        blob = json.dumps(payload, indent=2, default=str, ensure_ascii=False)

    msgs = payload.get("imessages_7d") or []
    while len(blob) > limit and len(msgs) > 15:
        msgs = msgs[: max(15, int(len(msgs) * 0.75))]
        payload["imessages_7d"] = msgs
        blob = json.dumps(payload, indent=2, default=str, ensure_ascii=False)

    mails = payload.get("mail_7d") or []
    while len(blob) > limit and len(mails) > 10:
        mails = mails[: max(10, int(len(mails) * 0.75))]
        payload["mail_7d"] = mails
        blob = json.dumps(payload, indent=2, default=str, ensure_ascii=False)

    contacts = payload.get("contacts") or []
    while len(blob) > limit and len(contacts) > 15:
        contacts = contacts[: max(15, int(len(contacts) * 0.75))]
        payload["contacts"] = contacts
        blob = json.dumps(payload, indent=2, default=str, ensure_ascii=False)

    msgs = payload.get("imessages_7d") or []
    while len(blob) > limit and len(msgs) > 5:
        msgs = msgs[: max(5, int(len(msgs) * 0.6))]
        payload["imessages_7d"] = msgs
        blob = json.dumps(payload, indent=2, default=str, ensure_ascii=False)

    mails = payload.get("mail_7d") or []
    while len(blob) > limit and len(mails) > 5:
        mails = mails[: max(5, int(len(mails) * 0.6))]
        payload["mail_7d"] = mails
        blob = json.dumps(payload, indent=2, default=str, ensure_ascii=False)

    if len(blob) > limit:
        print(
            f"WARN: home_ops prompt payload still {len(blob)} > {limit} after shrink; "
            "clearing imessages_7d + mail_7d",
            file=sys.stderr,
        )
        payload["imessages_7d"] = []
        payload["mail_7d"] = []
        blob = json.dumps(payload, indent=2, default=str, ensure_ascii=False)
        info["cleared"] = True

    info["final_size"] = len(blob)
    return blob, info


def build_user_prompt(gather: dict, mode: str) -> tuple[str, dict]:
    """Build the user prompt. Returns (prompt, shrink_info).

    shrink_info is the dict returned from _shrink_payload — callers that
    don't care can just unpack and ignore the second element.
    """
    mode = (mode or "").lower().strip()
    if mode not in {"evening", "morning"}:
        mode = "evening"

    payload = {
        "mode": mode,
        "now": gather.get("now"),
        "today": gather.get("today"),
        "tomorrow": gather.get("tomorrow"),
        "calendar_7d": gather.get("calendar_7d") or [],
        "reminders": gather.get("reminders") or {},
        "contacts": _slim_contacts(_filter_contacts(gather.get("contacts") or [])),
        "imessages_7d": _slim_messages(_filter_imessages(gather.get("imessages_7d") or [])),
        "mail_7d": _slim_mail(_filter_mail(gather.get("mail_7d") or [])),
        "weather": _slim_weather(gather.get("weather")),
        "loose_ends": gather.get("loose_ends") or [],
        "learned_facts": gather.get("learned_facts") or [],
    }

    blob, shrink_info = _shrink_payload(payload, limit=24000)
    day_labels = _build_day_labels()

    mode_hint = (
        "evening mode: it's 8 PM. focus on tomorrow prep + day after + week-ahead items that need action tonight. "
        "think 'what am I forgetting for tomorrow morning'."
        if mode == "evening"
        else "morning mode: it's 6:30 AM. focus on today's anchor points, timing, the first thing, weather impact. "
        "think 'what does today look like and what's first'."
    )

    prompt = (
        f"{FEW_SHOT_EXAMPLES}\n\n"
        f"--- END EXAMPLES ---\n\n"
        f"{mode_hint}\n\n"
        f"{day_labels}\n\n"
        f"Here is the gathered data as JSON:\n\n"
        f"{blob}\n\n"
        f"Write the {mode} brief now. Plain text only. No preamble. No signoff."
    )
    return prompt, shrink_info

"""home-ops synthesizer prompts."""
from __future__ import annotations
import json

SYSTEM_PROMPT = """You are the home-ops assistant for John Cornelius. You write a short, question-driven household brief twice a day (evening prep at 8 PM, morning anchor at 6:30 AM). This brief is HOME LIFE ONLY. Do not mention Sentry AI Thermal, his employer, work projects, job search, or any business matter. Another brief handles that.

VOICE — follow these rules exactly:

1. Dry, competent, terse. No fluff. No emojis. No exclamation marks. No "Good morning", "Hope you're well", "Just a heads up", "I noticed", "I see that", "It looks like", "FYI". State the thing.
2. Questions beat assertions. If there are two things happening at once, ask "who's driving?" — don't declare "CONFLICT DETECTED". If a kid has a game, ask "you or Ashley on this one?" — don't assume.
3. Never claim certainty about family intent you can't verify. If a text said "baseball saturday", the brief says "jude's game saturday — you or ashley?", not "Jude has baseball Saturday and you are driving".
4. Normal sentence capitalization — write like a sharp human. Capitalize proper nouns (names, places, brands) and start sentences normally. Section cues in plain lowercase: "today", "tomorrow", "this week", "loose ends", "weather". No CAPS headers. No markdown bold. No bullets except in loose ends. Plain text. Short lines.
5. Max ~300 words. Shorter is better. If there's nothing to say in a section, drop the section entirely.
6. John is a 20-year Army retiree and senior engineer. He cusses, he doesn't need hand-holding, he hates being over-explained to. Write like a sharp XO who's been paying attention all week.
7. Family: wife Ashley, kids Jude and James. Discover others (parents, in-laws, siblings) dynamically from the contacts relations in the gather data — never hardcode. If a name shows up in messages/mail/calendar and matches a contact relation, you can use it.
8. 7-day horizon. Focus depends on mode (see below). Mention week-ahead items ONLY if they need action now (gear to pack, reservation to make, someone to call, a driver to pick).
9. Loose ends = unresolved threads from messages/mail — someone waiting on a reply, a quote not returned, a return window closing, a birthday this week, a bill pending. Pull these from imessages_7d, mail_7d, and loose_ends. Max 4 bullets. Skip if nothing real.
10. Weather only if it changes a plan (outdoor event, drive time, kid activity, yard work mentioned). Skip otherwise.

MODE:
- evening (8 PM): focus is tomorrow prep and the day after. "What am I forgetting for tomorrow morning?" Gear, departure times, who's driving, what's the weather, anything due.
- morning (6:30 AM): focus is today's anchor points and timing. "What does today look like and what's the first thing?" First event, drive time, weather for it, the big thing to not drop.

OUTPUT SHAPE (plain text, no headers, no signoff, no preamble):

<1-2 line situation line that frames the next 24h>

<today or tomorrow events with real times and the actual questions>

<week ahead items that need prep now — only if any>

loose ends:
- <short bullet>
- <short bullet>

weather: <one line, only if it matters>

That's it. No "Sent from home-ops". No closing. End on the last useful line."""


FEW_SHOT_EXAMPLES = """Here are four example briefs showing the voice. Match this register. Note the capitalization: proper sentence case, names and places capitalized.

--- EVENING EXAMPLE 1 ---
Tomorrow's light until the afternoon — Jude's practice at 5 is the only hard anchor.

Truck inspection 10am at the dealer. Paperwork's on the kitchen counter from Tuesday.
Jude practice 5pm, Field 3. Glove and cleats — last you mentioned them they were in the garage bin.
Ashley's flight lands Sunday 7:10pm, Dulles. ~90 min each way, leave by 5:30.

loose ends:
- Plumber hasn't replied on the kitchen quote (5 days out)
- Monitor return window closes Tuesday
- James's library books due Thursday

weather: Clear tomorrow morning, rain moves in around 3pm — practice at 5 might get pushed.

--- EVENING EXAMPLE 2 ---
Quiet tomorrow. No hard anchors until James's dentist at 2.

James dentist 2pm, Dr. Harper. Insurance card — Ashley had it last, check her purse or the glove box.

this week: Saturday's baseball tournament starts 9am, Concord. Registration packet wasn't in the email forwarded — worth asking coach tonight before it's too late.

loose ends:
- Jude's field trip permission slip sitting in the mail pile since Monday
- Amazon refund for the grill cover — still no credit

weather: Cold snap Friday night, low 28. Faucet drip on the outside spigot.

--- MORNING EXAMPLE 1 ---
Two things today: Jude's orthodontist at 11 and dinner with Ashley's parents at 6.

Ortho 11am at SmileWright. Ashley on pickup or you? Jude said he's got a math quiz after, don't be late getting him back.
Dinner 6pm at Osteria. Reservation's under Cornelius, Ashley booked it Thursday.

loose ends:
- James's coach texted yesterday about the rain-out makeup, still unanswered
- Car inspection sticker expires Friday

weather: 62 and clear all day, nothing to dodge.

--- MORNING EXAMPLE 2 ---
Heavy day. Three anchors and a drive.

James pediatrician 8:45am, arrive 8:30 for paperwork.
Focused block 10-12 (you flagged it yesterday as a don't-miss).
Jude baseball 5pm, Field 2 — Ashley said she could take him if you're still tied up at noon, worth confirming now before she starts her afternoon.

loose ends:
- Ashley's mom's birthday Thursday, no card in the mail pile
- DMV renewal notice sitting since Saturday

weather: Thunderstorms 4-6pm, Jude's 5pm practice is probably getting canceled — check the league text thread before you head out."""


def _filter_contacts(contacts):
    if not contacts:
        return []
    with_rel = [c for c in contacts if c.get("relations")]
    if len(contacts) < 30:
        return contacts
    return with_rel


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
    def score(m):
        urg = m.get("urgency") or m.get("urgency_score") or 0
        try:
            urg = float(urg)
        except (TypeError, ValueError):
            urg = 0
        has_from = 1 if m.get("from") or m.get("from_name") else 0
        return (urg, has_from, m.get("ts") or "")
    prioritized = [m for m in mails if (m.get("urgency") or 0) or m.get("from")]
    pool = prioritized if prioritized else mails
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


def build_user_prompt(gather: dict, mode: str) -> str:
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

    blob = json.dumps(payload, indent=2, default=str, ensure_ascii=False)

    if len(blob) > 14000:
        msgs = payload["imessages_7d"]
        while len(blob) > 14000 and len(msgs) > 20:
            msgs = msgs[: max(20, len(msgs) - 10)]
            payload["imessages_7d"] = msgs
            blob = json.dumps(payload, indent=2, default=str, ensure_ascii=False)
        mails = payload["mail_7d"]
        while len(blob) > 14000 and len(mails) > 10:
            mails = mails[: max(10, len(mails) - 5)]
            payload["mail_7d"] = mails
            blob = json.dumps(payload, indent=2, default=str, ensure_ascii=False)
        if len(blob) > 14000:
            blob = blob[:14000] + "\n... [truncated]"

    mode_hint = (
        "evening mode: it's 8 PM. focus on tomorrow prep + day after + week-ahead items that need action tonight. "
        "think 'what am I forgetting for tomorrow morning'."
        if mode == "evening"
        else "morning mode: it's 6:30 AM. focus on today's anchor points, timing, the first thing, weather impact. "
        "think 'what does today look like and what's first'."
    )

    return (
        f"{FEW_SHOT_EXAMPLES}\n\n"
        f"--- END EXAMPLES ---\n\n"
        f"{mode_hint}\n\n"
        f"Here is the gathered data as JSON:\n\n"
        f"{blob}\n\n"
        f"Write the {mode} brief now. Plain text only. No preamble. No signoff."
    )

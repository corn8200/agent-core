#!/usr/bin/env python3
"""Benchmark: Managed Agents vs in-process SDK on a real agent-core task.

Runs an enrichment-shaped prompt (structured JSON extraction) through
both code paths with the same input, reports latency + output parity.

Usage:
    .venv/bin/python tests/bench_managed_vs_sdk.py
"""

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv("/Users/johncornelius/Projects/job-agent/.env")
# Managed Agents path needs raw API billing — read the renamed var from secrets.env.
# ANTHROPIC_API_KEY was renamed to ANTHROPIC_CONSOLE_KEY on 2026-04-12 to stop
# silent API billing leaks (see ~/.config/secrets.env). Set it here ONLY for this
# explicit test — do not export it globally.
_console_key = os.environ.get("ANTHROPIC_CONSOLE_KEY") or os.environ.get("ANTHROPIC_API_KEY")
assert _console_key, "need ANTHROPIC_CONSOLE_KEY in ~/.config/secrets.env for managed path"
os.environ["ANTHROPIC_API_KEY"] = _console_key

from core.managed import managed_query  # noqa: E402
from claude_agent_sdk import query, ClaudeAgentOptions  # noqa: E402  # allow-direct-sdk
from core.hooks import AGENT_HOOKS  # noqa: E402
from core.thinking import STANDARD  # noqa: E402

# A realistic enrichment-style prompt — exactly the shape the old research pipeline used
SAMPLE_RESEARCH = """The DJI Matrice 30T is a commercial thermal drone with a 640x512 radiometric thermal
sensor and 48MP wide-angle camera. Weight 3.77 lbs, flight time 41 minutes, transmission range
15 km. MSRP $13,999 for base package. Popular with power line inspection and search-and-rescue.

The Autel EVO II Dual 640T has the same 640x512 thermal resolution, 8K visible camera, 42 minute
flight time. MSRP $9,495. Lighter at 2.86 lbs. Better radiometric accuracy per user reports but
smaller ecosystem.

For commercial thermal roof inspection on residential and small commercial buildings in the
mid-Atlantic region, the M30T is overkill. EVO II Dual offers 85% of the capability at 68% of the
price, and the Pix4D workflow is equivalent. Battery logistics favor the M30T for long days, but
a second EVO battery closes that gap for $450.
"""

ENRICH_PROMPT = f"""You are a data enrichment agent. Read the research below and extract structured data.

Output ONLY valid JSON (no prose, no fences):
{{"charts": [{{"title":"...","type":"bar|line|radar|doughnut","labels":["..."],"datasets":[{{"label":"...","data":[0]}}]}}],
 "locations": [{{"name":"...","address":"...","lat":0.0,"lng":0.0,"note":"..."}}],
 "screenshots": ["url1","url2"]}}

Rules: Only include if real data exists. Max 3 screenshots. Empty arrays if nothing fits.

RESEARCH:
{SAMPLE_RESEARCH}"""


def extract_json(raw: str) -> dict:
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
    if m:
        raw = m.group(1)
    m2 = re.search(r'(\{.*\})', raw, re.DOTALL)
    if m2:
        raw = m2.group(1)
    try:
        return json.loads(raw)
    except Exception:
        return {"__parse_error__": True, "raw": raw[:200]}


async def run_sdk() -> tuple[str, float]:
    t0 = time.perf_counter()
    result = ""
    try:
        async for msg in query(
            prompt=ENRICH_PROMPT,
            options=ClaudeAgentOptions(
                model="opus",
                permission_mode="bypassPermissions",
                max_turns=2,
                # max_budget_usd removed 2026-04-22 (#183) — vestigial under Max
                hooks=AGENT_HOOKS,
                thinking=STANDARD,
                effort="max",
            ),
        ):
            if hasattr(msg, "content"):
                for block in msg.content:
                    if hasattr(block, "text"):
                        result += block.text
            if hasattr(msg, "result") and msg.result:
                result = msg.result
    except Exception:
        pass
    return result.strip(), time.perf_counter() - t0


async def run_managed() -> tuple[str, float]:
    t0 = time.perf_counter()
    result = await managed_query(
        ENRICH_PROMPT,
        model="opus",
        system="You are a data enrichment agent. Output only valid JSON as instructed.",
        agent_name="bench-enrichment",
        title="bench-enrichment",
    )
    return result, time.perf_counter() - t0


async def main():
    print("=== Benchmark: Managed Agents vs SDK (enrichment pass) ===\n")

    print("[1/2] Running in-process SDK path...")
    sdk_text, sdk_dt = await run_sdk()
    print(f"      {sdk_dt:.2f}s, {len(sdk_text)} chars")

    print("[2/2] Running Managed Agents path...")
    mng_text, mng_dt = await run_managed()
    print(f"      {mng_dt:.2f}s, {len(mng_text)} chars")

    print()
    print(f"=== Latency ===")
    print(f"  SDK     : {sdk_dt:.2f}s")
    print(f"  Managed : {mng_dt:.2f}s")
    print(f"  Delta   : {mng_dt - sdk_dt:+.2f}s ({(mng_dt/sdk_dt - 1)*100:+.0f}%)")

    print()
    print("=== JSON parse ===")
    sdk_json = extract_json(sdk_text)
    mng_json = extract_json(mng_text)
    print(f"  SDK     : {'ok' if '__parse_error__' not in sdk_json else 'FAIL'}  charts={len(sdk_json.get('charts', []))} locations={len(sdk_json.get('locations', []))}")
    print(f"  Managed : {'ok' if '__parse_error__' not in mng_json else 'FAIL'}  charts={len(mng_json.get('charts', []))} locations={len(mng_json.get('locations', []))}")

    # Dump raw outputs for manual review
    out_dir = Path("/tmp/bench-managed-vs-sdk")
    out_dir.mkdir(exist_ok=True)
    (out_dir / "sdk_output.txt").write_text(sdk_text)
    (out_dir / "managed_output.txt").write_text(mng_text)
    (out_dir / "sdk_parsed.json").write_text(json.dumps(sdk_json, indent=2))
    (out_dir / "managed_parsed.json").write_text(json.dumps(mng_json, indent=2))
    print(f"\nRaw outputs dumped to {out_dir}/")


if __name__ == "__main__":
    asyncio.run(main())

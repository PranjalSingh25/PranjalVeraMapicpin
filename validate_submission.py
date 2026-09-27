"""Validate submission.jsonl against the 4 acceptance checks."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
import bot  # noqa: E402

DATA = ROOT / "magicpin-ai-challenge" / "dataset" / "expanded"
REQUIRED = {"test_id", "body", "cta", "send_as", "suppression_key", "rationale"}

lines = (ROOT / "submission.jsonl").read_text(encoding="utf-8").splitlines()
print(f"[1] line count: {len(lines)} (expect 30)")
assert len(lines) == 30, "must have exactly 30 lines"

records = [json.loads(ln) for ln in lines]  # raises if any line invalid
ids = [r["test_id"] for r in records]
print(f"[1] test_ids: {ids[0]}..{ids[-1]} contiguous={ids == [f'T{i:02d}' for i in range(1, 31)]}")
assert ids == [f"T{i:02d}" for i in range(1, 31)], "must be T01..T30 in order"

missing = [{k for k in REQUIRED if k not in r} for r in records]
assert all(not m for m in missing), f"missing keys: {missing}"
print("[2] all lines contain test_id, body, cta, send_as, suppression_key, rationale")

src = (ROOT / "bot.py").read_text(encoding="utf-8")
assert '"temperature": 0' in src
worst = max(r.get("latency_s", 0) for r in records)
print(f"[3] temperature=0 in Gemini call: True; worst latency={worst}s (<30s: {worst < 30})")
assert worst < 30

# Load categories + triggers for taboo/routing checks.
categories = {}
for f in (DATA / "categories").glob("*.json"):
    c = json.loads(f.read_text(encoding="utf-8"))
    categories[c["slug"]] = c
merchants = {}
for f in (DATA / "merchants").glob("*.json"):
    m = json.loads(f.read_text(encoding="utf-8"))
    merchants[m["merchant_id"]] = m
triggers = {}
for f in (DATA / "triggers").glob("*.json"):
    t = json.loads(f.read_text(encoding="utf-8"))
    triggers[t["id"]] = t
pairs = {p["test_id"]: p for p in json.loads((DATA / "test_pairs.json").read_text(encoding="utf-8"))["pairs"]}

taboo_hits, generic_hits, route_mismatch = [], [], []
for r in records:
    p = pairs[r["test_id"]]
    trig, merch = triggers[p["trigger_id"]], merchants[p["merchant_id"]]
    cat = categories[merch["category_slug"]]
    body = r["body"]
    for taboo in bot.get_taboo_list(cat):
        if re.search(re.escape(taboo), body, re.IGNORECASE):
            taboo_hits.append((r["test_id"], taboo))
    if bot.GENERIC_OFFER_RE.search(body):
        generic_hits.append(r["test_id"])
    exp = bot.ROUTING_TABLE[trig["kind"]]
    if r["send_as"] != exp["send_as"] or r["cta"] != exp["cta"]:
        route_mismatch.append((r["test_id"], trig["kind"], r["send_as"], r["cta"], exp))

print(f"[4] taboo hits: {len(taboo_hits)} {taboo_hits}")
print(f"[4] generic '% off' hits: {len(generic_hits)} {generic_hits}")
print(f"[4] routing mismatches: {len(route_mismatch)} {route_mismatch}")
assert not taboo_hits and not generic_hits and not route_mismatch
print("[4] 0 taboos, 0 generic discounts, 30/30 routing match — ALL CHECKS PASSED")

"""Runner: compose across all 30 test pairs (T01-T30) -> submission.jsonl.

Usage:
    python run_submission.py [--out submission.jsonl]

Each compose() call is timed (must be <30s). Temperature=0 is enforced in
bot.call_gemini_copywriter (verified from source before running).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "magicpin-ai-challenge" / "dataset" / "expanded"

sys.path.insert(0, str(ROOT))
import bot  # noqa: E402


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_by_id(folder: Path, obj_id: str, id_keys: tuple[str, ...]) -> dict:
    direct = folder / f"{obj_id}.json"
    if direct.exists():
        return load_json(direct)
    for f in folder.glob("*.json"):
        try:
            data = load_json(f)
        except Exception:
            continue
        for k in id_keys:
            if data.get(k) == obj_id:
                return data
    raise FileNotFoundError(f"{obj_id} not found in {folder}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="submission.jsonl")
    args = ap.parse_args()

    # 0. Prove temperature=0 is wired into the LLM copywriter call.
    src = (ROOT / "bot.py").read_text(encoding="utf-8")
    assert '"temperature": 0' in src, "temperature=0 not found in bot.py Gemini call"
    print("[check] temperature=0 present in bot.call_gemini_copywriter")

    pairs = load_json(DATA / "test_pairs.json")["pairs"]
    assert len(pairs) == 30, f"expected 30 pairs, got {len(pairs)}"
    pairs = sorted(pairs, key=lambda p: p["test_id"])

    # Preload categories once.
    categories: dict[str, dict] = {}
    for f in (DATA / "categories").glob("*.json"):
        c = load_json(f)
        categories[c["slug"]] = c

    # Simulated "now" matching judge's tick (from challenge-testing-brief examples)
    SIMULATED_NOW = datetime(2026, 4, 26, 10, 35, tzinfo=timezone.utc)

    out_path = ROOT / args.out
    latencies: list[float] = []
    import os as _os
    paced = bool(_os.environ.get("GEMINI_API_KEY", "").strip())
    with out_path.open("w", encoding="utf-8") as fh:
        for i, p in enumerate(pairs):
            if paced and i > 0:
                time.sleep(5)  # stay under free-tier RPM quota
            merchant = load_by_id(DATA / "merchants", p["merchant_id"], ("merchant_id",))
            trigger = load_by_id(DATA / "triggers", p["trigger_id"], ("id",))
            customer = None
            if p.get("customer_id"):
                customer = load_by_id(DATA / "customers", p["customer_id"], ("customer_id",))
            category = categories[merchant["category_slug"]]

            t0 = time.perf_counter()
            msg = bot.compose(category, merchant, trigger, customer, now=SIMULATED_NOW)
            dt = time.perf_counter() - t0
            latencies.append(dt)

            record = {
                "test_id": p["test_id"],
                "trigger_id": p["trigger_id"],
                "merchant_id": p["merchant_id"],
                "customer_id": p.get("customer_id"),
                "body": msg["body"],
                "cta": msg["cta"],
                "send_as": msg["send_as"],
                "suppression_key": msg["suppression_key"],
                "rationale": msg["rationale"],
                "latency_s": round(dt, 3),
            }
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(f"[{p['test_id']}] kind={trigger.get('kind')} cta={msg['cta']} "
                  f"send_as={msg['send_as']} latency={dt:.2f}s")

    mx = max(latencies)
    print(f"[done] wrote {len(pairs)} lines -> {out_path}")
    print(f"[timing] max={mx:.2f}s mean={sum(latencies)/len(latencies):.2f}s "
          f"all_under_30s={all(t < 30 for t in latencies)}")
    assert mx < 30, "a call exceeded the 30s budget"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# Vera — magicpin Merchant Assistant (Gemini 3.8-flash, temp 0)

Deterministic pipeline around a temperature-0 LLM copywriter: route trigger →
check 24h session + auto-reply → Gemini drafts JSON `{body, rationale}` →
post-validate (taboo scrub, canonical `Service @ ₹Price` offers). No key is
hardcoded; set `$env:GEMINI_API_KEY` (bot) and it is also picked up by
`judge_simulator.py`. No API key → deterministic template fallback (<30s).

## 4 custom heuristics

1. **Revenue-at-Stake math** — from real lapsed counts only
   (`lapsed_180d_plus`/`lapsed_90d_plus`, never `total_unique_ytd`):
   `pool = lapsed × canonical offer price`, plus a realistic 10% figure
   (e.g. "95 lapsed × ₹299 = ₹28.4K pool — 10% reactivation ≈ ₹2.8K").
2. **Accurate Peer Gap → volume-leak pivot** — CTR is compared exactly and
   never misreported: below peer states the % deficit; at/above peer praises
   the lead and pivots to the true lagging JSON metric (e.g. T25: CTR 5.8% is
   +45% above peer, so the message targets the 21% call-volume gap instead).
   No invented numbers — every figure comes from the context dicts.
3. **Auto-Reply × 24h window** — `0 ≤ (now − last_merchant_ts) ≤ 86400` decides
   template vs free-form; if the in-window "reply" matches the canned-reply
   regex/3×-hash, free-form is kept but the copy never acknowledges it —
   straight to the hook, noted in `rationale`.
4. **Consent-scope recall** — customer triggers inspect `consent.scope`: with
   only `promotional_offers` (no `recall_reminders`), the message is framed as
   claiming the offer with STOP opt-out, flagged in `rationale`. Recall copy is
   category-aware (gyms = membership renewal, never dental cleaning).

## Files

`bot.py` (`compose()` + 5 FastAPI endpoints), `conversation_handlers.py`
(`respond()` multi-turn), `run_submission.py` (30-pair generator),
`submission.jsonl` (T01–T30), `validate_submission.py` (taboo/%-off/routing).

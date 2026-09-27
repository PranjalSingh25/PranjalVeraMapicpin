"""Multi-turn conversation handling for the Vera MagicPin bot.

Implements AGENTS.md checklist item 4 and Rule B:

  1. Auto-reply detection — canned WhatsApp Business replies (regex over
     common Hindi/English canned phrasing OR 3+ identical message hashes)
     bypass conversational fluff (-> wait/end, never a qualifying question).
  2. Immediate intent handoff — affirmative signals ("yes i want to join",
     "let's do it", "haan kar do", ...) switch pitch_mode ->
     action_confirmation_mode at once, with zero follow-up qualifying
     questions.
  3. Dynamic language switching — the reply mirrors the latest merchant
     message: Roman-script Hinglish (or Devanagari) -> Hinglish reply,
     otherwise English.
  4. Graceful exit — opt-out phrasing ("stop", "not interested", ...) or
     3+ consecutive unanswered outbound nudges ends the conversation and
     suppresses further tick sends for that merchant.

Public entry point:

    respond(state: ConversationState, merchant_message: str) -> dict

Return shapes match POST /v1/reply:
    {"action": "send", "body": ..., "cta": ..., "rationale": ...}
    {"action": "wait", "wait_seconds": ..., "rationale": ...}
    {"action": "end", "rationale": ...}
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

PITCH_MODE = "pitch_mode"
ACTION_MODE = "action_confirmation_mode"
CLOSING_MODE = "closing_mode"

MAX_UNANSWERED_NUDGES = 3
AUTO_REPLY_WAIT_SECONDS = 14400  # 4h backoff while the owner is away

# ---------------------------------------------------------------------------
# 1. Auto-reply detection (AGENTS.md Rule B + "away right now" family)
# ---------------------------------------------------------------------------

AUTO_REPLY_RE = re.compile(
    r"(?i)(automated assistant|thank you for contacting|aapki jaankari ke liye"
    r"|will get back to you shortly|auto-reply|out of office"
    r"|away right now|currently unavailable|will respond shortly"
    r"|team will (respond|contact|reach)|please contact us later"
    r"|will call you back|do not reply to this|this is an auto"
    r"|thodi der (mein|me) sampark|hamari team sampark karegi)"
)


def _msg_hash(text: str) -> str:
    return hashlib.sha256((text or "").strip().lower().encode("utf-8")).hexdigest()


def is_auto_reply(message: str, recent_hashes: Optional[list[str]] = None) -> bool:
    """Canned-reply regex OR 3 identical hashes in a row."""
    if message and AUTO_REPLY_RE.search(message):
        return True
    if recent_hashes is not None and len(recent_hashes) >= 3:
        last3 = recent_hashes[-3:]
        if last3[0] and last3[0] == last3[1] == last3[2]:
            return True
    return False


# ---------------------------------------------------------------------------
# 2. Intent handoff — affirmative signals (English + Roman Hinglish)
# ---------------------------------------------------------------------------

AFFIRM_STRONG_RE = re.compile(
    r"(?i)(i want to join|let'?s do it|lets do it|go ahead|yes please send"
    r"|yes,?\s*send|confirm|proceed|i agree|what'?s next|let'?s go"
    r"|haan?\s+kar\s*do|han\s*kardo|kardo|\bkar\s*do\b|\bbhej\s*do\b"
    r"|haan?\s*karo|shuru\s*karo|done\s*karo"
    r"|theek\s*hai\s*,?\s*(kar\s*do|karo|bhej\s*do|shuru\s*karo)"
    r"|mujhe.*chahiye|haan.*chahiye|ok.*do it|yes.*join)"
)

# Bare acknowledgements count as acceptance only while pitching.
AFFIRM_WEAK_RE = re.compile(r"^\s*(yes|yeah|yep|haan?|han|ha|ok|okay|theek|accha|acha)\W*\s*$", re.IGNORECASE)

OPT_OUT_RE = re.compile(
    r"(?i)(\bstop\b|not interested|don'?t message|do not message|useless|spam"
    r"|bothering me|unsubscribe|no more messages?|wrong number"
    r"|band karo|mat bhejo|nahi[mn] chahiye|pareshan mat karo)"
)

# Phrases that must NEVER appear once we are in action mode.
QUALIFYING_RE = re.compile(
    r"(?i)(would you|do you|can you tell|what if|how about|which of these"
    r"|tell me (more|about)|kya aap|aapko kaunsa|kaunsa.*pasand)"
)


# ---------------------------------------------------------------------------
# 3. Dynamic language switching — English vs Roman-script Hinglish
# ---------------------------------------------------------------------------

DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")

HINGLISH_WORDS = frozenset(
    "haan han karo kar kardo bhejo bhej aap aapki aapko aapke tum tumhe "
    "tumhara kya kaise kab kahan kaun nahi nahin naheen chahiye mujhe mujhko "
    "mera meri mere apka apki theek thik accha achha acha bahut bohat shukriya "
    "dhanyavad samajh gaya gayi baat batao bataiye bhejen zaroor jarur abhi "
    "thoda zyada jyada kaam din kal aaj mat band wapas aur lekin kyunki agle "
    "pichle shuru badhiya milna jaldi der raat subah shaam hafte mahine paise "
    "dukaan theekhai".split()
)


def detect_language(text: str) -> str:
    """Return 'hinglish' for Devanagari or Roman-script Hindi, else 'english'."""
    if not text:
        return "english"
    if DEVANAGARI_RE.search(text):
        return "hinglish"
    tokens = re.findall(r"[a-zA-Z]+", text.lower())
    if any(t in HINGLISH_WORDS for t in tokens):
        return "hinglish"
    return "english"


# ---------------------------------------------------------------------------
# Conversation state
# ---------------------------------------------------------------------------

@dataclass
class ConversationState:
    conversation_id: str = ""
    merchant_id: Optional[str] = None
    mode: str = PITCH_MODE
    outbound_unanswered: int = 0      # consecutive bot sends, no real reply yet
    merchant_history: list[str] = field(default_factory=list)
    merchant_hashes: list[str] = field(default_factory=list)
    auto_reply_streak: int = 0
    ended: bool = False
    language: str = "english"         # mirror of the latest merchant message

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ConversationState":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


# In-memory stores shared with bot.py tick/reply endpoints.
_states: dict[str, ConversationState] = {}
_merchant_unanswered: dict[str, int] = {}
_merchant_suppressed: set[str] = set()


def get_or_create_state(conversation_id: str, merchant_id: Optional[str] = None) -> ConversationState:
    st = _states.get(conversation_id)
    if st is None:
        st = ConversationState(conversation_id=conversation_id, merchant_id=merchant_id)
        _states[conversation_id] = st
    elif merchant_id and not st.merchant_id:
        st.merchant_id = merchant_id
    return st


def record_tick_send(conversation_id: str, merchant_id: Optional[str] = None) -> None:
    """Account one proactive outbound nudge (tick-time)."""
    st = get_or_create_state(conversation_id, merchant_id)
    st.outbound_unanswered += 1
    if merchant_id:
        _merchant_unanswered[merchant_id] = _merchant_unanswered.get(merchant_id, 0) + 1


def record_real_reply(merchant_id: Optional[str]) -> None:
    if merchant_id and merchant_id in _merchant_unanswered:
        _merchant_unanswered[merchant_id] = 0


def suppress_merchant(merchant_id: Optional[str]) -> None:
    if merchant_id:
        _merchant_suppressed.add(merchant_id)


def should_suppress_merchant(merchant_id: Optional[str], limit: int = MAX_UNANSWERED_NUDGES) -> bool:
    if not merchant_id:
        return False
    return merchant_id in _merchant_suppressed or _merchant_unanswered.get(merchant_id, 0) >= limit


def reset_all() -> None:
    """Test helper — clear every in-memory store."""
    _states.clear()
    _merchant_unanswered.clear()
    _merchant_suppressed.clear()


# ---------------------------------------------------------------------------
# Mirrored copy (no qualifying questions in action mode — enforced by tests)
# ---------------------------------------------------------------------------

_COPY = {
    "action_confirm": {
        "english": (
            "Great \u2014 on it. I have drafted the next step and pre-filled everything "
            "from your account. Reply CONFIRM and I will send it, or tell me one tweak."
        ),
        "hinglish": (
            "Badhiya \u2014 kaam shuru. Agla step draft karke aapke account se pre-fill "
            "kar diya hai. CONFIRM reply karo main bhej doonga, ya ek tweak bata do."
        ),
    },
    "action_progress": {
        "english": (
            "Done \u2014 the first step is complete and logged to your account. "
            "Reply YES and I will roll out the next one."
        ),
        "hinglish": (
            "Ho gaya \u2014 pehla step complete aur aapke account mein logged hai. "
            "YES reply karo, agla step rollout kar doonga."
        ),
    },
    "pitch_followup": {
        "english": (
            "Got it \u2014 one quick next step is drafted from your account. "
            "Want me to send it?"
        ),
        "hinglish": (
            "Samajh gaya \u2014 aapke account se agla step draft ready hai. Bhejoon?"
        ),
    },
}


def _send(body_key: str, lang: str, cta: str, rationale: str) -> dict:
    return {"action": "send", "body": _COPY[body_key][lang], "cta": cta, "rationale": rationale}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def respond(state: ConversationState, merchant_message: str) -> dict:
    """Advance one conversation turn. Mutates `state`; returns the bot action.

    Priority order (first match wins):
      ended -> opt-out -> auto-reply -> unanswered-cap -> intent handoff ->
      action-mode progress -> pitch-mode follow-up.
    """
    if isinstance(state, dict):  # tolerate plain-dict callers
        state = ConversationState.from_dict(state)

    msg = merchant_message or ""
    lang = detect_language(msg)
    state.language = lang
    state.merchant_history.append(msg)
    state.merchant_hashes.append(_msg_hash(msg))

    if state.ended:
        return {"action": "end", "rationale": "Conversation already closed; staying closed."}

    # 4a. Opt-out / hostile -> acknowledge and close, suppress future nudges.
    if msg and OPT_OUT_RE.search(msg):
        state.ended = True
        state.mode = CLOSING_MODE
        suppress_merchant(state.merchant_id)
        return {
            "action": "end",
            "rationale": (
                "Merchant opted out / expressed disinterest "
                f"({msg.strip()[:60]}); acknowledged, closing and suppressing future nudges."
            ),
        }

    # 1. Auto-reply -> bypass ALL conversational fluff (no questions, no pitch).
    if is_auto_reply(msg, state.merchant_hashes):
        state.auto_reply_streak += 1
        if state.auto_reply_streak >= 3:
            state.ended = True
            state.mode = CLOSING_MODE
            return {
                "action": "end",
                "rationale": (
                    "Canned auto-reply 3x in a row (regex/identical-hash); owner not at phone. "
                    "Graceful exit — will connect with the manager directly."
                ),
            }
        return {
            "action": "wait",
            "wait_seconds": AUTO_REPLY_WAIT_SECONDS,
            "rationale": (
                "Detected merchant auto-reply (canned WhatsApp Business phrasing); "
                "skipping follow-up questions and backing off to wait for the owner."
            ),
        }

    # Empty heartbeat with nobody home -> cap applies (conversation- or
    # merchant-level: 3+ tick nudges without a single real reply).
    if not msg.strip() and (
        state.outbound_unanswered >= MAX_UNANSWERED_NUDGES
        or should_suppress_merchant(state.merchant_id)
    ):
        state.ended = True
        state.mode = CLOSING_MODE
        suppress_merchant(state.merchant_id)
        return {
            "action": "end",
            "rationale": (
                f"{MAX_UNANSWERED_NUDGES}+ outbound messages unanswered; "
                "stopping nudges and closing gracefully."
            ),
        }

    # Genuine human message from here on.
    state.auto_reply_streak = 0
    state.outbound_unanswered = 0
    record_real_reply(state.merchant_id)

    # 2. Affirmative intent -> immediate handoff, zero qualifying questions.
    strong = bool(msg and AFFIRM_STRONG_RE.search(msg))
    weak = bool(msg and state.mode == PITCH_MODE and AFFIRM_WEAK_RE.match(msg))
    if strong or weak:
        state.mode = ACTION_MODE
        return _send(
            "action_confirm", lang, "binary",
            "Merchant signalled affirmative intent; switched pitch_mode -> "
            "action_confirmation_mode immediately with a single binary CONFIRM, no qualifying questions.",
        )

    # Mid-action conversation -> keep executing, never re-qualify.
    if state.mode == ACTION_MODE:
        return _send(
            "action_progress", lang, "binary",
            "Already in action_confirmation_mode; advancing execution with a binary YES, no re-qualifying.",
        )

    # 3/4. Default pitch follow-up, mirrored to the merchant's language.
    return _send(
        "pitch_followup", lang, "open_ended",
        f"Acknowledged merchant reply and advanced with one low-friction question, mirrored to {lang}.",
    )

"""Idaho READY / SET / GO evacuation-phase detection from free-text CAP content.

Real-world FEMA IPAWS civil alerts do NOT carry a machine-readable phase —
CAP ``<responseType>`` does not exist in the wild. The phase (READY / SET /
GO) instead shows up as free text inside ``headline``, ``description``, and
the ``CMAMtext``/``CMAMlongtext`` ``<parameter>`` values, phrased however the
issuing agency happened to write it ("Level 3 GO NOW", "Set to GO",
"Evacuation Warning", ...). This module scans that free text for the phrases
agencies actually use and returns the phase, or ``None`` when nothing
matches — callers must never guess a phase, since a false GO would broadcast
an evacuation order that was never issued.
"""
from __future__ import annotations

import re

# ── Strong multi-word phrases (checked case-insensitively) ───────────────────
# ANY match sets that phase's hit flag. Order within a list is irrelevant —
# the final result is decided purely by GO > SET > READY precedence below,
# never by which phrase or text argument matched first.
_GO_PHRASES = [
    r"\bGO\s+NOW\b",
    r"\bLEVEL\s+3\s+GO\b",
    r"\bLEVEL\s+3\b",
    r"\bLEVEL\s+III\b",
    r"\bGO\s+EVACUATION\b",
    r"\bIMMEDIATE\s+EVACUATION\b",
    r"\bEVACUATE\s+NOW\b",
    r"\bEVACUATION\s+ORDER\b",
    r"\bSET\s+TO\s+GO\b",   # "Set to GO" — the standalone-GO idiom, spelled out
]

_SET_PHRASES = [
    r"\bLEVEL\s+2\b",
    r"\bLEVEL\s+II\b",
    r"\bPREPARE\s+TO\s+EVACUATE\b",
    r"\bEVACUATION\s+WARNING\b",
    r"\bBE\s+READY\s+TO\s+LEAVE\b",
]

_READY_PHRASES = [
    r"\bLEVEL\s+1\b",
    r"\bLEVEL\s+I\b",
    r"\bEVACUATION\s+ADVISORY\b",
]

_PRECEDENCE = ("GO", "SET", "READY")

_PHRASE_RE = {
    "GO": re.compile("|".join(_GO_PHRASES), re.IGNORECASE),
    "SET": re.compile("|".join(_SET_PHRASES), re.IGNORECASE),
    "READY": re.compile("|".join(_READY_PHRASES), re.IGNORECASE),
}

# A bare level-word (GO/SET/READY) counts ONLY when it is:
#   1. standalone (word-boundaried) AND written in the exact uppercase form
#      (narrative prose never shouts a whole word in caps: "go to the
#      fairgrounds", "set to arrive", "via Go Creek Road" all fail this), AND
#   2. accompanied elsewhere in the same text by other alert/evacuation
#      vocabulary, so a bare "GO"/"SET"/"READY" floating in unrelated text
#      can't fire on its own.
# This is what lets "LEVEL I SET Alert" resolve as SET (word beats numeral —
# err upward) even though "LEVEL I" alone would read as READY.
_STANDALONE_TOKEN_RE = {
    "GO": re.compile(r"\bGO\b"),
    "SET": re.compile(r"\bSET\b"),
    "READY": re.compile(r"\bREADY\b"),
}
_CONTEXT_CUE_RE = re.compile(
    r"evacuat|level|alert|status|notice|prepar|leave|order|warning|advisory",
    re.IGNORECASE,
)


def detect_phase(*texts: "str | None") -> "str | None":
    """Scan the given texts for Idaho READY/SET/GO evacuation-phase language.

    All provided texts are combined and scanned together (case-insensitive
    for the strong phrases). The HIGHEST phase found wins — GO > SET > READY
    — never the first match. Returns None when nothing matches; never
    guesses.
    """
    combined = "\n".join(t for t in texts if t)
    if not combined:
        return None

    has_cue = bool(_CONTEXT_CUE_RE.search(combined))

    for phase in _PRECEDENCE:
        if _PHRASE_RE[phase].search(combined):
            return phase
        if has_cue and _STANDALONE_TOKEN_RE[phase].search(combined):
            return phase

    return None

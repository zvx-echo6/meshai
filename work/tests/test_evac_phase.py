"""Tests for meshai.notifications.evac_phase.detect_phase.

Cases are drawn from REAL FEMA IPAWS alert headline/CMAMtext/description
strings (see tests/fixtures/ipaws/) plus explicit false-positive guards, since
a wrong GO detection would broadcast an evacuation order that was never
issued.
"""
from __future__ import annotations

import pytest

from meshai.notifications.evac_phase import detect_phase


# ============================================================
# real strings -> expected phase
# ============================================================

@pytest.mark.parametrize("text, expected", [
    ("LEVEL 1 READY - Evacuation Status", "READY"),
    ("Level 2 Set Alert", "SET"),
    ("LEVEL I SET Alert", "SET"),                      # word beats numeral; err upward
    ("Level 3 - Go Now", "GO"),
    ("Level 3- Go Now", "GO"),
    ("Ohio Gulch GO Evacuation Status", "GO"),
    ("Indian Creek Set to GO.", "GO"),                 # highest wins, NOT SET
    ("Immediate Evacuation", "GO"),
    ("Prepare to Evacuate", "SET"),
    ("BRUSH FIRE", None),
    ("Owyhee County Closure Area", None),
    ("Endangered Missing Person Alert", None),
    (
        "Jackson County Sheriff's Office- Level 3 GO NOW evacuation notice "
        "UPGRADED for JAC-126",
        "GO",
    ),
])
def test_detect_phase_real_strings(text, expected):
    assert detect_phase(text) == expected


# ============================================================
# false-positive guards — bare lowercase / narrative usage must NOT match
# ============================================================

@pytest.mark.parametrize("text", [
    "residents should go to the Blaine County Fairgrounds",
    "crews are set to arrive by 0600",
    "evacuate via Go Creek Road",
])
def test_detect_phase_false_positive_guards(text):
    assert detect_phase(text) is None


# ============================================================
# precedence + multi-arg scanning
# ============================================================

def test_precedence_highest_always_wins_regardless_of_arg_order():
    # SET language in the first text, GO language in the second — GO must win.
    assert detect_phase("Prepare to Evacuate", "Level 3 - Go Now") == "GO"
    # Same phrases, arguments reversed — still GO (order-independent).
    assert detect_phase("Level 3 - Go Now", "Prepare to Evacuate") == "GO"


def test_detect_phase_scans_all_texts_together():
    # No single text alone carries a phase; combined they do (SET here,
    # since "LEVEL 2" is an explicit SET phrase and no GO phrase is present).
    assert detect_phase("Jackson County Sheriff's Office", "Level 2 Set Alert") == "SET"


def test_detect_phase_none_texts_and_empty_input_are_safe():
    assert detect_phase(None, None) is None
    assert detect_phase() is None
    assert detect_phase("") is None
    assert detect_phase(None, "BRUSH FIRE", None) is None

"""Two adjustments on top of the report's algorithm (triage.py stays unchanged).

Both were found while testing the continuous monitor, and both are deviations
from the text of the final report, agreed with the team in October 2026.

1. Delta-NEWS2 from comparable parameters only.
   The report upgrades the colour when NEWS2 rises by 2 or more in 60 s. Taken
   literally, ticking "supplemental oxygen" (+2) or typing the first cuff
   pressure counts as deterioration, and the same 2 points are counted twice:
   once in the score, once as an upgrade. Here the rise is summed only over
   parameters that had a score at both moments, and supplemental oxygen is
   left out because it is a treatment, not a sign.

2. Escalate at once, de-escalate after a hold.
   A score sitting on a band edge (HR 110/112, say) flipped the colour every
   second. The displayed colour is the worst raw colour of the last HOLD_S
   seconds: it rises immediately and only falls once the lower colour has held
   for the whole window.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from app.services import triage as TR

HOLD_S = 30.0
# Supplemental oxygen is a treatment decision, not a physiological sign
NON_PHYSIOLOGICAL = {"oksigen"}


def comparable_delta(past_parts: dict | None, now_parts: dict) -> int | None:
    """Rise in NEWS2 over parameters scored at both moments."""
    if past_parts is None:
        return None
    total = 0
    compared = 0
    for name, now_score in now_parts.items():
        if name in NON_PHYSIOLOGICAL:
            continue
        past_score = past_parts.get(name)
        if now_score is None or past_score is None:
            continue
        total += now_score - past_score
        compared += 1
    return total if compared else None


def classify(vitals: dict, op: TR.Operator, past_parts: dict | None) -> TR.Triage:
    """triage.classify with the comparable delta, keeping its order of rules."""
    # workup=False and no history: the delta upgrade and the work-up modifier
    # are applied below, in the same order classify() applies them.
    result = TR.classify(vitals, op, None, workup=False)
    result.delta = comparable_delta(past_parts, result.news.parts)

    upgrades: list[str] = []
    if result.delta is not None and result.delta >= TR.DELTA_UPGRADE and result.colour < TR.MERAH:
        result.colour += 1
        result.source = f"ΔNEWS2 +{result.delta}"
        upgrades.append(f"NEWS2 naik {result.delta} dalam {TR.DELTA_WINDOW_S:.0f} detik")

    if result.colour == TR.HIJAU and (op.elderly or op.paediatric or op.comorbid):
        result.colour = TR.KUNING
        result.source = "work-up"
        upgrades.append("dinaikkan karena usia rentan atau komorbiditas")

    result.alerts = upgrades + result.alerts
    return result


@dataclass
class Displayed:
    colour: int                 # what the screen shows
    raw: int                    # what this second's evaluation says
    hold_remaining_s: float | None   # seconds until a lower colour may show, if holding


class ColourHold:
    """Worst raw colour over the last HOLD_S seconds."""

    def __init__(self, hold_s: float = HOLD_S):
        self.hold_s = hold_s
        self._window: deque[tuple[float, int]] = deque()

    def reset(self) -> None:
        self._window.clear()

    def update(self, now: float, raw: int) -> Displayed:
        if self._window and self._window[-1][0] > now:
            # The wall clock went backwards (set by hand on a board without
            # RTC). Old entries would never expire and the hold would stick.
            self._window.clear()
        self._window.append((now, raw))
        while self._window and now - self._window[0][0] > self.hold_s:
            self._window.popleft()
        shown = max(colour for _, colour in self._window)
        remaining = None
        if shown > raw:
            # The newest moment the shown colour was still raw; it may drop hold_s after that
            last_high = max(t for t, colour in self._window if colour == shown)
            remaining = max(0.0, self.hold_s - (now - last_high))
        return Displayed(shown, raw, remaining)

"""Tell the crew when the patient gets worse, so they can hurry to hospital.

Two triggers, agreed with the team in October 2026:

  MERAH  The displayed category becomes MERAH during transport, from any
         lower category. Alarm that repeats until acknowledged. Not raised
         when the patient is already MERAH at pickup: the crew knows.

  NEWS2  NEWS2 has risen by RISE_ALERT or more against a reference, and has
         stayed up for CONFIRM_S seconds, so a sensor knocked for a second
         does not sound. One beep. HIJAU to KUNING alone is not announced.

The reference is kept per parameter: the first score measured after pickup.
After an alert it moves to the scores at that moment, so the same rise is
not announced twice (1 -> 4 alerts, 4 -> 5 does not, 4 -> 7 does). When a
parameter improves and stays better for LOWER_AFTER_S, its reference follows
it down, so getting worse again later is caught again.

The rise counts only parameters scored both in the reference and now, and
leaves out supplemental oxygen, as the delta-NEWS2 rule in triage_policy.py
does: giving oxygen is a treatment, not a sign of deterioration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services import triage as TR
from app.services.triage_policy import NON_PHYSIOLOGICAL

RISE_ALERT = 3
CONFIRM_S = 5.0
LOWER_AFTER_S = 60.0
HURRY = "Pertimbangkan mempercepat perjalanan ke rumah sakit."

PART_LABEL = {
    "respirasi": "laju napas", "spo2": "SpO₂", "sistolik": "TD sistolik",
    "nadi": "nadi", "kesadaran": "kesadaran", "suhu": "suhu",
}
COLOUR_NAME = {TR.HIJAU: "HIJAU", TR.KUNING: "KUNING", TR.MERAH: "MERAH"}


@dataclass
class Alert:
    kind: str                     # MERAH | NEWS2
    level: str                    # alarm (repeats) | beep (once)
    title: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)


class DeteriorationWatch:
    def __init__(self) -> None:
        self.reset()

    def reset(self, baseline: dict[str, int] | None = None, colour: int | None = None) -> None:
        """Start watching a patient. colour is the category already shown, if any."""
        self.baseline: dict[str, int] = dict(baseline or {})
        self.colour = colour
        self._rise_since: float | None = None
        self._lower_since: dict[str, float] = {}

    def rise(self, parts: dict[str, int | None]) -> tuple[int, list[tuple[str, int]]]:
        """Net NEWS2 change against the reference, and the parameters that went up."""
        total = 0
        worse = []
        for name, now in parts.items():
            then = self.baseline.get(name)
            if name in NON_PHYSIOLOGICAL or now is None or then is None:
                continue
            total += now - then
            if now > then:
                worse.append((name, now - then))
        return total, worse

    def update(self, now: float, parts: dict[str, int | None], colour: int,
               news_total: int | None, news_measured: int) -> Alert | None:
        self._fill_and_lower(now, parts)

        previous = self.colour
        self.colour = colour
        alert = None
        if previous is not None and previous != TR.MERAH and colour == TR.MERAH:
            alert = Alert(
                kind="MERAH",
                level="alarm",
                title="Pasien memburuk: kategori MERAH",
                message=f"Kategori naik dari {COLOUR_NAME[previous]} ke MERAH. {HURRY}",
                data={"from": COLOUR_NAME[previous], "to": "MERAH"},
            )

        rise, worse = self.rise(parts)
        if rise >= RISE_ALERT:
            if self._rise_since is None:
                self._rise_since = now
            if alert is None and now - self._rise_since >= CONFIRM_S:
                detail = ", ".join(f"{PART_LABEL.get(name, name)} +{delta}" for name, delta in worse)
                alert = Alert(
                    kind="NEWS2",
                    level="beep",
                    title=f"NEWS2 naik {rise} poin",
                    message=f"Naik pada {detail}. NEWS2 sekarang {news_total} ({news_measured}/7 parameter). {HURRY}",
                    data={"rise": rise, "news2": news_total},
                )
        else:
            self._rise_since = None

        if alert:
            # Whatever was announced becomes the new reference
            self.baseline.update({k: v for k, v in parts.items() if k not in NON_PHYSIOLOGICAL and v is not None})
            self._rise_since = None
            self._lower_since.clear()
            alert.data["baseline"] = dict(self.baseline)
        return alert

    def _fill_and_lower(self, now: float, parts: dict[str, int | None]) -> None:
        for name, score in parts.items():
            if name in NON_PHYSIOLOGICAL or score is None:
                continue
            if name not in self.baseline:
                self.baseline[name] = score          # first time this parameter is measured
                continue
            if score < self.baseline[name]:
                since = self._lower_since.setdefault(name, now)
                if now - since >= LOWER_AFTER_S:
                    self.baseline[name] = score      # improved and stayed better
                    self._lower_since.pop(name, None)
            else:
                self._lower_since.pop(name, None)

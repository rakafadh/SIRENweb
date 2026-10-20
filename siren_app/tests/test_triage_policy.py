"""The two agreed adjustments on top of the report's algorithm."""

from app.services import triage as TR
from app.services import triage_policy as policy

STABLE = {"hr": 75, "spo2": 98, "rr": 16, "temp": 36.8, "pi": 1.5}


def parts(vitals, **op):
    return TR.news2(vitals, TR.Operator(systolic=125, **op)).parts


# --- comparable delta ------------------------------------------------------
def test_starting_oxygen_is_not_deterioration():
    before = parts(STABLE)
    after = parts(STABLE, on_oxygen=True)
    assert TR.news2(STABLE, TR.Operator(systolic=125, on_oxygen=True)).total == 2
    assert policy.comparable_delta(before, after) == 0


def test_first_cuff_reading_is_not_deterioration():
    before = TR.news2(STABLE, TR.Operator()).parts          # systolic not measured yet
    after = TR.news2(STABLE, TR.Operator(systolic=88)).parts
    assert after["sistolik"] == 3
    assert policy.comparable_delta(before, after) == 0


def test_real_deterioration_still_counts():
    before = parts(STABLE)
    after = parts({**STABLE, "hr": 115, "spo2": 94})        # +2 nadi, +1 spo2
    assert policy.comparable_delta(before, after) == 3


def test_new_confusion_counts():
    assert policy.comparable_delta(parts(STABLE), parts(STABLE, acvpu="confusion")) == 3


def test_no_past_means_no_delta():
    assert policy.comparable_delta(None, parts(STABLE)) is None


def test_oxygen_case_stays_one_level():
    # NEWS2 3 (green) + oxygen = 5 (yellow). The report's literal rule would
    # then add a delta upgrade and show red.
    sick = {**STABLE, "hr": 112, "spo2": 95}
    op = TR.Operator(systolic=125, ambulatory=True, on_oxygen=True)
    result = policy.classify(sick, op, parts(sick, ambulatory=True))
    assert result.news.total == 5
    assert result.delta == 0
    assert result.name == "Kuning"


def test_delta_upgrade_and_work_up_keep_the_report_order():
    # Elderly and green: work-up lifts to yellow. With a delta upgrade first,
    # the patient is already yellow and work-up adds nothing (same as triage.classify).
    op = TR.Operator(systolic=125, ambulatory=True, elderly=True)
    worse = {**STABLE, "hr": 105, "spo2": 95}               # total 2, delta +2
    result = policy.classify(worse, op, parts(STABLE, ambulatory=True, elderly=True))
    assert result.name == "Kuning"
    assert result.source == "ΔNEWS2 +2"
    assert result.alerts[0] == "NEWS2 naik 2 dalam 60 detik"

    calm = policy.classify(STABLE, op, parts(STABLE))
    assert calm.name == "Kuning" and calm.source == "work-up"


# --- hold -------------------------------------------------------------------
def test_colour_rises_at_once_and_falls_after_hold():
    hold = policy.ColourHold(30)
    assert hold.update(0, TR.KUNING).colour == TR.KUNING
    up = hold.update(1, TR.MERAH)
    assert up.colour == TR.MERAH and up.hold_remaining_s is None

    held = hold.update(2, TR.KUNING)
    assert held.colour == TR.MERAH and held.raw == TR.KUNING
    assert held.hold_remaining_s == 29

    assert hold.update(30, TR.KUNING).colour == TR.MERAH
    assert hold.update(31.5, TR.KUNING).colour == TR.KUNING


def test_flapping_on_a_band_edge_shows_one_steady_colour():
    hold = policy.ColourHold(30)
    shown = {hold.update(t, TR.MERAH if t % 2 else TR.KUNING).colour for t in range(1, 25)}
    assert shown == {TR.MERAH}


def test_clock_going_backwards_does_not_freeze_the_hold():
    hold = policy.ColourHold(30)
    hold.update(5000, TR.MERAH)
    # Clock set back an hour: the old red must not be held forever
    assert hold.update(1400, TR.HIJAU).colour == TR.HIJAU


def test_flapping_between_lower_colours_steps_down_to_the_worse_of_them():
    hold = policy.ColourHold(30)
    hold.update(0, TR.MERAH)
    for t in range(1, 40):
        last = hold.update(t, TR.KUNING if t % 2 else TR.HIJAU)
    assert last.colour == TR.KUNING

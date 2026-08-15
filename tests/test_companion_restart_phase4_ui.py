import copy
import inspect
import os
import time
from types import SimpleNamespace

import pytest

from dzll_launcher.companion_restart_phase2_consumers import ConsumerModelStatus
from dzll_launcher.companion_restart_phase2_runtime import phase2_learning_summary
from dzll_launcher.server_companion_ui import (
    ServerCompanionPanel,
    format_restart_cycle_text,
    format_restart_local_time,
    restart_learning_presentation,
)


NOW = 1_800_000_000.0


def decision(
    status,
    *,
    schedule=0.65,
    period=0.0,
    cycle=None,
    prediction=None,
    usable=False,
):
    return SimpleNamespace(
        model_status=status,
        schedule_existence_confidence=schedule,
        fundamental_period_confidence=period,
        selected_period_seconds=cycle,
        prediction_at=prediction,
        prediction_usable=usable,
    )


class FakeWidget:
    def __init__(self):
        self.text = ""
        self.visible = True
        self.classes = set()
        self.sensitive = True
        self.active = False
        self.value = 0

    def set_text(self, value):
        self.text = value

    def set_visible(self, value):
        self.visible = bool(value)

    def add_css_class(self, value):
        self.classes.add(value)

    def remove_css_class(self, value):
        self.classes.discard(value)

    def set_sensitive(self, value):
        self.sensitive = bool(value)

    def set_active(self, value):
        self.active = bool(value)

    def get_active(self):
        return self.active

    def set_value(self, value):
        self.value = value


def fake_panel():
    names = (
        "restart_learning_box",
        "restart_cycle_label",
        "restart_next_label",
        "restart_countdown_label",
        "restart_confidence_label",
        "restart_cycle_value_label",
        "restart_next_value_label",
        "restart_countdown_hh_label",
        "restart_countdown_mm_label",
        "restart_confidence_value_label",
        "restart_cycle_row",
        "restart_next_row",
        "restart_countdown_row",
        "restart_confidence_row",
    )
    panel = SimpleNamespace(**{name: FakeWidget() for name in names})
    panel._start_restart_countdown_colon = lambda: None
    panel._stop_restart_countdown_colon = lambda: None
    return panel


def apply_to_fake_panel(summary):
    panel = fake_panel()
    ServerCompanionPanel.set_restart_learning_summary(panel, summary)
    return panel


def test_pattern_observed_uses_schedule_confidence_and_learning_wording():
    summary = phase2_learning_summary(
        decision(ConsumerModelStatus.PATTERN_OBSERVED, schedule=0.65),
        now=NOW,
    )
    assert summary["cycle_text"] == "Recurring timing observed"
    assert summary["confidence_kind"] == "pattern"
    assert summary["confidence_percent"] == 65
    assert summary["confidence_label"] == "Pattern Confidence:"
    assert summary["confidence_severity"] == "moderate"
    assert summary["next_text"] == "Period still learning"
    assert summary["next_visible"]
    assert not summary["countdown_visible"]

    panel = apply_to_fake_panel(summary)
    assert panel.restart_cycle_label.text == "Restart Cycle:"
    assert panel.restart_cycle_value_label.text == "Recurring timing observed"
    assert panel.restart_confidence_label.text == "Pattern Confidence:"
    assert panel.restart_confidence_value_label.text == "65%"
    assert "0%" not in panel.restart_confidence_value_label.text
    assert panel.restart_next_value_label.text == "Period still learning"
    assert panel.restart_next_row.visible
    assert not panel.restart_countdown_row.visible
    assert panel.restart_confidence_value_label.classes == {"ping-yellow"}


def test_low_pattern_confidence_near_display_threshold_is_never_green():
    summary = phase2_learning_summary(
        decision(ConsumerModelStatus.PATTERN_OBSERVED, schedule=0.60),
        now=NOW,
    )
    presentation = restart_learning_presentation(summary)
    assert presentation["confidence_percent"] == 60
    assert presentation["confidence_style_class"] == "ping-yellow"
    assert presentation["confidence_style_class"] not in {"ping-greeny", "ping-good"}


def test_live_normal_four_hour_schedule_confidence_reaches_final_widget_as_95_percent():
    summary = {
        "authority_consumer": True,
        "confidence_percent": 95,
        "confidence_kind": "schedule",
        "confidence_label": "Confidence:",
        "confidence_visible": True,
        "cycle_text": "Confirmed: Every 4 hours",
        "next_text": "Prediction suspended",
        "next_restart_at": None,
        "next_visible": False,
        "countdown_text": "--",
        "countdown_visible": False,
        "countdown_safe": False,
        "prediction_usable": False,
        "presentation_key": "confirmed_cycle",
        "reason_codes": ("normal_schedule_confidence_display",),
    }

    presentation = restart_learning_presentation(summary)
    panel = apply_to_fake_panel(summary)

    assert presentation["confidence_percent"] == 95
    assert panel.restart_cycle_value_label.text == "Every 4 Hours"
    assert panel.restart_confidence_label.text == "Confidence:"
    assert panel.restart_confidence_value_label.text == "95%"
    assert (
        f"{panel.restart_confidence_label.text} "
        f"{panel.restart_confidence_value_label.text}"
        == "Confidence: 95%"
    )
    assert not panel.restart_next_row.visible
    assert not panel.restart_countdown_row.visible


def test_selected_three_hour_period_uses_period_confidence_and_prediction():
    summary = phase2_learning_summary(
        decision(
            ConsumerModelStatus.LIKELY_PERIOD,
            schedule=0.85,
            period=0.72,
            cycle=3 * 3600,
            prediction=NOW + 5400,
            usable=True,
        ),
        now=NOW,
    )
    assert summary["cycle_text"] == "Every 3 hours"
    assert summary["confidence_kind"] == "period"
    assert summary["confidence_percent"] == 72
    assert summary["confidence_label"] == "Confidence:"
    assert summary["next_text"] not in {"Period still learning", "Prediction suspended"}
    assert summary["countdown_text"] == "01:30"

    panel = apply_to_fake_panel(summary)
    assert panel.restart_cycle_value_label.text == "Every 3 Hours"
    assert panel.restart_confidence_value_label.text == "72%"
    assert panel.restart_next_value_label.text == format_restart_local_time(
        summary["next_restart_at"]
    )
    assert panel.restart_countdown_hh_label.text == "01"
    assert panel.restart_countdown_mm_label.text == "30"
    assert panel.restart_next_row.visible
    assert panel.restart_countdown_row.visible
    assert panel.restart_confidence_value_label.classes == {"ping-yellow"}


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        ("Confirmed: Every 1 hour", "Every 1 Hour"),
        ("Confirmed: Every 2 hours", "Every 2 Hours"),
        ("Confirmed: Every 3 hours", "Every 3 Hours"),
        ("Confirmed: Every 6 hours", "Every 6 Hours"),
    ),
)
def test_confirmed_whole_hour_cycle_formatting_is_generic(source, expected):
    assert format_restart_cycle_text(source) == expected
    assert "Confirmed:" not in format_restart_cycle_text(source)


def test_likely_cycle_prefix_is_not_shown_in_companion_row():
    summary = {
        "confidence_percent": 95,
        "confidence_visible": True,
        "cycle_text": "Likely: Every 4 hours",
        "next_text": "Prediction suspended",
        "next_visible": False,
        "countdown_visible": False,
        "prediction_usable": False,
    }

    panel = apply_to_fake_panel(summary)

    assert (
        f"{panel.restart_cycle_label.text} {panel.restart_cycle_value_label.text}"
        == "Restart Cycle: Every 4 Hours"
    )


def test_confirmed_schema4_three_hour_panel_text_is_exact_and_non_mutating(
    utc_timezone,
):
    summary = {
        "authority_consumer": True,
        "confidence_percent": 97,
        "confidence_kind": "period",
        "confidence_label": "Confidence:",
        "confidence_visible": True,
        "cycle_text": "Confirmed: Every 3 hours",
        "cycle_period_seconds": 10800,
        "next_text": "46860",
        "next_restart_at": 46860.75,
        "countdown_text": "02:14",
        "next_visible": True,
        "countdown_visible": True,
        "countdown_safe": True,
        "prediction_usable": True,
        "presentation_key": "confirmed_cycle",
        "reason_codes": ("safe_h3_prediction_available",),
    }
    before = copy.deepcopy(summary)

    panel = apply_to_fake_panel(summary)

    assert f"{panel.restart_cycle_label.text} {panel.restart_cycle_value_label.text}" == (
        "Restart Cycle: Every 3 Hours"
    )
    assert panel.restart_next_value_label.text == "13:01"
    assert panel.restart_confidence_value_label.text == "95%"
    assert "Confirmed:" not in panel.restart_cycle_value_label.text
    assert "46860" not in panel.restart_next_value_label.text
    assert summary == before
    assert summary["cycle_period_seconds"] == 10800


@pytest.mark.parametrize(
    ("internal_percent", "visible_percent"),
    ((72, 72), (94, 94), (95, 95), (97, 95)),
)
def test_user_visible_confidence_caps_at_95_without_changing_policy_fields(
    internal_percent, visible_percent
):
    summary = {
        "authority_consumer": True,
        "confidence_percent": internal_percent,
        "confidence_kind": "period",
        "confidence_label": "Confidence:",
        "confidence_visible": True,
        "cycle_text": "Confirmed: Every 3 hours",
        "next_text": "1900010800",
        "next_restart_at": 1900010800.0,
        "countdown_text": "01:23",
        "next_visible": True,
        "countdown_visible": True,
        "countdown_safe": True,
        "prediction_usable": True,
        "presentation_key": "confirmed_cycle",
        "reason_codes": ("safe_h3_prediction_available",),
    }
    before = copy.deepcopy(summary)

    presentation = restart_learning_presentation(summary)
    panel = apply_to_fake_panel(summary)

    assert presentation["confidence_percent"] == visible_percent
    assert panel.restart_confidence_value_label.text == f"{visible_percent}%"
    assert presentation["prediction_usable"]
    assert presentation["countdown_visible"]
    assert panel.restart_countdown_row.visible
    assert panel.restart_countdown_hh_label.text == "01"
    assert panel.restart_countdown_mm_label.text == "23"
    assert summary == before


def test_non_hour_cycle_is_preserved_safely_after_confirmed_prefix_removal():
    assert (
        format_restart_cycle_text("Confirmed: Every 90 minutes")
        == "Every 90 minutes"
    )
    assert (
        format_restart_cycle_text("Confirmed: Every 1.5 hours")
        == "Every 1.5 hours"
    )


def test_unsafe_schema4_cycle_never_obtains_schema3_cycle_text():
    summary = {
        "authority_consumer": True,
        "confidence_percent": 79,
        "cycle_text": "Likely new: Every 4 hours",
        "next_text": "Prediction suspended",
        "next_restart_at": None,
        "countdown_text": "--",
        "next_visible": True,
        "countdown_visible": False,
        "countdown_safe": False,
        "prediction_usable": False,
        "presentation_key": "likely_new_cycle",
    }

    presentation = restart_learning_presentation(summary)

    assert presentation["cycle_text"] == "Likely new: Every 4 hours"
    assert presentation["next_text"] == "Prediction suspended"
    assert not presentation["countdown_visible"]
    assert "Every 3" not in presentation["cycle_text"]


@pytest.fixture
def utc_timezone():
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "UTC0"
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


@pytest.mark.parametrize("timestamp", (46860, 46860.0, 46860.75))
def test_restart_occurrence_formats_as_controlled_local_hhmm(
    timestamp, utc_timezone
):
    assert format_restart_local_time(timestamp) == "13:01"


def test_restart_occurrence_uses_system_local_timezone():
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "UTC-2"
    time.tzset()
    try:
        assert format_restart_local_time(0) == "02:00"
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


@pytest.mark.parametrize(
    "value",
    (None, "", "not-an-epoch", float("nan"), float("inf"), float("-inf"), True),
)
def test_invalid_restart_occurrence_is_unavailable(value):
    assert format_restart_local_time(value) is None


def test_local_time_presentation_contains_only_hhmm_and_preserves_policy_values(
    utc_timezone,
):
    summary = {
        "confidence_percent": 97,
        "cycle_text": "Confirmed: Every 3 hours",
        "next_text": "46860",
        "next_restart_at": 46860.75,
        "countdown_text": "02:14",
        "next_visible": True,
        "countdown_visible": True,
        "countdown_safe": True,
        "prediction_usable": True,
        "scheduled_warning_at": 46560.75,
        "presentation_key": "confirmed_cycle",
        "authority_consumer": True,
    }
    before = copy.deepcopy(summary)

    presentation = restart_learning_presentation(summary)

    assert presentation["next_text"] == "13:01"
    assert presentation["next_text"].count(":") == 1
    assert all(
        item not in presentation["next_text"]
        for item in ("46860", "Today", "Tomorrow", "-", "/")
    )
    assert presentation["countdown_text"] == "02:14"
    assert summary["scheduled_warning_at"] == 46560.75
    assert summary == before


def test_invalid_usable_occurrence_hides_next_row_without_changing_countdown():
    presentation = restart_learning_presentation(
        {
            "confidence_percent": 97,
            "cycle_text": "Confirmed: Every 3 hours",
            "next_text": "invalid",
            "next_restart_at": float("nan"),
            "countdown_text": "02:14",
            "next_visible": True,
            "countdown_visible": True,
            "countdown_safe": True,
            "prediction_usable": True,
            "authority_consumer": True,
        }
    )
    assert presentation["next_text"] == "--"
    assert not presentation["next_visible"]
    assert presentation["countdown_text"] == "02:14"


def fake_alert_status_panel():
    return SimpleNamespace(
        sound_row=FakeWidget(),
        volume_row=FakeWidget(),
        restart_alert_info_label=FakeWidget(),
        restart_alert_info_box=FakeWidget(),
        alert_audio_status_label=FakeWidget(),
    )


@pytest.mark.parametrize(
    "state",
    (
        "confirmed_h3",
        "h1",
        "h2",
        "provisional",
        "challenger",
        "prediction_suspended",
        "confirmed_offline",
        "schema3_fallback",
        "missing_schema4",
        "unavailable_schema4",
        "quarantined_schema4",
        "unknown",
    ),
)
def test_restart_alert_status_is_absent_for_every_model_state(state):
    panel = fake_alert_status_panel()
    panel.restart_alert_info_label.set_text(f"legacy status for {state}")
    panel.restart_alert_info_box.set_visible(True)
    panel.alert_audio_status_label.set_text(f"audio status for {state}")
    panel.alert_audio_status_label.set_visible(True)

    ServerCompanionPanel.set_restart_alert_usability(
        panel,
        {
            "usable": state == "confirmed_h3",
            "message": f"Restart alerts are learning: {state}.",
        },
    )

    assert panel.restart_alert_info_label.text == ""
    assert not panel.restart_alert_info_box.visible
    assert panel.alert_audio_status_label.text == ""
    assert not panel.alert_audio_status_label.visible
    assert panel.sound_row.visible
    assert panel.volume_row.visible


def test_repeated_model_source_transitions_cannot_restore_status_text():
    panel = fake_alert_status_panel()
    for usable, message in (
        (False, "schema-3 learning"),
        (True, "schema-4 established"),
        (False, "schema-3 fallback"),
        (False, ""),
    ):
        panel.restart_alert_info_label.set_text("stale")
        panel.restart_alert_info_box.set_visible(True)
        ServerCompanionPanel.set_restart_alert_usability(
            panel, {"usable": usable, "message": message}
        )
        assert panel.restart_alert_info_label.text == ""
        assert not panel.restart_alert_info_box.visible


def test_audio_status_refresh_is_also_permanently_suppressed():
    panel = fake_alert_status_panel()
    ServerCompanionPanel.set_alert_audio_status(panel, "Audio playback failed")
    assert panel.alert_audio_status_label.text == ""
    assert not panel.alert_audio_status_label.visible


def test_status_widgets_are_not_attached_below_alert_volume():
    constructor_source = inspect.getsource(ServerCompanionPanel.__init__)
    assert "self.server_box.append(self.restart_alert_info_box)" not in constructor_source
    assert "self.server_box.append(self.alert_audio_status_label)" not in constructor_source


def test_alert_toggle_sound_and_volume_controls_remain_unchanged():
    panel = SimpleNamespace(
        restart_alert_switch=FakeWidget(),
        alert_sound_button=FakeWidget(),
        alert_volume_scale=FakeWidget(),
        alert_volume_percent_label=FakeWidget(),
        alert_test_button=FakeWidget(),
        alert_sound_label=FakeWidget(),
        _alert_sound_value="female",
    )

    ServerCompanionPanel.set_restart_alert_enabled(panel, True)
    ServerCompanionPanel.set_alert_sound(panel, "beep")
    ServerCompanionPanel.set_alert_volume(panel, 65)

    assert panel.restart_alert_switch.active
    assert panel.alert_sound_button.sensitive
    assert panel.alert_volume_scale.sensitive
    assert panel.alert_volume_percent_label.sensitive
    assert panel.alert_test_button.sensitive
    assert panel._alert_sound_value == "beep"
    assert panel.alert_sound_label.text == "Beep Alarm ∨"
    assert panel.alert_volume_scale.value == 65
    assert panel.alert_volume_percent_label.text == "65%"


def test_strong_selected_period_confidence_uses_green():
    summary = phase2_learning_summary(
        decision(
            ConsumerModelStatus.CONFIRMED_PERIOD,
            schedule=0.95,
            period=0.92,
            cycle=3 * 3600,
            prediction=NOW + 3600,
            usable=True,
        ),
        now=NOW,
    )
    presentation = restart_learning_presentation(summary)
    assert presentation["confidence_percent"] == 92
    assert presentation["confidence_style_class"] == "ping-good"


def test_no_pattern_has_no_summary_or_misleading_percentage():
    assert phase2_learning_summary(
        decision(ConsumerModelStatus.NO_PATTERN, schedule=0.0),
        now=NOW,
    ) is None
    panel = apply_to_fake_panel(None)
    assert not panel.restart_learning_box.visible
    assert panel.restart_confidence_value_label.text == ""


def test_legacy_summary_dictionary_remains_accepted_by_ui_normalizer():
    presentation = restart_learning_presentation(
        {
            "confidence_percent": 72,
            "cycle_text": "Legacy cycle",
            "next_text": "Tue 12:00",
            "countdown_text": "02:10",
            "prediction_usable": True,
            "phase2": True,
        }
    )
    assert presentation["confidence_kind"] == "period"
    assert presentation["confidence_label"] == "Confidence:"
    assert presentation["confidence_style_class"] == "ping-yellow"
    assert presentation["next_visible"]
    assert presentation["countdown_visible"]


def test_authority_summary_normalizer_retains_safety_and_hides_unsafe_countdown():
    summary = {
        "authority_consumer": True,
        "confidence_percent": 79,
        "confidence_kind": "period",
        "cycle_text": "Likely new: Every 4 hours",
        "next_text": "Prediction suspended",
        "countdown_text": "--",
        "next_visible": True,
        "countdown_visible": False,
        "countdown_safe": False,
        "prediction_usable": False,
        "presentation_key": "likely_new_cycle",
        "reason_codes": (
            "unsafe_prediction_suspended",
            "provisional_regime_transition",
        ),
    }

    presentation = restart_learning_presentation(summary)
    panel = apply_to_fake_panel(summary)

    assert presentation["authority_consumer"]
    assert not presentation["countdown_safe"]
    assert presentation["reason_codes"] == summary["reason_codes"]
    assert not presentation["countdown_visible"]
    assert not presentation["prediction_usable"]
    assert not panel.restart_countdown_row.visible

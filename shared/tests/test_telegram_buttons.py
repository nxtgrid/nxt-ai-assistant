"""Tests for shared/utils/telegram_buttons.py keyboard builders."""

from __future__ import annotations

from shared.utils.telegram_buttons import (
    ESCALATION_ALERT_DISMISS_PREFIX,
    ESCALATION_CLOSE_NOTIFY_PREFIX,
    ESCALATION_CLOSE_SILENT_PREFIX,
    ESCALATION_TRACK_CALLBACK_PREFIX,
    build_escalation_track_keyboard,
    build_stale_alert_keyboard,
    parse_callback_data,
)

MID = "00000000-0000-0000-0000-000000000001"


def _datas(kb):
    return [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]


def test_track_keyboard_default_has_all_three_actions():
    kb = build_escalation_track_keyboard(MID)
    datas = _datas(kb)
    assert f"{ESCALATION_TRACK_CALLBACK_PREFIX}:{MID}" in datas
    assert f"{ESCALATION_CLOSE_SILENT_PREFIX}:{MID}" in datas
    assert f"{ESCALATION_CLOSE_NOTIFY_PREFIX}:{MID}" in datas


def test_track_keyboard_can_omit_close_and_inform():
    kb = build_escalation_track_keyboard(MID, include_close_notify=False)
    datas = _datas(kb)
    assert f"{ESCALATION_CLOSE_SILENT_PREFIX}:{MID}" in datas
    assert f"{ESCALATION_CLOSE_NOTIFY_PREFIX}:{MID}" not in datas
    assert f"{ESCALATION_TRACK_CALLBACK_PREFIX}:{MID}" in datas


def test_track_keyboard_can_omit_track_and_close_notify_together():
    kb = build_escalation_track_keyboard(MID, include_track=False, include_close_notify=False)
    datas = _datas(kb)
    assert datas == [f"{ESCALATION_CLOSE_SILENT_PREFIX}:{MID}"]


def test_stale_alert_keyboard_one_close_button_per_entry():
    kb = build_stale_alert_keyboard(
        [
            ("11111111-1111-1111-1111-111111111111", "#ExampleOrg"),
            ("22222222-2222-2222-2222-222222222222", "Jane Doe"),
        ]
    )
    rows = kb["inline_keyboard"]
    assert len(rows) == 2
    assert (
        rows[0][0]["callback_data"]
        == f"{ESCALATION_ALERT_DISMISS_PREFIX}:11111111-1111-1111-1111-111111111111"
    )
    assert (
        rows[1][0]["callback_data"]
        == f"{ESCALATION_ALERT_DISMISS_PREFIX}:22222222-2222-2222-2222-222222222222"
    )
    assert "#ExampleOrg" in rows[0][0]["text"]
    assert rows[0][0]["text"].startswith("✅")


def test_stale_alert_keyboard_truncates_long_labels_within_callback_budget():
    long_label = "x" * 200
    kb = build_stale_alert_keyboard([("33333333-3333-3333-3333-333333333333", long_label)])
    btn = kb["inline_keyboard"][0][0]
    assert len(btn["callback_data"].encode("utf-8")) <= 64
    assert len(btn["text"]) <= 64


def test_stale_alert_keyboard_empty_entries_yields_no_keyboard():
    assert build_stale_alert_keyboard([]) is None


def test_parse_callback_data_recognises_the_dismiss_prefix():
    parsed = parse_callback_data(
        f"{ESCALATION_ALERT_DISMISS_PREFIX}:44444444-4444-4444-4444-444444444444"
    )
    assert parsed == {
        "type": ESCALATION_ALERT_DISMISS_PREFIX,
        "mapping_id": "44444444-4444-4444-4444-444444444444",
    }

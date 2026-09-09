"""Tests for shared/utils/telegram_buttons.py keyboard builders."""

from __future__ import annotations

from shared.utils.telegram_buttons import (
    ESCALATION_CLOSE_NOTIFY_PREFIX,
    ESCALATION_CLOSE_SILENT_PREFIX,
    ESCALATION_TRACK_CALLBACK_PREFIX,
    build_escalation_track_keyboard,
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

"""The agent must not be allowed to call itself finished early. A model ends its turn
whenever it replies in text -- twice in a row it did so mid-task, saying 'let me verify...'
and stopping. Completion is therefore a property of the work, checked from the store."""

from __future__ import annotations

import time

from chui_reporter.agent.run import unfinished


def _touch(store):
    store.ensure_report("F", "Q2 2026")
    store.set_section("1.1", "Overview", "In Q2 2026, the Fund deployed capital.", 101)


def test_an_empty_report_is_unfinished(store):
    store.ensure_report("F", "Q2 2026")
    assert len(unfinished(store)) == 2


def test_written_but_not_rendered_is_unfinished(store):
    _touch(store)
    assert any("render" in m for m in unfinished(store))


def test_rendered_but_not_inspected_is_unfinished(store):
    _touch(store)
    store.mark("rendered_at")
    missing = unfinished(store)
    assert len(missing) == 1 and "inspect_pages" in missing[0]


def test_rendered_and_inspected_is_finished(store):
    _touch(store)
    store.mark("rendered_at")
    time.sleep(0.05)
    store.mark("inspected_at")
    assert unfinished(store) == []


def test_editing_after_the_render_makes_it_unfinished_again(store):
    _touch(store)
    store.mark("rendered_at")
    store.mark("inspected_at")
    assert unfinished(store) == []
    time.sleep(0.05)
    store.set_section("1.2", "Capital Activity Summary", "The Fund made no distributions.", 102)
    assert any("render" in m for m in unfinished(store))


def test_inspecting_before_the_latest_render_does_not_count(store):
    _touch(store)
    store.mark("inspected_at")
    time.sleep(0.05)
    store.mark("rendered_at")
    assert any("inspect_pages" in m for m in unfinished(store))

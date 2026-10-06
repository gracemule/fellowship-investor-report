"""Files attached in the composer: stored apart from the synced folder, never lost by a folder sync,
safe to name, and described to the agent."""

from __future__ import annotations

import hashlib

import pytest

from chui_reporter.runtime.runner import Runtime
from chui_reporter.workspace import sync


def _h(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def test_an_attachment_is_stored_under_uploads_and_listed(store):
    info = sync.put_attachment(store, "Macro snapshot.csv", b"country,gdp\nKenya,5.1\n")
    assert info == {"path": "Uploads/Macro snapshot.csv", "name": "Macro snapshot.csv", "size": 22, "kind": "document"}
    assert [a["name"] for a in sync.attachments(store)] == ["Macro snapshot.csv"]
    assert sync.put_attachment(store, "shot.png", b"\x89PNG....")["kind"] == "image"


def test_a_different_file_with_the_same_name_is_kept_alongside(store):
    a = sync.put_attachment(store, "notes.txt", b"first")
    same = sync.put_attachment(store, "notes.txt", b"first")
    b = sync.put_attachment(store, "notes.txt", b"second")
    assert same["path"] == a["path"] and b["path"] == "Uploads/notes (2).txt"


def test_unsafe_names_and_unknown_kinds_are_refused_or_cleaned(store):
    assert sync.put_attachment(store, "../../etc/passwd.txt", b"x")["path"] == "Uploads/passwd.txt"
    assert sync.clean_attachment_name("a/b\\c<d>.pdf") == "c_d_.pdf"
    with pytest.raises(sync.SyncError, match="cannot be attached"):
        sync.put_attachment(store, "program.exe", b"MZ")


def test_a_folder_sync_does_not_remove_attachments(store):
    sync.put_attachment(store, "keep.csv", b"a,b\n")
    data = b"fund model"
    sync.put_file(store, "Portfolio Company Data/Fund Model.xlsx", data, _h(data))
    manifest = [{"path": "Portfolio Company Data/Fund Model.xlsx", "sha256": _h(data)}]
    sync.commit(store, manifest)                 # the folder has no Uploads/: they must survive
    assert [a["name"] for a in sync.attachments(store)] == ["keep.csv"]


def test_an_attachment_can_be_removed_but_only_attachments(store):
    info = sync.put_attachment(store, "gone.csv", b"x,y\n")
    assert sync.remove_attachment(store, info["path"]) is True
    assert sync.attachments(store) == []
    with pytest.raises(sync.SyncError):
        sync.remove_attachment(store, "Fund Financials/anything.xlsx")


def test_attachments_are_not_listed_as_unrecognised_folder_files(store):
    sync.put_attachment(store, "x.csv", b"1")
    assert sync.unplaced(store) == []


def test_the_agent_is_told_what_was_attached_and_that_the_users_figures_are_theirs_to_give():
    text = Runtime.with_attachments("Use the new macro numbers.", ["Uploads/gdp.csv", "Uploads/chart.png"])
    assert text.startswith("Use the new macro numbers.")
    assert "Uploads/gdp.csv, Uploads/chart.png" in text and "recorded as provided by the user" in text and "cannot be recorded" not in text
    assert Runtime.with_attachments("Hello", []) == "Hello"

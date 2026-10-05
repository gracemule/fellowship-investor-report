"""The Postgres-backed report store. Each test gets an isolated schema."""

from __future__ import annotations

import uuid

import pytest

from chui_reporter.agent.store import Fact, Store, StoreConfigError, database_url
from chui_reporter.render.report_writer import UngroundedNumber, render_report


def test_no_silent_sqlite_fallback(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    with pytest.raises(StoreConfigError, match="DATABASE_URL"):
        database_url()


def test_schema_name_is_validated():
    with pytest.raises(StoreConfigError):
        Store(url="postgresql://x", schema="x; DROP SCHEMA public")


def test_section_is_mutable_in_place(store):
    store.ensure_report("Chui Ventures Fund I", "Q2 2026")
    store.set_section("1.4", "Fair Value Movements", "first draft", 4)
    store.set_section("1.4", "Fair Value Movements", "second draft", 4)
    secs = store.sections()
    assert len(secs) == 1, "an update must replace the section, not add a second"
    assert secs[0]["body"] == "second draft"


def test_sections_come_back_in_report_order(store):
    store.ensure_report("F", "Q2 2026")
    store.set_section("3.1", "Macro", "c", 3)
    store.set_section("1.1", "Overview", "a", 1)
    store.set_section("2.1", "Fund", "b", 2)
    assert [s["key"] for s in store.sections()] == ["1.1", "2.1", "3.1"]


def test_hidden_sections_are_not_rendered(store):
    """Sections appear and disappear between quarters (3.2 existed in Q1, not Q2),
    so presence is data, not a template branch."""
    store.ensure_report("F", "Q2 2026")
    store.set_section("3.2", "Public Health", "x", 5, present=False)
    store.set_section("3.1", "Macro", "y", 4, present=True)
    assert [s["key"] for s in store.sections()] == ["3.1"]


def test_table_roundtrips_through_jsonb(store):
    cols = ["Company", "Fair Value (Chui)", "Multiple"]
    rows = [["Alpha Co", 614, "1.18x"], ["Beta Co", None, "n/a"]]
    store.set_table("5.4", "Portfolio Performance Summary", cols, rows, section_key="5")
    t = store.tables()["5.4"]
    assert t["columns"] == cols
    assert t["rows"] == rows, "None and mixed types must survive"
    assert t["section_key"] == "5"


def test_only_extracted_facts_ground_a_number(store):
    """A quarantined value (e.g. an Excel #REF!) must never license a figure."""
    store.add_facts([
        Fact("nav", 5_204_117.0, unit="USD", source_file="a.xlsx", source_cell="C43"),
        Fact("broken", 123_456.0, status="quarantined", note="#REF!"),
        Fact("no_number", None, text_value="n/a"),
    ])
    assert store.grounded_values() == {5_204_117.0}


def test_facts_are_searchable_with_provenance(store):
    store.add_facts([Fact("alpha_fair_value", 614_000.0, unit="USD",
                          source_file="ALPHA.xlsx", source_sheet="Q2 June 2026", source_cell="N19")])
    hit = store.find_facts("ALPHA_FAIR")[0]
    assert hit["source_cell"] == "N19" and hit["source_sheet"] == "Q2 June 2026"
    assert store.fact_count() == 1


def test_schemas_are_isolated_from_each_other(store):
    other = Store(schema=f"t_{uuid.uuid4().hex[:10]}")
    try:
        store.add_facts([Fact("x", 1.0)])
        assert other.fact_count() == 0
    finally:
        other.drop_schema()


def test_render_is_blocked_by_an_ungrounded_figure(store):
    """The whole point: stored prose cannot reach a document with a number the
    ledger does not support. Transposed $3.8M vs the grounded $8.3M."""
    store.ensure_report("F", "Q2 2026")
    store.add_facts([Fact("portfolio_fv", 8_326_000.0, source_file="r.pdf")])
    store.set_section("1.4", "Fair Value Movements",
                      "The total unrealized value of the portfolio was $3.8M.", 1)
    with pytest.raises(UngroundedNumber, match=r"\$3\.8M"):
        render_report(store, "should-not-exist")


@pytest.mark.slow
def test_grounded_report_renders_to_docx_and_pdf(store, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store.ensure_report("F", "Q2 2026")
    store.add_facts([Fact("portfolio_fv", 8_326_000.0, source_file="r.pdf")])
    store.set_section("1.4", "Fair Value Movements",
                      "The total unrealized value of the portfolio was $8.3M.", 1)
    docx, pdf, info = render_report(store, "ok")
    assert docx.exists() and pdf.exists() and info["sections"] == 1


def test_a_failing_first_operation_does_not_lose_the_schema(store):
    """Regression from the first live run: the first call failed, its transaction
    rolled back the table creation, and every later call hit 'relation does not
    exist' because the store believed the schema was ready."""
    with pytest.raises(RuntimeError):
        with store.conn():
            raise RuntimeError("the first operation fails")
    store.ensure_report("F", "Q2 2026")          # must still work
    store.add_facts([Fact("x", 1.0)])
    assert store.fact_count() == 1


def test_a_database_error_does_not_poison_later_calls(store):
    store.ensure_report("F", "Q2 2026")
    with pytest.raises(Exception):
        store.add_facts([Fact("bad", value="2026-10-01")])  # type: ignore[arg-type]
    store.add_facts([Fact("good", 2.0)])
    assert store.fact_count() == 1


def test_the_connection_string_never_appears_in_a_repr_or_traceback(store):
    """A failing test printed the full DATABASE_URL, password included, because the
    dataclass repr included it. Secrets must not be in anything printable."""
    assert "postgresql://" not in repr(store) and "@" not in repr(store)
    assert "postgresql://" not in str(store)


def test_removing_a_section_removes_its_tables_and_charts(store):
    """report_remove_section promised this, but left the tables behind as orphans, which
    then blocked the render."""
    store.ensure_report("F", "Q2 2026")
    store.set_section("4.3", "Capital Call Schedule", "", 1)
    store.set_table("t_calls", "Calls", ["Call", "Amount"], [["1", "100"]], "4.3")
    store.set_chart("c_x", "X", "bar", ["a"], [1.0], "4.3")
    store.set_section("1.1", "Overview", "text", 2)
    store.set_table("t_keep", "Keep", ["a"], [["1"]], "1.1")
    store.delete_section("4.3")
    assert set(store.tables()) == {"t_keep"} and not store.charts()
    assert [s["key"] for s in store.sections()] == ["1.1"]


def test_a_fact_in_thousands_licenses_its_dollar_form_in_prose(store):
    """The ledger held the Q1 total as 6,478 (US$000, as the page prints it). The gate
    compared only the raw number, so '$6.48M' could never pass however it was written."""
    from chui_reporter.agent.store import Fact
    from chui_reporter.render.gate import check_grounded

    store.add_facts([Fact("q1 invested", 6478.0, unit="USD_thousands", source_file="q1.pdf")])
    g = store.grounded_values()
    assert check_grounded("up from $6.48M across 18 companies", g) == []
    assert check_grounded("6,478", g) == [], "the table form stays licensed too"
    assert check_grounded("up from $6.4M", g) == ["$6.4M"], "a wrong figure still fails"

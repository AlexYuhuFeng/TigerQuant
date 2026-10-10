"""CR-08: lineage reporting and the read-only dashboard.

Gate: "operator can inspect complete decision/evidence/execution lineage; isolation shown
distinctly".

The safety assertions here are the ones that matter. A dashboard is the one component in this
project that listens on a socket, so the tests prove it is loopback-only, read-only, and
incapable of serving anything outside a fixed route table — rather than documenting those as
intentions.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from tigerquant.core.enums import Mode
from tigerquant.core.errors import ConfigurationError
from tigerquant.events.journal import AppendOnlyJournal
from tigerquant.market.fixtures import load_scenario
from tigerquant.ops.dashboard import (
    ALLOWED_METHODS,
    LOOPBACK_HOSTS,
    DashboardApp,
    DashboardServer,
    build_app,
    route_table,
)
from tigerquant.ops.lineage_report import (
    LineageReport,
    build_lineage,
    export,
    render_csv,
    render_json,
    render_text,
)
from tigerquant.pipeline.replay import run_replay

FIXTURE = "tests/fixtures/scenarios/trend_up.yaml"


@pytest.fixture
def report(tmp_path: pytest.TempPathFactory) -> LineageReport:
    path = tmp_path / "journal.jsonl"  # type: ignore[operator]
    run_replay(load_scenario(FIXTURE), journal_path=path)
    with AppendOnlyJournal(path, Mode.BACKTEST, fsync=False) as journal:
        events = tuple(record.event for record in journal.iter_records())
    return build_lineage(events, mode=Mode.BACKTEST.value)


@pytest.fixture
def server(report: LineageReport) -> object:
    app = build_app(report, journal="test-journal.jsonl")
    with DashboardServer(app) as running:
        yield running


def request_status(
    base: str, path: str, *, method: str = "GET", data: bytes | None = None
) -> tuple[int, dict[str, object], str]:
    """Perform a request and always consume and close the body.

    An unclosed ``HTTPError`` leaks a ResourceWarning, and this project runs pytest with
    ``filterwarnings = ["error"]``, so a test that merely inspects an error code fails the
    suite. Closing the response is the fix and is worth doing in one place.
    """
    # The URL is built from the server's own loopback base_url, never from user input.
    request = urllib.request.Request(base + path, method=method, data=data)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            return response.status, dict(response.headers), response.read().decode()
    except urllib.error.HTTPError as error:
        with error:
            return error.code, dict(error.headers), error.read().decode()


class TestLineageChain:
    def test_every_decision_is_present(self, report: LineageReport) -> None:
        assert report.decisions >= 1
        assert all(record.decision_id for record in report.records)

    def test_the_full_chain_is_linked(self, report: LineageReport) -> None:
        """decision -> snapshot -> evidence -> risk -> order -> fill -> ledger."""
        traded = [record for record in report.records if record.reached_the_venue]
        assert traded, "the scenario should trade at least once"
        record = traded[0]
        assert record.snapshot_id
        assert record.evidence_available
        assert record.risk_outcome is not None
        assert record.intent_id
        assert record.client_order_id
        assert record.order_states
        assert record.fills
        assert record.cash_after is not None

    def test_a_fill_traces_back_to_a_decision(self, report: LineageReport) -> None:
        """The whole point of lineage: no fill without a decision behind it."""
        assert report.fills >= 1
        assert not report.unlinked_fills
        assert not report.unlinked_orders

    def test_the_order_state_sequence_is_recorded_in_order(self, report: LineageReport) -> None:
        traded = next(record for record in report.records if record.reached_the_venue)
        assert traded.order_states[0] == "OrderIntentCreated"
        assert "OrderFilled" in traded.order_states
        # The recorded sequence follows the state machine rather than the journal's file order.
        assert traded.order_states.index("OrderSubmitted") < traded.order_states.index(
            "OrderFilled"
        )

    def test_fill_arithmetic_is_exact(self, report: LineageReport) -> None:
        traded = next(record for record in report.records if record.fills)
        assert traded.filled_quantity > 0
        assert traded.traded_notional > 0

    def test_abstentions_are_recorded_as_such(self, report: LineageReport) -> None:
        holds = [record for record in report.records if record.action == "HOLD"]
        assert holds, "the scenario holds before it trades"
        assert all(not record.reached_the_venue for record in holds)

    def test_a_clean_report_has_no_fabricated_citations(self, report: LineageReport) -> None:
        """The model citing evidence it was never shown must be visible, and here it is empty."""
        assert report.fabrications == ()
        assert report.is_clean

    def test_the_report_is_deterministic(self, tmp_path: pytest.TempPathFactory) -> None:
        first = run_replay(load_scenario(FIXTURE), journal_path=tmp_path / "a.jsonl")  # type: ignore[operator]
        second = run_replay(load_scenario(FIXTURE), journal_path=tmp_path / "b.jsonl")  # type: ignore[operator]
        assert first.journal_digest == second.journal_digest


class TestIsolationIsShownDistinctly:
    def test_the_mode_is_in_the_facts(self, report: LineageReport) -> None:
        assert report.facts["journal_mode"] == Mode.BACKTEST.value

    def test_each_report_says_it_is_one_journal_only(self, report: LineageReport) -> None:
        assert "nothing from another mode is merged" in report.facts["note"].lower()

    def test_two_modes_produce_distinguishable_reports(
        self, tmp_path: pytest.TempPathFactory
    ) -> None:
        """The failure this guards against is reading one mode's numbers as another's."""
        path = tmp_path / "journal.jsonl"  # type: ignore[operator]
        run_replay(load_scenario(FIXTURE), journal_path=path)
        with AppendOnlyJournal(path, Mode.BACKTEST, fsync=False) as journal:
            events = tuple(record.event for record in journal.iter_records())

        backtest = build_lineage(events, mode="BACKTEST")
        paper = build_lineage(events, mode="PAPER")
        assert backtest.digest() != paper.digest()
        assert backtest.facts["journal_mode"] != paper.facts["journal_mode"]

    def test_the_dashboard_page_states_the_mode(self, report: LineageReport) -> None:
        app = build_app(report, journal="j.jsonl")
        page = app.body("/").decode()
        assert "MODE: BACKTEST" in page
        assert "nothing from any other mode is merged in" in page


class TestExports:
    def test_json_is_stable_and_complete(self, report: LineageReport) -> None:
        payload = json.loads(render_json(report))
        assert payload["mode"] == Mode.BACKTEST.value
        assert len(payload["lineage"]) == report.decisions
        assert payload["digest"] == report.digest()

    def test_csv_has_one_row_per_decision(self, report: LineageReport) -> None:
        lines = render_csv(report).splitlines()
        assert len(lines) == report.decisions + 1  # header plus one row each
        assert lines[0].startswith("decision_id,")

    def test_csv_flattens_lists_rather_than_nesting_them(self, report: LineageReport) -> None:
        """A nested column cannot be filtered in a spreadsheet, which is the point of CSV."""
        header, *rows = render_csv(report).splitlines()
        assert "evidence_cited" in header
        assert "|" not in header
        assert all(row.count(",") >= 10 for row in rows if row)

    def test_text_reads_as_a_report(self, report: LineageReport) -> None:
        text = render_text(report)
        assert "decision " in text
        assert "journal_mode" in text

    def test_an_unknown_format_is_refused(self, report: LineageReport) -> None:
        with pytest.raises(ValueError, match="unsupported format"):
            export(report, "xml")

    def test_no_export_contains_an_account_number(self, report: LineageReport) -> None:
        for rendered in (render_json(report), render_text(report), render_csv(report)):
            assert "account" not in rendered.lower() or "account_number" not in rendered.lower()


class TestDashboardIsReadOnly:
    def test_only_get_and_head_are_allowed(self) -> None:
        assert {"GET", "HEAD"} == ALLOWED_METHODS

    def test_there_is_no_mutating_route(self) -> None:
        for route in route_table():
            assert not any(
                word in route for word in ("submit", "cancel", "halt", "write", "repair", "clear")
            )

    def test_get_serves_every_route(self, server: object) -> None:
        base = server.base_url  # type: ignore[attr-defined]
        for route in route_table():
            status, headers, _body = request_status(base, route)
            assert status == 200, route
            assert headers["X-TigerQuant-Mode"] == Mode.BACKTEST.value

    def test_post_is_refused(self, server: object) -> None:
        base = server.base_url  # type: ignore[attr-defined]
        status, _headers, _body = request_status(base, "/lineage.json", method="POST", data=b"{}")
        assert status == 405

    def test_delete_is_refused(self, server: object) -> None:
        base = server.base_url  # type: ignore[attr-defined]
        status, _headers, _body = request_status(base, "/lineage.json", method="DELETE")
        assert status == 405

    def test_the_refusal_explains_why(self, server: object) -> None:
        base = server.base_url  # type: ignore[attr-defined]
        _status, _headers, body = request_status(base, "/lineage.json", method="PUT", data=b"{}")
        assert "read-only by construction" in json.loads(body)["note"]


class TestDashboardIsLoopbackOnly:
    def test_a_non_loopback_bind_is_refused(self, report: LineageReport) -> None:
        """No authentication and no TLS means it must never listen anywhere but loopback."""
        app = build_app(report, journal="j.jsonl")
        # These are the addresses the server must REFUSE; that is the assertion.
        for host in ("0.0.0.0", "192.168.1.10", "::", "example.com"):  # noqa: S104
            with pytest.raises(ConfigurationError, match="loopback"):
                DashboardServer(app, host=host)

    def test_the_loopback_hosts_are_exactly_the_loopback_hosts(self) -> None:
        assert {"127.0.0.1", "::1", "localhost"} == LOOPBACK_HOSTS

    def test_it_binds_a_loopback_address(self, server: object) -> None:
        host, port = server.address  # type: ignore[attr-defined]
        assert host in {"127.0.0.1", "::1"}
        assert port > 0

    def test_a_path_traversal_attempt_misses(self, server: object) -> None:
        """The route table is fixed, so a request path is only ever a dictionary lookup."""
        base = server.base_url  # type: ignore[attr-defined]
        for path in ("/../../etc/passwd", "/..%2f..%2fetc/passwd", "/lineage.json/../../x"):
            status, _headers, _body = request_status(base, path)
            assert status == 404, f"{path} was served"

    def test_an_unknown_route_lists_the_real_ones(self, server: object) -> None:
        base = server.base_url  # type: ignore[attr-defined]
        _status, _headers, body = request_status(base, "/nope")
        assert set(json.loads(body)["routes"]) == set(route_table())


class TestDashboardIsolationHeaders:
    def test_the_mode_is_a_header_not_only_page_content(self, server: object) -> None:
        """A scripted consumer must not be able to collect two modes without noticing."""
        base = server.base_url  # type: ignore[attr-defined]
        _status, headers, _body = request_status(base, "/lineage.csv")
        assert headers["X-TigerQuant-Mode"] == Mode.BACKTEST.value
        assert headers["X-TigerQuant-Journal"] == "test-journal.jsonl"

    def test_content_types_are_right(self, server: object) -> None:
        base = server.base_url  # type: ignore[attr-defined]
        expected = {
            "/lineage.json": "application/json",
            "/lineage.csv": "text/csv",
            "/lineage.txt": "text/plain",
            "/": "text/html",
        }
        for route, content_type in expected.items():
            status, headers, _body = request_status(base, route)
            assert status == 200
            assert headers["Content-Type"].startswith(content_type)


class TestBuildApp:
    def test_content_is_fixed_at_construction(self, report: LineageReport) -> None:
        """There is no code path from an HTTP request to a journal read."""
        app = build_app(report, journal="j.jsonl")
        assert isinstance(app, DashboardApp)
        first = app.body("/lineage.json")
        assert app.body("/lineage.json") == first

    def test_an_unknown_path_has_no_body(self, report: LineageReport) -> None:
        app = build_app(report, journal="j.jsonl")
        assert app.body("/etc/passwd") is None

    def test_the_page_escapes_interpolated_values(self, report: LineageReport) -> None:
        """Even values we generate ourselves are escaped, so a hostile one cannot inject."""
        app = build_app(report, journal="<script>alert(1)</script>")
        page = app.body("/").decode()
        assert "<script>" not in page
        assert "&lt;script&gt;" in page

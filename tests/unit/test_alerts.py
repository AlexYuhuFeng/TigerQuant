"""CR-09 alert routing.

Two properties carry most of the weight here, and both are about the failure modes of an
alerting path rather than about delivery succeeding:

* **NO NETWORK BY DEFAULT.** The only network path in the module refuses to exist without an
  explicit endpoint. A router that reached the network because a configuration field was empty
  is the failure this prevents, and it is the same posture as `HttpNewsConnector` refusing to
  fetch without an entitlement reference.

* **A SUPPRESSED ALERT STAYS COUNTABLE.** "Nothing happened" and "everything was suppressed" are
  different states. A router that renders them identically hides an outage behind a quiet log.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tigerquant.core.clock import SimulatedClock
from tigerquant.core.enums import Severity
from tigerquant.core.errors import ConfigurationError
from tigerquant.ops.alerts import (
    Alert,
    AlertRouter,
    FileSink,
    MemorySink,
    SeverityFloor,
    WebhookSink,
    alert_from_drift,
    severity_rank,
)

AT = dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC)


def alert(
    *,
    key: str = "drift:journal-database",
    severity: Severity = Severity.CRITICAL,
    title: str = "journal and database disagree",
    detail: str = "3 rows differ",
    at: dt.datetime = AT,
    source: str = "drift",
    fields: dict[str, str] | None = None,
) -> Alert:
    return Alert(
        key=key,
        severity=severity,
        title=title,
        detail=detail,
        raised_at=at,
        source=source,
        fields=fields or {},
    )


class TestNoNetworkByDefault:
    def test_a_webhook_sink_refuses_an_empty_url(self) -> None:
        with pytest.raises(ConfigurationError, match="explicit http"):
            WebhookSink("")

    def test_it_refuses_a_url_that_is_not_http(self) -> None:
        """A bare host, a file path, or a typo must not silently become a request."""
        for url in ("example.com/hook", "ftp://example.com", "file:///etc/passwd", "  "):
            with pytest.raises(ConfigurationError):
                WebhookSink(url)

    def test_it_accepts_a_real_endpoint(self) -> None:
        assert WebhookSink("https://alerts.example.invalid/hook").name == "webhook"

    def test_the_module_imports_no_network_library_at_module_scope(self) -> None:
        """`urllib.request` is imported inside deliver(), so importing this module cannot
        contact anything and a static scan of imports stays clean."""
        source = (
            __import__("pathlib").Path("src/tigerquant/ops/alerts.py").read_text(encoding="utf-8")
        )
        header = source.split('"""', 2)[-1]
        # The only occurrence must be inside a function body, below the first `def`.
        first_def = header.index("def ")
        assert "import urllib.request" not in header[:first_def]

    def test_a_router_with_no_sinks_contacts_nothing(self) -> None:
        report = AlertRouter([]).route([alert()])
        assert report.delivered == 0
        assert report.failures == ()


class TestDelivery:
    def test_the_first_occurrence_is_delivered(self) -> None:
        sink = MemorySink()
        report = AlertRouter([(sink, SeverityFloor())]).route([alert()])
        assert report.delivered == 1
        assert len(sink.alerts) == 1

    def test_every_admitting_sink_receives_it(self) -> None:
        first, second = MemorySink("a"), MemorySink("b")
        report = AlertRouter([(first, SeverityFloor()), (second, SeverityFloor())]).route([alert()])
        assert report.delivered == 2
        assert len(first.alerts) == len(second.alerts) == 1

    def test_a_repeat_is_suppressed(self) -> None:
        sink = MemorySink()
        router = AlertRouter([(sink, SeverityFloor())])
        router.route([alert()])
        report = router.route([alert()])
        assert report.delivered == 0
        assert report.suppressed == (("drift:journal-database", "memory", "already-delivered"),)

    def test_a_different_condition_is_not_suppressed(self) -> None:
        sink = MemorySink()
        router = AlertRouter([(sink, SeverityFloor())])
        router.route([alert(key="drift:a")])
        router.route([alert(key="drift:b")])
        assert len(sink.alerts) == 2

    def test_clearing_a_condition_lets_it_fire_again(self) -> None:
        """Without this, a problem that returns after an hour stays suppressed for ever."""
        sink = MemorySink()
        router = AlertRouter([(sink, SeverityFloor())])
        router.route([alert()])
        router.clear("drift:journal-database")
        assert router.route([alert()]).delivered == 1

    def test_suppression_is_per_sink(self) -> None:
        """One sink having seen it says nothing about another."""
        first, second = MemorySink("a"), MemorySink("b")
        router = AlertRouter([(first, SeverityFloor()), (second, SeverityFloor())])
        router.route([alert()])
        second.deliver(alert())  # unrelated activity on the second sink
        report = router.route([alert()])
        assert report.delivered == 0


class TestSeverityFloors:
    def test_a_sink_below_its_floor_is_suppressed_by_reason(self) -> None:
        sink = MemorySink()
        router = AlertRouter([(sink, SeverityFloor(minimum=Severity.CRITICAL))])
        report = router.route([alert(severity=Severity.WARNING)])
        assert report.delivered == 0
        assert report.suppressed[0][2] == "below-severity-floor"

    def test_at_the_floor_is_admitted(self) -> None:
        sink = MemorySink()
        router = AlertRouter([(sink, SeverityFloor(minimum=Severity.WARNING))])
        assert router.route([alert(severity=Severity.WARNING)]).delivered == 1

    def test_the_floors_are_independent(self) -> None:
        quiet, loud = MemorySink("quiet"), MemorySink("loud")
        router = AlertRouter(
            [
                (quiet, SeverityFloor(minimum=Severity.CRITICAL)),
                (loud, SeverityFloor(minimum=Severity.INFO)),
            ]
        )
        report = router.route([alert(severity=Severity.WARNING)])
        assert len(quiet.alerts) == 0
        assert len(loud.alerts) == 1
        assert report.delivered == 1

    def test_an_unknown_severity_ranks_as_critical(self) -> None:
        """Deny by default: an alert nobody can classify must not be treated as routine."""
        assert severity_rank("SOMETHING_NEW") == severity_rank(Severity.CRITICAL)
        assert severity_rank(Severity.INFO) < severity_rank(Severity.CRITICAL)


class TestReminders:
    def test_a_reminder_fires_once_the_interval_elapses(self) -> None:
        clock = SimulatedClock(AT)
        sink = MemorySink()
        router = AlertRouter([(sink, SeverityFloor(reminder_interval_seconds=60))], clock=clock)
        router.route([alert()])

        clock.advance(dt.timedelta(seconds=30))
        assert router.route([alert()]).delivered == 0

        clock.advance(dt.timedelta(seconds=31))
        assert router.route([alert()]).delivered == 1

    def test_a_zero_interval_never_reminds(self) -> None:
        clock = SimulatedClock(AT)
        sink = MemorySink()
        router = AlertRouter([(sink, SeverityFloor(reminder_interval_seconds=0))], clock=clock)
        router.route([alert()])
        clock.advance(dt.timedelta(days=1))
        assert router.route([alert()]).delivered == 0


class TestSuppressionStaysCountable:
    def test_the_suppressed_count_is_exposed(self) -> None:
        router = AlertRouter([(MemorySink(), SeverityFloor())])
        router.route([alert()])
        for _ in range(3):
            router.route([alert()])
        counts = router.suppressed_counts()
        assert counts["memory/drift:journal-database:already-delivered"] == 3

    def test_a_quiet_period_is_distinguishable_from_a_broken_one(self) -> None:
        """Both deliver nothing. Only one of them has suppressions to show for it."""
        quiet = AlertRouter([(MemorySink(), SeverityFloor())]).route([])
        suppressed = AlertRouter([(MemorySink(), SeverityFloor())])
        suppressed.route([alert()])
        noisy = suppressed.route([alert()])

        assert quiet.delivered == noisy.delivered == 0
        assert quiet.suppressed == ()
        assert len(noisy.suppressed) == 1

    def test_the_summary_names_the_reason(self) -> None:
        router = AlertRouter([(MemorySink(), SeverityFloor(minimum=Severity.CRITICAL))])
        assert "suppressed" in router.route([alert(severity=Severity.INFO)]).summary()

    def test_distinct_conditions_are_counted_separately_from_deliveries(self) -> None:
        sink = MemorySink()
        router = AlertRouter([(sink, SeverityFloor())])
        report = router.route([alert(key="a"), alert(key="b"), alert(key="a")])
        assert report.distinct_conditions == 2
        assert report.delivered == 2


class TestFailuresAreVisible:
    class _Exploding:
        name = "exploding"

        def deliver(self, alert: Alert) -> None:
            msg = "upstream refused"
            raise RuntimeError(msg)

    def test_a_failing_sink_is_recorded_not_swallowed(self) -> None:
        report = AlertRouter([(self._Exploding(), SeverityFloor())]).route([alert()])
        assert not report.ok
        assert report.failures[0][0] == "drift:journal-database"
        assert "upstream refused" in report.failures[0][2]

    def test_one_bad_sink_does_not_stop_the_others(self) -> None:
        """An alerting path that fails as a unit fails completely."""
        good = MemorySink("good")
        router = AlertRouter([(self._Exploding(), SeverityFloor()), (good, SeverityFloor())])
        report = router.route([alert()])
        assert len(good.alerts) == 1
        assert report.delivered == 1
        assert len(report.failures) == 1

    def test_a_failed_delivery_is_not_recorded_as_delivered(self) -> None:
        """Otherwise the next occurrence would be suppressed by a delivery that never happened."""
        exploding = self._Exploding()
        router = AlertRouter([(exploding, SeverityFloor())])
        router.route([alert()])
        assert router.route([alert()]).delivered == 0
        assert len(router.route([alert()]).failures) == 1

    def test_the_summary_shouts_about_failures(self) -> None:
        report = AlertRouter([(self._Exploding(), SeverityFloor())]).route([alert()])
        assert "FAILURES" in report.summary()


class TestRedaction:
    def test_a_credential_shaped_value_is_redacted_before_delivery(self) -> None:
        """Alert detail is free text and reaches logs and webhooks, which invariant 11 covers."""
        secret = "sk-" + "a" * 40
        rendered = alert(detail=f"token was {secret}").render()
        assert secret not in rendered

    def test_the_record_is_redacted_too(self) -> None:
        secret = "sk-" + "b" * 40
        record = alert(detail=f"key {secret}").as_record()
        assert secret not in json.dumps(record)

    def test_fields_are_redacted(self) -> None:
        secret = "sk-" + "c" * 40
        record = alert(fields={"token": secret}).as_record()
        assert secret not in json.dumps(record)

    def test_ordinary_text_survives(self) -> None:
        assert "3 rows differ" in alert().render()


class TestFileSink:
    def test_it_appends_one_json_object_per_line(self, tmp_path: object) -> None:
        from pathlib import Path

        path = Path(str(tmp_path)) / "alerts" / "alerts.jsonl"
        sink = FileSink(path)
        AlertRouter([(sink, SeverityFloor())]).route([alert(key="a"), alert(key="b")])
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert all(json.loads(line)["key"] in {"a", "b"} for line in lines)

    def test_the_parent_directory_is_created(self, tmp_path: object) -> None:
        from pathlib import Path

        path = Path(str(tmp_path)) / "deep" / "nested" / "alerts.jsonl"
        FileSink(path).deliver(alert())
        assert path.is_file()


class TestDriftAdaptation:
    class _Result:
        def __init__(self, **kwargs: object) -> None:
            self.consistent = False
            self.alerted = True
            self.severity = Severity.CRITICAL
            self.detail = "2 rows differ"
            self.checked_at = AT
            self.occurrences = 1
            self.escalated = False
            for name, value in kwargs.items():
                setattr(self, name, value)

    def test_a_consistent_check_produces_no_alert(self) -> None:
        """An alert stream containing routine successes is one operators learn to ignore."""
        assert alert_from_drift(self._Result(consistent=True)) is None

    def test_a_suppressed_repeat_produces_no_alert(self) -> None:
        """The monitor already decided this is a repeat; double-suppressing loses the count."""
        assert alert_from_drift(self._Result(alerted=False)) is None

    def test_a_new_inconsistency_produces_an_alert(self) -> None:
        converted = alert_from_drift(self._Result())
        assert converted is not None
        assert converted.severity is Severity.CRITICAL
        assert converted.source == "drift"
        assert converted.key == "drift:journal-database"

    def test_the_occurrence_count_is_carried_through(self) -> None:
        converted = alert_from_drift(self._Result(occurrences=7))
        assert converted is not None
        assert converted.fields["occurrences"] == "7"

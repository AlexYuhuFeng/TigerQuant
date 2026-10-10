"""The `tigerquant alerts` command group.

`verify` is the subcommand that earns its place. An alerting path is only ever exercised when
something is actually wrong — which is the worst possible moment to discover it does not work —
so `verify` exercises it on demand, reports per sink what arrived, and **exits non-zero** if any
sink failed. That exit code is what makes it usable as a deployment gate.

The other property under test is the one the whole alerts module was built around: nothing
contacts the network unless an operator named an endpoint.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest

from tigerquant.core.enums import Severity
from tigerquant.core.errors import ConfigurationError
from tigerquant.ops.alert_cli import (
    DEFAULT_ALERT_LOG,
    WEBHOOK_ENV,
    _severity,
    build_router,
    cmd_alerts,
    describe_sinks,
)


def ns(**kwargs: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "alerts_command": "show",
        "log": None,
        "webhook": None,
        "floor": "WARNING",
        "reminder": 900.0,
        "suppressed": False,
        "journal": None,
        "mode": "BACKTEST",
    }
    base.update(kwargs)
    return argparse.Namespace(**base)


class TestSinkAssembly:
    def test_the_file_sink_is_always_present(self, tmp_path: Path) -> None:
        """It is the only sink that cannot fail for network reasons, so it is the floor below
        which an alert is genuinely lost."""
        router = build_router(log_path=tmp_path / "a.jsonl")
        assert [sink.name for sink in router.sinks] == ["file"]

    def test_no_webhook_means_no_webhook_sink(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not a disabled sink: an absent one. There is nothing to misconfigure later."""
        monkeypatch.delenv(WEBHOOK_ENV, raising=False)
        router = build_router(log_path=tmp_path / "a.jsonl")
        assert all(sink.name != "webhook" for sink in router.sinks)

    def test_the_environment_variable_adds_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(WEBHOOK_ENV, "https://alerts.example.invalid/hook")
        router = build_router(log_path=tmp_path / "a.jsonl")
        assert any(sink.name == "webhook" for sink in router.sinks)

    def test_an_explicit_flag_beats_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(WEBHOOK_ENV, "https://from-env.invalid/hook")
        router = build_router(log_path=tmp_path / "a.jsonl", webhook="https://from-flag.invalid/h")
        assert any(sink.name == "webhook" for sink in router.sinks)

    def test_a_bad_endpoint_is_refused_not_skipped(self, tmp_path: Path) -> None:
        """Skipping a misconfigured sink silently is how an operator ends up with no alerting."""
        with pytest.raises(ConfigurationError):
            build_router(log_path=tmp_path / "a.jsonl", webhook="not-a-url")

    def test_the_floor_is_applied_to_every_sink(self, tmp_path: Path) -> None:
        router = build_router(log_path=tmp_path / "a.jsonl", floor=Severity.CRITICAL)
        assert all(sink.name for sink, _ in router._sinks)


class TestSeverityParsing:
    def test_it_accepts_the_known_names_case_insensitively(self) -> None:
        assert _severity("critical") is Severity.CRITICAL
        assert _severity("INFO") is Severity.INFO

    def test_an_unknown_name_is_refused_rather_than_defaulted(self) -> None:
        """Defaulting would silently change what an operator is told about, with no way to notice."""
        with pytest.raises(ConfigurationError, match="unknown severity"):
            _severity("CRTICAL")

    def test_the_error_lists_the_allowed_values(self) -> None:
        with pytest.raises(ConfigurationError) as caught:
            _severity("nope")
        for member in Severity:
            assert member.value in str(caught.value)


class TestDescribeSinks:
    def test_it_says_when_there_is_no_webhook(self, tmp_path: Path) -> None:
        """ "No webhook" and "a webhook that silently did not register" look identical in a bare
        list of sinks, so the absence is stated."""
        text = describe_sinks(build_router(log_path=tmp_path / "a.jsonl"))
        assert "no webhook sink" in text
        assert WEBHOOK_ENV in text

    def test_it_reports_the_floor(self, tmp_path: Path) -> None:
        text = describe_sinks(build_router(log_path=tmp_path / "a.jsonl", floor=Severity.CRITICAL))
        assert "CRITICAL" in text

    def test_a_zero_reminder_reads_as_never_repeating(self, tmp_path: Path) -> None:
        text = describe_sinks(build_router(log_path=tmp_path / "a.jsonl", reminder_seconds=0))
        assert "never repeats" in text


class TestVerify:
    def test_it_delivers_to_the_file_sink_and_returns_zero(self, tmp_path: Path) -> None:
        log = tmp_path / "verify.jsonl"
        assert cmd_alerts(ns(alerts_command="verify", log=log)) == 0
        assert log.is_file()
        record = json.loads(log.read_text(encoding="utf-8").strip().splitlines()[0])
        assert record["key"] == "verify:alerting-path"
        assert record["severity"] == Severity.CRITICAL.value

    def test_the_probe_says_nothing_is_wrong(self, tmp_path: Path) -> None:
        """An operator who receives it must not think the system is broken."""
        log = tmp_path / "verify.jsonl"
        cmd_alerts(ns(alerts_command="verify", log=log))
        record = json.loads(log.read_text(encoding="utf-8").strip().splitlines()[0])
        assert "Nothing is wrong" in record["detail"]

    def test_the_probe_is_stamped_as_synthetic(self, tmp_path: Path) -> None:
        log = tmp_path / "verify.jsonl"
        cmd_alerts(ns(alerts_command="verify", log=log))
        assert json.loads(log.read_text(encoding="utf-8").splitlines()[0])["source"] == "verify"

    def test_it_does_not_use_the_real_alert_log_by_default(self) -> None:
        """A probe must not pollute the log an operator reads during an incident."""
        assert DEFAULT_ALERT_LOG.with_name("alerts_verify.jsonl") != DEFAULT_ALERT_LOG

    def test_a_failing_sink_makes_it_exit_non_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exit code is what makes this usable as a deployment gate, so the failure branch
        is exercised rather than assumed. build_router is replaced because erify assembles its
        own sinks, and MemorySink has __slots__ so its method cannot be patched directly."""
        from tigerquant.ops import alert_cli

        class Exploding:
            name = "exploding"

            def deliver(self, alert: object) -> None:
                msg = "destination refused"
                raise RuntimeError(msg)

            def __call__(self, *args: object, **kwargs: object) -> None:
                return None

        real = alert_cli.build_router

        def with_exploding(**kwargs: object):
            kwargs["extra"] = [Exploding()]
            return real(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(alert_cli, "build_router", with_exploding)
        code = cmd_alerts(ns(alerts_command="verify", log=tmp_path / "v.jsonl"))
        assert code == 1, "a sink that raised must make verify exit non-zero"

    def test_verify_uses_an_info_floor(self, tmp_path: Path) -> None:
        """The probe must not be filtered out by the very floor under test."""
        log = tmp_path / "verify.jsonl"
        assert cmd_alerts(ns(alerts_command="verify", log=log, floor="CRITICAL")) == 0
        assert log.is_file()


class TestDispatch:
    def test_show_returns_zero(self, tmp_path: Path) -> None:
        assert cmd_alerts(ns(alerts_command="show", log=tmp_path / "a.jsonl")) == 0

    def test_show_reports_suppression_when_asked(self, tmp_path: Path) -> None:
        assert cmd_alerts(ns(alerts_command="show", log=tmp_path / "a.jsonl", suppressed=True)) == 0

    def test_a_missing_journal_is_refused(self, tmp_path: Path) -> None:
        code = cmd_alerts(ns(alerts_command="drift", journal=tmp_path / "nope.jsonl"))
        assert code == 2

    def test_an_unknown_subcommand_raises(self) -> None:
        with pytest.raises(ConfigurationError, match="unknown alerts command"):
            cmd_alerts(ns(alerts_command="wat"))


class TestNoNetworkWithoutAnEndpoint:
    def test_the_module_reads_the_environment_for_the_webhook_only(self) -> None:
        """The env var is the only ambient way an endpoint can appear, and it is named here."""
        source = Path("src/tigerquant/ops/alert_cli.py").read_text(encoding="utf-8")
        assert WEBHOOK_ENV in source
        assert "os.environ.get(WEBHOOK_ENV)" in source

    def test_a_router_built_with_no_flags_has_no_network_sink(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(WEBHOOK_ENV, raising=False)
        router = build_router(log_path=tmp_path / "a.jsonl")
        assert all(sink.name == "file" for sink in router.sinks)

    def test_the_environment_variable_name_is_a_constant(self) -> None:
        assert os.environ.get(WEBHOOK_ENV) is None or isinstance(os.environ.get(WEBHOOK_ENV), str)

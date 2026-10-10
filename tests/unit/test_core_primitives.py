"""Core primitives: exact money, canonical encoding, deterministic ids, time, redaction."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from tigerquant.core.canonical import canonical_json_bytes, digest_of, to_canonical
from tigerquant.core.clock import SimulatedClock, SystemClock, ensure_utc
from tigerquant.core.errors import CanonicalizationError, MoneyError
from tigerquant.core.ids import IdFactory, encode_base32, stable_token
from tigerquant.core.money import dec_str, q_cash, q_price, q_weight, to_decimal, to_quantity
from tigerquant.core.redaction import REDACTED, redact, scan_for_secrets


class TestMoney:
    def test_float_is_rejected(self) -> None:
        with pytest.raises(MoneyError, match="float is not allowed"):
            to_decimal(0.1)

    def test_exact_decimal_from_string(self) -> None:
        assert to_decimal("0.1") + to_decimal("0.2") == Decimal("0.3")

    def test_bool_is_rejected(self) -> None:
        with pytest.raises(MoneyError):
            to_decimal(True)  # type: ignore[arg-type]

    def test_non_finite_rejected(self) -> None:
        with pytest.raises(MoneyError):
            to_decimal(Decimal("NaN"))

    def test_quantisation_uses_bankers_rounding(self) -> None:
        assert q_cash("1.005") == Decimal("1.00")
        assert q_cash("1.015") == Decimal("1.02")
        assert q_price("600.123456") == Decimal("600.1235")

    def test_weight_scale(self) -> None:
        assert q_weight("0.050000001") == Decimal("0.05")

    def test_quantity_must_be_int(self) -> None:
        assert to_quantity(7) == 7
        with pytest.raises(MoneyError):
            to_quantity(7.0)  # type: ignore[arg-type]

    def test_dec_str_is_plain_and_normalised(self) -> None:
        assert dec_str(Decimal("600.5000")) == "600.5"
        assert dec_str(Decimal("-0.00")) == "0"
        assert dec_str(Decimal("1E+2")) == "100"


class TestCanonical:
    def test_key_order_does_not_matter(self) -> None:
        assert canonical_json_bytes({"b": 1, "a": 2}) == canonical_json_bytes({"a": 2, "b": 1})

    def test_float_is_not_canonicalisable(self) -> None:
        with pytest.raises(CanonicalizationError, match="float"):
            canonical_json_bytes({"x": 1.5})

    def test_set_is_not_canonicalisable(self) -> None:
        with pytest.raises(CanonicalizationError, match="unordered"):
            canonical_json_bytes({"x": {1, 2, 3}})

    def test_non_string_keys_rejected(self) -> None:
        with pytest.raises(CanonicalizationError, match="mapping keys"):
            canonical_json_bytes({1: "a"})

    def test_decimal_scaling_is_irrelevant(self) -> None:
        assert digest_of({"v": Decimal("1.50")}) == digest_of({"v": Decimal("1.5")})

    def test_datetime_normalised_to_utc(self) -> None:
        aware = dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC)
        other = aware.astimezone(dt.timezone(dt.timedelta(hours=1)))
        assert canonical_json_bytes({"t": aware}) == canonical_json_bytes({"t": other})

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(ValueError, match="naive"):
            to_canonical({"t": dt.datetime(2026, 3, 2, 14, 40)})

    def test_digest_is_stable(self) -> None:
        payload = {"b": [1, 2], "a": "x"}
        assert digest_of(payload) == digest_of({"a": "x", "b": [1, 2]})


class TestClock:
    def test_system_clock_is_utc_aware(self) -> None:
        now = SystemClock().now()
        assert now.tzinfo is not None
        assert now.utcoffset() == dt.timedelta(0)

    def test_simulated_clock_does_not_move_on_its_own(self) -> None:
        clock = SimulatedClock(dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC))
        first = clock.now()
        assert clock.now() == first

    def test_simulated_clock_rejects_going_backwards(self) -> None:
        clock = SimulatedClock(dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC))
        with pytest.raises(ValueError, match="backwards"):
            clock.advance_to(dt.datetime(2026, 3, 2, 14, 39, tzinfo=dt.UTC))

    def test_ensure_utc_rejects_naive(self) -> None:
        with pytest.raises(ValueError, match="naive"):
            ensure_utc(dt.datetime(2026, 3, 2, 14, 40))


class TestIds:
    def test_ids_are_deterministic_for_the_same_inputs(self) -> None:
        start = dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC)
        first = IdFactory("bt", SimulatedClock(start))
        second = IdFactory("bt", SimulatedClock(start))
        assert [first.next_id("oi") for _ in range(3)] == [second.next_id("oi") for _ in range(3)]

    def test_ids_increase_within_a_kind(self) -> None:
        factory = IdFactory("bt", SimulatedClock(dt.datetime(2026, 3, 2, tzinfo=dt.UTC)))
        a, b = factory.next_id("oi"), factory.next_id("oi")
        assert a < b

    def test_namespace_is_part_of_the_id(self) -> None:
        clock = SimulatedClock(dt.datetime(2026, 3, 2, tzinfo=dt.UTC))
        assert IdFactory("pa", clock).next_id("oi").startswith("pa-")

    def test_base32_rejects_overflow(self) -> None:
        with pytest.raises(CanonicalizationError):
            encode_base32(1 << 40, 4)

    def test_stable_token_is_a_pure_function(self) -> None:
        assert stable_token("abc") == stable_token("abc")
        assert len(stable_token("abc", 12)) == 12


class TestRedaction:
    def test_private_key_block_is_redacted(self) -> None:
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
        assert "MIIabc" not in redact(text)

    def test_key_value_assignment_keeps_the_field_name(self) -> None:
        redacted = redact("private_key=SUPERSECRETVALUE123")
        assert "SUPERSECRETVALUE123" not in redacted
        assert "private_key" in redacted

    def test_bearer_token_is_redacted(self) -> None:
        assert REDACTED in redact("Authorization: Bearer abcdefghijklmnop")

    def test_scan_reports_pattern_names(self) -> None:
        assert "bearer_token" in scan_for_secrets("Bearer abcdefghijklmnop")

    def test_clean_text_is_untouched(self) -> None:
        assert redact("SPY closed at 602.50") == "SPY closed at 602.50"

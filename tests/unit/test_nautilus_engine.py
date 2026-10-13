"""CR-04: the engine interface contract and the guarded execution client.

These tests need ``nautilus_trader==1.231.0`` installed and are marked ``requires_engine``.
They exist because ADR-0010 forbids trusting the published documentation for this version, and
working from the wheel's own source proved it right — twice:

* the documented constructor starts with ``loop``, which does not exist in 1.231.0;
* the documented override points are ``async def _submit_order``, while 1.231.0 exposes the
  **synchronous** ``cpdef`` ``submit_order(SubmitOrder)``.

An adapter written from the guides would have been non-functional in a way that still imported
cleanly. The first test below is the one that would have caught it.
"""

from __future__ import annotations

import pytest

from tigerquant.adapters.nautilus.compat import (
    EXPECTED_ENVIRONMENTS,
    PINNED_VERSION,
    check_interface,
)
from tigerquant.adapters.nautilus.execution_client import (
    EngineOrderTranslator,
    SubmissionGate,
    engine_available,
    status_for_broker_state,
)

# This module reaches an optional SDK through the adapter it imports, so it needs that
# extra installed. The fast lane syncs only `--extra dev` and DESELECTS it rather than
# failing: the marker existed in pyproject all along and nothing wore it, so CI ran these
# against an environment that deliberately lacks the dependency. Placed after the imports
# so it neither lands inside the docstring nor trips E402.
pytestmark = pytest.mark.requires_engine

requires_engine = pytest.mark.skipif(
    not engine_available(), reason="nautilus_trader is not installed"
)


class TestInterfaceContract:
    """ADR-0010: a wheel bump must fail loudly instead of drifting silently."""

    def test_the_installed_wheel_satisfies_the_contract(self) -> None:
        result = check_interface()
        assert result.available, "the engine should be installed for this lane"
        assert result.version == PINNED_VERSION
        assert result.ok, result.summary() + "\n" + "\n".join(result.problems)

    def test_the_engine_has_no_paper_environment(self) -> None:
        """The fact the whole mode translation table rests on."""
        from nautilus_trader.core.nautilus_pyo3 import Environment

        variants = {str(name) for name in Environment.variants()}
        assert variants == EXPECTED_ENVIRONMENTS
        assert "PAPER" not in variants

    def test_environment_is_not_where_the_name_suggests(self) -> None:
        """It is in the pyo3 extension module, not ``common.enums``.

        The first probe of this work looked in ``common.enums`` and failed. Pinning the real
        location means a future reader does not repeat the mistake.
        """
        import nautilus_trader.common.enums as common_enums

        assert not hasattr(common_enums, "Environment")
        from nautilus_trader.core.nautilus_pyo3 import Environment

        assert Environment is not None

    def test_the_override_points_are_synchronous_not_coroutines(self) -> None:
        """The v2 documentation shows ``async def _submit_order``. 1.231.0 does not."""
        import inspect

        from nautilus_trader.execution.client import ExecutionClient

        for name in ("submit_order", "cancel_order", "modify_order"):
            method = getattr(ExecutionClient, name)
            assert not inspect.iscoroutinefunction(method), (
                f"{name} is a coroutine in this version, so the adapter's synchronous "
                "override is wrong and would silently never be called"
            )
        # And the private coroutine names the v2 docs use do not exist here.
        assert not hasattr(ExecutionClient, "_submit_order")

    def test_the_constructor_takes_no_loop_argument(self) -> None:
        """Read from the shipped ``.pyx``: (client_id, venue, oms_type, account_type,
        base_currency, msgbus, cache, clock, config=None)."""
        from nautilus_trader.execution.client import ExecutionClient

        signature = _pyx_signature()
        assert "loop" not in signature
        assert "client_id" in signature
        assert "oms_type" in signature
        assert ExecutionClient is not None


class TestGuardedExecutionClient:
    def test_the_client_constructs_against_the_real_engine(self) -> None:
        from tigerquant.adapters.nautilus.execution_client import build_execution_client
        from tigerquant.core.enums import Mode, SubmissionTarget

        client = build_execution_client(
            broker=_NeverCalledBroker(),
            approvals={},
            submission_target=SubmissionTarget.NONE,
            mode=Mode.SHADOW,
        )
        client.tq_connect()
        assert type(client).__name__ == "TigerQuantExecutionClient"

    def test_a_shadow_gate_refuses_and_records(self) -> None:
        from tigerquant.core.enums import Mode, ReasonCode, SubmissionTarget
        from tigerquant.core.errors import RiskDenied

        gate = SubmissionGate(submission_target=SubmissionTarget.NONE, mode=Mode.SHADOW)
        assert not gate.allows_submission()
        with pytest.raises(RiskDenied) as caught:
            gate.check(client_order_id="TQ-1")
        assert ReasonCode.MODE_CAPABILITY_ABSENT in caught.value.reason_codes
        # The attempt is recorded, so a shadow run's would-be orders are visible evidence.
        assert len(gate.refusals) == 1
        assert "TQ-1" in gate.refusals[0]

    def test_a_paper_gate_permits(self) -> None:
        from tigerquant.core.enums import Mode, SubmissionTarget

        gate = SubmissionGate(submission_target=SubmissionTarget.BROKER, mode=Mode.PAPER)
        assert gate.allows_submission()
        gate.check(client_order_id="TQ-1")
        assert gate.refusals == []


class TestOrderTranslation:
    def test_a_market_buy_translates(self) -> None:
        result = EngineOrderTranslator().translate(_command(side="BUY", order_type="MARKET"))
        assert result.ok
        assert result.instrument == "SPY.US"
        assert result.quantity == 8
        assert result.side is not None

    def test_an_unknown_side_is_refused(self) -> None:
        result = EngineOrderTranslator().translate(_command(side="NO_ORDER_SIDE"))
        assert result.refused
        assert "long-only" in result.refusal

    def test_an_unsupported_order_type_is_refused(self) -> None:
        result = EngineOrderTranslator().translate(_command(order_type="STOP_MARKET"))
        assert result.refused
        assert "US equities only" in result.refusal

    def test_a_limit_order_without_a_price_is_refused(self) -> None:
        result = EngineOrderTranslator().translate(_command(order_type="LIMIT", price=None))
        assert result.refused
        assert "requires a price" in result.refusal

    def test_a_non_positive_quantity_is_refused(self) -> None:
        result = EngineOrderTranslator().translate(_command(quantity=0))
        assert result.refused
        assert "quantity must be positive" in result.refusal

    def test_a_missing_client_order_id_is_refused(self) -> None:
        result = EngineOrderTranslator().translate(_command(client_order_id=""))
        assert result.refused
        assert "cannot be correlated" in result.refusal

    def test_a_command_with_no_order_is_refused(self) -> None:
        assert EngineOrderTranslator().translate(object()).refused


class TestStatusMapping:
    def test_known_states_map(self) -> None:
        assert status_for_broker_state("FILLED") == "FILLED"
        assert status_for_broker_state("REJECTED") == "REJECTED"
        assert status_for_broker_state("PARTIALLY_FILLED") == "PARTIALLY_FILLED"

    def test_an_unknown_state_is_not_guessed(self) -> None:
        """Claiming FILLED for an unknown state would corrupt the engine's cache and P&L."""
        assert status_for_broker_state("SOMETHING_NEW") == "PENDING_UPDATE"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _pyx_signature() -> str:
    """Read the ``__init__`` signature from the ``.pyx`` shipped inside the wheel."""
    from pathlib import Path

    import nautilus_trader.execution.client as module

    # __file__ is the compiled .pyd (client.cp314-win_amd64.pyd), so with_suffix would look
    # for client.cp314-win_amd64.pyx and miss. The source sits beside it as client.pyx.
    source = Path(module.__file__).parent / "client.pyx"
    if not source.is_file():  # pragma: no cover - depends on the wheel layout
        pytest.skip("this wheel does not ship .pyx sources")
    text = source.read_text(encoding="utf-8")
    start = text.index("def __init__(")
    return text[start : text.index("):", start)]


def _command(**overrides: object) -> object:
    """A stand-in for the engine's ``SubmitOrder`` command."""
    fields: dict[str, object] = {
        "client_order_id": "TQ-1",
        "instrument_id": "SPY.US",
        "side": "BUY",
        "order_type": "MARKET",
        "quantity": 8,
        "time_in_force": "DAY",
        "price": None,
    }
    fields.update(overrides)

    class _Order:
        pass

    order = _Order()
    for name, value in fields.items():
        if name in {"side", "order_type", "time_in_force"}:
            setattr(order, name, type("E", (), {"name": value})())
        else:
            setattr(order, name, value)

    class _Command:
        pass

    command = _Command()
    command.order = order
    return command


class _NeverCalledBroker:
    """A broker that fails loudly if anything reaches it.

    Used to prove the gate refuses *before* a submission is attempted: if the gate is broken,
    this raises rather than silently succeeding.
    """

    def submit(self, approval: object, *, at: object) -> object:
        msg = "the gate must refuse before the broker is reached"
        raise AssertionError(msg)

    def cancel(self, *, client_order_id: str, at: object) -> object:
        msg = "not used"
        raise AssertionError(msg)

    def query(self, *, client_order_id: str) -> object | None:
        return None

    def poll(self, *, at: object) -> tuple[object, ...]:
        return ()

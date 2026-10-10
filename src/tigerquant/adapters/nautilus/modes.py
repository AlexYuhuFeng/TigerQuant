"""Mode translation between TigerQuant's four modes and NautilusTrader's three environments.

This module is deliberately free of any engine import. It is pure mapping logic with a long
explanation, because the mapping is where the most dangerous mistake in CR-04 lives: it is
tempting to translate our ``PAPER`` to NautilusTrader's ``SANDBOX`` because both sound like
"not real money". They are not the same thing, and conflating them would put a false label on
every result the system produces.

The verified facts (ADR-0010, re-checked by the compatibility test):

    NautilusTrader ``Environment`` = {BACKTEST, SANDBOX, LIVE}
    There is NO ``PAPER`` environment.

So:

* ``BACKTEST`` -> ``BACKTEST``. Obvious, and safe.
* ``PAPER``    -> ``LIVE``. A Tiger paper account is reached over the ordinary production
  gateway with a real HTTP API and a real order state machine. From the engine's point of
  view that is a live connection to an external venue. What makes it *paper* is the
  **account identity**, enforced by :class:`~tigerquant.adapters.tiger.guard.TigerAccountGuard`,
  not the engine's environment. Reporting it as ``PAPER_VERIFIED`` rather than
  ``LIVE_VERIFIED`` is our label, applied because the venue account is a simulator.
* ``SHADOW``   -> ``SANDBOX`` with ``SubmissionTarget.NONE``: the strategy runs and produces
  intents, and nothing is submitted anywhere. Shadow is about *observing* decisions, so the
  engine executes nothing.
* ``LIVE``     -> **refused**. There is no mapping. This is not an oversight and not a
  ``NotImplementedError`` waiting to be filled in: translating to ``Environment.LIVE`` would
  arm a path this project must never arm autonomously, so the function raises and the
  refusal is tested.

``SANDBOX`` is also what a pure offline simulation maps to, which is why the mode alone is
not enough to decide: :func:`translate` takes the submission target too, and refuses to infer
one from the other.
"""

from __future__ import annotations

from dataclasses import dataclass

from tigerquant.core.enums import Mode, SubmissionTarget
from tigerquant.core.errors import ConfigurationError

__all__ = [
    "ENGINE_ENVIRONMENTS",
    "ModeTranslation",
    "describe_translation_table",
    "translate",
]

#: The engine's Environment members, as verified against the pinned tag. Kept here as data so
#: the compatibility test can compare it against the installed wheel without importing it.
ENGINE_ENVIRONMENTS = ("BACKTEST", "SANDBOX", "LIVE")


@dataclass(frozen=True, slots=True)
class ModeTranslation:
    """The result of translating one of our modes into engine terms."""

    mode: Mode
    #: The engine's environment name, or ``None`` when the mode does not use the engine.
    engine_environment: str | None
    #: Where orders would go. Carried explicitly so it can never be inferred from the mode.
    submission_target: SubmissionTarget
    #: Whether the engine is allowed to execute anything at all.
    engine_may_execute: bool
    #: The implementation status a result from this configuration may claim.
    implementation_status: str
    rationale: str

    @property
    def is_simulated(self) -> bool:
        return self.submission_target is SubmissionTarget.NONE


def translate(
    mode: Mode,
    *,
    submission_target: SubmissionTarget | None = None,
) -> ModeTranslation:
    """Translate a mode into engine terms.

    ``submission_target`` must be supplied for the modes where it is not implied, rather than
    defaulted. Inferring "paper means the broker" or "shadow means nothing" would bake an
    assumption into the one place where being wrong is expensive.
    """
    if mode is Mode.BACKTEST:
        if submission_target not in (None, SubmissionTarget.NONE):
            msg = (
                f"BACKTEST cannot submit anywhere, but submission_target="
                f"{submission_target.value!r} was requested. A backtest that reaches a venue "
                "is not a backtest."
            )
            raise ConfigurationError(msg)
        return ModeTranslation(
            mode=mode,
            engine_environment="BACKTEST",
            submission_target=SubmissionTarget.NONE,
            engine_may_execute=False,
            implementation_status="SIMULATED",
            rationale=(
                "Historical replay. The engine's own simulation is the counterparty, so "
                "Environment.BACKTEST is an exact match and nothing external is contacted."
            ),
        )

    if mode is Mode.SHADOW:
        if submission_target not in (None, SubmissionTarget.NONE):
            msg = (
                "SHADOW observes decisions; it never submits. "
                f"submission_target={submission_target.value!r} is refused."
            )
            raise ConfigurationError(msg)
        return ModeTranslation(
            mode=mode,
            engine_environment="SANDBOX",
            submission_target=SubmissionTarget.NONE,
            engine_may_execute=False,
            implementation_status="SIMULATED",
            rationale=(
                "Shadow runs the real strategy loop and records what it WOULD have done. "
                "The engine still needs an execution client to construct a node, so SANDBOX "
                "is used, but submission_target is NONE and the client refuses every submit. "
                "A shadow run that reaches a venue is a bug, not a mode."
            ),
        )

    if mode is Mode.PAPER:
        if submission_target is None:
            msg = (
                "PAPER requires an explicit submission_target. Defaulting it to BROKER would "
                "make an unconfigured run reach a real venue; defaulting it to NONE would "
                "silently make a paper run a simulation. Neither is acceptable, so the caller "
                "must say which it means."
            )
            raise ConfigurationError(msg)
        if submission_target is SubmissionTarget.NONE:
            return ModeTranslation(
                mode=mode,
                engine_environment="SANDBOX",
                submission_target=SubmissionTarget.NONE,
                engine_may_execute=False,
                implementation_status="SIMULATED",
                rationale=(
                    "PAPER with submission_target NONE is a paper-shaped rehearsal against the "
                    "local simulator. It is labelled SIMULATED, not PAPER_VERIFIED, because no "
                    "broker was involved."
                ),
            )
        return ModeTranslation(
            mode=mode,
            engine_environment="LIVE",
            submission_target=submission_target,
            engine_may_execute=True,
            implementation_status="PAPER_VERIFIED",
            rationale=(
                "A Tiger paper account is reached over the ordinary production gateway with a "
                "real API and a real order state machine, so the engine must be in LIVE to "
                "connect to it. What makes this paper is the ACCOUNT IDENTITY, verified from "
                "the broker's own response by TigerAccountGuard -- not the engine environment. "
                "PAPER_VERIFIED, never LIVE_VERIFIED: the venue account is a simulator."
            ),
        )

    # Mode.LIVE
    msg = (
        "LIVE has no translation and never will in autonomous development. Mapping it to "
        "Environment.LIVE would arm the one path this project must not arm without a separate, "
        "deliberate human authorization (AGENTS.md invariants 1 and 9, ADR-0010). This refusal "
        "is the control, not a missing implementation."
    )
    raise ConfigurationError(msg)


def describe_translation_table() -> tuple[dict[str, str], ...]:
    """The whole table, for the operator surface and the docs."""
    rows: list[dict[str, str]] = []
    for mode in (Mode.BACKTEST, Mode.SHADOW, Mode.PAPER, Mode.LIVE):
        for target in (
            (SubmissionTarget.NONE,)
            if mode is not Mode.PAPER
            else (SubmissionTarget.NONE, SubmissionTarget.BROKER)
        ):
            try:
                result = translate(mode, submission_target=target)
            except ConfigurationError as exc:
                rows.append(
                    {
                        "mode": mode.value,
                        "submission_target": target.value,
                        "engine_environment": "(refused)",
                        "status": "(refused)",
                        "rationale": str(exc).split(".")[0] + ".",
                    }
                )
                continue
            rows.append(
                {
                    "mode": mode.value,
                    "submission_target": target.value,
                    "engine_environment": result.engine_environment or "(none)",
                    "status": result.implementation_status,
                    "rationale": result.rationale,
                }
            )
    return tuple(rows)

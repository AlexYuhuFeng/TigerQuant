"""CR-06: the LiteLLM vendor binding -- the properties that must hold BEFORE a provider is called.

`requires_llm` was declared, selected in CI, and **worn by no test at all**. The `llm` extra is
installed in the extras lane and the only thing verified about it was `import litellm` and
`hasattr(litellm, "completion")` -- an import-compatibility check, which says nothing about the
adapter.

These tests need the extra and NOTHING ELSE: no API key, no network, no provider. Every one exercises
`litellm_transport` with a monkeypatched `litellm.completion`, so they run in CI and would run
against a real provider only if somebody wired one up.

The properties are the ones the module's own docstring claims:

* a missing key raises ``ConfigurationError`` **BEFORE the network**, because a missing key is a
  misconfiguration and a provider outage is not, and conflating them hides the actual problem;
* ``num_retries=0`` is deliberate -- a retry makes latency unbounded and hides an outage behind a
  slower failure, and a decision that arrives after the market moved is worse than no decision;
* a provider error is **REDACTED**, because a provider message can echo the request and the request
  can carry a URL with a token in it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("litellm", reason="the llm extra is not installed")

from tigerquant.adapters.llm.litellm_transport import litellm_transport
from tigerquant.core.errors import ConfigurationError, IntegrationUnavailable

pytestmark = pytest.mark.requires_llm

MESSAGES = [{"role": "user", "content": "decide"}]


class _Capture:
    """A stand-in for `litellm.completion` that records how it was called."""

    def __init__(self, *, raises: Exception | None = None, returns: object = None) -> None:
        self.kwargs: dict[str, object] = {}
        self.called = False
        self._raises = raises
        self._returns = returns if returns is not None else {"choices": []}

    def __call__(self, **kwargs: object) -> object:
        self.called = True
        self.kwargs = kwargs
        if self._raises is not None:
            raise self._raises
        return self._returns


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> _Capture:
    import litellm

    stub = _Capture()
    monkeypatch.setattr(litellm, "completion", stub, raising=True)
    return stub


def call(**overrides: object) -> object:
    payload: dict[str, object] = {
        "messages": MESSAGES,
        "model": "deepseek/deepseek-chat",
        "timeout_seconds": 20.0,
        "max_tokens": 1024,
        "temperature": 0.0,
    }
    payload.update(overrides)
    return litellm_transport(**payload)  # type: ignore[arg-type]


class TestAMissingKeyIsAMisconfigurationNotAnOutage:
    def test_it_raises_before_the_network(self, capture: _Capture) -> None:
        """THE property. `api_key_env` names an unset variable, so the call must refuse WITHOUT
        calling the provider -- there is no key, so there is nothing to send."""
        with pytest.raises(ConfigurationError):
            call(api_key_env="TIGERQUANT_DEFINITELY_NOT_SET_12345")
        assert not capture.called, (
            "the provider was called without a key. A missing key is a CONFIGURATION error and must "
            "be caught before the network, or a misconfiguration is reported as an outage."
        )

    def test_no_retry_means_no_retry_after_a_refusal(self, capture: _Capture) -> None:
        with pytest.raises(ConfigurationError):
            call(api_key_env="TIGERQUANT_DEFINITELY_NOT_SET_12345")
        assert not capture.called

    def test_the_message_names_the_variable(self, capture: _Capture) -> None:
        with pytest.raises(ConfigurationError) as caught:
            call(api_key_env="TIGERQUANT_DEFINITELY_NOT_SET_12345")
        assert "TIGERQUANT_DEFINITELY_NOT_SET_12345" in str(caught.value)

    def test_the_message_says_the_key_is_not_read_from_a_file(self, capture: _Capture) -> None:
        """Stated because it is the property that keeps a key out of the repository."""
        with pytest.raises(ConfigurationError) as caught:
            call(api_key_env="TIGERQUANT_DEFINITELY_NOT_SET_12345")
        assert "never from a file" in str(caught.value)

    def test_with_no_key_env_the_call_proceeds(self, capture: _Capture) -> None:
        """The check is opt-in: a deployment using ambient provider credentials is not forced to name
        an environment variable."""
        call(api_key_env=None)
        assert capture.called


class TestTheCallIsBounded:
    def test_num_retries_is_zero(self, capture: _Capture) -> None:
        """Deliberate, and the docstring says why: a retry makes latency unbounded and hides an
        outage behind a slower failure."""
        call()
        assert capture.kwargs["num_retries"] == 0

    def test_the_timeout_is_passed(self, capture: _Capture) -> None:
        call(timeout_seconds=7.5)
        assert capture.kwargs["timeout"] == 7.5

    def test_the_budget_is_passed(self, capture: _Capture) -> None:
        call(max_tokens=321, temperature=0.25)
        assert capture.kwargs["max_tokens"] == 321
        assert capture.kwargs["temperature"] == 0.25

    def test_the_messages_and_model_are_passed_through_unchanged(self, capture: _Capture) -> None:
        call()
        assert capture.kwargs["messages"] == MESSAGES
        assert capture.kwargs["model"] == "deepseek/deepseek-chat"


class TestAProviderErrorIsRedacted:
    def test_it_becomes_integration_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A provider outage is NOT a configuration error, and the two must not be conflated."""
        import litellm

        monkeypatch.setattr(
            litellm, "completion", _Capture(raises=RuntimeError("upstream exploded"))
        )
        with pytest.raises(IntegrationUnavailable):
            call()

    def test_a_token_in_the_error_message_does_not_leak(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE security property. A provider error can echo the request, and the request can carry a
        URL with a token in it. The module redacts for exactly this reason, and nothing tested it."""
        import litellm

        # A FAKE credential, shaped like the real thing so redaction has something to find.
        secret = "sk-" + "0123456789abcdef" * 2
        monkeypatch.setattr(
            litellm,
            "completion",
            _Capture(raises=RuntimeError(f"bad request to https://api.example.com?key={secret}")),
        )
        with pytest.raises(IntegrationUnavailable) as caught:
            call()
        rendered = str(caught.value)
        assert secret not in rendered, (
            "a credential appeared in an exception message. Provider errors are echoed into logs and "
            "audit output, so this is a leak, not a cosmetic problem."
        )

    def test_the_error_type_is_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The class name is useful and carries no payload."""
        import litellm

        monkeypatch.setattr(litellm, "completion", _Capture(raises=RuntimeError("x")))
        with pytest.raises(IntegrationUnavailable) as caught:
            call()
        assert "RuntimeError" in str(caught.value)


class TestTheResponseIsPlainData:
    def test_a_mapping_comes_back(self, capture: _Capture) -> None:
        """The transport returns a plain mapping so the decision layer never sees a vendor type."""
        result = call()
        assert isinstance(result, dict)

    def test_a_vendor_object_is_normalised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """LiteLLM returns a pydantic-ish object; the boundary must not leak one upward."""

        class Vendor:
            def model_dump(self) -> dict[str, object]:
                return {"choices": [{"message": {"content": "{}"}}]}

        import litellm

        monkeypatch.setattr(litellm, "completion", _Capture(returns=Vendor()))
        result = call()
        assert isinstance(result, dict)
        assert result.get("choices")

"""Tiger OpenAPI adapters: OAuth2 session, account guard, read-only client, paper execution.

The execution client is exported deliberately: it is the single broker write path, and
naming it here keeps it visible rather than hidden behind a deep import. Constructing it
requires a write-scoped credential, which this repository's development grant does not
hold — see :func:`~tigerquant.adapters.tiger.execution.require_write_scope`.
"""

from __future__ import annotations

from tigerquant.adapters.tiger.execution import (
    WRITE_SCOPE,
    TigerPaperExecutionClient,
    require_write_scope,
)
from tigerquant.adapters.tiger.guard import (
    AccountIdentity,
    AccountVerdict,
    TigerAccountGuard,
    mask_account,
    select_unique_paper_account,
    verify_paper_account,
)
from tigerquant.adapters.tiger.oauth import (
    FORBIDDEN_SCOPES,
    READ_ONLY_SCOPES,
    TigerEnvironment,
    TigerQuantOAuthSession,
    build_oauth2_config,
    default_token_home,
)
from tigerquant.adapters.tiger.read import (
    BrokerBalances,
    BrokerOrder,
    BrokerPosition,
    BrokerSnapshot,
    ReconciliationReport,
    TigerReadOnlyClient,
    reconcile,
)

__all__ = [
    "FORBIDDEN_SCOPES",
    "READ_ONLY_SCOPES",
    "WRITE_SCOPE",
    "AccountIdentity",
    "AccountVerdict",
    "BrokerBalances",
    "BrokerOrder",
    "BrokerPosition",
    "BrokerSnapshot",
    "ReconciliationReport",
    "TigerAccountGuard",
    "TigerEnvironment",
    "TigerPaperExecutionClient",
    "TigerQuantOAuthSession",
    "TigerReadOnlyClient",
    "build_oauth2_config",
    "default_token_home",
    "mask_account",
    "reconcile",
    "require_write_scope",
    "select_unique_paper_account",
    "verify_paper_account",
]

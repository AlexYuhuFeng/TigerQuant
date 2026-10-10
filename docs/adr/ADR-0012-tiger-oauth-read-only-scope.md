# ADR-0012 — OAuth 2.0 authorization with a read-only scope ceiling

- Status: Accepted
- Date: 2026-10-10
- CR: CR-02 (authorization and account verification)
- Supersedes: none. Extends ADR-0003 (mode and account isolation) and ADR-0009 §3.

## Context

`tigeropen==3.8.0` ships a complete OAuth 2.0 client (`tigeropen/auth/oauth2/`, eleven
modules) that removes the need for a `private_key` and a `tiger_id`. Verified against the
live authorization server on 2026-10-10:

| Discovery field | Value |
|---|---|
| `issuer` | `https://openapi-oauth2.tigerfintech.com` |
| `authorization_endpoint` | `…/oauth2/authorize` |
| `token_endpoint` | `…/oauth2/token` |
| `registration_endpoint` | `…/oauth2/register` (RFC 7591 dynamic registration) |
| `device_authorization_endpoint` | `…/oauth2/device_authorization` (RFC 8628) |
| `revocation_endpoint` | `…/oauth2/revoke` (RFC 7009) |
| `scopes_supported` | `api.basic:read`, `api.quote:read`, `api.account:read`, `api.trade:read`, **`api.trade:write`** |
| `grant_types_supported` | `authorization_code`, `refresh_token`, `urn:ietf:params:oauth:grant-type:device_code` |
| `code_challenge_methods_supported` | `S256` |

Three properties of this design drove the decision:

1. **The SDK requests every advertised scope.** `OAuth2SessionManager._scopes()` joins
   `metadata.scopes_supported` wholesale. Used as-is, the owner is asked to consent to
   `api.trade:write`, and this process would hold a credential that can place orders.
2. **There is no separate paper authorization server.** The sandbox host is a *business
   gateway* only (`SANDBOX_SERVER_URL`); `…/openapi-sandbox…/.well-known/oauth-authorization-server`
   does not exist. One production issuer serves both paper and live Tiger identities.
3. **The token is not paper-bound.** The authorization covers whichever accounts the
   holder approved, and those routinely include funded live accounts. Paper/live is *not*
   discoverable from the token exchange — `_to_token()` reads only
   `access_token`/`refresh_token`/`token_type`/`scope`/`expires_in`. It is discoverable only
   afterwards, via `get_managed_accounts()`.

## Decision

1. **Request exactly four read scopes and nothing else.**
   `api.basic:read api.account:read api.quote:read api.trade:read`. `api.trade:write` is
   never requested. Implemented in
   `adapters/tiger/oauth.py::READ_ONLY_SCOPES`.
2. **Verify the granted scope, and refuse the token if it is wider than requested.** A
   server that ignores the requested scope set would otherwise hand us a write-capable
   credential. `_assert_no_write_scope` raises rather than accepting it. This is the
   defence against the failure mode being silent.
3. **Make "no broker write capability" a property of the credential, not of the code.**
   ADR-0007 §7 already ensures a single write call site. With this ADR, a token that was
   never granted `api.trade:write` cannot place an order *whatever* any module does with
   it. The two controls are independent, which is the point.
4. **Guard the mechanism with a compatibility test.** The scope ceiling is implemented by
   pinning the SDK's own scope-resolution cache, so
   `tests/unit/test_tiger_oauth.py` asserts (a) the SDK still advertises
   `api.trade:write`, (b) our session resolves to the read-only set, and (c) the pin fails
   loudly rather than silently degrading if the SDK changes. A future wheel bump breaks CI
   instead of quietly requesting trade-write.
5. **Never infer paper status from the token.** `adapters/tiger/guard.py` requires the
   *broker-reported* account list to contain the configured account with a paper identity,
   on every use. An unknown or missing account type is a refusal, never a pass.
6. **Register our own OAuth surface.** `surface="tigerquant"`, `client_name="TigerQuant"`
   — the SDK documentation is explicit that a host application must pass its own value,
   otherwise it shares the SDK's client identity and token file, and cannot be revoked
   independently on the owner's authorized-apps page.
7. **Tokens live outside Git and are never printed.** Default store
   `<workspace>/.secrets/tiger` (git-ignored), overridable with `TIGERQUANT_TIGER_HOME`.
   `describe()` returns a redacted form; the audit journal records `token: ***REDACTED***`
   and a masked account number only.
8. **CR-02 is read-only, and CR-03 does not weaken this.** CR-02 performs no order call and
   does not import a write path. CR-03 may place paper orders, but only after
   re-verifying the account in the same process and asserting
   `order.account == verified.account` at the single write call site — closing the
   documented hazard that `place_order` sends `order.account`, not `config.account`.
9. **Live access is refused at runtime, not merely discouraged.** The guard rejects any
   account that is not all-digit with length ≥ 17. A human who authorizes a funded account
   produces a token that this code will decline to use.

## Consequences

- Authorization is a **human step**: the owner signs in to Tiger's consent page in a
  browser. This is consistent with invariant 10 — a mode/identity change is a deployment
  authorization, never an AI tool call. The repository contains no code that completes a
  consent on the owner's behalf.
- We lose the ability to test the write path end-to-end without a second, separately
  authorized grant. That is the intended trade: the read-only token used by development can
  never place an order.
- Order submission (CR-03) will need a *write-scoped* grant. That must be a separate,
  deliberate decision by the owner, with its own ADR, and it does not weaken items 1–9 for
  any process that is not the execution service.
- The device-code flow is available as a fallback but is ranked lower: the SDK's own
  documentation notes it lacks redirect binding, so approval cannot be tied to this machine.
  `tools/tiger_login.py --flow device` exists for headless environments only.
- Configuration cannot relax any of this. There is no flag that enables a production
  client (`build_oauth2_config(..., allow_production=True)` is required and no code path in
  this repository sets it) and no flag that disables the account guard.

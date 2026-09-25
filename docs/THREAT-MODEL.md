# Threat model and security review

Written for **T33** at 0.13.0, reviewing the four paths the task names: **mTLS, OIDC, the UI,
A2A push**. Every claim below was read in the code and, where it could be tested, tested - the
two findings are fixed, with a test each.

## What is being protected

- **coord2.db** - the organisation's state: claims, messages, discussions and decisions, documents
  (including `private` ones with named readers), tasks, project memory.
- **Credentials**: agent certificates and keys in `~/.config/coord/`, Keycloak access tokens, and
  the tokens coord stores to call a webhook back (wake hooks, A2A push).
- **The machine itself** - coord can be made to send HTTP requests on the operator's behalf, which
  is what the push allow-list is for.

## Trust boundaries

| # | Crossing | Enforced by |
|---|---|---|
| 1 | Network → coord-server | loopback by default; `build_server` **refuses** a non-loopback bind without TLS **and** an identity method (`--client-ca` mTLS, or OIDC) - `SystemExit`, not a warning |
| 2 | Agent → ops | mTLS certificate verified against the CA, or a Keycloak bearer token introspected per request |
| 3 | Browser → UI | bound to `127.0.0.1`, a per-launch token, Host/Origin/cookie checks |
| 4 | coord-server → external webhooks | `host_allowed()`: loopback or `--push-allow` only |

Anything not listed has no network surface: there is no other listener, and the A2A binding sits
behind the same handler and the same identity checks as `POST /call`.

## mTLS

Certificates come from the local CA (`coord-admin enroll`). With `--pki` the server asks
`pki.Authority` **on every request** whether the presented certificate is still valid, so
revocation applies at once - no restart - and a certificate past `RENEW_AFTER_DAYS` is renewed
and handed back in the response. The server's own certificate is renewed by `--cert-source` and
never outlives `pki.MAX_DAYS` (47).

The mode separation is deliberate and tested: `local_and_oidc_modes_never_load_pki` proves that
local mode and OIDC mode never import the PKI code at all, so a deployment that does not use
mTLS does not carry mTLS's attack surface. Tests: `mtls_with_crl`, `mtls_renewal_and_live_revocation`,
`server_cert_auto_renewal_and_47_day_cap`, `old_ca_is_upgraded_for_strict_clients`,
`ca_is_safe_across_processes`.

## OIDC

The bearer token is introspected against Keycloak; the result is cached for
`--oidc-cache-seconds` (**default 60**).

**Accepted risk:** a token revoked during that window may still work for up to a minute. The
cache exists because introspection is a network round trip per request. Lower it if revocation
latency matters more than throughput; the flag says so in its own help text.

**Verified:** the logs record `sub` and `active`, never the token (`oidc: token introspection
cached, sub=...`). A missing or inactive token is `unauthenticated`.

## The UI (a browser on the same machine)

Bound to `127.0.0.1` only. Access is a per-launch token: `http://127.0.0.1:<port>/?t=<token>` is
exchanged for a cookie that is `HttpOnly; SameSite=Strict; Path=/`. Four independent checks:

- **`_host_ok`** - the `Host` header must be `127.0.0.1:<port>` or `localhost:<port>`, which is
  what defeats DNS rebinding: a domain pointing at 127.0.0.1 arrives with its own Host and gets 403.
- **`_cookie_ok`** - the cookie must equal the token, compared with `secrets.compare_digest`
  (constant time, no timing signal).
- **`_origin_ok`** - every POST must carry the UI's own `Origin`, so a page on another origin
  cannot write, even if it could send the cookie. `SameSite=Strict` also withholds the cookie
  from cross-site requests in the first place.
- **CSP** on the HTML: `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'
  data:; connect-src 'self'` - no inline script, no external anything. Plus `no-store`,
  `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer` on every reply.

The UI cannot mint identities: `whoami` through `/api/call` returns `bad_op` (tested).

### Finding 1 - the UI token reached the log (fixed)

`log_message` logged the request line verbatim, and the request line is
`GET /?t=<token> HTTP/1.1`. With `-vv` (DEBUG) the one-shot access token was written to stderr.
That contradicts the project's own rule - *nothing secret in logs* - and the token is a bearer
credential for the whole UI.

**Fixed** in `ui.py`: each argument is passed through `str(a).replace(token, "<token>")` before
logging, so the line reads `/?t=<token>`. Tested in `ui_guards_and_calls`, which captures the
`coord-server` logger at DEBUG during the exchange and asserts both that the token appears
nowhere and that the redacted form does.

## A2A push and wake hooks (SSRF)

`host_allowed(url, allow)` is the whole gate: `http`/`https` with a hostname; **loopback is
always allowed**; otherwise the host must match `--push-allow` (`name`, `.suffix`, or `*`).

Details that matter:
- A domain suffix (`.example.com`) **never** matches an IP literal - you cannot make `169.254.169.254`
  pass by allow-listing a suffix.
- The check does **not** resolve DNS, so a name cannot be tricked into *looking* like loopback:
  anything that is not literally loopback needs the allow-list.
- It is applied at registration for **both** entry points - `wake_hook_set` (`server.py`) and
  `CreateTaskPushNotificationConfig` (`a2a.py`) - and re-checked at delivery for wake hooks, so a
  hook registered under an older configuration is re-evaluated.

### Finding 2 - a redirect escaped the allow-list (fixed)

Both delivery paths built the default `urllib` opener, and the default opener **follows
redirects**. The allow-list is checked once, before the request. So an allow-listed - or
loopback - endpoint answering `302 Location: http://169.254.169.254/...` made coord-server POST
the payload to a host nobody allow-listed, **carrying the `Authorization: Bearer <wake token>`
header with it**. The allow-list was a precondition, not a property of the transport.

**Fixed** in `a2a.py`: `_opener()` installs `_NoRedirect`, which raises
`HTTPError("redirect refused: the push allow-list was checked for this URL, not for the next one")`
instead of following; both `wake()` and `_post()` use it. `_post()` now also re-checks
`host_allowed` at delivery, the way `wake()` already did.

Tested in `a_push_follows_no_redirect`: a loopback hook answers 302 to a second loopback server,
driven through a real `coord-server` (the push runs in the server's thread). The wake request
ends `failed` with the redirect reason, and the second server **received nothing**.

### Residual risks, accepted

- **Rebinding after the check.** A name allow-listed today could resolve somewhere else tomorrow;
  the check does not resolve. The default posture - loopback only, no allow-list - has no such
  name in it.
- **`--push-allow "*"` is wide open**, including link-local and metadata addresses. It is opt-in
  and named `*`; do not set it on a host with anything worth reaching.
- **Webhook credentials are stored in the database in the clear** (wake-hook token, A2A
  `auth_credentials`) because coord must replay them on the next delivery. Protect the file: it
  is the same trust as `coord2.db`, not a new one.
- **`-vv` logs op arguments** (truncated to 300 chars). Useful when debugging, wrong when the log
  is shared - a `doc_import` body or a message note can pass through it.

## What was not found

No `verify=False`/unverified TLS, no `pickle`, no `eval`/`exec`, no shell interpolation, no
hardcoded credential anywhere in `coordination/`. Claim paths are normalized and rejected before
they reach the filesystem (`../`, absolute, drive letters). The UI never runs an op as an
arbitrary identity. None of this survives an audit by itself - it is the state of the code on
25 September 2026, at 0.13.0.

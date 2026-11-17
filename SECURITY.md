# Security policy

Only the latest stable release and latest beta receive fixes. Beta builds are
experimental; do not use tariff sensors for critical billing decisions.

Report a vulnerability privately to the repository owner through an access-
restricted channel, using GitHub private vulnerability reporting when available.
Do not publish credentials or reproduction captures in an issue, PR, discussion
or chat.

The integration uses an unsupported private portal API. Session headers are
stored in Home Assistant config entries and backups, which are not guaranteed
encrypted. Protect HA administrator access, `.storage`, backups and filesystem
permissions. Headers can authorize broader portal access than this integration
needs. Revoke exposed sessions where the provider allows it.

The persistent browser-login option stores renewable tokens and the selected
account binding in the HA config entry; no password is supplied to HA.
It uses state, nonce, one-use S256 PKCE, a pinned HTTPS token/JWKS origin and
RS256 issuer/audience/expiry validation. Initial identity must contain the
matching nonce; refresh must preserve subject and signed customer membership.
Only signed customer IDs scope account initialization, and only the explicitly
selected account is used for tariffs. Profile/address fields in initialization
are discarded. Public application keys are fetched anonymously from fixed-origin
frontend assets, not embedded or executed.

Concurrent API updates serialize refresh. Token rotation is persisted before
API use; transient failures do not erase credentials. Reauthentication must
match the existing subject and contract account. Changing an entry back to
manual/session import removes its locally stored renewable credentials.
Cancellation/expiry clears unconfirmed tokens but does not revoke a provider
grant. Protect config entries and backups: there is no separate token encryption.

The optional [local Edge companion](edge-extension/README.md) transfers only an
explicitly paired portal session to the chosen signed-in HA tab. Pairing is
single-use and bounded to ten minutes. It adds no anonymous callback and stores
no portal credentials or HA tokens in extension storage. Use HTTPS except for
local loopback testing; trust the HA origin before connecting. Disconnect cannot
retract an already-sent request: cancel the HA flow to discard unconfirmed data.

The experimental password test is opt-in and diagnostic only. It submits a
password once to the fixed provider identity endpoint, using an isolated
temporary cookie jar. It does not persist the username/password or session,
does not create or update an integration entry, and stops on CAPTCHA/MFA.
Credential POST redirects are never followed. Its warning log contains only
fixed stage/outcome labels and an HTTP status; never enable HTTP wire logging.

The separate app-browser OAuth test uses the app's public client identifier
(not a client secret) and exact registered callback. It does not add mobile-client
flags. Each attempt uses random state and S256 PKCE, expires in ten minutes,
and is consumed before any exchange. The masked callback field is sensitive.
Code exchange and a single refresh are sent only to the fixed provider HTTPS
token endpoint, with no redirects/retries/cookies. Tokens are discarded, not
stored in config entries. Only fixed stage/outcome and HTTP status are logged.
It does not authenticate an identity, validate identity-token signatures, read
account APIs or revoke the temporary grant at the provider. Provider approval
of this HA usage is unconfirmed.

Do not include passwords, tokens, cookies, account identifiers, addresses, actual
contract prices, HAR files, or raw portal responses in reports or chat. Enter
your password only in the local masked test form, not in debugging messages.
Automated tests must use invented data.
Diagnostics are an allowlist of health metadata, without credentials or tariffs.

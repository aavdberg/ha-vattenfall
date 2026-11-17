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

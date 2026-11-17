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

Do not submit passwords, tokens, cookies, account identifiers, addresses, actual
contract prices, HAR files, or raw portal responses. Tests must use invented data.
Diagnostics are an allowlist of health metadata, without credentials or tariffs.

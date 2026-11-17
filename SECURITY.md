# Security policy

Only the latest stable release and latest beta receive fixes. Beta builds are
experimental; do not use tariff sensors for critical billing decisions.

Report a vulnerability privately to the repository owner through an access-
restricted channel. Do not publish credentials or reproduction captures in an
issue, PR, discussion or chat. GitHub private vulnerability reporting may not be
available for this private repository.

The integration uses an unsupported private portal API. Session headers are
stored in Home Assistant config entries and backups, which are not guaranteed
encrypted. Protect HA administrator access, `.storage`, backups and filesystem
permissions. Headers can authorize broader portal access than this integration
needs. Revoke exposed sessions where the provider allows it.

Do not submit passwords, tokens, cookies, account identifiers, addresses, actual
contract prices, HAR files, or raw portal responses. Tests must use invented data.
Diagnostics are an allowlist of health metadata, without credentials or tariffs.

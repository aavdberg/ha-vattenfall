<p align="center">
  <img src="custom_components/vattenfall/brand/icon.png" width="128" height="128" alt="Unofficial Mijn Vattenfall integration icon">
</p>

# Mijn Vattenfall for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://www.hacs.xyz/docs/faq/custom_repositories/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Unofficial, experimental and read-only.** View Dutch electricity/gas tariffs,
monthly usage summaries and installment information in Home Assistant.
Not affiliated with, endorsed by, or supported by Vattenfall or Home Assistant.
The original project icon is not an official company logo.

> **Authentication:** browser login can now create an account-bound connection
> with automatic token renewal. Password/CAPTCHA/MFA stay on Vattenfall's site.
> This uses the app's public OAuth identifier, not an officially supported HA
> grant. Initial setup still requires copying one native callback link.
> The optional local Edge companion and manual session import remain available;
> those imported sessions do **not** gain automatic refresh. An optional
> **experimental password test** sends credentials once from HA to Vattenfall for
> diagnostics only; it does not connect or update the integration. A separate
> **experimental app-browser test** checks code exchange and one immediate
> refresh after normal provider login. It does not save tokens or connect HA.

## Features

Entities are created only when the selected account supplies valid data.

| Entity | Unit / meaning |
| --- | --- |
| Electricity normal/off-peak or single tariff | EUR/kWh, including VAT and energy tax |
| Gas tariff | EUR/m³, including VAT and energy tax |
| Monthly installment | EUR, active monthly payment amount |
| Recommended monthly installment | EUR, only if the portal reports successful advice |
| Contract status | Status reported by the consumption service |
| Electricity imported / exported previous month | kWh; export only for a feed-in supply |
| Gas consumption previous month | m³ |
| Electricity / gas cost previous month | EUR including VAT, as reported per supply |
| Last successful update | Diagnostic timestamp, stale flags and last-known tariffs |

Entity names are available in English and Dutch. Existing tariff entity IDs are
preserved when updating. A dual-rate contract intentionally has no fabricated
combined electricity price.

Monthly summaries cover the **previous complete calendar month** in
**Europe/Amsterdam**, with inclusive `period_start` and `period_end_inclusive`
attributes. Timestamp conversion handles daylight-saving time. Import and export
are separate; net electricity usage is not mislabeled as imported energy.
The portal may report delayed or estimated readings, even for a smart meter.

These are monthly snapshots, **not cumulative meter counters**. Do not select
them as Energy dashboard energy sources or interpret them as live power.
Per-supply costs can exclude adjustments presented separately by the portal;
they are not a complete invoice or a household total. Tariffs are consumption
unit prices, not standing charges, grid fees, tax credits or feed-in remuneration.
Invoice PDFs, correspondence, account identifiers and addresses are not exposed.

## Requirements

- Home Assistant **2026.9.0 or newer**; CI covers 2026.9.0 and 2026.10.0.
- A Dutch Mijn Vattenfall account with the relevant contract/data available.
- Internet access to Vattenfall's HTTPS portal services.
- HACS for HACS installation, or access to HA's `custom_components` directory.
- Microsoft Edge for browser sign-in and the one-time callback/session handoff.

## Installation

### HACS custom repository

This is a **custom repository**, not a listing in HACS's default store.
The repository is public; no GitHub credentials should be entered into this
integration. Configuring this repository does not itself publish a release:
the implementation/workflows must first reach `dev` or `main` through review.

[![Open the repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=aavdberg&repository=ha-vattenfall&category=integration)

1. Open **HACS > menu > Custom repositories**.
2. Add `https://github.com/aavdberg/ha-vattenfall` with category **Integration**.
3. Find **Mijn Vattenfall (unofficial)** and download a published version.
4. Restart Home Assistant, then follow **Configuration** below.

For beta testing, enable **Show beta releases** in the HACS download/version
dialog and choose `vX.Y.Z-beta.N`. Beta releases are experimental and never
marked as the latest stable release.

### Manual installation

Download `vattenfall.zip` from a GitHub release. Create
`<config>/custom_components/vattenfall` and extract the archive **into that
directory**, so `manifest.json` sits directly inside it. Alternatively, copy
`custom_components/vattenfall` from the intended source branch.
Restart HA. Do not overwrite or remove HA's `.storage`.

An unmerged PR is not a published HACS release. If no release exists yet, use
the source branch containing the integration for manual testing.

## Configuration

### Connect with browser login and automatic renewal

Select **Connect with browser login and automatic renewal**, not the diagnostic
test. Existing entries can choose this under **Reconfigure** or reauthentication;
only the same contract account can replace an existing entry.

1. Follow the private Edge / F12 / Preserve log instructions in the HA dialog.
   Open its **new login link**, complete normal provider login and CAPTCHA/MFA,
   then copy the final response `Location: vfea://oauth?code=...&state=...`.
2. Paste the entire link only in HA's masked field within ten minutes.
   Do not reuse a callback from the earlier diagnostic test.
3. Explicitly select the intended **customer number / contract account**.
   HA checks tariff access before saving the connection. Cancel to discard it.

This flow uses fresh state, nonce and S256 PKCE. It validates RS256 signatures
against the fixed provider JWKS endpoint, the exact issuer/client audience,
expiry, nonce and signed customer IDs. Initialization is scoped to those IDs;
only account identifiers are retained from its response, not profile/address
fields. The portal's public application keys are discovered from named frontend
configuration fields at runtime, never embedded in the repository. No JavaScript
is executed. Configuration/schema changes fail explicitly instead of guessing.

The app's **verified ID token** is used for the fixed account/contract services,
following the app protocol; the OAuth access token is not silently substituted.
Tokens, scope, nonce and account binding are stored in the HA config entry.
Refresh is serialized before API use when expiry is within two minutes; rotated
tokens are saved before subsequent API requests. Refresh must preserve the
verified subject and selected customer. A rejected grant/identity requests
reauthentication; network/service failures preserve stored credentials and
last-known data. There is no background refresh while HA is stopped.

HA configuration and backups need filesystem/access protection: this integration
does **not** separately encrypt tokens. Removing the entry removes local
credentials but does not revoke the grant at Vattenfall. Do not enable HTTP wire
logging, post callback links, or export authenticated network captures.

Real-user code exchange and one immediate refresh were verified in the earlier
diagnostic. Persistent authentication, signed claims and contract access need a
fresh user authorization after installing this version; success is not assumed
from the earlier transport-only result. Long-term provider behavior remains
unsupported and may change.

### Test app browser login and refresh (experimental)

Select **Test app browser login and refresh (experimental)** in setup,
reconfiguration or reauthentication. This diagnostic uses the **public OAuth
client identifier** from the Dutch consumer app and its existing
`vfea://oauth` callback, not an API key/client secret or an invented HA callback.
This unsupported use is not provider-approved or guaranteed to work. No mobile
client flags are added and CAPTCHA/MFA are completed normally with Vattenfall.

1. Keep the HA dialog open. Open a private **Microsoft Edge** window, open
   **F12 > Network**, enable **Preserve log**, and open the login link from the
   HA dialog in that private window. Complete the normal login and challenges.
2. Do not approve opening a mobile app. The final `vfea://oauth` link normally
   cannot open on a desktop. Find the last `authorize` request in Network and
   copy its **response Location header**, starting with
   `vfea://oauth?code=...&state=...` (parameter order may differ).
3. Paste that entire sensitive link in the masked HA field immediately. Never
   post it in chat/issues or export a HAR. The HA attempt expires after ten
   minutes; the provider's code may expire much sooner.

HA verifies exact callback and state, exchanges the code using the original S256
PKCE verifier, and, if a refresh token is returned, tests one immediate refresh.
Both requests use the fixed provider HTTPS token endpoint without redirects or
automatic retries. Each form submission consumes the local attempt, even an
invalid callback. The result screen's Submit button starts a new attempt; it
does not repeat a token request.

Only stage (`callback`, `exchange` or `refresh`), HTTP status and fixed outcome
classification are shown/logged. **No tokens are saved, no identity or account
API is called and no config entry is created or changed**, even on success.
Identity-token signatures/claims are not validated or used for authentication
in this transport-only test. Tokens may remain valid at the provider after
being discarded locally; the test does not revoke them. A successful immediate
refresh does not prove long-term refresh or compatibility with contract APIs.
Follow the separate persistent-login option above to connect HA; remaining
real-account/long-term verification is tracked in
[issue #4](https://github.com/aavdberg/ha-vattenfall/issues/4).

### Test username/password login (experimental)

Choose **Add integration > Mijn Vattenfall > Test username/password login
(experimental)**, or select that option under **Reconfigure** / reauthentication.
Enter your credentials locally in HA, never in an issue or chat. The password
field is masked. Submitting starts a fresh, isolated cookie session through the
real portal OAuth/PKCE flow, then sends one form-encoded credential request to
Vattenfall. Only the fixed provider HTTPS origins are allowed for redirects;
the credential POST is never automatically redirected or retried.

This is a **diagnostic test, not working end-to-end sign-in**. No integration is
created, no existing session is changed, and username/password and temporary
cookies are not persisted. No CAPTCHA token is generated; CAPTCHA/MFA responses
stop the test. Even an accepted response does not import a session or enable
refresh. Native sign-in remains tracked in [issue #4](https://github.com/aavdberg/ha-vattenfall/issues/4).

The result screen and a warning in the HA log show only the stage (`portal`,
`authorize` or `credentials`), HTTP status and a fixed outcome classification.
Share those three values for debugging, not HTTP bodies, headers or screenshots
of credentials. **Do not enable HTTP wire/debug logging.** Submit on the result
screen only clears it and opens an empty form; it does not repeat the request.
Cancel to finish. If rate-limited or blocked, stop testing and wait.

### Log in using Edge (local companion)

Install the unpacked [Edge companion](edge-extension/README.md) once. In HA choose
**Add integration > Mijn Vattenfall > Log in using Edge**, or choose the same
method under **Reconfigure** / reauthentication for an existing entry.

Keep the HA setup dialog open. In **that HA tab**, click the extension and
**Connect and open Vattenfall**. Log in normally in the portal tab it opens,
complete MFA yourself and select the intended account. Once the badge says
**ON**, reopen the companion **in the portal tab** and choose **Transfer this
selected account**. It reloads Contract; once the badge says **OK**, return to
HA and submit the setup to validate/save the received session.
Use **the same account** when replacing an existing entry.

No headers or pairing codes need copying. Pairing is single-use and expires after
ten minutes; canceling the HA flow discards its temporary session. The companion
uses the chosen signed-in HA tab and its existing authenticated API, not an
anonymous callback or a remote relay. HA must use HTTPS, except that local
loopback HTTP is allowed (the test installation uses `http://localhost:8124`).
The extension is source-distributed, not listed in the Edge store.

### Manual session import (fallback)

1. In **Microsoft Edge**, log in to
   [Mijn Vattenfall > Contract](https://www.vattenfall.nl/service/mijn-vattenfall/vf/direct-regelen/contract),
   complete MFA yourself and select the intended account.
2. Open **F12 > Network**, reload the contract page and select the successful
   `GET https://api.vattenfall.nl/api/mvf/contracts/v1/contracts` request.
   Do not select `contractaccount` or copy the browser's Cookie header.
3. Privately copy these **Request Headers**: `Authorization`,
   `Ocp-Apim-Subscription-Key` and `Vf-Context`. The `Bearer ` prefix is accepted.
4. Open **Settings > Devices & services > Add integration** and search for
   **Mijn Vattenfall (unofficial)**. Choose **Import session headers manually**
   and enter the three values in its masked fields.
   Setup checks actual current tariffs before creating an entry.

Never paste credentials, HAR files or full cURL exports into an issue or chat.
Without the companion, the integration does not transfer a browser session.
`Vf-Context` selects the account; account identities are deliberately not fetched.
Use a separate integration entry for another account. Reconfigure/reauthenticate
only with headers for the **same account**: a change of identity cannot be detected.

## Updates, failures and reauthentication

Both tariffs and the overview refresh on setup and hourly, independently.
A failed overview does not remove working tariff values. Changes appear on the
next refresh, not necessarily exactly at midnight.

- **Expired session / HTTP 401 or 403 / redirect:** HA requests reauthentication.
  Use browser login again for a renewable entry; imported sessions can use the
  companion or replace all three headers manually.
  **Reconfigure** can replace them proactively. The optional password test does
  not repair an expired session; MFA codes never go through this integration.
- **Network, rate limit, server or schema failure:** the affected entities become
  unavailable instead of presenting stale numbers as current.
- **Missing monthly readings or unsuccessful installment advice:** no zero or
  recommendation is invented. A previously discovered missing metric becomes
  unavailable. The diagnostic sensor lists unavailable overview sections.
- **Previous snapshot:** the diagnostic timestamp sensor retains last-known
  tariffs and stale metadata in memory. This is not a persistent cache.
  Recorder can retain historical states under your HA retention settings.

Tariff start dates are inclusive and their end dates **exclusive**, unlike the
portal's inclusive monthly summary dates. Future tariffs do not become current
prematurely. Unknown units/blocks, overlapping/tiered tariffs, invalid numbers and
missing current rates fail explicitly.

Session lifetime and revocation are controlled by Vattenfall. There is no confirmed
supported public API for these personal data. Private endpoint schemas, headers,
login and access policies may change without warning. Do not rely on this
integration for critical billing or unattended financial decisions.

## Privacy and security

Tokens and session headers are credentials. They may authorize broader portal access than
this integration uses. HA stores them in config entries; this is **not guaranteed
encrypted**, and masked fields are not encryption. Protect administrator access,
`.storage`, backups and filesystem permissions. Revoke exposed sessions where
possible; logout alone is not guaranteed to invalidate a token.

Normal updates make only read-only requests to fixed Vattenfall HTTPS services: contract
tariffs, installment/advice, consumption availability and the selected supplies'
previous-month summaries. Redirects are not followed. Supply identifiers are used
transiently to request usage and are discarded before snapshots/entities.
There are no invoice/document downloads, write operations, telemetry or arbitrary
URL configuration.

Renewable entries additionally use the fixed token/JWKS endpoints. Initial
browser setup fetches anonymous portal configuration and scoped account
initialization, retaining only account identifiers from that response.

The explicitly selected experimental password test additionally visits the
provider's login pages and posts credentials once. It uses a separate temporary
cookie jar, cleared when the test ends. It does not save credentials, response
bodies, redirects or sessions, and does not change config entries. The HA log
contains only locally defined stage/outcome labels and an HTTP status.

Diagnostics export only allowlisted health metadata: timestamps, success flags,
metric names and unavailable-section names. They exclude credentials, identifiers,
tariffs and usage/cost values. The integration does not log headers or response
bodies. Avoid HTTP wire/debug logging, HAR exports and screenshots of private data.
HA entity states and Recorder do contain your own financial/usage data.
Tests and examples use invented data only. See [SECURITY.md](SECURITY.md).

## Troubleshooting and support

First restart HA after installing/updating, then inspect the integration entry and
**Last successful update** sensor. Missing advice is normal when the provider
cannot calculate it. A usage snapshot may lag behind the current date; it is not
a real-time meter feed.

Use [GitHub Discussions](https://github.com/aavdberg/ha-vattenfall/discussions)
for setup help and the [issue forms](https://github.com/aavdberg/ha-vattenfall/issues/new/choose)
for reproducible bugs/features. Include the integration/HA versions and sanitized
integration errors, **never** actual prices, consumption, addresses, identifiers,
tokens or raw portal responses.

## Development and contribution

Use Linux and Python **3.14.2+** with an isolated environment:

```shell
python -m venv .venv
. .venv/bin/activate
python -m pip install ".[test]" "homeassistant==2026.10.0"
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
node --test edge-extension/*.test.js
```

Tests use real HA core/platform objects and synthetic fixtures, including JSON
wire serialization, stale/recovery behavior, safe diagnostics and monthly period
semantics. No live portal login is needed. Keep Decimal precision internally and
expose only JSON-safe attributes.
The optional companion uses Node.js 24+ for dependency-free tests.

JWT/cryptography are supplied by Home Assistant itself and are not independently
pinned or installed by this integration. The Python project installs test
dependencies/metadata only; HACS/manual copying
installs the integration. Do not register `custom_components` as an editable pip
namespace: HA expects its search paths to be real directories.

Open a detailed English issue before implementing a feature/fix. Keep code, issues,
PRs and Conventional Commit messages in English. Preserve config-entry and entity
identity. Do not commit local browser output, HA storage or production captures.

## Branches and releases

Maintenance follows `ha-dtek`'s model, adapted to this integration:

| Branch | Purpose |
| --- | --- |
| `dev` | Feature/fix PR target; successful CI on pushes publishes beta pre-releases. |
| `main` | Stable releases; promote `dev` only after an explicit release request. |

Both branches require up-to-date Ruff, real-HA tests, Hassfest, HACS and secret-scan
checks, PRs and resolved conversations. Automatic Copilot review is configured;
read the actual review, address or justify every finding, and reply/resolve each
thread before merging. Do not manually request another review.

During bootstrap, GitHub's license metadata still reflects the initial default
branch, not the PR. CI verifies the checkout's **complete MIT text** against
GitHub's canonical template before temporarily substituting that check for HACS's
default-branch license lookup. The standard HACS license check resumes
automatically once GitHub detects the license on `main`; other checks are unchanged.

The reusable release workflow is called only after CI succeeds on a push to `dev`
or `main`. PRs and manual CI runs do not publish. Beta tags are `v<base>-beta.<N>`;
stable tags are `v<base>`. Both include `vattenfall.zip` with integration files at
its root and the exact release version in its manifest. Only tracked integration
files are packaged. A repeated beta commit reuses its tag; stable tags are never
moved or overwritten.

Keep the base SemVer identical in `manifest.json` and `pyproject.toml`.
Use patch bumps for fixes/docs/maintenance and minor bumps for real features;
bump both before the next release cycle. To release stable, explicitly approve
promotion, open a `dev` -> `main` PR, wait for CI and Copilot review, resolve
conversations, then merge. Repository setup alone does not authorize promotion.

## License and acknowledgments

[MIT](LICENSE). Built as a community custom integration using Home Assistant and
HACS conventions. Original project branding is included locally under `brand/`;
no official Vattenfall or Home Assistant logo is used.

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

> **Authentication:** the optional local Edge companion lets you log in normally
> in the portal and transfer the session without copying headers. Passwords and
> MFA stay with Vattenfall. Sessions are still short-lived: there is **no automatic
> token refresh** or supported OAuth grant. Manual session import remains available.

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
- Microsoft Edge to sign in and obtain the current session headers.

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
  Log in through the companion again, or replace all three headers manually.
  **Reconfigure** can replace them proactively. Passwords and MFA codes never go
  through this integration.
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

Session headers are credentials. They may authorize broader portal access than
this integration uses. HA stores them in config entries; this is **not guaranteed
encrypted**, and masked fields are not encryption. Protect administrator access,
`.storage`, backups and filesystem permissions. Revoke exposed sessions where
possible; logout alone is not guaranteed to invalidate a token.

Only read-only requests to fixed Vattenfall HTTPS services are made: contract
tariffs, installment/advice, consumption availability and the selected supplies'
previous-month summaries. Redirects are not followed. Supply identifiers are used
transiently to request usage and are discarded before snapshots/entities.
There are no invoice/document downloads, write operations, telemetry or arbitrary
URL configuration.

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

The Python project installs test dependencies/metadata only; HACS/manual copying
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

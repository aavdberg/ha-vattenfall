# Mijn Vattenfall for Home Assistant (unofficial)

An experimental, read-only custom integration for Dutch **consumption contract
tariffs**. This is not an official Vattenfall integration. It uses the undocumented
Mijn Vattenfall portal endpoint
`https://api.vattenfall.nl/api/mvf/contracts/v1/contracts`.
There is no confirmed supported public API for these personal contract prices.
Vattenfall can change the schema, headers, access policy or login at any time.

## What it provides

Depending on the selected contract account, sensors for electricity normal and
off-peak tariffs, or electricity single tariff, and gas tariff. Units are EUR/kWh
and EUR/m³, plus a diagnostic **Last successful update** sensor. Prices come from
`tariffData.includingVat.specs.*.prices[].total`:
the portal's VAT-inclusive consumption unit price including energy tax. Attributes
include the delivery-rate and energy-tax components, effective dates, last
successful retrieval and last-known rate.

Prices are selected using the **Europe/Amsterdam** calendar day. The start date is
inclusive and the end date is **exclusive**, as observed in adjacent portal price
periods. Future prices never become today's price prematurely. Overlapping periods,
unknown blocks/units, tiered prices, missing VAT-inclusive data and missing current
prices are explicit errors, not guessed defaults.

These are not all-in household costs: standing charges, grid fees, tax credits,
feed-in remuneration/fees, discounts, consumption, bills and usage history are not
exposed. A day/night contract intentionally has no fabricated combined electricity
price. Choosing the applicable block for an Energy dashboard is outside this
integration; do not directly substitute a day-only price for a dual-rate contract.

## Installation and setup

Requires Home Assistant **2026.9 or newer**. Copy `custom_components/vattenfall`
into your HA configuration's `custom_components` directory and restart HA.
No browser, Playwright, portal password or MFA code is required inside HA.
The repository is private; installation currently requires authorized repository
access and manual copying. It is not published in the default HACS store.

1. In **Microsoft Edge**, log in to
   [Mijn Vattenfall > Contract](https://www.vattenfall.nl/service/mijn-vattenfall/vf/direct-regelen/contract)
   and complete any MFA yourself. Select the intended contract account.
2. Press **F12**, select **Network**, then reload the contract page. Filter for
   `contracts` and select the successful GET request to
   `https://api.vattenfall.nl/api/mvf/contracts/v1/contracts` (not `contractaccount`).
3. Under **Request Headers**, privately copy the values of **Authorization**,
   **Ocp-Apim-Subscription-Key** and **Vf-Context**. The Authorization value may
   include the `Bearer ` prefix. Never paste a full HAR/cURL export into chat,
   issues or source files; it may contain additional secrets and personal data.
4. In HA, go to **Settings > Devices & services > Add integration** and search for
   **Mijn Vattenfall (unofficial)**. Paste the three values into its masked fields.
   Setup checks the real endpoint and current tariffs before creating an entry.

The browser session used during development is **not automatically installed or
transferred** into HA. A selected contract account is determined by the imported
`Vf-Context`; the integration does not enumerate accounts or fetch `contractaccount`.
Add a separate integration entry for another account. Reconfigure/reauthenticate
only with headers for the same account: identity is deliberately not retrieved,
so changing accounts in an existing entry cannot be detected.

## Refresh, stale values and recovery

The integration polls hourly and fetches immediately on setup. Session headers are
**short-lived credentials**, not a permanent API key. There is **no automatic
token refresh**, username/password login, CAPTCHA handling or MFA bypass.
Session lifetime is controlled by Vattenfall and may make frequent manual
reauthentication necessary. This version is therefore not a set-and-forget
integration and must not be used for critical billing or unattended financial
decisions.

HTTP 401/403 or a redirect triggers HA's reauthentication flow. Log in in Edge again
and replace **all three headers**. You can also use the integration entry's
**Reconfigure** action proactively. HTTP 429/server errors, timeouts, unexpected
HTML/JSON and unsupported tariff schemas surface as update/setup errors.

After an update error, entities become **unavailable** rather than continuing to
advertise a stale price as current. The previous successful sanitized snapshot
stays in memory. HA hides extra attributes on unavailable entities, so use the
always-readable diagnostic **Last successful update** sensor: its timestamp remains
the last successful retrieval, and its attributes retain `last_known_tariffs`
(prices, units and effective dates) with `stale: true`. That snapshot is not a disk
cache and does not survive a restart. HA's Recorder may retain prior states under
your retention policy. Expired and removed tariff blocks are unavailable, never zero; new active
blocks are discovered on a successful refresh. Effective-date changes become
visible on the next hourly refresh, not necessarily exactly at midnight.

## Privacy and security

- Session headers are stored in HA's **config-entry storage**, not in this repo.
  HA does **not** guarantee encrypted config-entry storage. Protect `.storage`,
  backups, filesystem access and HA administrator accounts. Masked form fields
  are not encryption.
- Treat all three headers as sensitive; they may authorize broader portal access
  than this integration uses. No real credentials, cookies, account-specific
  rates or personal data belong in code, fixtures, logs or issue reports.
- Only the fixed HTTPS contracts URL is requested. Redirects are not followed.
  No custom URL, cookie import, telemetry, browser automation or account lookup is
  implemented. Only sanitized tariff fields survive response parsing in memory;
  unrelated fields returned by the endpoint are discarded.
- The integration does not log headers or response bodies and provides no
  diagnostics export. Avoid HTTP wire/debug logging, HAR exports and screenshots
  of header values. Removing the integration removes its config entry; protect or
  expire any existing backups separately. Log out/revoke the portal session if
  credentials are exposed; provider-side revocation behavior is not guaranteed.
- Tests use invented fixtures only. Do not contribute production response dumps.

## Development

Run inside a Linux environment with a matching Home Assistant installation:

```text
python -m pip install pytest pytest-asyncio ruff
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
```

Tests exercise the observed contract schema with synthetic prices and dates,
request scoping, error handling, configuration/reauthentication, and stale sensor
behavior. No live portal login is needed for tests.

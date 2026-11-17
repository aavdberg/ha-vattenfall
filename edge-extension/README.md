# Local Edge session companion

This unpacked Manifest V3 extension is an optional part of the unofficial
integration, not an Edge store listing or a Vattenfall login client.

## Install and connect

1. Download/check out the source branch containing `edge-extension` to a trusted
   local directory. Keep it there while using the extension.
2. In Microsoft Edge open `edge://extensions`, enable **Developer mode**, select
   **Load unpacked** and select that `edge-extension` directory. Pin its icon.
3. Open **your own Home Assistant** in Edge, using HTTPS or HTTP on localhost
   (`http://localhost:8124` for the approved test installation).
4. Add Mijn Vattenfall, or choose **Reconfigure** / its reauthentication prompt.
   Select **Log in using Edge** and leave that dialog open.
5. In that same HA tab, open the companion popup and select **Connect and open
   Vattenfall**. It reads the visible, single-use setup marker automatically.
6. Log in normally in the portal tab it opens, complete MFA yourself and choose
   the intended account on Contract. **In that portal tab**, reopen the companion
   popup and choose **Transfer this selected account**. It reloads Contract.
7. Once the badge says **OK**, return to the original HA dialog and select
   **Submit**. HA validates the session against current tariffs before saving it.

Finish within ten minutes. An unsuccessful/expired attempt requires pairing
again. On reauthentication/reconfiguration use the **same portal account**;
the integration deliberately does not retrieve account identities.

**Disconnect** stops further capture; it cannot retract an already-sent request.
Cancel the HA dialog to discard a transferred but unconfirmed session. Closing
either paired tab disconnects. Remove/disable the extension when not needed.
After a source update, reload it on `edge://extensions` and reconnect.

## Security and limitations

- `activeTab`/`scripting` permit the user-chosen HA tab only, not blanket access
  to every HA installation. The page origin is rechecked before delivery.
  Never connect from an untrusted or impersonated HA page.
- `webRequest` needs Vattenfall portal/API host access, but code inspects only
  the exact GET contracts endpoint initiated by the portal in the tab it opens.
  Capture is not armed until you explicitly confirm the selected account;
  initial/default-account requests during login are ignored.
  It reads only three allowlisted headers, never cookies, bodies, identity data,
  passwords or MFA. It does not modify/intercept login requests.
- `storage.session` keeps pairing IDs/nonces, tab/origin metadata and generic
  status only, not portal credentials or HA tokens. `alarms` expires pairing
  after ten minutes, including across service-worker restarts.
- Captured headers pass directly in memory to HA's existing authenticated
  config-flow API through its signed-in frontend. No new anonymous callback,
  remote relay, telemetry, browser debug port or HA-token extraction is used.
  HA stages them in memory for confirmation, clearing them on timeout/cancel;
  after confirmation normal HA config-entry/backup security applies.
- The session is still provider-controlled and short-lived. There is no
  automatic token refresh, registered OAuth grant or CAPTCHA/MFA bypass.
- Source is intentionally inspectable/unminified. No network dependencies,
  package installation, background portal polling or persistent credential
  store is used. Do not enable HTTP/debug capture of credential requests.

## Development

Node.js 24+; no npm dependencies:

```shell
node --test edge-extension/*.test.js
```

HA-side handoff/expiry/confirmation tests run in the Python suite.

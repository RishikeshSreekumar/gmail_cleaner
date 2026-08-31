"""OAuth2 device-code login, for providers that no longer accept a password.

Microsoft retired IMAP basic authentication for Outlook.com and Microsoft 365,
so an app password cannot reach those mailboxes at all. The device-code flow is
the least invasive replacement: no local web server, no redirect URI, no client
secret. You get a short code, paste it into a browser once, and this tool keeps
a refresh token in the same keychain entry the other providers use.

Only stdlib is used, and the only hosts contacted are the provider's own login
endpoints plus its IMAP server.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .base import MailError

#: Thunderbird's public client id. It is a public (secret-less) client already
#: registered for IMAP access, which spares every user an Azure app
#: registration. Override with MAILCLEANER_MS_CLIENT_ID or the account's
#: `oauth_client_id` if your tenant requires its own.
MICROSOFT_CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"


@dataclass(frozen=True)
class OAuthFlow:
    name: str
    device_url: str
    token_url: str
    scopes: str
    client_id: str

    def with_client_id(self, client_id: str | None) -> "OAuthFlow":
        return self if not client_id else OAuthFlow(
            self.name, self.device_url, self.token_url, self.scopes, client_id
        )


MICROSOFT = OAuthFlow(
    name="microsoft",
    device_url="https://login.microsoftonline.com/common/oauth2/v2.0/devicecode",
    token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
    scopes=(
        "https://outlook.office.com/IMAP.AccessAsUser.All "
        "offline_access openid email"
    ),
    client_id=os.environ.get("MAILCLEANER_MS_CLIENT_ID") or MICROSOFT_CLIENT_ID,
)

FLOWS = {"microsoft": MICROSOFT}


def flow_for(name: str, client_id: str | None = None) -> OAuthFlow:
    try:
        return FLOWS[name].with_client_id(client_id)
    except KeyError:
        raise MailError(f"No OAuth flow named {name!r}")


def _post(url: str, fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            raise MailError(f"{url} returned HTTP {e.code}: {body[:200]!r}") from e
    except urllib.error.URLError as e:
        raise MailError(f"Could not reach {url}: {e.reason}") from e


def start_device_login(flow: OAuthFlow) -> dict:
    """Ask the provider for a user code. Returns the device-code response."""
    res = _post(flow.device_url,
                {"client_id": flow.client_id, "scope": flow.scopes})
    if "device_code" not in res:
        raise MailError(
            "Device login could not be started: "
            f"{res.get('error_description') or res}"
        )
    return res


def poll_for_token(flow: OAuthFlow, device: dict, on_wait=None) -> dict:
    """Block until the user finishes in the browser. Returns a token blob."""
    interval = int(device.get("interval") or 5)
    deadline = time.time() + int(device.get("expires_in") or 900)
    while time.time() < deadline:
        time.sleep(interval)
        res = _post(flow.token_url, {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": flow.client_id,
            "device_code": device["device_code"],
        })
        error = res.get("error")
        if not error:
            return _blob(flow, res)
        if error == "authorization_pending":
            if on_wait:
                on_wait()
            continue
        if error == "slow_down":
            interval += 5
            continue
        raise MailError(
            f"Sign-in failed: {res.get('error_description') or error}"
        )
    raise MailError("Sign-in timed out. Run the command again.")


def refresh_token(flow: OAuthFlow, blob: dict) -> dict:
    if not blob.get("refresh_token"):
        raise MailError("No refresh token stored. Re-run: mclean login")
    res = _post(flow.token_url, {
        "grant_type": "refresh_token",
        "client_id": flow.client_id,
        "refresh_token": blob["refresh_token"],
        "scope": flow.scopes,
    })
    if res.get("error"):
        raise MailError(
            "Could not refresh the sign-in: "
            f"{res.get('error_description') or res['error']}. "
            "Re-run: mclean login"
        )
    merged = _blob(flow, res)
    merged.setdefault("refresh_token", blob["refresh_token"])
    return merged


def _blob(flow: OAuthFlow, res: dict) -> dict:
    return {
        "flow": flow.name,
        "client_id": flow.client_id,
        "access_token": res.get("access_token", ""),
        "refresh_token": res.get("refresh_token", ""),
        # 60s of slack so a token cannot expire mid-connection.
        "expires_at": int(time.time()) + int(res.get("expires_in") or 3600) - 60,
    }


def is_expired(blob: dict) -> bool:
    return int(blob.get("expires_at") or 0) <= int(time.time())

"""Google OAuth for the Smart Device Management API, via Nest Partner Connections."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from ...providers import ProviderError

REDIRECT_URI = "https://www.google.com"
SCOPE = "https://www.googleapis.com/auth/sdm.service"
TOKEN_URL = "https://www.googleapis.com/oauth2/v4/token"

HINTS = {
    "invalid_grant": "The code was already used or has expired. Start over for a fresh one.",
    "invalid_client": "GOOGLE_CLIENT_SECRET doesn't match GOOGLE_CLIENT_ID.",
    "redirect_uri_mismatch": f"The OAuth client must list exactly {REDIRECT_URI}.",
}
REFRESH_HINTS = {
    **HINTS,
    "invalid_grant": (
        "The refresh token was revoked or has expired (an OAuth app left in Testing expires "
        "them after 7 days). Run `climatectl nest-auth` again."
    ),
}


class OAuthError(ProviderError):
    pass


def authorization_url(project_id: str, client_id: str) -> str:
    query = urlencode(
        {
            "redirect_uri": REDIRECT_URI,
            "access_type": "offline",
            "prompt": "consent",
            "client_id": client_id,
            "response_type": "code",
            "scope": SCOPE,
        }
    )
    return f"https://nestservices.google.com/partnerconnections/{project_id}/auth?{query}"


def extract_code(pasted: str) -> str:
    """The code from the URL Google redirects to, or the code itself if that's what was
    pasted."""
    pasted = pasted.strip()
    if "?" not in pasted:
        if not pasted:
            raise OAuthError("Nothing was pasted.")
        return pasted
    params = parse_qs(urlsplit(pasted).query)
    if "error" in params:
        raise OAuthError(f"Google returned an error instead of a code: {params['error'][0]}")
    if "code" not in params:
        raise OAuthError("That URL has no code= in it.")
    return params["code"][0]


def exchange_code(client_id: str, client_secret: str, code: str) -> dict[str, Any]:
    """Trade a one-time authorization code for an access token and a refresh token."""
    return _token_request(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        }
    )


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """A fresh access token (valid for about an hour) from the long-lived refresh token."""
    tokens = _token_request(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        hints=REFRESH_HINTS,
    )
    return tokens["access_token"]


def _token_request(data: dict[str, str], hints: dict[str, str] = HINTS) -> dict[str, Any]:
    # Error messages carry Google's error code and description only, never the request.
    try:
        resp = requests.post(TOKEN_URL, data=data, timeout=30)
    except requests.RequestException as e:
        raise OAuthError(f"Token request failed: {type(e).__name__}") from None
    if resp.ok:
        return resp.json()
    try:
        body = resp.json()
        code, detail = body.get("error"), body.get("error_description", "")
    except ValueError:
        code, detail = None, "unparseable error body"
    message = f"Token request -> {resp.status_code}: {code}: {detail}".rstrip(": ")
    if code in hints:
        message += f"\n{hints[code]}"
    raise OAuthError(message)

from urllib.parse import parse_qs, urlsplit

import pytest
from fake_google import CLIENT_ID, CLIENT_SECRET, CODE, PROJECT

from climatectl.providers.nest import oauth


def test_authorization_url():
    url = urlsplit(oauth.authorization_url(PROJECT, CLIENT_ID))
    assert url.netloc == "nestservices.google.com"
    assert url.path == f"/partnerconnections/{PROJECT}/auth"
    assert parse_qs(url.query) == {
        "redirect_uri": ["https://www.google.com"],
        "access_type": ["offline"],
        "prompt": ["consent"],
        "client_id": [CLIENT_ID],
        "response_type": ["code"],
        "scope": ["https://www.googleapis.com/auth/sdm.service"],
    }


@pytest.mark.parametrize(
    "pasted",
    [
        f"https://www.google.com/?code={CODE}&scope=https://www.googleapis.com/auth/sdm.service",
        f"https://www.google.com?code={CODE}",
        f"  {CODE}  ",
    ],
)
def test_extract_code(pasted):
    assert oauth.extract_code(pasted) == CODE


@pytest.mark.parametrize(
    ("pasted", "message"),
    [
        ("https://www.google.com/?error=access_denied", "access_denied"),
        ("https://www.google.com/?scope=x", "no code="),
        ("   ", "Nothing was pasted"),
    ],
)
def test_extract_code_errors(pasted, message):
    with pytest.raises(oauth.OAuthError, match=message):
        oauth.extract_code(pasted)


def test_exchange_code(google):
    tokens = oauth.exchange_code(CLIENT_ID, CLIENT_SECRET, CODE)
    assert tokens["refresh_token"] and tokens["access_token"]


def test_reused_code_explains_itself(google):
    oauth.exchange_code(CLIENT_ID, CLIENT_SECRET, CODE)
    with pytest.raises(oauth.OAuthError, match="invalid_grant") as excinfo:
        oauth.exchange_code(CLIENT_ID, CLIENT_SECRET, CODE)
    assert "already used or has expired" in str(excinfo.value)


def test_wrong_secret_is_not_echoed(google):
    with pytest.raises(oauth.OAuthError, match="invalid_client") as excinfo:
        oauth.exchange_code(CLIENT_ID, "GOCSPX-wrong", CODE)
    assert "GOCSPX" not in str(excinfo.value)

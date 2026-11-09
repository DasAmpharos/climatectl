"""`climatectl nest-auth`: one-time authorization that saves GOOGLE_REFRESH_TOKEN to .env.

Prints the Partner Connections URL, takes the URL Google redirects to (as an argument or
pasted at the prompt), exchanges its code for tokens and writes the refresh token into
.env. Tokens and the client secret are never printed. Ends with a devices.list call,
which completes the authorization and starts Pub/Sub events."""

from __future__ import annotations

import argparse
import sys

from ... import envfile
from . import oauth, sdm

REQUIRED = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "SDM_PROJECT_ID")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="climatectl nest-auth", description=__doc__.splitlines()[0]
    )
    parser.add_argument(
        "redirect_url", nargs="?", help="the https://www.google.com/?code=... URL (or the code)"
    )
    args = parser.parse_args(argv)

    path = envfile.env_path()
    if not path.exists():
        sys.exit(f"No .env at {path}. Copy .env.example and fill it in first.")
    env = envfile.load(path)
    missing = [k for k in REQUIRED if not env.get(k)]
    if missing:
        sys.exit(f"Missing in {path}: {', '.join(missing)}")

    pasted = args.redirect_url
    if pasted is None:
        print("1. Open this URL, signed in to the Google account your thermostats are on:\n")
        print(oauth.authorization_url(env["SDM_PROJECT_ID"], env["GOOGLE_CLIENT_ID"]))
        print("\n2. Turn on your home and every thermostat, and allow access.")
        print("3. Paste the full https://www.google.com/?code=... URL you land on.\n")
        pasted = input("Redirect URL: ")

    try:
        code = oauth.extract_code(pasted)
        tokens = oauth.exchange_code(env["GOOGLE_CLIENT_ID"], env["GOOGLE_CLIENT_SECRET"], code)
    except oauth.OAuthError as e:
        sys.exit(str(e))
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        sys.exit(
            "No refresh_token in the response. Remove the app at "
            "https://nestservices.google.com/partnerconnections and run this again."
        )
    envfile.set_value(path, "GOOGLE_REFRESH_TOKEN", refresh_token)
    print(f"\nSaved GOOGLE_REFRESH_TOKEN to {path} (not shown).")

    try:
        devices = sdm.list_devices(env["SDM_PROJECT_ID"], tokens["access_token"])
    except sdm.SdmError as e:
        sys.exit(f"The refresh token is saved, but listing devices failed: {e}")
    print(f"\ndevices.list returned {len(devices)} device(s):")
    for device in devices:
        print(f"  - {sdm.describe(device)}")
    if devices:
        print("\nAuthorization is complete, and Pub/Sub events will start flowing.")
    else:
        print("  None. Run this again and switch on your thermostats when asked.")


if __name__ == "__main__":
    main()

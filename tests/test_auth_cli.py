import pytest
from fake_google import ACCESS_TOKEN, CLIENT_SECRET, CODE, REFRESH_TOKEN

from climatectl import envfile
from climatectl.providers.nest import auth_cli

REDIRECT = f"https://www.google.com/?code={CODE}&scope=https://www.googleapis.com/auth/sdm.service"


def test_saves_refresh_token_and_lists_devices(google, env_file, capsys):
    auth_cli.main([REDIRECT])
    assert envfile.load(env_file)["GOOGLE_REFRESH_TOKEN"] == REFRESH_TOKEN
    out = capsys.readouterr().out
    assert "Downstairs [THERMOSTAT], 21.3°C / 70°F, HVAC HEATING, mode HEAT" in out
    assert "Upstairs [THERMOSTAT], 22.0°C / 72°F, HVAC OFF" in out
    assert "Authorization is complete" in out
    assert google.calls[-1].startswith("GET /v1/enterprises/")


def test_prompts_for_the_redirect_url(google, env_file, capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: REDIRECT)
    auth_cli.main([])
    out = capsys.readouterr().out
    assert "https://nestservices.google.com/partnerconnections/" in out
    assert envfile.load(env_file)["GOOGLE_REFRESH_TOKEN"] == REFRESH_TOKEN


def test_never_prints_secrets(google, env_file, capsys):
    auth_cli.main([REDIRECT])
    captured = capsys.readouterr()
    for secret in (CLIENT_SECRET, ACCESS_TOKEN, REFRESH_TOKEN):
        assert secret not in captured.out
        assert secret not in captured.err


def test_missing_settings(env_file, capsys):
    env_file.write_text("GOOGLE_CLIENT_ID=x\n")
    with pytest.raises(SystemExit, match="GOOGLE_CLIENT_SECRET, SDM_PROJECT_ID"):
        auth_cli.main([REDIRECT])


def test_expired_code_leaves_env_untouched(google, env_file):
    before = env_file.read_text()
    with pytest.raises(SystemExit, match="invalid_grant"):
        auth_cli.main(["https://www.google.com/?code=4/0Ab-stale"])
    assert env_file.read_text() == before

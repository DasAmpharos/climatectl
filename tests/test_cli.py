import pytest

from climatectl import cli


def test_lists_every_command(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    for command in cli.COMMANDS:
        assert f"  {command} " in out


def test_passes_the_rest_to_the_command(google, authorized, capsys):
    cli.main(["status", "--json"])
    assert '"device_id": "down"' in capsys.readouterr().out


def test_unknown_command(capsys):
    with pytest.raises(SystemExit):
        cli.main(["thermostat-dance"])
    assert "invalid choice" in capsys.readouterr().err

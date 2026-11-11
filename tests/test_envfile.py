from climatectl import envfile


def test_set_value_replaces_in_place(tmp_path):
    path = tmp_path / ".env"
    path.write_text('# comment\nA=1\nTOKEN=\nB="two"\n')
    envfile.set_value(path, "TOKEN", "abc")
    assert path.read_text() == '# comment\nA=1\nTOKEN=abc\nB="two"\n'
    assert envfile.load(path) == {"A": "1", "TOKEN": "abc", "B": "two"}


def test_set_value_appends_and_locks_down(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=1\n")
    path.chmod(0o644)
    envfile.set_value(path, "TOKEN", "abc")
    assert path.read_text() == "A=1\nTOKEN=abc\n"
    assert path.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / ".env.tmp").exists()


def test_env_path_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("CLIMATECTL_ENV_FILE", str(tmp_path / "custom.env"))
    assert envfile.env_path() == tmp_path / "custom.env"


def test_missing_names_the_file_or_the_environment(env_file, monkeypatch, tmp_path):
    assert str(envfile.missing(["A", "B"])) == f"Missing in {env_file}: A, B"
    monkeypatch.setenv("CLIMATECTL_ENV_FILE", str(tmp_path / "none" / ".env"))
    assert str(envfile.missing(["A"])) == "Missing (set them in .env or the environment): A"

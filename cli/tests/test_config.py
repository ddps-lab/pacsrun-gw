"""Where the token is stored, and who can read it.

The permission checks are the point. A token readable by every user on a shared
machine is the same as no token.
"""

import json
import os
import stat

import pytest

from ddpsrun import config


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Point XDG_CONFIG_HOME at a temp directory and clear the env overrides.

    Without this every test in this file would read and write the developer's
    own ~/.config/ddpsrun/config.json.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv(config.SERVER_ENV, raising=False)
    monkeypatch.delenv(config.TOKEN_ENV, raising=False)
    return tmp_path


def test_what_was_saved_is_what_is_loaded():
    config.save(config.Credentials(server="https://run.example", token="s3cret"))
    loaded = config.load()
    assert loaded.server == "https://run.example"
    assert loaded.token == "s3cret"


def test_the_file_is_readable_by_nobody_else():
    path = config.save(config.Credentials(server="https://run.example", token="s3cret"))
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"token file is {oct(mode)}, must be 0o600"
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700


def test_a_trailing_slash_on_the_server_is_dropped_once():
    # Otherwise every URL the client builds has a double slash in it.
    config.save(config.Credentials(server="https://run.example/", token="s3cret"))
    assert config.load().server == "https://run.example"


def test_the_environment_beats_the_file(monkeypatch):
    config.save(config.Credentials(server="https://saved.example", token="saved"))
    monkeypatch.setenv(config.SERVER_ENV, "https://ci.example")
    monkeypatch.setenv(config.TOKEN_ENV, "ci-token")
    loaded = config.load()
    assert loaded.server == "https://ci.example"
    assert loaded.token == "ci-token"


def test_no_file_and_no_environment_names_the_command_that_fixes_it():
    with pytest.raises(config.NotLoggedIn, match="ddpsrun login"):
        config.load()


def test_a_half_written_file_is_refused_rather_than_half_used():
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"server": "https://run.example"}))
    with pytest.raises(config.NotLoggedIn, match="missing a server or a token"):
        config.load()


def test_a_corrupt_file_says_so_instead_of_raising_a_traceback():
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not json")
    with pytest.raises(config.NotLoggedIn, match="unreadable"):
        config.load()


def test_logout_removes_the_file_and_says_whether_there_was_one():
    assert config.forget() is False
    config.save(config.Credentials(server="https://run.example", token="s3cret"))
    assert config.forget() is True
    assert not config.config_path().exists()


def test_saving_twice_does_not_leave_the_old_token_behind():
    config.save(config.Credentials(server="https://run.example", token="first-token-is-longer"))
    config.save(config.Credentials(server="https://run.example", token="second"))
    assert "first-token-is-longer" not in config.config_path().read_text()

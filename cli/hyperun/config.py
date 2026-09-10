"""Where the CLI keeps the server address and the user's token.

END-TO-END FLOW of this file:

  1. `hyperun login` asks for a server URL and a token and calls `save()`.
  2. `save()` writes them to `~/.config/hyperun/config.json` with mode 0600 and
     creates the directory with mode 0700.
  3. Every other command calls `load()`, which returns the same two values, or
     raises `NotLoggedIn` with the exact command to run.

WHY A FILE AND NOT AN ENVIRONMENT VARIABLE. A token in an environment variable
is in the shell's history if it was ever exported on a command line, is
inherited by every process the user starts, and is gone on a new terminal. A
file survives, and 0600 is a boundary the operating system enforces.
`HYPERUN_TOKEN` is still honoured for scripts and CI, where a file is the wrong
shape — `load()` prefers it when it is set.

WHY XDG AND NOT ~/.hyperun. `XDG_CONFIG_HOME` is what a Linux user's backup and
dotfile tooling already knows about, and it falls back to `~/.config`, which is
where macOS users' tools look too.

★ THE COMMAND WAS `ddpsrun` UNTIL 2026-09-10, AND NOBODY IS LOGGED OUT BY THE
RENAME. Two pieces of state carried the old name and both are user-visible, so
both accept it still:

  the directory   `save()` writes `~/.config/hyperun/`, and `config_path()`
                  falls back to `~/.config/ddpsrun/config.json` when the new
                  one does not exist. A rename that silently logged everybody
                  out would be the same class of defect as the ones this file's
                  comments already record — the fix is one `exists()` check.
  the env vars    `HYPERUN_SERVER` / `HYPERUN_TOKEN` are read first, then
                  `DDPSRUN_SERVER` / `DDPSRUN_TOKEN`. CI files and shell
                  profiles already export the old pair.

Neither fallback is dated for removal here. Deleting them is a decision about
how long the old spelling has to keep working, which belongs to whoever knows
who still has it exported.

Grep anchor: DDPSRUN-CLI-CONFIG
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

CONFIG_FILENAME = "config.json"
CONFIG_DIRNAME = "hyperun"
# The pre-2026-09-10 spelling of everything below. Read, never written.
LEGACY_CONFIG_DIRNAME = "ddpsrun"
SERVER_ENV = "HYPERUN_SERVER"
TOKEN_ENV = "HYPERUN_TOKEN"
LEGACY_SERVER_ENV = "DDPSRUN_SERVER"
LEGACY_TOKEN_ENV = "DDPSRUN_TOKEN"


class NotLoggedIn(Exception):
    """No credentials anywhere. `cli.py` prints this and exits 2."""


@dataclass(frozen=True)
class Credentials:
    """Everything needed to reach the server.

    Attributes:
        server: the gateway URL.
        token: what goes in `Authorization: Bearer`. Either a static token an
            operator issued, or a Cognito id_token from `hyperun login`.
        refresh_token: only set after a browser sign-in. An id_token lives an
            hour; this buys a new one without opening a browser again, and is
            why the file is written at mode 0600.
    """

    server: str
    token: str
    refresh_token: str = ""


def _xdg_base() -> Path:
    """The directory the config directory sits in.

    Returns:
        `$XDG_CONFIG_HOME`, or `~/.config` when that is unset.
    """
    return Path(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config"))


def config_dir() -> Path:
    """Where a NEW config file is written.

    Returns:
        `$XDG_CONFIG_HOME/hyperun`, or `~/.config/hyperun` when that is unset.
        Always the new name: `save()` migrates by writing here, and nothing
        writes the old directory again.
    """
    return _xdg_base() / CONFIG_DIRNAME


def legacy_config_path() -> Path:
    """The file the command wrote while it was called `ddpsrun`.

    Returns:
        `<xdg base>/ddpsrun/config.json`. It may not exist, which is the normal
        case for anybody who first logged in after the rename.
    """
    return _xdg_base() / LEGACY_CONFIG_DIRNAME / CONFIG_FILENAME


def config_path() -> Path:
    """The config file to READ, new location first.

    Returns:
        `<xdg base>/hyperun/config.json` when it exists; otherwise the old
        `ddpsrun` path when THAT exists; otherwise the new path, so a
        "not logged in" message names the place a login will write.
    """
    current = config_dir() / CONFIG_FILENAME
    if current.exists():
        return current
    legacy = legacy_config_path()
    return legacy if legacy.exists() else current


def save(credentials: Credentials) -> Path:
    """Write the credentials, readable by this user only.

    Args:
        credentials: what `hyperun login` collected.

    Returns:
        The path written, so the caller can tell the user where it went.

    The chmod happens AFTER the write and the file is opened with 0600 from the
    start: creating it world-readable and narrowing it afterwards would leave a
    window in which another user on a shared machine could read the token.
    """
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, stat.S_IRWXU)

    path = config_path()
    # O_CREAT | O_WRONLY | O_TRUNC with mode 0600, rather than open(path, "w"),
    # which would create it with the process umask applied to 0666.
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        document = {"server": credentials.server, "token": credentials.token}
        # Only written when there is one. A file from a static-token login stays
        # exactly the shape it was before Cognito existed.
        if credentials.refresh_token:
            document["refresh_token"] = credentials.refresh_token
        json.dump(document, handle, indent=2)
        handle.write("\n")
    return path


def load() -> Credentials:
    """Find the credentials to use.

    Order: environment first, then the file. The environment wins so that a
    script or a CI job can override a developer's own login without touching
    their file.

    Returns:
        The credentials.

    Raises:
        NotLoggedIn: neither source had both values. The message names the
            command that fixes it, because "not logged in" on its own has sent
            more than one person to the documentation.
    """
    # New spelling first, old one second. Both are read so a shell profile or CI
    # file that exports the pre-rename names keeps working.
    server = (os.environ.get(SERVER_ENV, "")
              or os.environ.get(LEGACY_SERVER_ENV, "")).strip()
    token = (os.environ.get(TOKEN_ENV, "")
             or os.environ.get(LEGACY_TOKEN_ENV, "")).strip()
    if server and token:
        return Credentials(server=server.rstrip("/"), token=token)

    path = config_path()
    if not path.exists():
        raise NotLoggedIn(
            f"not logged in. Run:\n"
            f"    hyperun login --server <url>\n"
            f"or set {SERVER_ENV} and {TOKEN_ENV}."
        )
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise NotLoggedIn(f"{path} is unreadable ({exc}). Run `hyperun login` again.") from exc

    file_server = str(document.get("server", "")).strip()
    file_token = str(document.get("token", "")).strip()
    if not file_server or not file_token:
        raise NotLoggedIn(f"{path} is missing a server or a token. Run `hyperun login` again.")

    return Credentials(
        server=(server or file_server).rstrip("/"),
        token=token or file_token,
        refresh_token=str(document.get("refresh_token", "")).strip(),
    )


def forget() -> bool:
    """Delete the stored credentials.

    Returns:
        True if a file was removed, False if there was nothing to remove.
    """
    path = config_path()
    if not path.exists():
        return False
    path.unlink()
    return True

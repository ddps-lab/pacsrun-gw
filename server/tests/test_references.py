"""The generated agent references, and what makes them worth generating.

These live in the server's suite because the generator imports the server. What
they defend: `agent/references/api.md` and `cli.md` are read by a coding agent
as the syntax of this tool, and a hand-maintained copy of a route table is
always the half that goes stale.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = REPO_ROOT / "agent" / "scripts" / "generate_references.py"
REFERENCES = REPO_ROOT / "agent" / "references"


def test_the_generated_references_match_the_code():
    # The same check CI runs. Running it here too means a developer who changes
    # a route sees it before they push.
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"{result.stdout}{result.stderr}\n"
        f"run `python3 agent/scripts/generate_references.py` and commit the result"
    )


def test_every_route_appears_in_the_api_reference():
    from ddpsrun_server.main import app

    text = (REFERENCES / "api.md").read_text(encoding="utf-8")
    for route in app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/v1/"):
            assert f"`{path}`" in text, path


def test_every_command_appears_in_the_cli_reference():
    text = (REFERENCES / "cli.md").read_text(encoding="utf-8")
    for command in ("login", "logout", "explain", "schema", "estimate",
                    "validate", "submit", "status", "logs"):
        assert f"## ddpsrun {command}" in text, command


def test_the_generated_files_say_not_to_edit_them():
    for name in ("api.md", "cli.md"):
        assert "Do not edit" in (REFERENCES / name).read_text(encoding="utf-8")


def test_the_hand_written_references_are_not_generated():
    # The split is the point: syntax from the code, pitfalls from people. A
    # banner on these would mean somebody wired the generator to overwrite them.
    for name in ("script-contract.md", "troubleshooting.md"):
        assert "GENERATED" not in (REFERENCES / name).read_text(encoding="utf-8")


def test_the_skill_frontmatter_has_the_two_fields_that_make_it_load():
    text = (REPO_ROOT / "agent" / "skills" / "ddpsrun" / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    frontmatter = text.split("---")[1]
    assert "name: ddpsrun" in frontmatter
    # The description is what decides WHEN the skill loads, so an empty or
    # generic one makes the whole plugin inert.
    description = [line for line in frontmatter.splitlines() if line.startswith("description:")]
    assert description and len(description[0]) > 80


def test_the_plugin_and_the_marketplace_agree_on_the_name():
    import json

    marketplace = json.loads(
        (REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    plugin = json.loads(
        (REPO_ROOT / "agent" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    entry = marketplace["plugins"][0]
    assert entry["name"] == plugin["name"]
    assert entry["version"] == plugin["version"]
    # `source` is the path Claude Code loads the plugin from. A wrong one fails
    # silently: the marketplace lists a plugin that never appears.
    assert (REPO_ROOT / entry["source"].lstrip("./")).is_dir()


def test_the_generated_output_does_not_depend_on_terminal_width():
    # The first version captured `--help`, which argparse wraps to
    # shutil.get_terminal_size(). CI rejected it because a developer's terminal
    # and the runner's produced different files, which made the check
    # meaningless. Reading the parser's actions depends on neither the width nor
    # the Python version (3.13 changed how argparse prints `-f, --file FILE`).
    import os

    outputs = []
    for width in ("40", "200"):
        environment = dict(os.environ, COLUMNS=width)
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, 'agent/scripts');"
             "import generate_references as g; print(g.render_cli(), end='')"],
            cwd=REPO_ROOT, capture_output=True, text=True, env=environment,
        )
        assert result.returncode == 0, result.stderr
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]


def test_the_cli_reference_lists_each_command_with_its_flags():
    text = (REFERENCES / "cli.md").read_text(encoding="utf-8")
    # A table, not a wall of help text: the flag and what it does, per command.
    assert "| `--server SERVER` | yes | the gateway URL" in text
    assert "| `--gpu-vram GB` |  | minimum GPU memory" in text

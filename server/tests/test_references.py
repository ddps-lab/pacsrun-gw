"""The generated agent references, and what makes them worth generating.

These live in the server's suite because the generator imports the server. What
they defend: `agent/references/api.md` and `cli.md` are read by a coding agent
as the syntax of this tool, and a hand-maintained copy of a route table is
always the half that goes stale.
"""

import re
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
        assert f"## hyperun {command}" in text, command


def test_the_generated_files_say_not_to_edit_them():
    for name in ("api.md", "cli.md"):
        assert "Do not edit" in (REFERENCES / name).read_text(encoding="utf-8")


def test_the_hand_written_references_are_not_generated():
    # The split is the point: syntax from the code, pitfalls from people. A
    # banner on these would mean somebody wired the generator to overwrite them.
    for name in ("script-contract.md", "troubleshooting.md"):
        assert "GENERATED" not in (REFERENCES / name).read_text(encoding="utf-8")


def test_the_skill_frontmatter_has_the_two_fields_that_make_it_load():
    text = (REPO_ROOT / "agent" / "skills" / "hyperun" / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    frontmatter = text.split("---")[1]
    assert "name: hyperun" in frontmatter
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


def test_the_skill_points_at_reference_paths_that_exist():
    """SKILL.md 가 주는 경로가 그 파일 위치에서 실제로 풀리는지.

    2026-09-08 까지 SKILL.md 는 `references/script-contract.md` 라고 8번 적었고
    그 파일은 `agent/references/` 에 있었다. SKILL.md 는
    `agent/skills/hyperun/SKILL.md` 이므로 그 경로는 skill 디렉터리 기준으로
    풀리지 않는다 — 규칙 목록은 있는데 문서가 가리키는 주소로는 못 여는 상태였다.
    """
    skill = REFERENCES.parent / "skills" / "hyperun" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    cited = set(re.findall(r"`([./]*references/[a-z-]+\.md)`", text))
    assert cited, "SKILL.md 가 reference 파일을 하나도 안 가리킨다"
    for path in sorted(cited):
        assert (skill.parent / path).resolve().is_file(), (
            f"SKILL.md 의 {path} 가 {skill.parent} 기준으로 풀리지 않는다"
        )


def test_the_skill_lists_every_rule_the_contract_has():
    """SKILL.md 의 규칙 표가 script-contract.md 의 절 개수와 맞는지.

    표는 agent 가 파일을 열기 전에 무엇이 있는지 보라고 있는 것이므로, 규칙이
    하나 늘고 표가 그대로면 그 규칙은 아무도 모른다.
    """
    contract = (REFERENCES / "script-contract.md").read_text(encoding="utf-8")
    numbers = [int(m) for m in re.findall(r"^## (\d+)\.", contract, re.M)]
    assert numbers == list(range(1, len(numbers) + 1)), f"절 번호가 연속이 아니다: {numbers}"

    skill = (REFERENCES.parent / "skills" / "hyperun" / "SKILL.md").read_text(encoding="utf-8")
    listed = [int(m) for m in re.findall(r"^\| (\d+) \|", skill, re.M)]
    assert listed == numbers, (
        f"script-contract.md 는 규칙 {len(numbers)}개인데 SKILL.md 표는 {listed} 를 적었다. "
        f"규칙을 더하거나 뺐으면 그 표도 고친다."
    )

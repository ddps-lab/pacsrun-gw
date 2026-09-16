"""server/test_monitor_dm.py

HYPERUN-MONITOR 의 Slack DM 로컬 테스트. **진짜 DM 을 보낸다.**

실행:
    python3 server/test_monitor_dm.py --to jglee
    python3 server/test_monitor_dm.py --to jglee --explain      # AI 문장까지
    python3 server/test_monitor_dm.py --to jglee --dry-run      # 보내지 않고 화면에만

무엇을 확인하는가:
    `monitor.py` 가 만드는 메시지가 **사람에게 실제로 도착하는지**. 규칙이
    맞는지는 `tests/test_monitor.py` 가 24개로 이미 지키고 있고, 이 script 가 지키는
    것은 그 다음 한 칸 — owner label 에서 Slack user id 를 찾아 DM 이 열리고 글이
    도착하는지다. 그건 단위 test 로는 절대 확인할 수 없다.

    cloud-usage 의 `monitor_v2/test_main3.py` 와 같은 모양이다: 진짜 자격증명으로
    진짜 채널에 한 번 보내보는 script 이고, CI 는 안 돈다.

자격증명은 어디서 오는가:
    `cloud-usage/.env` 의 `SLACK_BOT_TOKEN`, 그리고 owner→Slack id 는 같은 저장소의
    `monitor_v2/iam_to_slack.json`. 이 저장소는 PUBLIC 이므로 둘 다 **읽기만 하고
    절대 여기에 적지 않는다.** 경로는 --env-file / --user-map 으로 바꿀 수 있다.

    ★ 토큰은 화면에도 안 찍는다. 붙었는지 여부만 말한다.

보내는 내용:
    2026-09-15 에 실제로 거꾸로 간 학습의 숫자다 — `bank/adapters/AD/iter_1` 의
    score 가 처음 5 step 평균 2.850 에서 마지막 5 step 평균 2.140 으로 갔다. 가짜
    문구가 아니라 진짜 사건이라, 받는 사람이 "이게 오면 무슨 뜻인가" 를 바로 안다.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ddpsrun_server import metrics, monitor          # noqa: E402

# 그 run 의 진짜 score 20개. `tests/test_monitor.py` 와 같은 값이다.
REAL_SCORES = [2.4413, 2.7344, 3.6300, 3.0712, 2.3713, 2.4700, 2.2800, 3.1000,
               2.4000, 3.3800, 2.9400, 3.4100, 3.0800, 2.8100, 3.4400, 1.5100,
               3.0000, 1.5400, 2.2800, 2.3700]

DEFAULT_ENV = pathlib.Path.home() / "ddps-projects" / "cloud-usage" / ".env"
DEFAULT_MAP = (pathlib.Path.home() / "ddps-projects" / "cloud-usage"
               / "monitor_v2" / "iam_to_slack.json")


def load_env_file(path: pathlib.Path) -> dict:
    """`KEY = "value"` 꼴을 읽는다. cloud-usage 의 `print_test/utils/environment.py` 와 같은 규칙."""
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def build_message(owner: str, explanation: str) -> tuple[str, list, list]:
    """monitor.py 가 진짜로 만드는 메시지. 여기서 다시 만들지 않는다.

    Returns:
        (평문, 블록, 점검 결과). 평문은 Slack 의 알림 미리보기용이고 블록이 본문이다.
    """
    import time as _time

    lines = []
    start = _time.time() - 600
    for i, score in enumerate(REAL_SCORES, start=1):
        row = {"_series": "bank/adapters/AD/iter_1", "step": i, "score": score}
        stamp = _time.strftime("%Y-%m-%dT%H:%M:%S", _time.gmtime(start + i))
        lines.append(stamp + ".000000000Z PACSRUN_METRIC=" +
                     json.dumps(row, separators=(",", ":")))

    reading = metrics.scan(lines, monitor.WINDOW_SECONDS)
    findings = monitor.findings_for(
        reading, lines, _time.time(), monitor._last_line_time(lines))
    text = monitor.message_for(
        "job-a24568ecfc16", "c3-job1-fix", findings, explanation,
        cost_per_hour=6.36, hours=9.74)
    blocks = monitor.blocks_for(
        "job-a24568ecfc16", "c3-job1-fix", findings, explanation,
        cost_per_hour=6.36, hours=9.74)
    return text, blocks, findings


def main() -> int:
    parser = argparse.ArgumentParser(description="hyperun 감시 DM 을 실제로 보내본다")
    parser.add_argument("--to", default="jglee",
                        help="owner label. iam_to_slack.json 의 key 와 같은 이름")
    parser.add_argument("--explain", action="store_true",
                        help="Upstage 에게 한국어 설명을 받아서 함께 보낸다 (약 $0.0002)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Slack 에 보내지 않고 만들어진 글만 보여준다")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV))
    parser.add_argument("--user-map", default=str(DEFAULT_MAP))
    args = parser.parse_args()

    env = load_env_file(pathlib.Path(args.env_file))
    token = (env.get("SLACK_BOT_TOKEN") or os.environ.get("HYPERUN_SLACK_BOT_TOKEN", "")).strip()

    # ★ 토큰 자체는 절대 안 찍는다. 붙었는지와 길이만.
    print(f"[dm] env 파일      {args.env_file}")
    print(f"[dm] SLACK_BOT_TOKEN  {'있음' if token else '없음'} ({len(token)}자)")

    try:
        user_map = json.loads(pathlib.Path(args.user_map).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[dm] user map 을 못 읽었다: {exc}")
        return 2
    os.environ["HYPERUN_SLACK_USER_MAP"] = json.dumps(user_map)

    slack_id = monitor.slack_id_for(args.to)
    print(f"[dm] owner {args.to!r} -> Slack id {'찾음' if slack_id else '없음'}"
          f" ({len(user_map)}명 중)")
    if not slack_id:
        print(f"[dm] {args.to!r} 가 {args.user_map} 에 없다. key 목록: "
              f"{', '.join(sorted(user_map))}")
        return 2

    explanation = ""
    if args.explain:
        upstage = os.environ.get("HYPERUN_UPSTAGE_API_KEY", "").strip()
        if not upstage:
            print("[dm] --explain 인데 HYPERUN_UPSTAGE_API_KEY 가 없다. 설명 없이 보낸다")
        else:
            _, _, findings = build_message(args.to, "")
            explanation = monitor.explain(findings, [], upstage)
            print(f"[dm] 모델 답 {len(explanation)}자")

    text, blocks, findings = build_message(args.to, explanation)
    print(f"[dm] 규칙이 잡은 것 {len(findings)}건: "
          f"{', '.join(f['rule'] for f in findings)}")
    print("-" * 70)
    for b in blocks:
        kind = b.get("type")
        if kind == "header":
            print("=" * 70); print(" " + b["text"]["text"]); print("=" * 70)
        elif kind == "divider":
            print("-" * 70)
        elif kind == "context":
            print("  " + " ".join(e.get("text", "") for e in b.get("elements", [])))
        elif "fields" in b:
            for f in b["fields"]:
                print("   " + f["text"].replace("\n", "  "))
        else:
            print(" " + b["text"]["text"])
    print("-" * 70)

    if args.dry_run:
        print("[dm] --dry-run 이라 보내지 않았다")
        return 0
    if not token:
        print("[dm] 토큰이 없어서 보낼 수 없다")
        return 2

    ok = monitor.notify(slack_id, text, token, blocks=blocks)
    print(f"[dm] 발송 {'성공' if ok else '실패'}")
    if ok:
        print(f"[dm] Slack 에서 bot 과의 DM 을 확인하십시오. 받는 사람: {args.to}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

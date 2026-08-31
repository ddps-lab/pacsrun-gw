# pacsrun-gw

**kubectl 도 AWS IAM 도 없는 사용자가 GPU job 을 제출하고 결과를 받는 경로.**

PACSrun 앞단이다. 사용자가 보는 이름은 `ddpsrun` 이다.

| | 상태 |
|---|---|
| 설계 | `docs/00-overview.md` 부터 아홉 편 |
| 서버 1단계 | 있다. `server/`, `docs/09-server.md` |
| CLI 2단계 | 있다. `cli/`, `docs/10-cli.md` |
| UI | 없다 (6단계) |

```bash
# 서버
cd server && python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q

# CLI
cd cli && python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/ddpsrun --help
```

이 저장소의 문서와 코드에는 계정 식별자를 쓰지 않는다. `<ACCOUNT_ID>`, `<RESULT_BUCKET>`
같은 자리표시자를 쓰고 CI 가 매 push 마다 검사한다.

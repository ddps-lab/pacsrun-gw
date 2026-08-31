# ddpsrun — AI coding agent 안내

이 저장소는 **PACSrun 앞단**이다. kubectl 도 AWS IAM 도 없는 사용자가 GPU job 을 제출하고
결과를 받는 경로를 제공한다.

## 먼저 이것부터

**문서를 읽기 전에 도구에 물어본다.** 답이 항상 최신이다.

```bash
ddpsrun explain          # 이 도구가 무엇이고 어떻게 쓰는지
ddpsrun schema           # 요청 본문의 형식
ddpsrun estimate ...     # 시간과 비용, 권장 GPU. 아무것도 제출하지 않는다
ddpsrun validate ... --script run.sh   # 이 job 의 문제를 지적. 제출하지 않는다
ddpsrun submit -f job.yaml
ddpsrun status <job_id>
ddpsrun logs <job_id> --follow
```

**GPU 크기, 구매 방식, 예상 시간을 스스로 판단하지 말고 `ddpsrun estimate` 를 불러라.**
판단 로직은 서버에 한 벌만 두는 것이 이 설계의 전제이고, agent 가 임시로 채운 값은 그 한 벌과
어긋난다.

**`estimate` 가 `unknown` 을 답하면 그것을 그대로 사용자에게 전하라.** 그것은 실패가 아니라
답이다. 재본 적 없는 조합에 숫자를 답했다가 96% 틀린 적이 있어서 그렇게 만든 것이고,
agent 가 그 자리를 자기 추측으로 메우면 그 방어가 사라진다.

**제출 전에 `ddpsrun validate` 를 부르고, exit 1 이면 멈춰라.** 답의 `not_checked` 는
어떤 검사도 못 본 것들이다. 통과가 곧 완전함은 아니다.

**아직 없는 명령이 하나다.** `gpus`(빌릴 수 있는 GPU 와 단가). 서버에 vendor API key 와
catalog 캐시가 있어야 한다.

## 사용자 저장소를 읽어 학습 script 를 만들 때

`agent/references/script-contract.md` 를 읽는다. 규칙 아홉 개이고 **전부 실제로 깨져 본
것들**이라, 각 규칙에 그것이 어긋났을 때 무슨 일이 일어났는지 붙어 있다. 요점은 이렇다.

1. 문서에 적힌 경로와 저장소 실제 구조를 대조한다
2. 학습의 출력 경로와 추론의 입력 경로를 한 변수로 묶는다
3. 요구된 산출물 중 명령이 만들지 않는 것을 script 가 만들게 한다
4. clone 직후 파일 줄 수, 학습 시작 직후 표본 수를 찍는다
5. `trap ... EXIT` 로 어느 단계에서 죽어도 그때까지를 올린다
6. 학습이 끝나면 추론을 기다리지 말고 어댑터를 먼저 올린다
7. 긴 학습에는 checkpoint 감시를 붙이고, 끝낼 때 그 프로세스를 죽인다
8. GPU 상태를 `PACSRUN_GPU=` 한 줄로 30 초마다 찍는다
9. 판단은 서버에 묻는다

## 실패를 만나면

`agent/references/troubleshooting.md` 에 우리가 실제로 겪은 것과 **그것을 알아보는 로그 줄**이
있다. 증상 문자열로 찾으면 된다.

## reference 넷의 경계

| 파일 | 무엇 | 누가 유지하나 |
|---|---|---|
| `agent/references/api.md` | 라우트와 요청 필드 | **자동 생성.** 고치지 말 것 |
| `agent/references/cli.md` | 명령과 flag | **자동 생성.** 고치지 말 것 |
| `agent/references/script-contract.md` | run.sh 를 만드는 법 | 사람 |
| `agent/references/troubleshooting.md` | 겪은 실패와 로그 | 사람 |

**문법은 코드에서 뽑고 함정은 사람이 쓴다.** 앞의 둘은
`agent/scripts/generate_references.py` 가 서버의 OpenAPI 와 CLI 의 parser 에서 만들고,
CI 가 매 push 마다 코드와 어긋나지 않았는지 검사한다. 손으로 고치면 그 검사에서 막힌다.

## Claude Code 로 쓸 때

`agent/skills/ddpsrun/SKILL.md` 가 있다. plugin 규격은 `agent/.claude-plugin/plugin.json`
이고 저장소 최상위 `.claude-plugin/marketplace.json` 이 그것을 가리킨다. **Codex 등 다른
agent 는 그 파일을 안 읽으므로, 모두가 읽는 안내문은 이 `AGENTS.md` 다.**

## 설계

`docs/` 에 있다. `00-overview.md` 부터 읽는다.

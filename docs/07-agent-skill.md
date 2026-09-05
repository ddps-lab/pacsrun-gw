# agent skill 과 Codex 대비 (2026-08-31)

## 문제 — skill 형식은 Claude 전용이다

SkyPilot 을 보면 이렇게 되어 있다.

```
agent/.claude-plugin/plugin.json        Claude Code 의 plugin 규격
agent/skills/skypilot/SKILL.md          frontmatter 로 트리거를 정의
.claude-plugin/marketplace.json         Claude Code 의 marketplace 규격
```

`SKILL.md` 의 frontmatter `description` 이 **언제 이 skill 을 쓸지** 를 정하는데, 그것을
읽고 판단하는 것은 Claude Code 다. **Codex 는 그 파일을 안 본다.**

그리고 SkyPilot 저장소 최상위의 `AGENTS.md` 첫 줄이 이렇다.

```
# CLAUDE.md - SkyPilot Development Guide
```

**파일 이름은 `AGENTS.md` 인데 내용 제목은 `CLAUDE.md` 다.** Claude 용으로 쓴 것을 이름만
바꿔 둔 흔적이다. 즉 SkyPilot 도 Codex 를 따로 대비하지는 않았다.

## 해법 — 세 층으로 나눈다

**지식은 한 벌만 두고 포장만 여럿 만든다.**

```
pacsrun-gw/
  agent/
    references/                  <- 실제 내용. 여기가 한 벌
      api.md                     (자동) 서버 OpenAPI 에서 생성
      cli.md                     (자동) CLI --help 에서 생성
      script-contract.md         (사람) 저장소를 읽어 run.sh 를 만드는 법
      troubleshooting.md         (사람) 우리가 겪은 실패와 로그
    skills/ddpsrun/
      SKILL.md                   <- Claude 용 포장. references 를 가리킨다
    .claude-plugin/plugin.json
  .claude-plugin/marketplace.json
  AGENTS.md                      <- Codex 를 포함한 모두가 읽는 짧은 안내문
```

| 층 | 대상 | 유지 비용 | 우선순위 |
|---|---|---|---|
| **`ddps explain` / `validate` / `estimate`** | **모든 agent, 사람** | **없음.** 서버 로직이 곧 답 | **1** |
| `AGENTS.md` | Codex 등 | 낮음. 짧은 안내문 | 2 |
| `agent/skills/` + plugin | Claude Code | 중간 | 3 |

**첫 번째를 먼저 만든다.** 도구가 스스로 설명하면 나머지 둘은 그것을 가리키기만 하면 된다.
반대로 하면 문서 세 벌이 서로 어긋난다.

### 자기 설명 명령

```bash
ddps explain                    # 이 도구가 무엇이고 어떻게 쓰는지
ddps schema                     # 제출 본문의 형식
ddps validate run.sh            # 이 script 의 문제를 지적
ddps estimate --dry-run ...     # 시간과 비용
ddps gpus                       # 지금 빌릴 수 있는 GPU 와 단가
```

**agent 가 이 명령을 실행해서 답을 받으면 된다.** skill 파일을 안 읽어도, 문서를 안 읽어도
된다. 셸을 쓸 수 있으면 어느 agent 든 통한다.

## 자동 생성과 사람이 쓰는 것의 경계

SkyPilot 의 경계를 그대로 쓴다. `agent/scripts/generate_references.py` 가 RST/Click/AST 에서
문법 문서 셋을 다시 만든다 (LLM 을 안 쓴다).

```
references/yaml-spec.md         1,640줄  (자동 생성)
references/cli-reference.md       797줄  (자동 생성)
references/python-sdk.md        1,513줄  (자동 생성)
references/examples.md          1,999줄  (사람이 씀)
references/advanced-patterns.md 1,455줄  (사람이 씀)
references/troubleshooting.md   1,089줄  (사람이 씀)
```

**문법은 코드에서 뽑고 함정은 사람이 쓴다.**

## `script-contract.md` 에 들어갈 것 — 전부 실측에서 나왔다

agent 가 사용자 저장소를 읽어 `run.sh` 를 만들 때 지켜야 할 것들이다. 우리가 실험 8 건에서
손으로 메운 것과 같다.

**저장소에서 경로를 확인한다.** 문서에 적힌 경로가 실제 구조와 다를 수 있다. 우리는
`dpo-training/` 접두어가 없어서 clone 직후 죽을 뻔했다.

**한 변수로 묶어야 하는 짝을 찾는다.** 학습의 출력 경로와 추론의 입력 경로가 같아야 한다.
문서에 `--out adapter_x` 와 `--lora /root/ab/adapter_x` 가 따로 적혀 있으면 **학습이 끝난
뒤에야** 추론이 어댑터를 못 찾는다.

**명령이 만들지 않는 산출물을 script 가 만들게 한다.** 우리 경우 다섯 중 셋(학습 로그,
채점 출력, pip freeze)이 그랬다.

**중간 확인점을 앞에 둔다.** clone 직후 파일 줄 수를 찍고, 학습 시작 직후 표본 수를 찍는다.
25 시간 돌고 나서 파일이 틀렸음을 아는 것보다 낫다.

**어느 단계에서 죽어도 그때까지를 올린다.** `trap ... EXIT` 를 건다.

**학습이 끝나면 즉시 어댑터를 올린다.** 추론을 기다리지 않는다. 학습이 25 시간이고 추론이
1 시간이면, 추론에서 죽었을 때 25 시간을 잃으면 안 된다.

**긴 학습에는 checkpoint 감시를 넣는다.** 폴더가 조용해진 것을 확인하고 압축한다. 쓰는
중에 tar 를 뜨면 반쪽짜리가 올라간다.

**판단은 서버에 묻는다.** GPU 크기, spot 여부, 예상 시간을 skill 에 적지 않는다.
`ddps estimate` 를 부른다. 그래야 로직이 한 곳에 있고 UI 도 같은 답을 받는다.

## `troubleshooting.md` 에 들어갈 것

전부 우리가 겪었고 로그가 있다.

| 증상 | 원인 |
|---|---|
| `empty_strided_cuda((2, s, 151936))` + OutOfMemoryError | 캡에 가까운 긴 표본. 요청 바이트를 `2 × 151936 × 2` 로 나누면 토큰 수가 나온다 |
| `status=RUNNING runtime=null` 이 30분 | `status` 는 준비 신호가 아니다. 같은 물리 서버에 job 이 몰리면 image pull 이 느려진다 |
| `Field "objectMounts" is not defined by type ...` | vendor API 쪽 고장. 우리 요청과 무관 |
| job 이 `Succeeded` 인데 S3 가 비어 있다 | 12 시간 자격증명 만료. fetch mode 가 필요 |
| `AccessDenied ... CreateMultipartUpload` | fetch mode 에서는 정상. 원격은 읽기 전용이고 driver 가 대신 올린다 |
| `no offering left` 인데 재고는 있다 | 400 을 재고 부족으로 오분류하던 문제. 2026-08-28 수정 |

## 배포

SkyPilot 과 같이 저장소 최상위 `.claude-plugin/marketplace.json` 이 `agent/` 를 plugin 으로
가리킨다.

```json
{
  "name": "ddpsrun",
  "plugins": [
    { "name": "ddpsrun", "source": "./agent", "description": "..." }
  ]
}
```

**조직에 이미 선례가 있다.** `ddps-lab/skypilot-agent-skill` 이 같은 모양이다.

# pacsrun-gw — 무엇이고 왜 있는가 (2026-08-31)

## 한 줄

**kubectl 도 AWS IAM 도 없는 사용자가 job 을 제출하고 결과를 받는 경로다.**

## 이름

| | 이름 | 누가 보나 |
|---|---|---|
| GitHub 저장소 | `ddps-lab/pacsrun-gw` | 우리 |
| 로컬 폴더 | `~/ddps-projects/pacsrun-gw` | 우리 |
| 배포 패키지 | `ddpsrun` | 사용자 |
| 명령 | `ddpsrun` | 사용자 |
| UI 제목 | DDPS Run | 사용자 |

**`pacsrun` 과 `kubepacs` 는 사용자가 보는 표면에 쓰지 않는다.** 패키지와 명령을 같은
이름으로 두었다. SkyPilot 은 `skypilot` / `sky` 로 나눴지만, 이름이 하나면 "설치한 것과
치는 것이 왜 다른가" 를 묻지 않아도 된다.

**이 저장소의 문서에는 계정 식별자를 쓰지 않는다.** `<ACCOUNT_ID>`, `<RESULT_BUCKET>`,
`role/<REMOTE_ROLE>` 같은 자리표시자를 쓴다. 나중에 public 으로 열 때 손볼 것이 없게 하려는
것이고, CI 가 매 push 마다 검사한다 (`.github/workflows/ci.yml`). 저장소 이름은 그 표면이
아니므로 관계가 드러나게 `pacsrun-gw` 로 두었다. SkyPilot 이 패키지 `skypilot` 에 명령
`sky` 인 것과 같은 구조다 (`setup.py:173`, `'console_scripts': ['sky = sky.cli:cli']`).

## 왜 서버가 필요한가 — 확인한 사실

```
# aws eks describe-cluster --name pacsrun --region us-west-2 --query 'cluster.accessConfig'
{ "authenticationMode": "API" }

# aws eks list-identity-provider-configs --cluster-name pacsrun --region us-west-2
{ "identityProviderConfigs": [] }
```

접근 목록이 전부 IAM ARN 이고 OIDC provider 는 비어 있다. **AWS 신원이 없는 사람은
`kube-apiserver` 에 인증할 수단이 없다.** 그래서 신원을 가진 무언가가 대신 붙어야 하고,
그것이 이 저장소의 서버다.

`kube-apiserver` 가 이미 있지 않느냐는 물음의 답은 이렇다. **전송 통로로는 있다. 그러나
그것이 아는 말은 kubernetes 객체뿐이다.** "GPU 가 몇 GB 필요한가", "얼마나 걸리는가",
"이 script 에 함정이 있는가" 는 답하지 않는다.

## 전체 그림

```
[브라우저 UI]          [ddps CLI]          [code agent + skill]
   클릭으로 form         터미널 명령           사용자 repo 를 읽고
        │                   │                 script 와 요청을 만든다
        └───────────┬───────┴─────────────────────────┘
                    │  HTTPS + ddpsrun token
                    ▼
             ┌──────────────────┐
             │  pacsrun-gw 서버  │   IAM role 을 여기가 든다
             │  /validate       │   판단 로직이 전부 여기 있다
             │  /estimate       │   측정표도 여기 있다
             └────────┬─────────┘
                      │  EKS access entry 로 인증
                      ▼
             [kube-apiserver]  ->  PacsJob  ->  driver pod  ->  RunPod GPU
                      │
                      ▼
             S3  pacsrun/<namespace>/<job>/
```

**세 입구가 하나의 API 를 본다.** 지식이 서버에 한 벌만 있고 UI 와 agent 가 나눠 쓴다.

## 왜 pacsrun 저장소와 나누는가

1. **공개 범위가 갈릴 수 있다.** CLI 는 사용자가 설치해야 한다. 그때 이쪽만 열면 되고
   operator 와 driver 코드는 닫아 둘 수 있다.
2. **CI 가 다르다.** `pacsrun` 은 Go 바이너리와 Python solver 를 한 이미지로 굽는다.
   이쪽은 서버, CLI 패키지, UI 빌드 셋이고 언어가 다르다.
3. **변경 속도가 다르다.** operator 는 몇 주에 한 번, UI 와 CLI 는 매일 바뀐다.
4. **신원 경계가 저장소 경계와 맞는다.** 사용자 token 은 이쪽만 다루고 `pacsrun` 은 모른다.

합칠 근거도 하나 있다. CRD 가 바뀌면 이쪽도 바뀌어야 하는데 저장소가 나뉘면 PR 이 둘이
된다. 다만 CRD 는 자주 안 바뀐다. `spec` 필드가 9 개이고 몇 주째 그대로다.

## 문서 순서

| 파일 | 내용 |
|---|---|
| `00-overview.md` | 이 문서 |
| `01-research.md` | Kubeflow 와 SkyPilot 조사, 방향 비교 |
| `02-auth.md` | 두 구간의 인증. EKS token 이 무엇인지 |
| `03-api.md` | 라우트와 요청 본문 |
| `04-estimate.md` | 시간과 비용을 어떻게 답하는가. 측정표 |
| `05-monitoring.md` | UI 가 무엇을 보여 주는가 |
| `06-multi-tenancy.md` | namespace 와 S3 prefix 분리 |
| `07-agent-skill.md` | skill 구성과 Codex 대비 |
| `08-plan.md` | 만드는 순서와 미결 항목 |

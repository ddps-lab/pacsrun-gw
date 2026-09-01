# 만드는 순서와 미결 항목 (2026-08-31)

## 순서

### ~~1단계 — 서버 (얇게)~~ **끝. 2026-08-31. `09-server.md` 참고**

`/healthz`, `/v1/jobs` 제출, `/v1/jobs/{id}` 조회, `/v1/jobs/{id}/logs`. **판단은 없다.**
인증은 static token 파일 한 종류이고, Cognito 는 CLI 가 생기는 2단계 뒤로 미뤘다.

**규모 [추정]은 600~800 줄이었고 실제는 1,924 줄이다.** 코드 7 파일 1,350 줄, test 4 파일
574 줄. 추정이 빗나간 이유가 둘이다. test 를 안 세었고, `pacsrun` 의 주석 규칙(파일마다
end-to-end 흐름 docstring, 함수마다 목적과 입출력)을 적용하면 주석과 빈 줄이 절반 가까이
된다. 주석과 docstring 과 빈 줄을 뺀 실제 구문은 585 줄이다.

cluster 에 아직 안 올렸다. 확인 못 한 것 셋은 `09-server.md` 끝에 적어 두었다.

### ~~2단계 — CLI~~ **끝. 2026-08-31. `10-cli.md` 참고**

`ddpsrun login / logout / explain / schema / submit / status / logs`. 서버 라우트를 그대로
부르고, 노트북에는 `requests` 와 `PyYAML` 만 설치된다.

`gpus` 는 안 만들었다. 답하려면 이 pod 에 vendor API key 와 catalog 캐시가 있어야 하는데
그것은 `04-estimate.md` 의 주제이지 라우트 하나로 붙일 것이 아니다. `validate` 와
`estimate` 는 예정대로 3단계다.

서버에 `/v1/explain` 과 `/v1/schema` 를 더했다. 판단은 하지 않는다 — 하나는 고정된 산문,
하나는 요청 모델에서 생성한 JSON Schema 다.

### ~~3단계 — `/validate` 와 `/estimate`~~ **끝. 2026-08-31. `11-judgement.md` 참고**

측정표 8 줄, 검사 여덟 개, 그리고 `spec.placement.capacityType` 을 서버가 쓰기 시작했다.
숫자(`measurements.py`)와 산술(`estimate.py`)을 파일로 갈랐다.

`03-api.md` 의 검사 일곱 중 넷을 코드로 옮겼고, 나머지 셋은 저장소를 clone 해야 볼 수 있어
`not_checked` 로 내보낸다.

### ~~4단계 — monitoring 뒷단~~ **끝. 2026-08-31. `12-monitoring.md` 참고**

`GET /v1/jobs/{id}/metrics` 와 `ddpsrun watch`. **시계열을 저장하지 않기로 했다** — 두 줄
모양이 이미 로그에 있고, `since_seconds` 로 창만 읽으면 되므로 서버가 상태를 들 이유가 없다.
대가는 pod 이 사라지면 지표도 사라지는 것이고, 그것은 로그가 원래 그렇다.

### ~~5단계 — agent skill~~ **끝. 2026-08-31**

`agent/` 에 skill, plugin, reference 넷이 있고 저장소 최상위 `.claude-plugin/marketplace.json`
이 가리킨다. **문법 둘(`api.md`, `cli.md`)은 코드에서 생성하고 함정 둘은 사람이 쓴다.**
CI 가 매 push 마다 생성본이 코드와 어긋나지 않았는지 검사한다 — 실제로 4단계에서 라우트를
더했을 때 그 검사가 막았다.

### 6단계 — serverless 로 옮기기

**2026-09-01 에 5단계와 6단계 사이에 끼워 넣었다.** pod 으로 둔 서버를 Lambda 로 옮기고 화면을
S3 와 CloudFront 에 둔다. 미결 8, 12, 14 를 한꺼번에 닫는다. `14-serverless.md`

### 7단계 — UI

목록, 제출 form, monitoring 화면 넷, 결과 내려받기. **정적 HTML 이 `fetch()` 로 Lambda 를
부른다.** 서버가 판단을 다 하므로 화면은 받아 그리기만 한다.

### 그리고 병행해야 하는 것 — 다중 사용자

`06-multi-tenancy.md` 의 넷은 **1단계와 같이 가야 한다.** prefix 에 namespace 를 안 넣으면
사용자가 둘만 돼도 결과가 섞인다. terraform 작업이다.

---

## 미결 항목

| # | 항목 | 지금 상태 |
|---|---|---|
| ~~1~~ | ~~명령 이름~~ | **정함: `ddpsrun`.** 패키지와 명령을 같은 이름으로 |
| ~~2~~ | ~~서버 위치~~ | **정함: EKS cluster 안의 pod.** 노드에 pod 가 많지 않고 별도 기계를 늘리지 않는다. 대가는 cluster 를 끄면 서버도 꺼지는 것 |
| ~~3~~ | ~~로그인 방식~~ | **정함: Cognito.** 사용자가 수십 명이 되고 UI 를 여럿이 쓴다. 가입, 비밀번호, 재설정, MFA 를 우리가 만들지 않는다 |
| 4 | **CLI 로그인 흐름** | Cognito 를 쓰면 브라우저를 띄우고 돌아오는 흐름이 필요하다. `aws sso login` 이 하는 것과 같다 |
| ~~5~~ | ~~namespace 이름 규칙~~ | **정함: `<team>-<user>`, 사람마다 하나.** 격리는 kubernetes 가 하고 팀은 label 이다. 팀 수치는 서버가 자기 token 파일로 모은다. `13-tenancy.md` |
| ~~6~~ | ~~bucket 을 나눌 것인가 prefix 로 둘 것인가~~ | **정함: prefix, namespace 단위.** 팀 단위로 나누려다 되돌렸다 — 팀 수치가 S3 key 가 아니라 PacsJob 에서 나오므로 그 분할이 사는 곳이 없었고, controller 자물쇠가 팀을 dash 로 잘라 내야 했다 |
| ~~7~~ | ~~`/estimate` 가 어디까지 답하는가~~ | **정함: `measured` / `interpolated` / `unknown` 셋.** 잰 범위 밖으로 20% 넘게 나가면 `unknown` 이고 이유를 붙인다 |
| ~~8~~ | ~~UI 기술~~ | **다시 정함 2026-09-01: S3 에 정적 파일, CloudFront 로 배포.** 서버가 Lambda 로 가면서 HTML 을 그릴 프로세스가 없어졌다. 연구실이 이미 쓰는 방식이고 대가는 CORS 설정 하나다. `14-serverless.md` |
| 9 | **`--resume-from-checkpoint`** | 학습 script 소유자에게 요청해 둔 상태. 긴 학습의 복구가 여기 걸려 있다 |
| 10 | **shell 접근** | VM 경우에 한해 나중에. 보안 설계가 필요 |
| 11 | **Lightning Thunder** | 아직 안 씀. 쓰면 측정표에 컴파일러 열 추가 |
| ~~12~~ | ~~서버 앞에 무엇을 두는가~~ | **정함: 아무것도 안 둔다.** 서버를 Lambda 로 옮기면 Function URL 이 HTTPS 주소를 준다. Gateway API CRD, load balancer controller, ACM 인증서, Route53 레코드가 전부 필요 없어진다. (`ingress-nginx` 는 2026-03-24 에 archive 되었고 Gateway API 가 대안이지만, 그 길도 결국 ALB 월 $22 이다) `14-serverless.md` |
| ~~13~~ | ~~image 를 어느 registry 로~~ | **정함: us-west-2 의 `ddpsrun/gateway` 하나.** cluster 가 us-west-2 이고 이미지를 당기는 것은 서버 pod 하나뿐이라 두 번째 사본은 값만 든다. `terraform/registry/` 가 ECR 과 전용 IAM role 을 만들고 `release.yml` 이 push 한다. 2026-08-31 apply 함. **2026-09-01 주석: Lambda 로 가면 zip 배포라 이 저장소를 쓰지 않는다. 의존성이 92.4 MB 로 250 MB 한도 안이다. 지우지는 않는다 — pod 으로 되돌릴 판단이 남아 있고 월 $0.40 이다** |
| ~~15~~ | ~~Lambda cold start~~ | **2026-09-01 실측: 4.1 초.** 거의 전부가 import 이고, `kubernetes` client (74 MB) 를 빼면 1.5 초가 된다. 메모리를 올려도 안 줄어드는 것으로 보아 연산이 아니라 파일 읽기다. `14-serverless.md` |
| 17 | **`kubernetes` client 를 뺄 것인가** | 우리가 쓰는 것은 REST 호출 다섯 개뿐이다. 빼면 cold start 가 2.6 초 줄지만, EKS token 만들기와 CA 인증서 처리를 직접 짜야 한다 |
| ~~16~~ | ~~Lambda 실행 role 로 apiserver 에 붙어 본 적이 없다~~ | **2026-09-01 확인.** 같은 모양의 IAM role 로 붙어서 `Groups` 에 `kubernetesGroups` 값이 들어오는 것을 봤다. IAM 전파에 약 10 초가 걸리고, username 에 `{{SessionName}}` 이 들어가므로 RBAC 은 group 에 걸어야 한다. `14-serverless.md` |
| ~~14~~ | ~~로그 `follow` 가 몇 시간을 버티는가~~ | **정함: streaming 을 버리고 polling 으로 간다.** Lambda 는 한 번 실행이 15 분이라 30 시간 연결이 성립하지 않는다. `timestamps=True` 로 받아 클라이언트가 마지막 시각 하나만 기억하면 서버는 무상태로 남는다. 6 초 간격 / 30 초 창으로 실측했다 (`14-serverless.md`) |

---

## CI

`pacsrun` 의 `release.yml` 이 OIDC 로 AWS 에 붙어 ECR 에 올린다. 같은 방식을 쓰려면
**`pacsrun-gw` 용 IAM role 과 GitHub 저장소 변수를 새로 만들어야 한다.** terraform 작업이다.

처음에는 이 정도면 된다.

```
lint 과 test        server 와 cli
build               UI 빌드가 깨지지 않는지
(나중에) release    서버 이미지를 ECR 로, CLI 를 패키지로
```

---

## 저장소를 나눈 판단

`00-overview.md` 에 근거를 적었다. 요약하면 공개 범위, CI, 변경 속도, 신원 경계 넷이다.

**보안상 새로 생기는 위험은 없다.** 둘 다 private 이고 계정 번호와 cluster 이름은 이미
`pacsrun` 문서 전체에 있다.

**다만 나중에 CLI 를 public 으로 열 것을 대비해 규칙 하나를 지킨다.** 계정 번호와 bucket
이름을 코드에 박지 않고 환경변수나 설정으로 받는다. `pacsrun` 이 이미 그렇게 되어 있다.

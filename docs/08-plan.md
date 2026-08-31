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

### 2단계 — CLI

`ddps submit / status / logs / get / gpus` 와 자기 설명 명령 `explain / schema`.
서버 라우트를 그대로 부른다. **사용자에게 kubeconfig 도 kubectl 도 없다.**

### 3단계 — `/validate` 와 `/estimate`

여기부터가 이 저장소의 값어치다. 우리가 손으로 메운 함정 여덟 개와 측정표가 들어간다.
`04-estimate.md` 와 `03-api.md` 의 `/validate` 표를 코드로 옮긴다.

### 4단계 — monitoring 뒷단

`run.sh` 에 `PACSRUN_GPU=` 감시를 넣고, 서버가 그 줄과 학습 진행 줄을 골라내 시계열로
저장한다. `05-monitoring.md` 참고.

### 5단계 — agent skill

`ddps explain` 이 이미 있으므로 `AGENTS.md` 와 `references/` 는 그것을 가리키기만 하면 된다.
Claude plugin 은 그 위에 얹는다.

### 6단계 — UI

목록, 제출 form, monitoring 화면 넷, 결과 내려받기. **서버가 이미 다 하므로 화면만 붙인다.**

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
| 5 | **namespace 이름 규칙** | `lab-<사용자>` 인지 팀 단위인지 |
| 6 | **bucket 을 나눌 것인가 prefix 로 둘 것인가** | prefix 가 간단. bucket 을 나누면 요금과 lifecycle 을 따로 봄 |
| 7 | **`/estimate` 가 어디까지 답하는가** | 재본 적 없는 조합에 무엇을 답할지. 지금 판단은 `unknown` |
| 8 | **UI 기술** | Next.js 인지 더 가벼운 것인지 |
| 9 | **`--resume-from-checkpoint`** | 학습 script 소유자에게 요청해 둔 상태. 긴 학습의 복구가 여기 걸려 있다 |
| 10 | **shell 접근** | VM 경우에 한해 나중에. 보안 설계가 필요 |
| 11 | **Lightning Thunder** | 아직 안 씀. 쓰면 측정표에 컴파일러 열 추가 |
| 12 | **서버 앞에 무엇을 두는가** | TLS 를 끝내고 8080 으로 넘길 것. Ingress 인지 ALB 인지 tunnel 인지. 그전까지는 `kubectl port-forward` |
| 13 | **image 를 어느 registry 로** | CI 가 빌드만 하고 push 를 안 한다. PACSrun 은 ECR 두 곳에 올린다 |
| 14 | **로그 `follow` 가 몇 시간을 버티는가** | 앞단의 idle timeout 이 끊으면 CLI 가 재연결해야 한다. driver 가 RunPod 상대로 이미 겪은 문제다 |

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

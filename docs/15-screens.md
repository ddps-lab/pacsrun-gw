# 15. 화면 설계 — 무엇을 보여 줄 것인가

DDPSRUN-SCREENS

이 문서는 코드를 쓰기 전에 **화면의 목록과 각 화면이 담을 내용을 확정하기 위한** 설계
문서입니다. 지금까지 만든 `ui/`는 파일 3개 510줄에 화면 4개(작업 목록, 작업 상세, 제출,
팀 집계)뿐이고, 그 구성을 결정한 근거가 어디에도 남아 있지 않습니다. 근거 없이 화면을
더 붙이면 나중에 왜 그렇게 만들었는지 설명할 수 없으므로, 먼저 두 개의 성숙한 참조를
읽고 그중 무엇을 가져오고 무엇을 버릴지 판단합니다.

읽은 참조는 두 가지입니다.

- **SkyPilot dashboard** — 이 저장소에 원본 소스가 그대로 들어 있어서, 렌더링된 화면이
  아니라 코드를 직접 읽었습니다 (`sky/dashboard/src/`).
- **Kubeflow Central Dashboard** — 공식 문서 두 페이지를 읽었습니다
  (`/docs/components/central-dash/overview/`, `/docs/components/central-dash/customizing-menu/`).

---

## 15.1 용어

이 문서에서 반복해서 쓰는 낱말을 먼저 정의합니다.

- **화면(page)** — 주소가 따로 있고, 사용자가 네비게이션에서 직접 갈 수 있는 단위입니다.
- **패널(panel)** — 한 화면 안에서 한 가지 사실만 보여 주는 구역입니다. 표 하나, 그래프
  하나, 카드 한 장이 각각 패널입니다.
- **route** — 우리 서버가 열어 둔 HTTP 경로입니다. 예를 들어 `GET /v1/jobs`입니다.
- **필드** — 그 route가 돌려주는 JSON 안의 이름입니다. 예를 들어 `JobView.phase`입니다.

---

## 15.2 조사 1 — SkyPilot dashboard

### 화면 목록

`sky/dashboard/src/pages/` 아래에 파일이 22개 있고, 최상위 네비게이션은 7개입니다.

| 주소 | 화면 |
|---|---|
| `/` | 홈 |
| `/jobs` | managed job 목록 |
| `/clusters` | cluster 목록 |
| `/infra` | 어떤 cloud와 region에 무엇이 있는지 |
| `/volumes` | 저장소 |
| `/users` | 사용자 |
| `/workspaces` | workspace (권한 경계) |
| `/settings` | 설정 |

작업 하나를 파고 들어가는 계층은 3단계입니다.
`pages/jobs.js` (목록) → `pages/jobs/[job].js` (작업 하나) →
`pages/jobs/[job]/[task].js` (그 작업 안의 task 하나).

### 작업 목록 표의 열

`sky/dashboard/src/components/jobs.jsx:1440`의 `baseColumns` 배열이 열을 정의합니다.
순서대로 13개입니다.

| 줄 | 열 `id` | 내용 |
|---|---|---|
| 1443 | `id` | 작업 번호 |
| 1514 | `name` | 작업 이름 |
| 1583 | `user` | 제출한 사람 |
| 1604 | `workspace` | 권한 경계 |
| 1629 | `submitted` | 제출 시각 |
| 1644 | `duration` | 걸린 시간 |
| 1659 | `status` | 상태 |
| 1720 | `infra` | 실제로 어디서 돌았는지 |
| 1792 | `requested_resources` | 요청한 자원 |
| 1843 | `recoveries` | 다시 살아난 횟수 |
| 1864 | `pool` | 소속 pool |
| 1892 | `details` | 상세로 가는 링크 |
| 1923 | `logs` | 로그로 가는 링크 |

**여기서 배울 점.** 열 13개 중 4개(`user`, `workspace`, `infra`, `recoveries`)는 "누가,
어떤 경계 안에서, 어디서, 몇 번 끊기고 돌았는가"입니다. 목록 화면이 이름과 상태만
보여 주면 안 된다는 뜻입니다. 특히 `recoveries`는 spot instance를 쓰는 도구에서만 의미가
있는 열이고, 우리도 spot을 쓰므로 그대로 필요합니다.

### 상태 어휘와 탭

`components/jobs.jsx:97`의 `statusGroups`가 상태를 두 묶음으로 나눕니다.

```
active:   PENDING, RUNNING, RECOVERING, SUBMITTED, STARTING, CANCELLING
finished: SUCCEEDED, FAILED, CANCELLED, FAILED_SETUP, FAILED_PRECHECKS,
          FAILED_NO_RESOURCE, FAILED_CONTROLLER
```

`components/jobs.jsx:579`의 `activeTab`이 `all` / `active` / `finished` 세 탭을 관리합니다.

**여기서 배울 점 두 가지.** 첫째, 기본 화면은 전체가 아니라 **지금 돌고 있는 것**이어야
합니다. 끝난 작업이 수백 개 쌓여도 화면을 밀어내지 않습니다. 둘째, 실패는 한 단어가
아니라 **어느 단계에서 실패했는지**로 나뉩니다. `FAILED_SETUP`(준비 단계 실패),
`FAILED_NO_RESOURCE`(자원을 못 구함), `FAILED_CONTROLLER`(제어하는 쪽의 실패)는 사용자가
해야 할 일이 각각 다릅니다.

### 작업 상세 화면

`pages/jobs/[job].js`가 `Details`, `Logs`, `Controller Logs`, `Git Commit`,
`Show SkyPilot YAML` 구역을 가집니다. 즉 상세 화면은 **사용자 로그와 시스템 로그를 나누고,
제출할 때 쓴 정의 원문을 그대로 볼 수 있게** 합니다.

---

## 15.3 조사 2 — Kubeflow Central Dashboard

### 왼쪽 네비게이션

문서가 나열한 항목은 `Home`, `Manage Contributors`, `Notebooks`, `TensorBoards`,
`Volumes`, `Katib Experiments`, `KServe Endpoints`, 그리고 Pipelines 묶음 아래의
`Pipelines`, `Experiments`, `Runs`, `Recurring Runs`, `Artifacts`, `Executions`입니다.

### 항목을 등록하는 방법

`centraldashboard-config`라는 ConfigMap의 `links` 항목이 네비게이션을 만듭니다. 열쇠는
네 개입니다.

| 열쇠 | 뜻 |
|---|---|
| `menuLinks` | 왼쪽 사이드바에 들어가는 클러스터 안 애플리케이션 |
| `externalLinks` | 바깥 웹사이트 링크 |
| `documentationItems` | **홈 화면**의 문서 구역 |
| `quickLinks` | 예시에만 나오고 본문에 설명이 없습니다 (미확인) |

`menuLinks` 항목은 `type`(`"item"` 고정), `link`, `text`, `icon` 네 필드를 가집니다.

### namespace 선택

`link` 필드 안에 `{ns}`라고 쓰면 지금 고른 profile의 namespace로 치환됩니다. 즉
Kubeflow는 **화면 전체에 걸리는 namespace 선택이 하나 있고, 모든 링크가 그 선택을 물려
받는** 구조입니다.

### 확인하지 못한 것

홈 화면에 실제로 어떤 카드가 몇 개 놓이는지는 overview 문서에 그림만 있고 글로 적혀
있지 않아서 확인하지 못했습니다. `documentationItems`가 홈에 놓인다는 사실만 문서로
확인됩니다.

---

## 15.4 두 참조에서 무엇을 가져오고 무엇을 버리는가

우리와 저 둘은 조건이 다릅니다. 다른 점을 먼저 적어야 무엇을 버릴지가 정해집니다.

| 축 | SkyPilot / Kubeflow | 우리 (ddpsrun) |
|---|---|---|
| 구성 요소 | 여러 제품을 한 화면에 모음 | 제품 하나 |
| 실행 주체 | 상주하는 서버 | Lambda, 요청이 있을 때만 |
| 사용자 수 | 조직 전체 | 연구실 인원, 십수 명 |
| 사용자의 관심 | cluster, volume, endpoint까지 | **작업 하나가 끝났는가, 얼마 들었는가** |

**가져올 것.**

1. **상태 탭** (SkyPilot). 기본이 "지금 돌고 있는 것"입니다.
2. **실패의 세분화** (SkyPilot). 한 단어 `FAILED`로 뭉치지 않습니다.
3. **목록 표에 누가/어디서/몇 번 끊겼는지** (SkyPilot). 특히 `recoveries`입니다.
4. **사용자 로그와 시스템 로그의 분리, 그리고 제출 정의 원문** (SkyPilot 상세 화면).
5. **화면 전체에 걸리는 namespace 선택 하나** (Kubeflow). 우리에게는 팀 선택입니다.
6. **홈 화면에 문서와 바로가기 구역** (Kubeflow `documentationItems`).

**버릴 것.**

1. **`/clusters`, `/volumes`, `/infra` 같은 자원 화면.** 우리 사용자는 노드를 보지
   않습니다. PACSrun의 전제 자체가 "VM도 컨테이너도 넘기지 않는다"입니다.
2. **플러그인 등록 장치** (`centraldashboard-config`). 붙일 제품이 하나뿐입니다.
3. **`/users`, `/workspaces` 관리 화면.** 사용자와 팀은 token 파일이 정의하고
   (`server/ddpsrun_server/auth.py`), 그 파일은 Secrets Manager에 있습니다. 화면에서
   고칠 수 있게 만들면 Lambda에 쓰기 권한을 줘야 하므로 만들지 않습니다.

---

## 15.5 결정 — 화면 5개

### 최상위 네비게이션

```
Home  |  Jobs  |  Submit  |  Team
                                        [team]  [Sign out]
```

팀 선택은 오른쪽 위에 고정되고 모든 화면이 그 값을 물려받습니다 (Kubeflow의 `{ns}`와
같은 역할입니다). 팀이 하나뿐인 사용자에게는 이름만 보이고 고를 수 없게 합니다.

### 화면 1 — 홈

목적은 **화면을 열자마자 지금 상태를 아는 것**입니다.

| 패널 | 내용 | 쓰는 route |
|---|---|---|
| 요약 카드 4장 | 돌고 있는 작업 수, 오늘 끝난 수, 오늘 실패한 수, 이번 달 비용 | `GET /v1/stats`, `GET /v1/jobs` |
| 지금 돌고 있는 작업 | 최대 5줄, 진행률 막대 포함 | `GET /v1/jobs` + 각 작업의 `/metrics` |
| 시작하기 | CLI 설치 한 줄, agent skill 설치 한 줄, 문서 링크 | 없음 (정적) |

### 화면 2 — 작업 목록

탭은 `전체` / `진행 중` / `끝남` 세 개입니다.

표의 열은 SkyPilot 13개를 우리 조건으로 줄여 **9개**로 합니다.

| 열 | 출처 필드 | SkyPilot 대응 |
|---|---|---|
| 이름 | `JobView.name` | `name` |
| ID | `JobView.job_id` | `id` |
| 제출자 | (없음, 15.6 참조) | `user` |
| 상태 | `JobView.phase` | `status` |
| 제출 시각 | `JobView.created_at` | `submitted` |
| 경과 | (없음, 15.6 참조) | `duration` |
| GPU | `JobView.gpu` | `requested_resources` |
| 벤더 | `JobView.vendor` | `infra` |
| 재시작 | `JobView.recovery_count` | `recoveries` |

`workspace`와 `pool`은 우리에게 대응하는 개념이 없어서 뺐습니다. `details`와 `logs`
링크 열은 행 전체를 누르면 상세로 가게 만들어서 없앴습니다.

### 화면 3 — 작업 상세

주소는 `#/jobs/<job_id>`입니다. 패널은 5개입니다.

1. **머리말** — 이름, 상태 배지, 경과 시간, GPU, 벤더, 재시작 횟수, 결과 경로
   (`JobView` 전부)
2. **진행률** — `MetricsResponse.progress`의 `step` / `total_steps` / `percent` /
   `remaining` / `projected_total_hours`. `steady`가 거짓이면 "아직 속도가 안정되지
   않았습니다"를 함께 적습니다.
3. **GPU 그래프** — `MetricsResponse.gpu_series`를 꺾은선 두 개(사용률, 메모리)로
   그립니다. 지금은 400점까지 내려받습니다 (`server/ddpsrun_server/metrics.py`의
   `MAX_SAMPLES`).
4. **로그** — `GET /v1/jobs/{id}/logs`를 `last_timestamp`로 이어 받습니다. 이미 서버가
   `PACSRUN_KEEPALIVE`와 `PACSRUN_GPU=` 줄을 지워서 내려 줍니다.
5. **제출 정의** — 이 작업을 만든 요청 원문. SkyPilot의 `Show SkyPilot YAML`에
   해당하고, 지금은 route가 없습니다 (15.6 참조).

### 화면 4 — 제출

세 단계로 나눕니다. **비용을 보기 전에는 제출 단추가 눌리지 않습니다.**

1. **작성** — 이미지, 명령, GPU, 개수(`parallelism`), 구매 방식(`capacity_type`),
   결과 경로. 구매 방식은 사용자가 고릅니다. 서버가 정하지 않습니다.
2. **점검** — `POST /v1/validate`가 돌려준 `findings`를 등급별로 보여 줍니다.
   `level`이 `error`면 다음으로 못 갑니다.
3. **비용** — `POST /v1/estimate`의 `hours`, `cost_usd`, `gpu.recommended`,
   `capacity_type`, `warnings`. `confidence`가 `unknown`이면 그 사실을 크게 적습니다.

### 화면 5 — 팀

`GET /v1/stats`가 그대로 표가 됩니다. `MemberTotalsView`의 `user`, `jobs`,
`succeeded`, `failed`, `running`, `gpu_hours`, `cost_usd`가 열입니다.
`unpriced_jobs`가 0이 아니면 표 아래에 "값을 매기지 못한 작업 N건이 빠져 있습니다"를
적습니다.

---

## 15.6 모자란 route

위 화면을 그리려면 지금 없는 것이 4개 있습니다.

| 필요한 것 | 왜 | 어떻게 |
|---|---|---|
| `JobView.user` | 목록의 제출자 열, 팀 화면에서 사람별로 넘어가기 | `naming.OWNER_LABEL`이 이미 객체에 붙어 있습니다. `JobView`에 필드만 더하면 됩니다. |
| `JobView.started_at` / `finished_at` | 경과 시간 열. 지금은 `created_at`뿐이라 대기 시간과 실행 시간을 못 나눕니다. | PACSrun에 `status.startedAt` / `status.finishedAt`을 이미 넣었습니다 (`PACSRUN-JOB-CLOCK`). 서버가 읽어서 넘기기만 하면 됩니다. |
| `GET /v1/jobs/{id}/spec` | 상세 화면의 제출 정의 패널 | PacsJob 객체의 `spec`을 그대로 돌려줍니다. 값에 비밀이 섞일 수 있으므로 `env` 중 `valueFrom`이 붙은 항목은 값을 지웁니다. |
| `GET /v1/jobs`의 필터와 개수 | 홈의 요약 카드, 목록의 탭 | 지금은 전부 내려받아 브라우저에서 셉니다. 작업이 수백 개가 되면 응답이 커지므로 `?phase=`와 `?limit=`을 붙입니다. |

`GET /v1/gpus`(고를 수 있는 GPU 목록)와 `GET /v1/jobs/{id}/artifacts`(결과 파일 목록)는
이번 설계에 넣지 않습니다. 앞의 것은 `measurements.py`의 `GPUS` 두 줄을 화면에 그대로
박아도 되고, 뒤의 것은 Lambda에 S3 읽기 권한을 새로 줘야 해서 범위를 넘습니다.

---

## 15.7 폴링 간격과 그 비용

화면이 스스로 갱신하는 곳은 세 군데입니다.

| 화면 | 주기 | 근거 |
|---|---|---|
| 홈 | 30초 | 요약이라 늦어도 됩니다. |
| 작업 목록 | 15초 | 상태가 바뀌는 것을 눈으로 볼 정도입니다. |
| 작업 상세 | 5초 | 로그가 흐르는 것처럼 보여야 합니다. |

**비용.** Lambda 요금은 요청당 $0.20 / 백만 건, 그리고 GB-초당 $0.0000166667입니다.
메모리는 512 MB(0.5 GB)입니다. 배포된 함수에서 확인한 값입니다
(`aws lambda get-function-configuration --function-name ddpsrun-gw` → `"Memory": 512`).
따뜻한 상태의 응답 시간 0.4초를 실측했으므로 한 요청은 0.5 × 0.4 = 0.2 GB-초입니다.

한 사람이 상세 화면을 1시간 열어 두면 5초 주기로 720건입니다. 로그와 metrics 두
route를 부르므로 1,440건입니다.

- 요청 요금: 1,440 × $0.20 / 1,000,000 = **$0.000288**
- 실행 요금: 1,440 × 0.2 GB-초 × $0.0000166667 = **$0.0048**
- 합계 **시간당 약 $0.005**

열 명이 하루 8시간씩 20일을 열어 두면 10 × 8 × 20 × $0.005 = **월 $8**입니다. 상주
서버를 띄우는 대안인 t3.small은 us-west-2 기준 시간당 $0.0208이므로 (2026-09-01 AWS
Pricing API 조회, `$0.0208 per On Demand Linux t3.small Instance Hour`) 한 달
720시간이면 **$15**입니다. 폴링을 지금 간격으로 유지해도 Lambda 쪽이 쌉니다.

**이 계산이 덮지 않는 것.** CloudFront와 S3 요금은 위 숫자에 없습니다. 정적 파일
3개(510줄, 합쳐서 21,955 바이트)라 전송량 요금은 무시할 수준이지만, 그렇다고 0은
아닙니다. 그리고 차가운 상태의 첫 요청은 4.1초로 실측되었고 그 요청은 위 계산의
0.2 GB-초가 아니라 0.5 × 4.1 = 2.05 GB-초입니다. 한 사람이 하루 한 번 처음 열 때만
생기므로 열 명 × 20일 = 200건, 200 × 2.05 × $0.0000166667 = $0.0068로 무시할 수
있습니다.

---

## 15.8 만드는 순서

1. **모자란 route 4개를 서버에 먼저 넣습니다.** 화면이 없는 데이터를 그리려다 다시
   고치는 일을 막습니다.
2. **화면 2(작업 목록)와 화면 3(작업 상세).** 이 둘이 도구의 값어치 전부입니다.
3. **화면 4(제출).** 지금 있는 것을 3단계로 나눕니다.
4. **화면 1(홈)과 화면 5(팀).** 앞의 것들이 돌아간 다음에 얹습니다.
5. **팀 선택 상자.** 팀이 둘 이상인 사용자가 생기기 전까지는 이름만 보여 줍니다.

지금 있는 `ui/` 3개 파일은 화면 2, 3, 4, 5의 초안이 이미 들어 있으므로 버리지 않고
고쳐 씁니다.

---

## 15.9 시각 설계

15.5까지는 어떤 데이터를 어느 화면에 놓을지만 정했습니다. 이 절은 그것을 **어떻게
보이게 할지**를 정합니다. 값을 여기서 확정해 두는 이유는, 화면을 만들 때마다 색과
글자 크기를 즉석에서 고르면 다섯 화면이 서로 다른 물건처럼 보이기 때문입니다.

### 색

의미를 나르는 색과 강조색을 **분리합니다.** 강조색이 곧 "실행 중"을 뜻하게 만들면,
단추와 상태를 같은 색으로 칠하게 되어 둘을 구별할 수 없습니다.

| 쓰임 | 밝은 화면 | 어두운 화면 |
|---|---|---|
| 바탕 | `#F6F7F8` | `#14171A` |
| 판(카드, 표) | `#FFFFFF` | `#1C2024` |
| 본문 글자 | `#1A1D21` | `#E8EAED` |
| 흐린 글자 (설명, 단위) | `#6B7280` | `#9AA0A6` |
| 경계선 | `#E3E6E8` | `#2C3136` |
| **강조 (단추, 링크, 선택된 탭)** | `#116466` | `#3FA6A8` |

상태색은 강조색과 겹치지 않게 넷만 씁니다.

| 상태 | 색 | 어디에 |
|---|---|---|
| 성공 (`Succeeded`) | `#15803D` | 배지, 팀 표의 `succeeded` |
| 실행 중 (`Running`, `Starting`) | `#B45309` | 배지, 진행률 막대 |
| 실패 (`Failed`) | `#B91C1C` | 배지, 행 왼쪽 3px 띠 |
| 대기 (`Pending`) | `#6B7280` | 배지 |

`Recovering`은 실행 중과 같은 색을 쓰되 배지 글자를 "재시작 중"으로 다르게 적습니다.
색을 하나 더 늘리는 것보다 글자가 정확합니다.

**어두운 화면 처리.** 색은 전부 `:root`의 CSS 변수로 정의하고, 밝은 화면 값이 기본입니다.
`@media (prefers-color-scheme: dark)`에서 변수만 다시 정의합니다. 변수를 거치지 않고
색을 직접 적는 규칙은 만들지 않습니다. 그렇게 하면 한쪽 화면에서만 글자가 안 보이는
결함이 생깁니다.

### 글꼴

두 벌만 씁니다. Google Fonts에서 받습니다.

| 벌 | 어디에 | 왜 |
|---|---|---|
| **IBM Plex Sans KR** | 모든 본문, 제목, 단추, 표의 글자 열 | 화면 글자는 영문이지만 작업 이름은 한글일 수 있습니다. 이 글꼴은 라틴 문자와 한글이 한 가족으로 설계되어 있어서, 한 벌로 둘 다 되고 한 줄에 섞여도 높이가 어긋나지 않습니다. |
| **IBM Plex Mono** | 작업 ID, 숫자, 로그, GPU 이름, 명령어 | 숫자 폭이 고정이라 표에서 자릿수가 맞습니다. |

글자 크기는 다섯 단계로 고정합니다: 12 / 13 / 15 / 20 / 28 px. 이 밖의 크기는 쓰지
않습니다. 숫자가 세로로 늘어서는 모든 자리에는 `font-variant-numeric: tabular-nums`를
겁니다.

### 배치

```
+--------------------------------------------------------------+
| ddpsrun   Home  Jobs  Submit  Team              [team]  [Sign out] |  <- 56px
+--------------------------------------------------------------+
|                                                              |
|   content, centred, max 1180px                               |
|                                                              |
+--------------------------------------------------------------+
```

- 네비게이션은 **위쪽 한 줄**입니다. 항목이 5개뿐이라 Kubeflow처럼 왼쪽 세로 막대를
  두면 표에 쓸 가로 폭만 200px 잃습니다.
- 간격은 4px의 배수만 씁니다: 4 / 8 / 12 / 16 / 24 / 32 / 48.
- 형제 요소의 간격은 각자의 `margin`이 아니라 부모의 `gap`으로 줍니다. `margin`은 서로
  합쳐지거나 두 배가 되어서, 어느 규칙이 이겼는지 나중에 알아낼 수 없습니다.
- 가로로 넘치는 것(표, 로그, 그래프)은 각자 `overflow-x: auto`인 상자 안에서 넘칩니다.
  본문 전체가 가로로 스크롤되지 않습니다.

### 상태를 색 말고 형태로도 드러내기

색만으로 상태를 나르면 색을 구별하기 어려운 사람에게는 아무 정보가 없습니다. 그래서
상태는 항상 **세 가지가 함께** 갑니다.

1. 배지 안의 **점** (색)
2. 배지 안의 **글자.** 번역하지 않고 phase 이름을 그대로 적습니다:
   `Succeeded`, `Compared`, `Running`, `Starting`, `Recovering`, `Pending`,
   `Failed`. `kubectl get pacsjobs` 가 찍는 문자열과 같아야 독자가 화면과
   클러스터를 연결할 수 있습니다.
3. 실패한 행에만 붙는 **왼쪽 3px 띠**

진행률도 마찬가지로 막대와 숫자를 함께 적습니다 (`43% (86/200 step)`).

---

## 15.10 각 화면에서 할 수 있는 행동

"무엇을 할 수 있는지 명확히 보인다"는 것은, 화면마다 **주 행동이 하나 있고 그것이 가장
눈에 띄는 단추**라는 뜻입니다. 화면별로 정해 둡니다.

| 화면 | 주 행동 (강조색 단추 1개) | 곁 행동 (테두리만 있는 단추) |
|---|---|---|
| Home | **New job** | 진행 중 작업 줄을 눌러 상세로 |
| Jobs | **New job** | 탭 전환, 행을 눌러 상세로 |
| Job detail | **Run again** | Open in a tab, Copy, Back to jobs |
| Submit 1단계 | **Validate** | Clear |
| Submit 2단계 | **See the cost** (오류가 있으면 눌리지 않음) | Back |
| Submit 3단계 | **Submit job** | Back |
| Team | 없음 | 사람 이름을 눌러 그 사람 작업만 보기 |

팀 화면은 주 행동이 없습니다. 읽기만 하는 화면이고, 없는 단추를 억지로 만들지
않습니다.

**화면 글자는 전부 영문입니다.** 제목, 열 머리글, 단추, 안내 문구 모두입니다.
상태 배지만은 번역이 아니라 phase 이름 그대로라는 점이 위와 다릅니다. 작업 이름은
사용자가 한글로 지을 수 있으므로 (그래서 `ddpsrun.io/display-name` annotation 이
있습니다) 글꼴은 한글을 담은 IBM Plex Sans KR 을 씁니다.

**빈 화면도 행동을 안내합니다.** 작업이 하나도 없을 때 표를 비워 두지 않고
"You have not submitted a job yet." 와 **New job** 단추를 같은 자리에 놓습니다.
처음 들어온 사람이 가장 먼저 보는 화면이 이것입니다.

**단추의 글자는 눌렀을 때 일어나는 일을 적습니다.** "OK" 나 "Next" 라고 적지
않습니다. 제출 2단계의 단추는 "Next" 가 아니라 "See the cost" 이고, 오류가 있으면
"Fix 2 errors first" 로 바뀌면서 눌리지 않습니다.

**오류는 무엇이 잘못됐고 어떻게 고치는지를 함께 적습니다.** `ValidateResponse.findings`가
이미 `message`와 `fix` 두 필드를 가지고 있으므로 (`server/ddpsrun_server/models.py`의
`FindingView`), 화면은 둘을 모두 보여 줍니다. `fix`가 비어 있으면 `message`만 적습니다.

---

## 15.11 만들면서 찾은 결함 셋

설계대로 화면을 만든 다음 실제 클러스터의 PacsJob 24건을 통과시켰더니, 문서를 쓸 때는
보이지 않던 것 셋이 나왔습니다. 셋 다 고쳤고 그 근거를 남깁니다.

### 1. `Compared` 를 진행 중으로 분류하고 있었습니다

15.5 를 쓸 때 끝난 phase 를 `Succeeded` 와 `Failed` 둘로 잡았습니다. PacsJob 이
정의하는 phase 는 일곱 개이고 (`api/v1alpha1/pacsjob_types.go:64-69`, `85`), 그중
`Compared` 는 mode=compare 인 작업이 후보를 전부 가격만 매기고 아무것도 사지 않은
상태입니다. 끝난 상태이고 실패가 아닙니다.

실측: 2026-09-01 기준 클러스터의 24건 중 **12건이 `Compared`** 였고, 고치기 전에는
`?phase=active` 가 12건을 돌려주었습니다. 고친 뒤에는 0건, `?phase=finished` 가
24건입니다. 화면에서는 "비교만"이라는 글자를 붙인 성공색 배지로 나옵니다.

### 2. 끝난 작업이 "64시간 42분 대기"로 보였습니다

경과 열을 "`startedAt` 이 있으면 실행 시간, 없으면 지금까지 기다린 시간" 두 갈래로
만들었는데, 세 번째 경우를 빠뜨렸습니다. **이미 끝났는데 `startedAt` 이 없는 작업**입니다.

이 경우가 실제로 존재하는 이유는 이렇습니다. `startedAt` 과 `finishedAt` 을 찍는
controller pod 는 `2026-09-01T00:06:53Z` 에 시작했고, 클러스터에서 가장 최근 PacsJob 은
`2026-08-29T15:23:25Z` 에 만들어졌습니다. `PACSRUN-JOB-CLOCK` 은 phase 가 바뀔 때 한 번만
찍고 소급하지 않으므로, 24건 전부가 영원히 `startedAt` 없이 남습니다.

고치기 전에는 며칠 전에 성공한 `aiops-exp2` 가 "64시간 42분 대기"로 나왔습니다. 지금은
그 자리에 `-` 를 적고, 마우스를 올리면 "이 작업이 끝난 뒤에 시각 기록이 추가되어 남아
있지 않습니다"가 뜹니다.

### 3. 누를 수 없는 줄이 눌리는 것처럼 보였습니다

`kubectl` 로 직접 만든 PacsJob 은 `ddpsrun-<12 hex>` 이름 규칙을 따르지 않아서 job id 를
뽑을 수 없습니다. 24건 전부가 그렇습니다. 그런 줄은 눌러도 갈 상세 화면이 없는데 손
모양 커서와 hover 색은 그대로였습니다.

지금은 그런 줄에서 `click` 클래스를 빼고, ID 칸에 "직접 만든 작업"이라고 적습니다.

**여기서 배운 것.** 설계 문서만 보고 만들면 이런 것이 안 잡힙니다. 셋 다 실제 객체
24건을 통과시켰을 때 나왔고, 셋 다 "화면이 거짓말을 하는" 종류였습니다.

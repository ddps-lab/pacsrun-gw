# kubectl 없는 사용자를 위한 제출 인터페이스 (조사와 설계, 2026-08-30)

`docs/multi-user-submit-design.md`(2026-08-24)가 "cluster + 얇은 CLI" 한 방향만 놓고 쓴 문서다.
이 문서는 그 뒤 실제 실험 6건을 돌리며 드러난 함정을 반영해 **방향을 세 개로 다시 벌리고**,
Kubeflow와 SkyPilot이 같은 문제를 어떻게 풀었는지 조사한 결과를 함께 적는다.

읽는 순서: 1절이 문제, 2~3절이 조사, 4절이 비교, 5절이 권고, 6절이 agent skill 설계다.

---

## 1. 문제 — 사용자가 아는 것과 모르는 것

우리 실험 6건은 전부 **우리가** yaml을 쓰고 `kubectl apply`로 넣었다. 연구원은 md 문서 하나를
줬을 뿐이다. 그 md에서 우리가 손으로 메워야 했던 것이 여덟 가지였고, 전부 **연구원이 알 수
없는 종류**였다.

| 메운 것 | 왜 연구원이 몰랐나 |
|---|---|
| 저장소 경로 접두어 `dpo-training/` | 자기 기계에서는 그 위치에서 실행했으니 필요 없었다 |
| `--lora`의 `/root/ab` | 문서 28줄에만 있고 저장소 어디에도 정의가 없다 |
| 파일 절대 경로 | 자기 기계에서는 같은 폴더에 있었다 |
| 산출물 5개 중 3개 | 3절 명령이 만들지 않는다. 로그, 채점 출력, pip freeze |
| `PYTORCH_CUDA_ALLOC_CONF` | 자기 실행 체인에 있었는데 공유 때 빠졌다 (aiops OOM의 직접 원인) |
| `PATCH_TRL` 적용 범위 | 처음엔 market 전용이라 적었다가 8/29에 전 작업 권장으로 바뀌었다 |
| GPU VRAM 여유 | 자기 기계가 80GB였다. 48GB에서 터질 줄 몰랐다 |
| 12시간 자격증명 한계 | PACSrun 내부 사정이라 알 길이 없다 |

**핵심은 이것이다. 사용자는 자기 코드와 자기가 돌려 본 명령만 안다.** 실행 환경, vendor,
자격증명, 산출물 규약, GPU 용량 산정은 전부 우리 쪽 지식이다. 인터페이스는 그 경계에 선다.

그리고 이번에 하나 더 늘었다. **사용자는 kubectl이 없다.** 우리 연구실 IAM user는 있지만
cluster 접근은 없다.

---

## 2. Kubeflow 조사

### 2-1. 구조

Kubeflow는 kubernetes 위에 얹은 **모듈 묶음**이다. 하나의 제품이 아니라 컴포넌트 여럿이
kubernetes control plane을 통해 협력한다.

| 컴포넌트 | 역할 |
|---|---|
| Kubeflow Pipelines (KFP) | 워크플로 정의와 실행 |
| Kubeflow Trainer (구 Training Operator) | 분산 학습, LLM fine-tuning |
| Katib | hyperparameter tuning, NAS |
| Notebooks | 대화형 개발 |
| Spark Operator | 데이터 준비 |
| Central Dashboard | 컴포넌트 UI를 한곳에 모으는 hub |

### 2-2. 사용자 인터페이스가 두 갈래다 — 이것이 우리에게 중요하다

**갈래 A — KFP: 별도 API 서버에 HTTP로 붙는다.**

```python
import kfp
client = kfp.Client(host="http://localhost:3000")
```

`host`가 필수다. cluster 안이면 in-cluster DNS를 쓰고, 밖이면 주소를 준다. 인증은 넷을
지원한다. in-cluster ServiceAccount token, 직접 넘기는 token, 사용자 정의 credential,
OAuth/IAP(자격증명을 `$HOME/.config/kfp/credentials.json`에 저장).

cluster 밖에서 붙을 때 커뮤니티 배포판은 Dex(OIDC)를 거친다. 문서가 제시하는 방법은
port-forward다.

```bash
kubectl port-forward --namespace istio-system svc/istio-ingressgateway 8080:80
```

```python
kfp_client_manager = KFPClientManager(
    api_url="http://localhost:8080/pipeline",
    skip_tls_verify=True,
    dex_username="user@example.com",
    dex_password="12341234",
    dex_auth_type="local",
)
kfp_client = kfp_client_manager.create_kfp_client()
```

**즉 "kubectl 없이"가 완전히 달성되지는 않는다.** 문서의 표준 경로가 port-forward라
kubectl이 필요하다. ingress를 열면 없앨 수 있지만 그건 배포자의 몫이다.

**갈래 B — Trainer: kubernetes API에 직접 붙는다.**

```python
from kubeflow.trainer import TrainerClient, CustomTrainer

job_id = TrainerClient().train(
    trainer=CustomTrainer(
        func=train_pytorch,
        num_nodes=4,
        resources_per_node={"cpu": 3, "memory": "16Gi", "gpu": 1},
    )
)
```

`host`가 없다. **kubeconfig를 암묵적으로 쓴다.** 별도 API 서버가 없고 CRD(`TrainJob`)에
대한 타입 있는 wrapper일 뿐이다.

조회도 같은 방식이다.

```python
for s in TrainerClient().get_job(name=job_id).steps:
    print(f"Step: {s.name}, Status: {s.status}, Devices: {s.device} x {s.device_count}")
for logline in TrainerClient().get_job_logs(job_id, follow=True):
    print(logline)
```

### 2-3. Kubeflow에서 가져올 것과 버릴 것

- **가져올 것**: `TrainingRuntime`이라는 개념. 실행 환경을 미리 정의해 두고 사용자는
  이름으로 고른다. 우리로 치면 "이미지 + venv 구성 + 산출물 규약"을 묶은 것이다.
  사용자가 `runtime="dpo-qwen3"`만 고르면 우리 함정 여덟 개 중 넷이 사라진다.
- **가져올 것**: SDK가 로그 streaming과 상태 조회를 같은 객체에서 제공하는 것. 사용자가
  `kubectl logs`를 배울 필요가 없다.
- **버릴 것**: Dex/Istio 스택. 우리는 사용자가 5명 안쪽이고 같은 AWS 계정 IAM user다.
  OIDC provider를 세울 이유가 없다.
- **버릴 것**: Central Dashboard 같은 UI. 만드는 비용 대비 얻는 것이 적다. 우리 사용자는
  터미널을 쓴다.

---

## 3. SkyPilot 조사 — 우리에게 더 가까운 참고

SkyPilot은 이 저장소가 read-only 참고로 두고 있는 시스템이고, **우리가 하려는 것과 모양이
거의 같다.** CLI 하나로 여러 vendor에서 기계를 빌려 job을 돌린다.

### 3-1. CLI와 API 서버가 분리되어 있다

`sky` CLI는 kubernetes를 모른다. **HTTP로 자기 API 서버에 말한다.** 서버의 라우트가
`sky/server/server.py`에 있다.

```
post('/validate'      post('/optimize'     post('/launch'      post('/exec'
post('/stop'          post('/status'       post('/down'        post('/upload'
post('/list_accelerators'                  post('/realtime_kubernetes_gpu_availability'
get('/api/v1/auth/token'                   post('/api/v1/auth/authorize'
```

`/validate`와 `/optimize`가 `/launch`와 나란히 있는 것이 눈에 띈다. **제출 전에 검증하고
비용을 미리 계산하는 것이 일급 기능이다.**

### 3-2. 비동기다

`sky/client/sdk.py`의 `launch()`가 `RequestId`를 돌려주고, 클라이언트가 그걸로 결과를
streaming한다. 25시간짜리 job에 필요한 모양이다.

```python
request_id = sky.status()
statuses = sky.get(request_id)
```

### 3-3. 인증이 service account token이다

`sky/client/service_account_auth.py`가 token을 두 곳에서 찾는다. 환경변수, 그리고
`~/.sky/config.yaml`의 `api_server.service_account_token`. OAuth2 proxy도 선택지로 있다
(`sky/server/auth/oauth2_proxy.py`).

**kubeconfig가 필요 없다.** 사용자는 token 한 줄만 받는다.

### 3-4. 이미 agent skill이 있다

`agent/skills/skypilot/`에 있고 우리가 만들려는 것의 완성된 예다.

```
SKILL.md                        트리거 문장 + 행동 규칙
references/yaml-spec.md         1,640줄  (자동 생성)
references/cli-reference.md       797줄  (자동 생성)
references/python-sdk.md        1,513줄  (자동 생성)
references/examples.md          1,999줄  (사람이 씀)
references/advanced-patterns.md 1,455줄  (사람이 씀)
references/troubleshooting.md   1,089줄  (사람이 씀)
scripts/generate_references.py  RST/Click/AST 에서 앞의 셋을 다시 만든다 (LLM 없음)
```

**자동 생성 셋과 사람이 쓴 셋으로 나뉜다.** 문법은 코드에서 뽑고, 함정과 예시는 사람이
쓴다. 우리도 이 경계를 그대로 쓸 수 있다. `pacsrun.io_pacsjobs.yaml`에서 spec 문법을 뽑고,
함정은 우리가 겪은 것을 쓴다.

SKILL.md의 frontmatter `description`이 **트리거 표면**이다. 어떤 문장을 만나면 이 skill을
쓸지가 거기 적힌다.

---

## 4. 방향 셋과 비교

### 방향 1 — 얇은 CLI가 kubernetes API를 직접 부른다 (Kubeflow Trainer 방식)

사용자에게 kubeconfig를 주고, CLI가 그것으로 CRD를 만든다.

- **되는 것**: 서버를 새로 세우지 않는다. `docs/multi-user-submit-design.md`가 이 방향이고
  이미 terraform 설계까지 있다. EKS access entry에 `kubernetes_groups`만 주고 policy는
  안 붙이는 것까지 정해 뒀다.
- **안 되는 것**: 사용자가 kubeconfig를 갖게 된다. `aws eks update-kubeconfig`를 돌려야
  하고, 그러면 `kubectl`도 사실상 딸려 온다. "kubectl 없이"라는 목표와 어긋난다.
- **안 되는 것**: cluster를 껐다 켜면 사용자 쪽 설정이 깨진다. 우리는 실제로 노드를
  내렸다 올린다.

### 방향 2 — CLI + 우리 API 서버 (SkyPilot 방식)

CLI가 HTTP로 우리 서버에 말하고, 서버만 cluster를 만진다.

- **되는 것**: 사용자에게 kubeconfig도 kubectl도 없다. token 한 줄이다.
- **되는 것**: cluster 내부 사정을 전부 감출 수 있다. namespace, ServiceAccount,
  fetch mode, `PACSRUN_*` 환경변수 이름이 밖으로 안 나간다.
- **되는 것**: `/validate`와 `/estimate`를 넣어 **제출 전에 비용과 시간을 답할 수 있다.**
  우리 실험에서 이게 가장 아쉬웠다. 9.14시간이라고 계산한 것이 17.87시간이었다.
- **비용**: 서버를 세우고 지켜야 한다. 인증, TLS, 가용성. 그리고 EKS 밖에 두면 또 다른
  기계값이 든다.

### 방향 3 — 방향 2 + code agent skill

CLI와 서버를 만들되, **yaml과 학습 script를 사람이 쓰지 않는다.** 사용자가 자기 저장소에서
code agent에게 말하면 agent가 skill을 읽고 만들어 제출한다.

- **되는 것**: 우리 함정 여덟 개를 skill 문서가 흡수한다. 사용자는 "내 repo의 이 script로
  이 데이터 학습해줘"라고만 한다.
- **되는 것**: agent가 GPU 크기와 spot/on-demand를 추천할 수 있다. 근거를 skill에 적어 두면
  된다.
- **비용**: skill을 유지해야 한다. SkyPilot 기준 8,493줄이고 그중 절반이 자동 생성이다.

---

## 5. 권고

**방향 3으로 가되 단계를 나눈다.**

### 5-1. 이름

`pacsrun`과 `kubepacs`는 내부 이름이라 사용자에게 노출하지 않기로 한 방침이 있다
(`docs/multi-user-submit-design.md` 5a절). CLI 이름 후보는 이렇다. **아직 정하지 않았다.**

- `labrun` — 연구실에서 job을 돌린다는 뜻. 짧고 충돌이 적다
- `ddpsrun` — 연구실 이름을 앞에 둔다
- `gpurun` — 하는 일이 바로 드러난다. 다만 일반명사라 충돌 위험

### 5-2. 서버가 제공할 라우트 (SkyPilot을 따른다)

| 라우트 | 하는 일 | 왜 필요한가 |
|---|---|---|
| `POST /validate` | yaml과 script를 검사만 한다 | 우리가 손으로 메운 함정 여덟 개를 여기서 잡는다 |
| `POST /estimate` | 시간과 비용을 답한다 | 예산이 $34.98일 때 걸어도 되는지 미리 안다 |
| `POST /submit` | job을 만든다. `request_id` 반환 | |
| `GET /status/{id}` | 진행률 | `kubectl get pacsjob`을 감춘다 |
| `GET /logs/{id}` | 로그 streaming | `kubectl logs`를 감춘다 |
| `POST /cancel/{id}` | 취소 | |
| `GET /gpus` | 지금 빌릴 수 있는 GPU와 단가 | 우리가 매번 GraphQL로 물어보던 것 |

`/validate`와 `/estimate`가 이 설계의 핵심이다. **제출 전에 답할 수 있는 것을 전부 그때
답한다.** 25시간 돌고 나서 자격증명 만료를 알게 되는 일이 없어야 한다.

### 5-3. 인증

SkyPilot과 같이 **service account token**을 쓴다. 사용자는 token 한 줄을 `~/.<name>/config.yaml`
에 넣거나 환경변수로 준다. kubeconfig도 kubectl도 필요 없다.

### 5-4. 서버를 어디에 둘 것인가

**미결정.** 후보 둘이다.

- EKS 안의 Deployment + ingress. cluster를 끄면 서버도 꺼진다. 우리는 실제로 끈다.
- EKS 밖 작은 EC2. 항상 뜬다. `t4g.micro`가 us-west-2 기준 시간당 $0.0084이니 월 $6.13이다.
  다만 cluster 자격증명을 그 기계가 들고 있어야 한다.

---

## 6. agent skill 설계

### 6-1. 무엇을 담는가

SkyPilot의 경계를 그대로 쓴다. **문법은 자동 생성, 함정은 사람이 쓴다.**

```
SKILL.md                        트리거 + 행동 규칙
references/spec.md              (자동) config/crd/pacsrun.io_pacsjobs.yaml 에서 생성
references/cli.md               (자동) CLI 의 --help 에서 생성
references/gpu-and-cost.md      (사람) GPU 고르기, 시간과 비용 추정
references/script-contract.md   (사람) 학습 script 가 지켜야 할 것
references/troubleshooting.md   (사람) 우리가 실제로 겪은 실패와 로그
```

### 6-2. `script-contract.md` 에 들어갈 것 — 전부 실측에서 나왔다

**저장소에서 경로를 찾는 법.** 문서에 적힌 경로가 저장소 실제 구조와 다를 수 있다. agent는
저장소를 읽어 확인해야 한다. 우리는 `dpo-training/` 접두어가 없어서 clone 직후 죽을 뻔했다.

**한 변수로 묶어야 하는 짝.** 학습의 출력 경로와 추론의 입력 경로가 같아야 한다. 문서에
`--out adapter_x`와 `--lora /root/ab/adapter_x`가 따로 적혀 있으면 **학습이 끝난 뒤에야**
추론이 어댑터를 못 찾는다. agent는 이런 짝을 찾아 한 변수로 만들어야 한다.

**명령이 만들지 않는 산출물.** 요구된 산출물 목록과 명령의 출력을 대조한다. 우리 경우
다섯 중 셋(학습 로그, 채점 출력, pip freeze)을 script가 따로 만들어야 했다.

**중간 확인점을 앞에 둔다.** clone 직후 파일 줄 수를 찍는다. 학습 시작 직후 표본 수를
찍는다. 25시간 돌고 나서 파일이 틀렸음을 아는 것보다 낫다.

**어느 단계에서 죽어도 그때까지를 올린다.** `trap ... EXIT`. 학습이 25시간이고 추론이
1시간이면, 추론에서 죽었을 때 25시간을 잃으면 안 된다.

**학습이 끝나면 즉시 어댑터를 올린다.** 추론을 기다리지 않는다.

### 6-3. `gpu-and-cost.md` 에 들어갈 것 — 실측표

**스텝 수는 계산으로 나온다.** 학습 script의 `gradient_accumulation_steps`를 읽는다.

```
스텝 = ceil(학습쌍 ÷ (per_device_train_batch_size × gradient_accumulation_steps)) × 에폭
```

우리 실행 6건에서 전부 정확히 맞았다. 564->284, 675->340, 973->488, 1061->532, 742->372,
3311->1656.

**시간은 토큰 처리량으로 추정한다.** 학습 로그의 `num_tokens ÷ train_runtime`이다.

| GPU | 캡 | 토큰/초 | 근거 |
|---|---|---|---|
| L40S | 12288 | 1,474 ~ 1,505 | telecom 2건, bank 2건 |
| L40S | 18432 | 1,000 | market 실험2 |
| A100-SXM4-80GB | 12288 | 1,682 | AIOps 실험1 |

**같은 GPU, 같은 캡을 이미 잰 조합에서는 폭이 2.1%다. 조건이 하나라도 바뀌면 두 배까지
빗나간다.** market을 9.14시간으로 봤는데 17.87시간이었다. **그래서 agent는 추정을 답으로
내지 말고 "처음 5스텝을 재서 다시 계산한다"를 계획에 넣어야 한다.**

**VRAM은 캡으로 정한다.** 터지는 것은 attention이 아니라 마지막 logits이다.

```
logits 크기 = 2(chosen/rejected) × 문장길이 × vocabulary × 2바이트
Qwen3-4B 는 vocabulary 151,936 이므로 토큰 하나가 297 KiB
```

| 한 행 길이 | logits |
|---|---|
| 4,000 | 2.26 GiB |
| 8,000 | 4.53 GiB |
| 11,926 | 6.75 GiB  <- L40S 48GB 에서 터진 값 |
| 18,432 | 10.43 GiB |

**48GB에서 실사용 가능량이 44.39 GiB다.** 여유를 보려면 캡에서 나온 logits 크기에 모델과
activation을 더해 비교한다. 확신이 없으면 80GB로 간다. 80GB는 시간당 $1.59로 48GB의
$0.99보다 1.61배 비싼데, 우리 측정에서 A100이 L40S보다 1.61배 빨랐다. **거의 정확히
상쇄되므로 VRAM이 빠듯하면 큰 쪽이 손해가 아니다.**

**메모리 완화 수단 둘을 먼저 쓴다.** `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`
(단편화 방지, 학습 결과 무변경)와 logits를 답변 구간만 만드는 패치다. 이 둘로 L40S 48GB에서
AIOps가 돌았고, 스텝 시간도 85.90초에서 67.15초로 21.8% 줄었다.

### 6-4. spot 과 on-demand — 지금 사실대로 적어야 한다

**RunPod은 spot을 팔지 않는다.** decider가 `capacityType != "on-demand"`면 카탈로그를 읽기
전에 거절한다(`pkg/decider/runpod/decider.go:242`). 그리고 PACSrun의 기본값은 spot이다
(`pkg/decider/decider.go:287-288`). **즉 `capacityType: on-demand`를 안 적으면 RunPod이
후보에서 빠진다.** agent가 반드시 채워야 하는 자리다.

spot을 쓸 수 있는 것은 CSV로 가격이 매겨진 vendor(AWS, GCP)뿐이다
(`pkg/decider/skycatalog/decider.go`).

**추천 규칙은 이렇게 적는다.**

| 조건 | 권고 |
|---|---|
| RunPod을 쓴다 | `on-demand` 고정. 선택지가 없다 |
| 4시간 미만, checkpoint 없음 | spot 가능. 잃어도 다시 돌리면 된다 |
| 4시간 이상, checkpoint 있음 | spot 가능. 복구가 성립한다 |
| 4시간 이상, checkpoint 없음 | **on-demand.** 20시간째에 뺏기면 전부 잃는다 |

우리 AIOps 실험1은 25.29시간이었다. checkpoint 없이 spot으로 돌렸다면 도박이었다.

### 6-5. `troubleshooting.md` 에 들어갈 것

전부 우리가 겪은 것이고 로그가 있다.

- **`empty_strided_cuda((2, s, 151936))` + OutOfMemoryError** — 캡에 가까운 긴 표본 하나가
  원인이다. 요청 바이트를 `2 × 151936 × 2`로 나누면 몇 토큰짜리인지 나온다.
- **`status=RUNNING runtime=null`이 30분** — RunPod의 `status`는 준비 신호가 아니다.
  `runtime`이 null을 벗어나는 것만 신호다. 같은 물리 서버에 job이 몰리면 image pull이
  느려진다. **여러 job을 동시에 제출하지 말고 하나가 준비된 뒤 다음을 건다.**
- **`Field "objectMounts" is not defined by type "PodFindAndDeployOnDemandInput"`** — vendor
  API 쪽 고장이다. 우리 요청과 무관하다.
- **job이 `Succeeded`인데 S3가 비어 있다** — 12시간 자격증명이 만료된 뒤 업로드가 실패했고
  script가 그 실패를 삼켰다. 12시간을 넘는 학습은 fetch mode가 필요하다.
- **`AccessDenied ... CreateMultipartUpload`** — fetch mode에서는 정상이다. 원격은 읽기
  전용이고 driver가 대신 올린다.

---

## 7. 아직 정하지 않은 것

1. **CLI 이름.** `labrun` / `ddpsrun` / `gpurun` 중 하나, 또는 다른 것.
2. **API 서버를 어디에 둘 것인가.** EKS 안이면 cluster를 끌 때 같이 꺼진다.
3. **`/estimate`가 어디까지 답하는가.** 스텝 수는 계산으로 나오지만 스텝 시간은 재본
   조합에서만 믿을 수 있다. 재본 적 없는 조합에 대해 무엇을 답할지 정해야 한다.
4. **UI를 만들 것인가.** 지금 판단은 만들지 않는 쪽이다. 사용자가 5명 안쪽이고 터미널을
   쓴다. 다만 결과를 보는 페이지 하나는 값어치가 있을 수 있다.
5. **skill의 자동 생성 범위.** CRD에서 spec 문법을 뽑는 것은 확실하다. CLI `--help`도
   가능하다. 그 이상은 사람이 쓴다.
6. **`--resume-from-checkpoint`.** 학습 script가 아직 지원하지 않는다. 긴 학습의 복구가
   여기 걸려 있고, script 소유자에게 요청해 둔 상태다.

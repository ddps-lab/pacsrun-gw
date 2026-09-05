# API — 라우트와 요청 본문 (2026-08-31)

## 라우트

| 메서드 | 경로 | 하는 일 |
|---|---|---|
| POST | `/v1/uploads` | script 와 데이터를 올릴 presigned URL 발급 |
| POST | `/v1/validate` | 요청과 script 를 검사만 한다. 제출하지 않는다 |
| POST | `/v1/estimate` | 시간과 비용을 범위로 답한다. GPU 를 권한다 |
| POST | `/v1/jobs` | 제출한다. `job_id` 반환 |
| GET | `/v1/jobs` | 내 작업 목록 |
| GET | `/v1/jobs/{id}` | 상태, 진행률, **갱신된 예상 시간**, GPU 지표 |
| GET | `/v1/jobs/{id}/logs?follow=true` | 로그 streaming |
| GET | `/v1/jobs/{id}/artifacts` | 산출물 목록과 내려받기 URL |
| DELETE | `/v1/jobs/{id}` | 취소 |
| GET | `/v1/gpus` | 지금 빌릴 수 있는 GPU 와 단가 |
| GET | `/v1/explain` | 이 도구가 무엇이고 어떻게 쓰는지 (agent 용) |
| GET | `/v1/schema` | 제출 본문의 형식 (agent 용) |

`/validate` 와 `/estimate` 가 `/jobs` 와 나란히 있는 것이 이 설계의 핵심이다. SkyPilot 도
`/validate` 와 `/optimize` 를 `/launch` 와 나란히 둔다 (`sky/server/server.py`).

`/explain` 과 `/schema` 는 **agent 가 문서를 안 읽고도 쓸 수 있게 하는 자리**다. Claude 든
Codex 든 셸을 쓸 수 있으면 통한다.

## 제출 본문 — 사용자가 아는 것만 받는다

```json
{
  "name": "bank-exp2v2",
  "image": "runpod/pytorch:1.1.0-rc.154-cu1281-torch291-ubuntu2404",
  "script": "s3://.../_uploads/run.sh",
  "env": { "PAIRS": "train_exp2_bank_v2.jsonl", "EVAL": "v3_eval_bank.jsonl",
           "ML": "12288", "MP": "11264", "EPOCHS": "4", "PATCH_TRL": "on" },
  "secrets": ["GITHUB_PAT"],
  "gpu": { "vram_gb": 48, "count": 1 },
  "expected_hours": 8
}
```

**여기에 없는 것이 중요하다.** 서버가 채운다.

| 서버가 채우는 것 | 무엇으로 | 왜 |
|---|---|---|
| `namespace` | token 에서 유도 | 남의 namespace 를 못 적게 |
| `serviceAccountName` | 그 namespace 의 고정값 | 사용자가 알 필요 없음 |
| `resultPath` | `s3://<bucket>/pacsrun/<namespace>/<name>/` | 남의 폴더에 못 쓰게 |
| ~~`placement.capacityType`~~ | **2026-09-01 에 빠짐. 이제 요청이 들고 온다** | 아래 참고 |
| `placement.regions` | 가용성과 가격으로 판단 | |
| `parallelism` | 1 (지금은 고정) | |
| fetch mode | `expected_hours > 11` 이면 켬 | STS 12 시간 한계 |
| `SAVE_CKPT` | `expected_hours > 6` 이면 켬 | 중단 대비 |

### capacityType 은 서버가 정하지 않는다 (2026-09-01)

한동안 서버가 `/v1/estimate` 의 판단으로 이 값을 채웠다. **그러면 서른 시간짜리 job 이
회수 가능한 자원 위에 올라가는데도 아무도 묻지 않은 것이 된다.**

지금은 요청에 `capacity_type` 이 있고 **없으면 400 으로 거절한다.** 판단 자체는 그대로
`/v1/estimate` 가 이유와 함께 답하고, 그 답이 사람이나 agent 를 거쳐 요청에 실린다.

```
$ ddpsrun estimate ...
  구매 방식   on-demand   <- 제출할 때 --capacity-type on-demand 로 넣으십시오
              RunPod does not sell spot. ...

$ ddpsrun submit ... --capacity-type on-demand
```

비워 두면 안 되는 이유는 그대로다. 빈 값은 spot 을 뜻하고
(`pkg/decider/decider.go:331`), RunPod decider 는 on-demand 가 아니면 catalog 를 읽기 전에
거절한다 (`pkg/decider/runpod/decider.go:242`). **job 은 돌지만 RunPod 에서만 안 돈다.**

**지금은 사용자가 yaml 에 `resultPath` 를 직접 쓴다.** 그래서 남의 prefix 를 적을 수 있다.
이것이 다중 사용자로 갈 때 반드시 막아야 하는 자리다.

## PacsJob 의 실제 spec 필드

서버가 만들 대상이다. `config/crd/pacsrun.io_pacsjobs.yaml` 에서 뽑았다.

```
  args                 array
  command              array
  env                  array
  image                string          <- required 는 이것 하나
  parallelism          integer
  placement            object { capacityType, mode, onDemandFallback, regions, vendors }
  resources            object { cpus, gpus, instanceType, memory }
  resultPath           string
  serviceAccountName   string
```

status 에서 읽을 것들이다.

```
  phase                 currentOffering       excludedOfferings
  message               recoveryCount         capacityRefusals
  completedSlots        blamedNodes           fetchFailureCount
```

## `/estimate` 응답

```json
{
  "steps": 556,
  "hours": { "low": 6.5, "high": 7.5, "confidence": "measured" },
  "cost_usd": { "low": 6.4, "high": 7.4 },
  "basis": "L40S/cap12288 에서 4회 측정, 폭 2.1%",
  "gpu": { "recommended_vram_gb": 48, "estimated_peak_gb": 4.6,
           "reason": "logits 2 x 4144 x 151936 x 2B = 2.3GB + 모델 + activation" },
  "capacity_type": "on-demand",
  "capacity_reason": "RunPod 은 spot 을 팔지 않는다",
  "warnings": []
}
```

`confidence` 가 셋이다. 자세한 것은 `04-estimate.md` 에 있다.

## `/validate` 가 잡을 것

우리가 실험 8 건에서 손으로 메운 것들이다. 전부 사용자가 알 수 없는 종류였다.

| 검사 | 근거 |
|---|---|
| 저장소 경로에 접두어가 빠졌는가 | 문서는 `runs/xxx/` 인데 실제는 `dpo-training/runs/xxx/` |
| 학습의 `--out` 과 추론의 `--lora` 가 같은가 | 다르면 학습이 끝난 뒤에야 추론이 실패 |
| 요구된 산출물을 명령이 만드는가 | 다섯 중 셋을 script 가 따로 만들어야 했다 |
| `PYTORCH_CUDA_ALLOC_CONF` 가 있는가 | 누락이 aiops OOM 의 직접 원인 |
| 캡 대비 VRAM 이 충분한가 | logits 계산으로 미리 답할 수 있다 |
| 예상 시간이 12 시간을 넘는가 | 넘으면 fetch mode 가 필요 |
| `capacityType` 이 vendor 와 맞는가 | RunPod 인데 spot 이면 후보에서 빠진다 |

## 응답 규칙 둘

**첫째, 내부 이름을 밖으로 내보내지 않는다.** `PACSRUN_*` 환경변수 이름, namespace 이름,
ServiceAccount 이름이 응답에 나오면 안 된다. 로그 중계에서도 걸러 낸다.

**둘째, `job_id` 는 우리가 만든다.** kubernetes 객체 이름을 그대로 쓰지 않는다. 그래야
나중에 이름 규칙을 바꿔도 사용자 쪽이 안 깨진다.

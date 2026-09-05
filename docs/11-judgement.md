# 3단계 — `/v1/estimate` 와 `/v1/validate` (2026-08-31)

`08-plan.md` 의 3단계입니다. **여기부터가 이 저장소의 값어치**라고 적어 두었던 부분입니다.
kube-apiserver 가 답하지 못하는 세 가지, "얼마나 걸리는가", "무엇이 필요한가",
"이 script 에 함정이 있는가" 를 여기서 답합니다.

## 만든 것

```
server/ddpsrun_server/
  measurements.py   DDPSRUN-MEASUREMENTS  실측값만. 코드 없음
  estimate.py       DDPSRUN-ESTIMATE      산술만. 숫자 없음
  validate.py       DDPSRUN-VALIDATE      우리가 손으로 메운 것들
cli/ddpsrun/
  cli.py            estimate, validate 명령 추가
```

**숫자와 산술을 파일로 갈랐습니다.** `measurements.py` 의 모든 값은 실제 실행 로그에서 왔고,
`estimate.py` 에는 값이 하나도 없습니다. 추정을 믿지 못하는 사람이 볼 곳이 한 군데뿐이게
하려는 것입니다.

## 이 파일들이 지키는 규칙 하나

**틀린 숫자는 숫자가 없는 것보다 나쁩니다.** market 실험2 를 9.14 시간으로 답했는데 실제는
17.87 시간이었습니다. 96% 틀렸고, 원인은 그 길이에서 재본 적이 없는데도 답했기 때문입니다.

그래서 `unknown` 이 정식 답입니다. 재본 범위 밖으로 나가면 숫자 대신 `unknown` 과 그 이유가
나옵니다.

## 스텝 수는 오차가 없습니다

```
스텝 수 = ceil(학습 쌍 ÷ (per_device_train_batch_size × gradient_accumulation_steps)) × 에폭
```

실행 8 건 전부 로그와 정확히 일치했습니다. `test_the_step_formula_matches_every_log_we_have`
가 여덟 건 전부를 확인합니다.

## 스텝당 시간은 토큰/초에서 나옵니다

초/스텝은 문장 길이에 따라 흔들리므로 토큰/초로 저장하고 계산할 때 되돌립니다.

```
스텝당 토큰 = batch × grad_accum × 한 행 길이 × 2      (DPO 는 좋은 답과 나쁜 답 둘)
스텝당 초   = 스텝당 토큰 ÷ 토큰당 초
```

**각 실행에 자기 자신의 실측 토큰/초를 넣으면 그 실행의 로그 값이 나옵니다.**

| 실행 | 토큰/초 | 계산 | 로그 | 오차 |
|---|---|---|---|---|
| bank-exp2v2 | 1,555 | 42.19 초/스텝 | 42.32 | 0.3% |
| aiops-exp2 | 1,357 | 66.03 | 66.44 | 0.6% |
| market-exp2 | 1,000 | 169.9 | 172.9 | 1.7% |
| aiops-exp1 | 1,682 | 53.27 | 53.49 | 0.4% |

새 job 에 답할 때는 이 중 하나가 아니라 **길이가 비슷한 실행 전부의 중간값**을 씁니다.

## `confidence` 셋이 각각 언제 나오는가

| 값 | 조건 | 답하는 것 |
|---|---|---|
| `measured` | 그 GPU 에서 길이 ±15% 안을 이미 재봤다 | 그 실행들의 폭이 그대로 범위가 된다 |
| `interpolated` | 그 GPU 를 서로 다른 길이로 2회 이상 재봤고 그 사이다 | 점 추정 ±33% |
| `unknown` | 잰 범위 밖이거나, 그 GPU 를 재본 적 없거나, 측정점이 하나뿐이다 | 범위 대신 이유 |

- `interpolated` 의 ±33% 는 임의로 정한 값이 아닙니다. **끝까지 지켜본 유일한 긴 job 인
  aiops-exp2 가 실행 중에 자기 예상을 28.80 에서 38.21 시간까지 흔들었습니다.** 맞춘 선이
  job 자신의 실시간 예상보다 안정적이라고 볼 근거가 없습니다.
- **측정점이 하나면 선을 못 긋습니다.** A100 은 실행이 하나뿐이라 그 길이를 벗어나면
  `unknown` 입니다.

## 메모리는 평균이 아니라 캡으로 계산합니다

**할당은 batch 안의 가장 긴 표본에 맞춰 일어나고, 데이터셋에서 가장 긴 표본은 `--max-len`
까지 자랍니다.** AIOps 실험1 은 평균 행 길이가 약 5,600 토큰인데 11,926 토큰짜리 버퍼에서
죽었습니다. 캡이 12,288 이었으니 97.1% 입니다.

```
최대 logits = 2(좋은 답, 나쁜 답) × 캡 × vocabulary × 2 바이트
```

vocabulary 151,936 (Qwen3-4B) 이면 토큰 하나가 297 KiB 입니다. 이 항이 전체를 지배하므로
**다른 모델은 자기 숫자를 넣어야 하고**, 그래서 인자로 받습니다.

| 캡 | 최대 logits |
|---|---|
| 12,288 | 6.96 GiB |
| 18,432 | 10.43 GiB |

`peak_logits_gib(11926)` 이 6.75 GiB 를 내는지 test 가 확인합니다. AIOps 실험1 의 실제
실패 할당값입니다.

## GPU 권고 — 아는 것만 씁니다

측정으로 아는 것이 셋뿐입니다.

- 캡 12288, 완화 수단 **없이** L40S 에서 죽었습니다 (aiops-exp1).
- 캡 12288, 완화 수단 **둘 다** 켜고 L40S 에서 완주했습니다 (aiops-exp2). 스텝당 시간도
  85.90 초에서 66.44 초로 22.7% 빨라졌습니다.
- 캡 18432, TRL 패치를 켜고 L40S 에서 완주했습니다 (market-exp2).

**패치가 메모리를 몇 분의 일로 줄이는지는 재본 적이 없습니다.** 그래서 계산하지 않고,
저 세 실행이 뒷받침하는 규칙만 적용합니다. 완화 수단이 켜져 있으면 우리가 돌려 본 캡까지는
48GB, 그보다 길면 80GB 입니다.

## 구매 방식은 취향이 아니라 제약입니다

```
pkg/decider/decider.go:331     빈 CapacityType 은 spot 을 뜻한다
internal/controller/placement.go:202   PACSrun 의 기본값이 spot 이다
pkg/decider/runpod/decider.go:242      RunPod decider 는 on-demand 가 아니면
                                       catalog 를 읽기도 전에 거절한다
```

**즉 아무 말도 안 하면 RunPod 이 후보에서 조용히 빠집니다.** job 은 돌아갑니다. RunPod 에서만
안 돌 뿐이라 눈치채기 어렵습니다.

그래서 3단계부터 서버가 `spec.placement.capacityType` 을 씁니다. 1단계에서 placement 를
아예 안 쓴 것과 달라진 점입니다.

## `/v1/validate` 가 잡는 것 일곱

**전부 우리가 실제로 겪었고, 전부 제출하는 사람에게는 안 보이던 것들입니다.**

| code | 수준 | 무엇 |
|---|---|---|
| `secret-in-env` | error | 자격증명처럼 보이는 값이 `env` 에 평문으로 있다 |
| `gpu-too-small` | error | 캡 대비 요청한 VRAM 이 모자란다 |
| `prompt-cap-too-high` | error | `--max-prompt-len` 이 `--max-len` 이상이다 |
| `adapter-path-mismatch` | error | 추론이 읽는 어댑터와 학습이 쓰는 어댑터가 다르다 |
| `alloc-conf-missing` | warning | `PYTORCH_CUDA_ALLOC_CONF` 가 없다 |
| `trl-patch-missing` | warning | `patch_trl_liger_slice.py` 가 script 에 없다 |
| `no-exit-trap` | warning | `trap ... EXIT` 가 없어서 중간에 죽으면 다 잃는다 |
| `fetch-mode-needed` | info | 12 시간을 넘어 자기 자격증명으로 결과를 못 올린다 |

`adapter-path-mismatch` 가 특히 비쌉니다. **학습이 먼저 끝나고 나서야 추론이 실패하므로,
AIOps 라면 31 시간을 쓴 뒤에 드러납니다.**

## 못 보는 것을 적어 둡니다

`03-api.md` 의 일곱 중 셋은 문서와 저장소 실제 구조의 불일치였고, **저장소를 clone 하지 않고는
못 봅니다.** 그래서 `not_checked` 로 내보냅니다.

- script 의 경로가 저장소 실제 구조와 맞는지. 우리 문서는 `runs/xxx/` 였고 저장소는
  `dpo-training/runs/xxx/` 였습니다.
- 명령이 요구된 산출물을 실제로 만드는지. 다섯 중 셋을 wrapper 가 따로 만들어야 했습니다.
- 학습 데이터가 script 가 찾는 자리에 있는지.

**통과했다는 답이 다 봤다는 뜻으로 읽히면 안 되기 때문입니다.**

## 써 보는 모습

```
$ ddpsrun estimate --name bank-exp2v2 --image runpod/pytorch:1.1.0 \
    --gpu-vram 48 --pairs 1110 --epochs 4 --row-tokens 4100 --cap 12288
  스텝        556
  시간        6.52 ~ 6.87 시간  [measured]
  비용        $6.45 ~ $6.8
  근거        5 run(s) on L40S within 15% of 4,100 tokens per response:
              1,474-1,555 tokens/s (telecom-exp1, telecom-exp2, bank-exp1,
              bank-exp1v2, bank-exp2v2)
```

**실제 bank 실험2' 는 556 스텝, 6.54 시간, $6.47 이었습니다.**

```
$ ddpsrun validate ... --script run.sh
[막힘] adapter-path-mismatch
  inference reads adapter_bank but training writes adapter_bank_v2.
  Training would finish first and only then would inference fail.
  고치려면: use one shell variable for both --out and --lora.
```

`validate` 는 막히는 문제가 있으면 exit 1 입니다. script 에서 제출 앞에 걸 수 있습니다.

## 돌려 보다가 찾은 결함 셋

**첫째, 캡을 못 찾았는데 0 으로 계산했습니다.** 답이 "the logits buffer reaches 0.00 GiB at
cap 0" 이었고 그것을 근거로 80GB 를 권했습니다. **숫자가 나오면 믿게 되는데 그 숫자가
틀렸습니다.** 지금은 캡이 없으면 GPU 권고 자체를 하지 않고 무엇을 보내라고 말합니다.

**둘째, 실행 시간은 L40S 로 재고 비용은 권장 GPU 인 A100 단가로 매겼습니다.** $6.47 짜리
job 이 $10.37 로 나왔습니다. 57% 과대 계상입니다. 지금은 요청한 GPU 로 값을 매기고, 권고가
다르면 그것을 따로 말합니다.

**셋째, `/v1/schema` 가 실제로 받는 것과 어긋났습니다.** 라우트는 `JudgementRequest` 를 받는데
schema 는 `SubmitRequest` 를 내보내고 있어서, agent 가 `training` 과 `script` 를 알 수 없었습니다.
**생성한 schema 가 라우트와 다르면 없는 것보다 나쁩니다. 자신 있게 불완전하기 때문입니다.**

## 확인 안 된 것

1. **`interpolated` 를 실제로 검증한 적이 없습니다.** 두 측정점 사이 길이로 job 을 돌려서
   맞는지 본 적이 없습니다. 다음에 그런 job 이 생기면 그것이 첫 검증입니다.
2. **완화 수단이 메모리를 얼마나 줄이는지 모릅니다.** 켜고 껐을 때의 최대 할당량을 재면
   규칙 대신 계산으로 답할 수 있습니다.
3. **측정표가 여덟 줄뿐입니다.** L40S 여섯, A100 하나(중복 길이 제외), 그래서 A100 은
   보간이 안 됩니다.

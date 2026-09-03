# run.sh 를 만들 때 지키는 것

**전부 우리가 실험 8 건에서 손으로 메운 것들이다.** 규칙마다 그것이 어긋났을 때 실제로 무슨
일이 일어났는지 적어 두었다. 근거 없는 규칙은 여기 없다.

이 파일은 사람이 쓴다. 자동 생성되지 않는다.

---

## 1. 저장소의 실제 구조를 문서와 대조한다

연구자가 준 문서에 적힌 경로가 저장소 실제 구조와 다를 수 있다.

**실제로 있었던 일.** 문서는 `runs/gradedpairs_20260826/` 라고 했고 저장소는
`dpo-training/runs/gradedpairs_20260826/` 였다. clone 직후에 죽었을 것이다.

```bash
# clone 직후에 확인한다. 25 시간 뒤에 아는 것보다 낫다
git clone --depth 1 "$REPO" src
ls src/dpo-training/runs/*/pairs/ | head
wc -l src/.../pairs/train_exp2_bank.jsonl
```

---

## 2. 한 변수로 묶어야 하는 짝을 찾는다

학습의 출력 경로와 추론의 입력 경로는 **같아야 하는데 서로 다른 명령에 적힌다.**

```bash
# 이렇게 하지 않는다
python train_dpo_m3.py --out adapter_bank_v2
python gen_openrca_tasks_fast.py --lora /root/ab/adapter_bank      # 다르다

# 이렇게 한다
ADAPTER="adapter_${JOB}"
python train_dpo_m3.py --out "$ADAPTER"
python gen_openrca_tasks_fast.py --lora "/root/ab/$ADAPTER"
```

**어긋나면 학습이 먼저 끝나고 그 다음에 추론이 실패한다.** AIOps 는 학습만 31 시간이다.
`ddpsrun validate --script run.sh` 가 이것을 `adapter-path-mismatch` 로 잡는다.

---

## 3. 명령이 만들지 않는 산출물을 script 가 만든다

연구자가 돌려받기로 한 파일 다섯 중 **셋을 학습 명령이 만들지 않았다.**

| 파일 | 누가 만드나 |
|---|---|
| `adapter_<작업명>/` | 학습 명령 |
| `out_<작업명>.jsonl` | 추론 명령 |
| `train_<작업명>.log` | **script 가 `tee` 로 만들어야 한다** |
| `score_<작업명>.txt` | **script 가 `tee` 로 만들어야 한다** |
| `pipfreeze_<작업명>.txt` | **script 가 `pip freeze` 로 만들어야 한다** |

```bash
python train_dpo_m3.py ... 2>&1 | tee "train_${JOB}.log"
python score_openrca_corrected.py ... 2>&1 | tee "score_${JOB}.txt"
"$VENV_TRAIN/bin/pip" freeze > "pipfreeze_${JOB}.txt"
```

---

## 4. 중간 확인점을 앞쪽에 둔다

```bash
# clone 직후
echo "학습 쌍 파일 줄 수: $(wc -l < "$PAIRS")"
# 학습 시작 직후에 trainer 가 찍는 두 줄을 확인한다
#   학습 쌍 N개
#   [검증] 트레이너 최종 학습 표본 N / 투입 N     <- 둘이 같아야 탈락 0
```

**25 시간 돌고 나서 파일이 틀렸음을 아는 것보다 낫다.**

---

## 5. 데이터셋과 모델에 닿는지를 학습 전에 확인한다

**둘 다 컨테이너 밖에서 와야 하고, 둘 다 조용히 실패할 수 있습니다.** 그런데 실패가 드러나는
시점이 다릅니다.

```
학습 쌍       clone 직후에 없으면 바로 죽는다.  값이 싸다
모델          다운로드가 몇 분 걸리고, 그 뒤에 죽는다
              GPU 를 이미 빌린 뒤이므로 값이 비싸다
```

**그래서 학습 명령 앞에 확인 두 줄을 둡니다.**

```bash
# 데이터
test -s "$PAIRS" || { echo "학습 쌍 파일이 없거나 비어 있다: $PAIRS"; exit 1; }
echo "학습 쌍 파일 줄 수: $(wc -l < "$PAIRS")"

# 모델.  받아 보기 전에는 닿는지 알 수 없다
python - <<'PY'
import os
from huggingface_hub import model_info
name = os.environ["BASE_MODEL"]
info = model_info(name)                       # 없거나 권한이 없으면 여기서 죽는다
print(f"모델 확인: {name}, 파일 {len(info.siblings)}개")
PY
```

- `model_info` 는 **가중치를 받지 않고 목록만 봅니다.** 몇 초에 끝납니다.
- 이 두 줄이 통과하면 그 다음의 몇 시간짜리 학습이 데이터나 모델 때문에 죽지 않습니다.

**닿지 않는 경우가 셋이고 원인이 다 다릅니다.**

| 증상 | 원인 |
|---|---|
| `401` 또는 `403` | gated 모델이라 토큰이 필요하다. `secrets` 에 `HF_TOKEN` 을 넣는다 |
| `404` | 이름이 틀렸다. 조직명까지 정확해야 한다 |
| 연결 자체가 안 됨 | 원격이 인터넷으로 못 나가는 경우. vendor 설정 문제다 |

**저장소 안의 데이터가 Git LFS 로 관리되는 경우를 조심해야 합니다.** clone 은 되는데 파일
내용이 포인터 한 줄뿐입니다. 줄 수를 찍으면 그것이 바로 보입니다.

```bash
# LFS 포인터는 이렇게 생겼다.  줄 수가 3 이면 의심한다
version https://git-lfs.github.com/spec/v1
oid sha256:...
size 12345678
```

**결과를 올릴 자리도 같이 확인합니다.** 학습이 끝나고 나서 권한이 없다는 것을 알면 늦습니다.

```bash
echo probe | aws s3 cp - "$PACSRUN_RESULT_PATH.probe" \
  && aws s3 rm "$PACSRUN_RESULT_PATH.probe" \
  || { echo "결과 경로에 쓸 수 없다: $PACSRUN_RESULT_PATH"; exit 1; }
```

`ddpsrun validate` 는 이것들을 대신 봐 주지 못합니다. **사용자의 저장소도, vendor 의 자격증명도
서버에서는 보이지 않습니다.** 그래서 script 안에 두어야 합니다.

---

## 6. 어느 단계에서 죽어도 그때까지를 올린다

```bash
upload_everything() {
  aws s3 cp "train_${JOB}.log" "$RESULT_PATH" || true
  [ -d "$ADAPTER" ] && tar czf - "$ADAPTER" | aws s3 cp - "$RESULT_PATH$ADAPTER.tar.gz" || true
}
trap upload_everything EXIT
```

`trap ... EXIT` 는 정상 종료에서도, 오류에서도, SIGTERM 에서도 실행된다. 없으면 20 시간째에
죽었을 때 **돈은 다 쓰고 남는 것이 없다.** `ddpsrun validate` 가 `no-exit-trap` 으로 잡는다.

---

## 7. 학습이 끝나면 추론을 기다리지 말고 어댑터를 먼저 올린다

```bash
python train_dpo_m3.py ... | tee "train_${JOB}.log"
tar czf - "$ADAPTER" | aws s3 cp - "$RESULT_PATH$ADAPTER.tar.gz"   # 여기서 먼저 올린다
python gen_openrca_tasks_fast.py ...                                 # 그 다음 추론
```

학습이 25 시간이고 추론이 1 시간이면, **추론에서 죽었을 때 25 시간을 잃으면 안 된다.**

---

## 8. 긴 학습에는 checkpoint 감시를 붙인다

trainer 가 에폭마다 `checkpoint-NNN/` 을 로컬에 쓴다. 그것을 S3 로 옮기려면 **다 쓴 뒤에**
압축해야 한다.

```bash
watch_checkpoints() {
  while true; do
    sleep 60
    for dir in "$ADAPTER"/checkpoint-*; do
      [ -d "$dir" ] || continue
      [ -f "$dir/.uploaded" ] && continue
      # 120 초 동안 안 바뀐 것만 건드린다. 쓰는 중에 tar 를 뜨면 반쪽이 올라간다
      [ -n "$(find "$dir" -newermt '-120 seconds' -print -quit)" ] && continue
      tar czf - "$dir" | aws s3 cp - "$RESULT_PATH$(basename "$dir").tar.gz.part" \
        && aws s3 mv "$RESULT_PATH$(basename "$dir").tar.gz.part" \
                     "$RESULT_PATH$(basename "$dir").tar.gz" \
        && touch "$dir/.uploaded"
    done
  done
}
watch_checkpoints & WATCH_PID=$!
```

**감시 프로세스를 종료할 때 반드시 죽인다.** 살아 있으면 `tee` 가 EOF 를 못 받아서
`PACSRUN_EXIT=` 이 영영 안 찍히고, driver 가 job 이 끝난 줄 모른다.

```bash
on_exit() { kill "$WATCH_PID" 2>/dev/null || true; upload_everything; }
trap on_exit EXIT
```

---

## 9. GPU 상태는 이제 script 가 찍지 않아도 된다

**이 절은 더 이상 할 일이 아니다.** PACSrun 의 driver 가 `driver/common/gpu-watch.sh` 를
workload 의 command 앞에 붙여서 직접 찍는다. vendor 를 가리지 않는다 — VM 을 주는 쪽(AWS, GCP)
은 k3s pod 의 command 로, container 를 주는 쪽(RunPod)은 wrapper 로 같은 파일을 쓴다.
grep anchor 는 `PACSRUN-GPU-WATCH` 다.

원격 컨테이너에서 나오는 것이 stdout 한 줄기뿐이라는 사실은 그대로다. 달라진 것은 그 줄기에
누가 써 넣느냐이고, **연구자가 잊어버려도 지표가 나온다**는 것이 이 변경의 전부다.

찍히는 줄은 그대로다.

```
PACSRUN_GPU=94,38200,45440,71,298
```

- 형식은 `utilization,memory_used,memory_total,temperature,power` 다. 서버가 이 순서로 읽는다.
- 30 초에 한 줄이면 25 시간짜리 job 에 3,000 줄이다. 학습 로그가 35 만 줄인 것에 비하면
  무시할 수 있다.
- **예전 script 에 `watch_gpu` 가 남아 있어도 깨지지 않는다.** 같은 줄이 30 초에 두 번 찍히고,
  서버는 마지막 것을 쓴다. 지우고 싶으면 지워도 되고, 그대로 둬도 된다.
- 지표가 안 보이면 로그에서 `PACSRUN_GPU_WATCH` 로 시작하는 줄을 찾아볼 것. watcher 가 떴는지,
  아니면 image 에 `nvidia-smi` 가 없어서 건너뛰었는지를 그 줄이 말한다.
- 이 다섯 값은 `nvidia-smi` 가 주는 전부이고, **"카드가 얼마나 일했나"는 아니다.**
  `utilization.gpu` 의 정의가 "kernel 이 **하나라도** 돌던 시간의 비율"이라, H100 의 SM 132 개
  중 하나만 써도 100% 로 나온다. 그 답을 주는 값은 DCGM 의 profiling field 인데
  `CAP_SYS_ADMIN` 이 필요하고 RunPod 의 create 요청에는 capability field 자체가 없다.

---

## 10. 구매 방식은 사용자에게 묻는다

`--capacity-type` 은 **서버가 정하지 않습니다. 제출하는 사람이 정합니다.** 빠지면 제출이
거절됩니다.

```bash
ddpsrun estimate ...          # 권고와 이유가 나온다
ddpsrun submit ... --capacity-type on-demand
```

- `on-demand` 는 비싸고 뺏기지 않습니다.
- `spot` 은 싸고 도중에 회수될 수 있습니다. **checkpoint 가 없는 긴 학습에서는 전부 잃습니다.**
- RunPod 은 spot 을 팔지 않아서, `spot` 으로 내면 RunPod 이 후보에서 빠집니다.

**agent 가 대신 고르지 마십시오.** 권고와 이유를 보여 주고 사용자가 답하게 하십시오.

## 11. 나머지 판단은 서버에 묻는다

GPU 크기, 구매 방식, 예상 시간을 **script 에도 skill 에도 적지 않는다.**

```bash
ddpsrun estimate --gpu-vram 48 --pairs 1110 --epochs 4 --row-tokens 4100 --cap 12288
```

그래야 로직이 한 곳에 있고 UI 도 CLI 도 agent 도 같은 답을 받는다. 여기에 숫자를 적어 두면
서버가 새 측정을 쌓아도 이 파일만 옛날 답을 계속 준다.

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

**어떤 명령이 경로를 쓰고 뒤의 명령이 그것을 읽으면, 그 둘을 이어 주는 것은 shell 에 없다.**
어긋나도 앞의 명령은 성공하고, 뒤의 명령이 돌 때가 되어서야 드러난다. 짝은 여러 모양이다 —
checkpoint 디렉터리와 `--resume-from`, tokenize 한 데이터셋과 `--data-dir`, 내보낸 ONNX 파일과
그것을 읽는 server, 그리고 아래의 LoRA adapter. **한 변수를 만들어 양쪽에 쓴다.**

아래는 우리 job 의 예시다(TRL preference tuning + 추론).

```bash
# 이렇게 하지 않는다
python train_dpo_m3.py --out adapter_bank_v2
python gen_openrca_tasks_fast.py --lora /root/ab/adapter_bank      # 다르다

# 이렇게 한다
ADAPTER="adapter_${JOB}"
python train_dpo_m3.py --out "$ADAPTER"
python gen_openrca_tasks_fast.py --lora "/root/ab/$ADAPTER"
```

**어긋나면 앞이 먼저 끝나고 그 다음에 뒤가 실패한다.** AIOps 는 학습만 31 시간이다.
`ddpsrun validate --script run.sh` 가 `--out`/`--lora` 라는 **그 두 flag 이름일 때만**
`adapter-path-mismatch` 로 잡는다(recipe 층, DDPSRUN-CHECK-TIERS). 다른 이름을 쓰는 짝은
검사가 못 본다 — 그래서 이 규칙이 검사보다 먼저 있다.

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
  # 파일을 만들고 -> announce 한다. announce 가 §13 의 규약이고, driver 가 회수한다.
  cp "train_${JOB}.log" /root/work/ && echo "PACSRUN_ARTIFACT=/root/work/train_${JOB}.log"
  if [ -d "$ADAPTER" ]; then
    tar czf /root/work/adapter.tar.gz "$ADAPTER" \
      && echo "PACSRUN_ARTIFACT=/root/work/adapter.tar.gz"
  fi
  # k3s fetch 가 배포되기 전까지 AWS/GCP 에서만 필요한 이중 안전장치 (§13 의 ★).
  aws s3 cp "train_${JOB}.log" "$RESULT_PATH" || true
  [ -f /root/work/adapter.tar.gz ] \
    && aws s3 cp /root/work/adapter.tar.gz "$RESULT_PATH$ADAPTER.tar.gz" || true
}
trap upload_everything EXIT
```

`trap ... EXIT` 는 정상 종료에서도, 오류에서도, SIGTERM 에서도 실행된다. 없으면 20 시간째에
죽었을 때 **돈은 다 쓰고 남는 것이 없다.** `ddpsrun validate` 가 `no-exit-trap` 으로 잡는다.

**★ announce 를 trap 안에 두는 것이 왜 안전한가.** driver 는 workload 가 끝난 뒤에도 큐가
빌 때까지 최대 600초 machine 을 잡고 기다린다(§13). 그래서 마지막 순간에 찍은 줄도 회수된다 —
`aws s3 cp` 와 달리 announce 는 자격증명 만료와 무관하다.

---

## 7. 앞 단계의 산출물은 뒤 단계를 기다리지 말고 먼저 내보낸다

```bash
python train_dpo_m3.py ... | tee "train_${JOB}.log"
tar czf /root/work/adapter.tar.gz "$ADAPTER"                       # 여기서 먼저 내보낸다
echo "PACSRUN_ARTIFACT=/root/work/adapter.tar.gz"
python gen_openrca_tasks_fast.py ...                                 # 그 다음 추론
```

**긴 단계 뒤에 짧은 단계가 오는 job 은 모두 이 모양이다** — 학습 뒤 추론, pretraining 뒤
평가, 학습 뒤 export. 25 시간 + 1 시간이면 **뒤의 1 시간에서 죽었을 때 앞의 25 시간을 잃으면
안 된다.** announce 는 driver 에게 "이건 지금 가져가라" 는 뜻이고, 뒤 단계가 도는 동안 회수가
병행된다.

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
      # tar 를 먼저 닫고 그 다음에 announce 한다. 순서가 규칙이다 -- §13 의 크기 대조는
      # 쓰는 중인 파일을 회수 실패로 만든다(잘린 파일이 올라가는 것이 아니라 안 올라간다).
      tar czf "/root/work/$(basename "$dir").tar.gz" "$dir" \
        && echo "PACSRUN_ARTIFACT=/root/work/$(basename "$dir").tar.gz" \
        && touch "$dir/.uploaded"
    done
  done
}
watch_checkpoints & WATCH_PID=$!
```

**감시 프로세스를 종료할 때 반드시 죽인다.** 살아 있으면 `tee` 가 EOF 를 못 받아서
`PACSRUN_EXIT=` 이 영영 안 찍히고, driver 가 job 이 끝난 줄 모른다.

```bash
# ★ 기본값을 0 으로 두지 않는다. `kill 0` 은 PID 0 이 아니라 **프로세스 그룹 전체**이고,
# trap 이 watcher 시작 전에 불리면(위쪽 검사에서 die 하는 경우) 스크립트가 자기를 죽인다.
# `set -u` 를 의식해 `${WATCH_PID:-0}` 를 붙이는 것은 자연스러운 반사인데, 그 순간
# 조용한 자살로 바뀐다. 비었는지를 먼저 본다. 2026-09-09 에 한 세션이 스스로 만들고 잡았다.
on_exit() {
  [ -n "${WATCH_PID:-}" ] && kill "$WATCH_PID" 2>/dev/null || true
  upload_everything
}
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

---

## 12. script 가 커지거나 파일이 여럿이면 — args 에 그대로 넣지 않는다

`--script run.sh` 로 보낸 본문은 **job 객체 안에 들어간다.** `to_pacsjob` 이
`spec.args = ["bash", "-lc", <본문>]` 로 싣고, 그 객체가 etcd 에 저장된다. 그래서 상한이 있다:
`script` 는 **256 KiB** 까지고(`models.SCRIPT_MAX_CHARS`), 넘으면 제출이 422 로 거절된다.
GPU 를 빌리기 전이라 돈은 들지 않지만, 큰 것을 넣을 자리가 아니라는 뜻이다.

**실측 (2026-09-08, 클러스터의 `baseline-c`).** 학습 스크립트 자체는 작다 — 20 KiB 급이면
그대로 넣어도 상한의 8% 다. 그런데 그 job 은 이미 다른 방법을 쓰고 있었다:

```
PacsJob 객체 전체     4,682 bytes
spec.args               302 bytes      <- 아래 부트스트랩
S3 의 run.sh         19,655 bytes      <- 실제 학습 스크립트
```

`spec.args` 에 든 302 바이트가 전부다.

```bash
set -euo pipefail
pip install --quiet --no-input boto3
python3 - <<'PY2'
import os, urllib.parse, boto3
u = urllib.parse.urlparse(os.environ["PACSRUN_RESULT_PATH"])
base = u.path.lstrip("/").rstrip("/")
boto3.client("s3").download_file(u.netloc, f"{base}/run.sh", "/root/run.sh")
PY2
bash /root/run.sh
```

**두 방법과, 어느 것을 언제 쓰는가.**

| 방법 | 쓸 때 | 대가 |
|---|---|---|
| `--script run.sh` (본문을 args 에) | 한 파일, 256 KiB 미만. **기본값으로 이것을 쓴다** | 없음. job 이 자기가 실행한 것을 담고 있어서 `ddpsrun` 의 Scripts 화면, Submitted spec, 재제출이 다 된다 |
| S3 부트스트랩 (위 302 바이트) | 스크립트가 상한을 넘거나, 파일이 여럿이거나, 사람이 job 을 다시 내지 않고 스크립트만 갈아 끼우고 싶을 때 | 실패 지점이 하나 늘어난다. **GPU 를 이미 빌린 뒤에** S3 를 못 읽어 죽을 수 있으므로, 규칙 5 의 도달성 검사에 그 객체도 넣는다. 그리고 job 객체만 봐서는 무엇이 돌았는지 알 수 없다 |
| `git clone` (규칙 1) | 코드가 저장소에 있을 때 | 위와 같다. clone 이 학습 명령보다 앞에 있어야 한다 |

**S3 를 쓰기로 했으면 사용자에게 업로드를 부탁한다.** agent 는 자기 손으로 그 객체를 올리지
않는다 — `ddpsrun` 에 업로드 명령이 없고, 결과 prefix 는 서버가 job 마다 만들어 주는 것이라
제출 전에는 그 주소가 존재하지도 않는다. 순서는: 사용자가 `aws s3 cp run.sh <경로>` 로 올리고,
그 경로를 agent 에게 알려 주고, agent 는 위 부트스트랩을 `--script` 로 보낸다.

---

## 13. 결과는 `PACSRUN_ARTIFACT` 로 내보낸다 — `aws s3 cp` 로 쓰면 21시간 뒤에 잃는다

**이 절이 없어서 실제로 잃을 뻔했다.** 2026-09-08 에 저장소만 들고 작업 C 를 제출하려던 세션이
이 규약을 **문서 어디에서도 찾지 못했고**, 09-04 job 의 결과 tar 에 딸려 저장소에 커밋돼 있던
옛 wrapper 를 우연히 읽어서 알았다. 그 우연이 없었으면 `aws s3 cp` 로 썼을 것이고, 21시간 뒤
`AccessDenied` 로 결과가 전부 사라진다. `troubleshooting.md` 의 "job 이 Succeeded 인데 S3 가
비어 있다" 항목이 그 실패의 흔적이다.

### 규약

파일 하나를 **완성한 직후**, stdout 에 한 줄을 찍는다. 그러면 driver 가 그 파일을 회수한다.

```bash
tar czf /root/work/adapter.tar.gz "$ADAPTER"
echo "PACSRUN_ARTIFACT=/root/work/adapter.tar.gz"
```

- **경로는 컨테이너 안의 절대 경로**다. driver 가 그 경로를 읽어 밖으로 옮긴다.
- **파일마다 한 줄.** 여러 개면 여러 줄이고, 순서는 상관없다.
- **완성한 뒤에 찍는다.** 쓰는 중인 파일을 알리면 잘린 파일이 회수된다. `tar` 는 닫힌 뒤,
  로그는 마지막 flush 뒤.

### 파일 이름이 곧 S3 의 이름이다 — 같은 이름 둘은 서로를 덮는다

key 는 **job 의 result prefix + 파일 이름(basename)** 이다. `/root/work/adapter.tar.gz` 는
`s3://<bucket>/<prefix>adapter.tar.gz` 로 간다. 경로의 앞부분은 버려지므로
`runs/iter_1/ckpt.pt` 와 `runs/iter_2/ckpt.pt` 를 둘 다 announce 하면 **뒤엣것이 앞엣것을
덮는다.** 회차나 rank 를 파일 이름에 넣는다: `ckpt_iter2.pt`, `adapter_rank0.tar.gz`.

### 왜 `aws s3 cp` 가 아닌가

결과를 S3 에 쓰는 주체는 **어느 vendor 에서도 driver** 다. 이유는 둘이다.

- **컨테이너에 주는 자격증명이 먼저 만료된다.** AWS 상한이 43,200초(12시간)라 21시간 job 의
  **마지막** 업로드 — 그 run 을 한 이유 — 가 만료 뒤에 일어난다.
- **업로드 방식이 vendor 마다 다르다.** script 가 그것을 알아야 하면 script 가 vendor 종속이
  된다. announce 한 줄은 어디서나 같은 문장이다.

컨테이너가 받는 자격증명이 아무 쓸모가 없다는 뜻은 아니다. **그것으로 자기 prefix 를 읽는다** —
회차를 이어 갈 때 앞 회차의 checkpoint 를 되받는 것이 그 용도다(§13 마지막 절, `continue_from`).

driver 가 파일을 가져오는 길만 vendor 마다 다르고, **script 는 그 차이를 몰라도 된다.**

| vendor | driver 가 어떻게 가져오나 |
|---|---|
| RunPod | 컨테이너 안의 작은 HTTP server 에 `<pod-id>-8888.proxy.runpod.net` 으로 GET (`PACSrun/driver/runpod/driver.py:233` `ARTIFACT_RE`, `:2012` `_fetch_one`) |
| VM + k3s (AWS, GCP, 이후 Shadeform / Seeweb) | k3s API 의 exec 로 `stat -c %s` 로 크기를 받고 `cat` 으로 바이트를 받는다 (`PACSrun/driver/common/artifact_fetch.py`, grep `PACSRUN-K3S-FETCH`) |

**★ 2026-09-09 현재 상태: k3s 경로는 구현됐고 아직 배포되지 않았다.** 그래서 **AWS/GCP 에서는
그동안 `aws s3 cp` 를 announce 와 함께 둔다** — 그러면 어느 경로에서도 산다(RunPod 에서는
`aws s3 cp` 가 AccessDenied 로 조용히 실패하고 announce 가 일한다). 배포된 뒤에는 announce
하나로 충분하고, `aws s3 cp` 는 12시간 뒤 만료되는 그 자격증명에 의존하는 부분이라 지우는 것이
낫다. **어느 쪽인지는 `ddpsrun explain` 이 답한다** — 이 문서가 아니라 서버에 물어본다.

#### k3s 경로가 하는 검사 둘, script 가 알아야 하는 것

- **크기를 대조한다.** `stat` 이 말한 바이트 수와 실제로 올라간 수가 다르면 **object 를 지우고**
  다시 시도한다(3회). 그래서 **쓰는 중인 파일을 announce 하면 회수가 실패한다** — 잘린 파일이
  올라가는 것이 아니라 아예 안 올라간다. 완성 뒤에 찍으라는 위 규칙이 이것 때문이다.
- **exit 0 인데 announce 한 것이 S3 에 없으면 job 은 exit 34 로 끝난다.** 학습이 성공했는데
  결과가 안 나갔으면 그것은 성공이 아니라는 판정이고, Succeeded 로 표시된 빈 prefix 보다 낫다.

`explain` 이 "Write it there yourself" 라고만 말하는 것은 이 절이 있기 전의 문장이다.

### 관이 서는지 먼저 한 번 확인한다 (규칙 5 의 결과 경로 검사를 대신한다)

학습 21시간을 태운 뒤 회수가 안 되는 것을 알면 늦다. **작은 파일 하나로 먼저 찍어 본다.**

```bash
date > /root/work/_probe.txt
echo "PACSRUN_ARTIFACT=/root/work/_probe.txt"
```

그 줄이 driver 로그에 `fetched ... bytes` 로 되돌아오는지 보고 학습을 시작한다.

**로그에서 그 줄은 `<internal>=/root/work/_probe.txt` 로 보인다.** gateway 의 로그 relay 가
`PACSRUN_` 로 시작하는 이름을 가리기 때문이고(`server/ddpsrun_server/k8s.py` 의 `redact`,
`server/tests/test_k8s.py:23` 이 그 동작을 고정한다), **경로는 그대로 남으므로 확인은 된다.** 이름이 안 보이는
것이 실패가 아니다 — 그 줄이 아예 없는 것이 실패다.

### 회차로 나눠 내보내면 중단에도 남는다

`Recovering` 후 컨테이너는 **빈 상태로 다시 시작한다** — `training.resumable` 은 사용자가
주장하는 표시이고 도구가 되돌려주는 것은 없다. 회차가 끝날 때마다 그 산출물을 위 규약으로
내보내면, 15시간째에 회수돼도 그때까지의 회차는 남는다.

**의존해도 되는 사실:** 재시작 후에도 **result path 는 같다.** 서버가 job id 로 한 번 만들어
`spec.resultPath` 에 넣고, recovery 는 같은 PacsJob 을 쓰므로 그 필드가 바뀌지 않는다.

---

## 14. 두 번째 AWS 계정을 쓰는 job 은 세 변수를 스스로 가른다

**PACSrun 은 결과 회수용 자격증명을 `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_SESSION_TOKEN` 으로 주입한다.** job 이 **다른** AWS 계정을 부르는 경우 — 예를 들어 Bedrock
judge — 그 코드도 같은 세 이름을 찾는다. **boto3 는 환경변수를 프로파일보다 먼저 읽으므로
`AWS_PROFILE` 로는 갈라지지 않는다.**

**어느 쪽이 이겨도 나머지 절반이 깨진다.** judge 가 지면 평가가 거부되고, 회수 쪽이 지면
**21시간을 돌린 결과를 마지막에 못 올린다** — 회수는 job 이 끝날 때 일어나므로 그 실패는 가장
비싼 시점에 드러난다.

`validate` 가 이것을 먼저 말한다: 세 이름 중 하나를 job 이 직접 들고 있거나, `AWS` 와
`ACCESS_KEY` 를 함께 가진 다른 이름이 보이면 `aws-credential-collision` 이 뜬다
(`server/ddpsrun_server/validate.py` 의 `check_aws_credential_collision`).

**되는 모양 — judge 키는 자기 이름으로 받고 쓰는 자리에서만 명시적으로 넘긴다.**

```bash
python - <<'PY'
import os, boto3
s = boto3.session.Session(
    aws_access_key_id=os.environ["JUDGE_AWS_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["JUDGE_AWS_SECRET_ACCESS_KEY"],
    aws_session_token=os.environ.get("JUDGE_AWS_SESSION_TOKEN"),
)
bedrock = s.client("bedrock-runtime", region_name="us-west-2")
PY
```

**연구원 코드가 boto3 기본 체인을 쓰고 고칠 수 없는 경우**에만, 그 호출 구간 앞에서 세 변수를
judge 값으로 치환하고 **끝나면 되돌린다.** 되돌리지 않으면 13번의 회수가 깨진다.

```bash
# 치환 -- 되돌리기까지 한 쌍으로만 쓴다.
export _SAVED_KEY="$AWS_ACCESS_KEY_ID" _SAVED_SECRET="$AWS_SECRET_ACCESS_KEY" _SAVED_TOKEN="$AWS_SESSION_TOKEN"
export AWS_ACCESS_KEY_ID="$JUDGE_AWS_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$JUDGE_AWS_SECRET_ACCESS_KEY" AWS_SESSION_TOKEN="$JUDGE_AWS_SESSION_TOKEN"
python evaluate.py            # judge 를 부르는 구간
export AWS_ACCESS_KEY_ID="$_SAVED_KEY" AWS_SECRET_ACCESS_KEY="$_SAVED_SECRET" AWS_SESSION_TOKEN="$_SAVED_TOKEN"
```

---

## 15. 뜬 뒤에야 알 수 있는 것 셋 — script 가 한 줄씩 찍어 둔다

이 셋은 **submit 시점에 정할 수 없다.** 스키마에 필드가 없고, 값은 어느 host 를 받았는지에
달렸다. 그래서 규칙은 "요청한다" 가 아니라 "**확인하고 기록한다**" 다.

| 무엇 | 지금 값이 어디서 오나 | script 가 할 일 |
|---|---|---|
| 디스크 | operator 전역 `PACSRUN_DISK_GB=200`. job 별로 못 정한다 | `df -h /root` 를 학습 전에 한 줄 찍는다. venv 3개 + 모델 30GB 급이면 200GB 로 충분하다는 것이 09-04 실측이다 |
| `/dev/shm` | 받은 host 가 정한다. 필드 없음 | `df -h /dev/shm` 를 찍는다. TP4 vLLM 이 요구하므로 작으면 그 사실을 로그에 남기고 tensor parallel 크기를 낮추는 쪽을 사용자에게 알린다 |
| NCCL P2P | RunPod 일부 host 에서 첫 all-reduce 가 정지한다. 필드 없음 | `NCCL_P2P_DISABLE` 을 **한 곳에서** 정해 두고 되돌릴 수 있게 한다. 정지 자체는 플랫폼이 잡아 exit 21 로 끝낸다 |

```bash
# 학습 전에, 순서대로. 세 줄 다 로그로 남는 것이 목적이다.
df -h /root /dev/shm
nvidia-smi --query-gpu=index,name,memory.total --format=csv
: "${NCCL_P2P_DISABLE:=0}"; export NCCL_P2P_DISABLE
echo "NCCL_P2P_DISABLE=$NCCL_P2P_DISABLE"
```

---

## 16. 분산학습 — 좌표는 우리가 주고, launcher 에 넘기는 것은 script 가 한다

**pod 이 서로 이야기해야 하면 `--group-size N --group-mode distributed` 로 낸다.** 그것이
없으면 pod N 개는 **서로 모르는 독립 실행 N 개**다. 끝나기는 하고, 기계 N 대 값을 내고,
관계없는 결과 N 개를 남긴다.

### 우리가 주는 것, 이름 그대로

| 변수 | 무엇 | 누가 채우나 |
|---|---|---|
| `PACSRUN_GROUP_SIZE` | 이 group 이 pod 몇 개인가 | operator (`PACSRUN-GROUP-COORDS`) |
| `PACSRUN_GROUP_RANK` | 이 pod 이 자기 group 의 몇 번째인가 | operator |
| `PACSRUN_GROUP_INDEX` | 이 group 이 job 의 몇 번째 group 인가 | operator |
| `PACSRUN_MASTER_ADDR` | rank 0 machine 의 **사설** 주소 | driver (`PACSRUN-GROUP-HOSTNET`) |
| `PACSRUN_MASTER_PORT` | `29500 + group_index` | driver |
| `PACSRUN_POD_INDEX` | job 의 pod 전체에서 몇 번째인가. group 과 별개로 남는다 | operator |

**이름이 어느 framework 것도 아닌 것은 의도다.** torchrun 은 `--node_rank`/`--master_addr` 를
원하고 다른 launcher 는 다른 것을 원한다. **그 번역이 script 가 쓸 줄이다.**

```bash
torchrun \
  --nnodes "$PACSRUN_GROUP_SIZE" \
  --node_rank "$PACSRUN_GROUP_RANK" \
  --master_addr "$PACSRUN_MASTER_ADDR" \
  --master_port "$PACSRUN_MASTER_PORT" \
  --nproc_per_node 4 \
  train.py
```

### ★ 좌표를 안 읽으면 아무 소리 없이 멈춘다

`driver/common/remotek8s.py` 에 그 실측이 적혀 있다: **"NEITHER RANK PRINTED ANYTHING. Both
sat in `dist.init_process_group` with no error and no output."** 모든 rank 가 아무도 열지 않은
rendezvous 를 기다리고, **카드는 busy 로 읽히고**(NCCL 의 대기는 도는 kernel 이다),
3,600초 뒤 stall detector 가 exit 21 로 끝낼 때까지 과금된다.

`ddpsrun validate --group-size N --group-mode distributed --script run.sh` 가 넷을 본다:
좌표를 하나도 안 읽으면 **error**(`group-coords-unread`), launcher 가 없으면 warning,
`--nproc_per_node` 가 `--gpu-count` 와 다르면 error, `--nnodes` 가 group size 와 다르면 error.

### 값을 두 곳에 적지 않는다

`--nnodes 2` 라고 박아 두면 `--group-size 4` 로 바꾼 날 조용히 어긋난다. **`$PACSRUN_GROUP_SIZE`
를 쓴다.** `--nproc_per_node` 는 pod 당 카드 수이므로 `--gpu-count` 와 같아야 하고, 그 둘은
validate 가 대조한다.

### 성능 기대치는 낮춰 잡는다

**pod 경계 하나가 카드 한 장보다 느리다.** 같은 machine 안에서도 0.56배였다
(`facts/pod-boundary-costs-ddp.md`). 앗아가는 것은 P2P 가 아니라 **shared memory** 하나다.
기계 두 대는 그보다 3.07배 더 느리다. 그리고 region 을 넘으면 왕복 60~70 ms 에 GB 당 $0.02 가
붙는다 — 한 step 이 866,890,752 바이트를 옮긴 실측이 있다(2026-09-05).
**즉 분산은 "더 빠르게" 가 아니라 "한 장에 안 들어가서" 하는 것이다.**

### `/dev/shm` 과 NCCL P2P 는 뜬 뒤에야 안다

15번의 표가 그것이다. 특히 **RunPod 일부 host 에서 첫 all-reduce 가 정지한다** —
`NCCL_P2P_DISABLE=1` 로 되돌릴 수 있게 그 값을 한 곳에서 정해 둔다.

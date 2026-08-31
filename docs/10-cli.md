# CLI 2단계 — `ddpsrun` (2026-08-31)

`08-plan.md` 의 2단계다. **사용자에게 kubectl 도 kubeconfig 도 없다.** 노트북에 설치하는 것은
`requests` 와 `PyYAML` 둘뿐이고 kubernetes client 는 들어가지 않는다.

## 만든 것

```
cli/
  pyproject.toml            ddpsrun. console_scripts 로 `ddpsrun` 명령이 생긴다
  ddpsrun/
    config.py     DDPSRUN-CLI-CONFIG   ~/.config/ddpsrun/config.json, 0600
    client.py     DDPSRUN-CLI-CLIENT   HTTP 호출만
    cli.py        DDPSRUN-CLI          argparse 와 무엇을 출력할지
  tests/                               43 개
```

서버에도 라우트 둘을 더했다. `GET /v1/explain` 과 `GET /v1/schema` 다. 둘 다 판단을 하지
않는다. 하나는 고정된 산문이고 하나는 요청 모델에서 생성된다.

## 명령

| 명령 | 하는 일 |
|---|---|
| `ddpsrun login --server <url>` | token 을 받아 저장한다. 안 적으면 물어본다 |
| `ddpsrun logout` | 저장한 것을 지운다 |
| `ddpsrun explain` | 이 도구가 무엇인지. **서버에 물어본다** |
| `ddpsrun schema` | 제출 본문의 형식. **서버에 물어본다** |
| `ddpsrun submit -f job.yaml` | 제출한다 |
| `ddpsrun status <job_id>` | phase, GPU, 재시작 횟수 |
| `ddpsrun logs <job_id> --follow` | 출력 |

`gpus`, `validate`, `estimate` 는 없다. 서버에 라우트가 없고, CLI 가 자체적으로 답하면
이 설계가 피하려는 "판단이 두 곳에 있는 상태"가 된다.

## 한 번 쓰는 모습

```bash
$ ddpsrun login --server https://run.example
token:
saved to /Users/me/.config/ddpsrun/config.json
token accepted

$ cat job.yaml
name: bank-exp2
image: runpod/pytorch:1.1.0-rc.154-cu1281-torch291-ubuntu2404
args: ["bash", "-lc", "python train.py"]
env:
  ML: "12288"
  MP: "11264"
secrets: [GITHUB_PAT]
gpu:
  vram_gb: 48
  count: 1
expected_hours: 8

$ ddpsrun submit -f job.yaml --env EPOCHS=4
submitted  job-4cfb49b4c772
results    s3://<RESULT_BUCKET>/pacsrun/lab-alice/bank-exp2-4cfb49b4c772/
follow     ddpsrun logs job-4cfb49b4c772 --follow

$ ddpsrun status job-4cfb49b4c772
job-4cfb49b4c772  bank-exp2
  phase      accepted, not yet started
  results    s3://<RESULT_BUCKET>/pacsrun/lab-alice/bank-exp2-4cfb49b4c772/
```

## 파일과 flag 를 섞는 규칙

**flag 가 파일을 이긴다.** 파일에 안정된 값 열 개를 두고 실행마다 바뀌는 하나만 flag 로
덮는 쓰임을 위해서다. `env` 는 **덮어쓰기가 아니라 합치기**다. 위 예시에서 `EPOCHS=4` 가
더해지고 `ML` 과 `MP` 는 남는다.

**GPU 만 예외다.** 요청 방식이 둘(메모리 하한, 정확한 모델명)이고 서버가 하나만 받는다.

- 파일이 `name: L40S`, flag 가 `--gpu-vram 48` -> **flag 가 이기고 파일의 방식은 지워진다**
- flag 로 `--gpu-vram 48 --gpu-name L40S` 를 **둘 다** -> **거부한다.** exit 2

둘째 경우를 처음에는 첫째와 같이 처리했는데, 두 분기가 서로를 지워서 마지막에 실행된 것이
조용히 이겼다. 사용자가 요청하지 않은 GPU 로 job 이 나가는 상태였다. 2026-08-31 에 CLI 를
서버에 붙여 돌려 보다가 발견했고, `test_both_gpu_flags_at_once_is_refused_rather_than_silently_resolved`
가 그것을 잡는다.

## exit code

script 가 읽을 것이므로 셋으로 고정했다.

| | 뜻 |
|---|---|
| 0 | 됐다 |
| 1 | 서버가 거부했거나 서버에 못 닿았다 |
| 2 | 명령이 틀렸거나 자격증명이 없다 |

`logs --follow` 중의 Ctrl-C 는 0 이다. 사용자가 보기를 그만둔 것이고 job 은 계속 돈다.

## token 을 어디에 두는가

`~/.config/ddpsrun/config.json` 을 0600 으로, 디렉터리를 0700 으로 만든다. 파일을 만들 때부터
0600 으로 열지, 만들고 나서 좁히지 않는다. 그 사이에 공용 기계의 다른 사용자가 읽을 수 있는
창이 생긴다.

`DDPSRUN_SERVER` 와 `DDPSRUN_TOKEN` 이 있으면 그쪽이 이긴다. CI 에서는 파일이 맞는 형태가
아니기 때문이다.

`login` 은 저장한 뒤 **존재할 수 없는 id 로 `status` 를 한 번 호출한다.** token 이 맞으면
404 `no such job`, 틀리면 401 이 온다. 사용자가 진짜 명령을 처음 쓸 때가 아니라 지금
알려 주려는 것이다.

## 서버가 만드는 PacsJob — 살아 있는 CRD 로 확인함

2026-08-31, `kubectl apply --dry-run=server` 로 다섯 형태를 넣었다. server dry-run 은
admission 과 CEL 규칙까지 돌고 저장만 안 한다.

```
# 서버가 만드는 PacsJob 다섯 형태를, 살아 있는 CRD 에 저장 없이 넣어 본다
# for c in full byname cpuonly minimal korean; do
#   kubectl apply --dry-run=server -f dry2-$c.json | tail -1
# done
pacsjob.pacsrun.io/ddpsrun-a8acdef80a07 created (server dry run)
pacsjob.pacsrun.io/ddpsrun-b1c2d3e4f5a6 created (server dry run)
pacsjob.pacsrun.io/ddpsrun-c7d8e9f0a1b2 created (server dry run)
pacsjob.pacsrun.io/ddpsrun-d3e4f5a6b7c8 created (server dry run)
pacsjob.pacsrun.io/ddpsrun-e9f0a1b2c3d4 created (server dry run)
```

순서대로 이렇다.

| id | 형태 |
|---|---|
| `a8acdef80a07` | vram 으로 요청 + secret + env + args + expected_hours |
| `b1c2d3e4f5a6` | 모델명으로 요청, count 2 |
| `c7d8e9f0a1b2` | GPU 없이 cpu/memory 만 |
| `d3e4f5a6b7c8` | image 와 name 만 |
| `e9f0a1b2c3d4` | 한글 이름 |

**`09-server.md` 의 확인 못 한 것 첫째가 이것으로 닫혔다.**

## 한글 이름에서 나온 결함

Kubernetes label 값은 `[A-Za-z0-9._-]` 만 받는다. `은행 실험2` 를 넣으면 label 이 `2` 가 되고,
`ddpsrun status` 가 그 label 을 읽으므로 **사용자가 자기 job 이름을 못 알아본다.**

annotation 에는 문자 제한이 없다. 그래서 사용자가 친 이름을 `ddpsrun.io/display-name`
annotation 에 그대로 넣고, label 은 `kubectl -l` 용으로만 남겼다. `status` 는 annotation 을
먼저 읽고 없으면 label, 그것도 없으면 객체 이름으로 내려간다.

```
label       : 2
annotation  : 은행 실험2
status 가 보여줄 이름: 은행 실험2
```

**S3 폴더 이름은 아직 ASCII 다.** 위 job 의 결과는 `pacsrun/<namespace>/2-e9f0a1b2c3d4/` 로
간다. S3 key 는 UTF-8 을 담을 수 있지만 presigned URL 과 일부 도구에서 인코딩을 신경 써야
해서 미뤘다. hex 12 자리가 붙어 있어 충돌하지는 않는다.

## 확인 안 된 것

1. **진짜 HTTP 로는 안 돌려 봤다.** test 43 개는 가짜 세션으로 돌고, end-to-end 확인은
   CLI 와 서버를 한 프로세스에 올려서 했다. 서버가 cluster 에 올라가야 진짜 왕복이 된다.
2. **`--follow` 가 몇 시간을 버티는지.** 미결 항목 14 다.
3. **`pip install ddpsrun` 을 해 본 적이 없다.** 지금은 `pip install -e cli/` 로만 설치했다.
   패키지 이름 `ddpsrun` 은 PyPI 에 아직 올리지 않았다.

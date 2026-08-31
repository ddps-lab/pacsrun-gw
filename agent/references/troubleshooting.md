# 겪은 실패와 그것을 알아보는 로그 줄

**전부 우리가 실제로 겪었고 로그가 있다.** 증상을 알아보는 문자열을 먼저 적고, 원인과
조치를 붙였다. 이 파일은 사람이 쓴다.

---

## 학습이 몇 스텝 만에 OutOfMemoryError 로 죽는다

```
empty_strided_cuda((2, s87, 151936), (..., ...), torch.bfloat16)
torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 6.75 GiB.
GPU 0 has a total capacity of 44.39 GiB of which 3.43 GiB is free.
```

**터진 것은 attention 이 아니라 logits 이다.** 저 shape 의 세 숫자가 각각 좋은 답과 나쁜 답
둘, 문장 길이, vocabulary 다. Qwen3-4B 는 vocabulary 가 151,936 이라 **토큰 하나가 297 KiB** 다.

요청한 바이트를 `2 × 151936 × 2` 로 나누면 토큰 수가 나온다. 위 경우 11,926 토큰이고, 캡이
12,288 이었으니 **가장 긴 표본이 캡의 97.1% 까지 자란 것이다.** 평균 행 길이는 약 5,600
이었다. 즉 **평균이 아니라 캡이 메모리를 결정한다.**

조치 둘이다. **둘 다 학습 결과를 바꾸지 않는다.**

```bash
PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True python train_dpo_m3.py ...
python patch_trl_liger_slice.py $(python -c "import trl.trainer.dpo_trainer as m; print(m.__file__)")
```

**둘을 켜니 같은 L40S 에서 완주했고 스텝당 시간도 85.90 초에서 66.44 초로 22.7% 빨라졌다.**
`ddpsrun validate` 가 `alloc-conf-missing` 과 `trl-patch-missing` 으로 먼저 잡는다.

---

## `status=RUNNING` 인데 30 분째 아무 출력이 없다

```
{"id": "...", "desiredStatus": "RUNNING", "runtime": null}
```

**`status` 는 준비 신호가 아니다.** 실측으로 `status` 가 RUNNING 이 되고 42 초 뒤에 컨테이너가
시작한 적이 있다. 준비는 `runtime` 이 null 을 벗어나는 것으로 판단한다.

30 분씩 걸리는 것은 다른 문제다. **같은 물리 서버(RunPod 의 `machineId`)에 우리 job 이 둘
올라가면 image pull 대역폭을 나눠 쓴다.** 11.04 GiB 이미지를 혼자 받으면 35.9~97.5 MiB/s 인데
둘이 동시에 받으면 6.3 MiB/s 밑으로 떨어졌고, 1,800 초 제한을 넘겨 둘 다 죽었다. $0.99 를
버렸다.

조치는 **순차 제출**이다. 같은 이미지를 쓰는 job 을 동시에 넣지 않는다.

---

## job 이 `Succeeded` 인데 S3 가 비어 있다

원인은 **자격증명 만료**다. STS temporary credentials 는 `DurationSeconds` 가 최대 43200,
즉 12 시간이다. 그보다 긴 job 은 마지막에 자기 결과를 못 올린다.

조치는 fetch mode 다. 원격에 읽기 전용 자격증명만 주고 **driver pod 이 대신 가져와서 올린다.**
`ddpsrun estimate` 가 11 시간을 넘기면 미리 알려 준다.

**이것은 cluster 전체 스위치다.** operator 의 `PACSRUN_FETCH_MODE` 환경변수를 읽으며
(`internal/controller/vendorpod.go:1030-1035`), job 마다 켜고 끌 수 없다.

---

## fetch mode 인데 `AccessDenied ... CreateMultipartUpload` 가 보인다

```
An error occurred (AccessDenied) when calling the CreateMultipartUpload operation
```

**정상이다.** fetch mode 에서 원격은 읽기 전용 자격증명만 받는다. 올리는 것은 driver 다.
이 줄이 보인다고 job 이 실패한 것이 아니다.

---

## `no offering left` 인데 vendor 재고는 있다

```
stopped after 5 offering(s) refused
```

**2026-08-28 에 고친 오분류다.** RunPod 의 create 응답 HTTP 400 을 전부 재고 부족으로 읽고
있었다. 실제 원인은 이것이었다.

```
Field "objectMounts" is not defined by type "PodFindAndDeployOnDemandInput"
```

vendor 가 API schema 를 바꿔서 우리 요청이 거절된 것이고 재고와 무관했다. 확인 방법은
**`gpuCount` 를 99 로 넣어 보는 것**이다. 같은 오류가 나오면 재고 검사 전에 거절되는 것이므로
schema 문제다.

지금은 malformed request, quota, 재고 부족을 갈라서 각각 다른 exit code 로 끝난다.

---

## `표본 탈락: ... 재확인 필요` 로 학습이 시작하자마자 끝난다

`--max-prompt-len` 보다 긴 프롬프트가 데이터에 있다는 뜻이다. 캡 짝을 확인한다. 우리가 쓴
것은 `12288 / 11264` 와 `18432 / 17408` 이고, 둘 다 답을 위해 1,024 토큰을 남긴다.

`ddpsrun validate` 가 `prompt-cap-too-high` 로 잡는다.

---

## 학습은 끝났는데 추론이 어댑터를 못 찾는다

```
OSError: /root/ab/adapter_bank does not appear to have a file named adapter_config.json
```

학습의 `--out` 과 추론의 `--lora` 가 다르다. **학습이 먼저 다 돌고 나서 드러나므로 가장 비싼
실수다.** `script-contract.md` 2 번이 이것을 막고, `ddpsrun validate --script run.sh` 가
`adapter-path-mismatch` 로 미리 잡는다.

---

## 모델 다운로드가 즉시 죽는다

`HF_HUB_ENABLE_HF_TRANSFER=1` 인데 `hf_transfer` 패키지가 없으면 그렇다. `0` 으로 두거나
패키지를 설치한다.

---

## vllm 추론에서 tokenizer 오류가 난다

```
AttributeError: 'Qwen2Tokenizer' object has no attribute 'all_special_tokens_extended'
```

`transformers` 5.15 이상이 설치된 경우다. `"transformers<5"` 로 핀을 확인한다.

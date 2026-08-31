# ddpsrun — AI coding agent 안내

이 저장소는 **PACSrun 앞단**이다. kubectl 도 AWS IAM 도 없는 사용자가 GPU job 을 제출하고
결과를 받는 경로를 제공한다.

## 먼저 이것부터

**문서를 읽기 전에 도구에 물어본다.** 답이 항상 최신이다.

```bash
ddpsrun explain          # 이 도구가 무엇이고 어떻게 쓰는지
ddpsrun schema           # 제출 본문의 형식
ddpsrun gpus             # 지금 빌릴 수 있는 GPU 와 단가
ddpsrun validate run.sh  # 이 script 의 문제를 지적
ddpsrun estimate ...     # 시간과 비용, 권장 GPU
```

**GPU 크기, spot 여부, 예상 시간을 스스로 판단하지 않는다.** `ddpsrun estimate` 를 부른다.
판단 로직은 서버에 한 벌만 있다.

## 사용자 저장소를 읽어 학습 script 를 만들 때

`agent/references/script-contract.md` 를 읽는다. 요점만 적으면 이렇다.

1. 문서에 적힌 경로와 저장소 실제 구조를 대조한다
2. 학습의 출력 경로와 추론의 입력 경로를 한 변수로 묶는다
3. 요구된 산출물 중 명령이 만들지 않는 것을 script 가 만들게 한다
4. clone 직후 파일 줄 수, 학습 시작 직후 표본 수를 찍는다
5. `trap ... EXIT` 로 어느 단계에서 죽어도 그때까지를 올린다
6. 학습이 끝나면 추론을 기다리지 말고 어댑터를 먼저 올린다

## 실패를 만나면

`agent/references/troubleshooting.md` 에 우리가 실제로 겪은 것과 로그가 있다.

## 설계

`docs/` 에 있다. `00-overview.md` 부터 읽는다.

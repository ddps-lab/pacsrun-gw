# 다중 사용자 — namespace 와 S3 prefix (2026-08-31)

## 지금 상태

```
# aws s3 ls s3://<RESULT_BUCKET>/
                           PRE pacsrun/

# aws iam get-role-policy --role-name pacsrun-runpod-remote --policy-name result-bucket-access
arn:aws:s3:::<RESULT_BUCKET>/pacsrun/*
```

**모든 작업이 `pacsrun/` 하나 아래에 있다.** 지금은 job 마다 session policy 가
`pacsrun/<작업명>/` 으로 좁혀 주지만, **작업명이 겹치면 남의 결과를 덮어쓴다.**

operator 는 모든 namespace 를 watch 한다 (`cmd/main.go` 에 namespace 제한이 없다). 그래서
namespace 를 나누는 것 자체는 operator 변경이 필요 없다.

## 바꿀 것 넷

### 1. prefix 에 namespace 를 넣는다

```
지금  s3://pacsrun-results-.../pacsrun/<작업명>/
바꿀  s3://pacsrun-results-.../pacsrun/<namespace>/<작업명>/
```

### 2. session policy 에 namespace 를 끼운다

driver 가 STS 를 부를 때 붙이는 문서에 namespace 를 넣는다. 지금 그 문서는
`driver/runpod/driver.py` 의 `_session_policy` 가 만든다. 최종 권한이 role 의 권한과
이 문서의 **교집합**이므로, namespace 를 끼우면 job A 의 자격증명이 다른 namespace 를
못 본다.

### 3. `resultPath` 를 사용자가 못 정하게 한다

서버가 token 에서 유도해 채운다. 요청 본문에 그 필드를 받지 않는다.

### 4. namespace 마다 Secret 과 ServiceAccount 를 만든다

driver pod 의 RunPod key 가 `LocalObjectReference` 라 **job 과 같은 namespace** 에 있어야
한다 (`internal/controller/vendorpod.go:1309-1312`). 그래서 namespace 마다 이만큼이 필요하다.

```
namespace          lab-<사용자>
ServiceAccount     pacsjob-writer
Secret             pacsrun-runpod
Secret             (사용자 것, 예: git 토큰)
Pod Identity association   (namespace, pacsjob-writer) -> pacsrun-workload role
```

**association 은 계정을 만들 때 한 번만 만든다.** job 을 제출할 때가 아니다. 2026-08-29 에
`default/pacsjob-writer` 것이 없어서 fetch mode 가 exit 10 으로 멈춘 적이 있다. terraform 에
선언은 있었는데 apply 가 안 돼 있었다.

## 시점이 셋이다

```
1. 계정을 만들 때   namespace 와 S3 prefix 를 만든다        한 번만, terraform
2. 제출할 때        script 와 데이터를 올린다               매번, presigned URL
3. 실행할 때        원격 기계가 데이터를 가져간다            매번, git clone 또는 S3
```

### 제출할 때 — presigned URL

```
사용자가 [올리기] 클릭
  ↓
서버가 presigned URL 을 발급          POST /v1/uploads
  ↓
브라우저가 그 URL 로 직접 S3 에 올림   (서버를 거치지 않는다)
  ↓
s3://<bucket>/pacsrun/<namespace>/_uploads/run.sh
```

**서버가 자기 권한으로 서명해 준 일회용 주소**다. 사용자에게 AWS 자격증명을 주지 않고도
올릴 수 있고, 큰 파일이 서버를 거치지 않아 서버가 가볍다.

### 실행할 때 — 데이터 경로 둘

```
경로 A  private git repo 에서 clone      데이터가 repo 안에 있을 때
        run.sh 1단계가 git clone --depth 1, 토큰은 Secret 으로 주입
        매번 최신을 받는다 (평가 정답표 정정이 자동 반영된 이유)

경로 B  S3 에 올려 두고 내려받기          데이터가 로컬에 있을 때
```

**경로 A 가 기본이다.** 우리 실험 8 건이 전부 이 경로였다.

## 미결

1. **namespace 이름 규칙.** `lab-<사용자>` 인지 팀 단위인지.
2. **bucket 을 나눌 것인가 prefix 로 둘 것인가.** prefix 가 간단하지만 bucket 을 나누면
   요금과 lifecycle 을 따로 볼 수 있다.
3. **사용자끼리 서로의 job 을 볼 수 있게 할 것인가.** 지금 판단은 못 보게 하는 쪽이다.

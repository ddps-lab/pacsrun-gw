# 다중 사용자 — 사람 단위 격리, 팀 단위 통계 (2026-09-01)

`08-plan.md` 미결 항목 5 였습니다. 요구가 둘인데 서로 반대 방향이라 어느 쪽을 kubernetes 에
맡길지가 결정의 전부였습니다.

```
연구원은 자기 job 만 보여야 한다
연구실은 팀 단위 수치를 보고 싶다
```

## 결정 — namespace 는 사람, 팀은 그 namespace 에 붙는 이름표

```
namespace   ddps-alice, ddps-bob, ...        사람마다 하나
label       ddpsrun.io/team=ddps             사람이 kubectl -l 로 묶어 볼 때
S3          pacsrun/ddps-alice/              namespace 단위
IAM role    namespace 마다 하나, 자기 prefix 만
token       { user, namespace, team }
```

**격리는 kubernetes 가 합니다.** 조회가 그 사람 namespace 안에서만 일어나므로, 남의 job_id
를 알아도 없는 것으로 나옵니다.

**namespace 를 팀으로 두는 안을 버린 이유가 이것입니다.** 그렇게 하면 팀 수치는 쉬워지지만
사람 단위 격리가 서버의 필터링에 걸립니다. **서버에 버그 하나가 나면 한 사람이 남의 작업을
보게 됩니다.** 그 위험을 코드 품질에 맡기지 않기로 했습니다.

## S3 를 팀별로 나누려다 되돌린 기록

처음에는 경로를 `pacsrun/<team>/<user>/` 로 나눴습니다. 팀 지출이 prefix 목록 조회 한 번이
되니 좋아 보였습니다.

**되돌렸습니다. 두 가지가 걸립니다.**

- **팀 수치는 PacsJob 에서 나오지 S3 key 에서 나오지 않습니다.** 서버가 자기 token 파일에서
  팀 구성원을 압니다. 그러니 그 분할이 사는 곳이 없었습니다.
- **controller 의 자물쇠가 한 줄 규칙을 잃습니다.** 자물쇠는 namespace 만 알기 때문에, 팀을
  알려면 namespace 를 dash 로 잘라야 합니다. **팀 이름이 `ddps-lab` 이 되는 순간 깨집니다.**

`tenants.tf` 주석에 그대로 남겨 두었습니다. 조용히 되돌리면 다음 사람이 같은 길로 갑니다.

## 팀 통계 — `GET /v1/stats`

```
$ ddpsrun stats
팀 ddps
  사람               작업   성공   실패    실행중    GPU 시간        비용
  alice             3    1    2      0      7.54     $7.46
  bob               2    1    0      1       1.0     $7.92
  합계                5                       8.54    $15.38
  참고        1 job(s) ran on a machine we have no measured price for, so their
              hours are counted and their cost is not. The total below is
              therefore a floor, not the bill.
```

**같은 팀이면 같은 합계를 봅니다.** 그런데 개별 job 은 못 봅니다.

```
$ ddpsrun status <bob 의 job>   -> HTTP 404  no such job
```

**팀에 속한다는 것이 남의 job 을 읽을 권리는 아닙니다.** 응답에 job 이름조차 안 들어갑니다.

## 팀 구성원을 어디서 아는가

**서버 자신의 token 파일입니다.** cluster 에 안 물어봅니다.

```json
{ "tokens": [
    { "sha256": "…", "user": "alice", "namespace": "ddps-alice", "team": "ddps" },
    { "sha256": "…", "user": "bob",   "namespace": "ddps-bob",   "team": "ddps" }
] }
```

대안은 namespace 에 label 을 붙이고 그것을 list 하는 것이었습니다. 동작은 하는데 **서버에
cluster 전체 namespace 를 list 할 권한을 새로 줘야 합니다.** 이미 아는 것을 물어보려고 권한을
넓히는 셈이라 안 했습니다.

label 은 그대로 붙입니다. **사람이 `kubectl get ns -l ddpsrun.io/team=ddps` 로 볼 때 쓰고,
서버는 안 읽습니다.**

## 비용이 계산 가능해진 이유

**2026-09-01 에 PACSrun 에 시각 필드 둘이 생겼습니다** (`PACSRUN-JOB-CLOCK`). 그 전에는
`PacsJobStatus` 에 끝난 시각이 없어서, job 이 얼마 들었는지는 **돌아가는 동안 지켜봐야만**
알 수 있었습니다.

```
status.startedAt    처음 Running 에 도달한 시각
status.finishedAt   terminal phase 에 도달한 시각
```

**`metadata.creationTimestamp` 가 아닙니다.** 그 둘 사이에 solve 대기, 기계 대기, image pull
이 들어갑니다. 보통 몇 분이고 한 번은 1,800 초였습니다. 그것을 학습 시간으로 세면 모든 수치가
같은 방향으로 틀립니다.

살아 있는 cluster 에서 확인했습니다.

```
# kubectl apply -f clockcheck.yaml   (mode: compare, 아무것도 사지 않음)
  status.phase                Compared
  status.startedAt            (없음)
  status.finishedAt           2026-09-01T00:12:45Z
  제출부터 판정까지          2초
```

`compare` 는 컨테이너를 안 띄우므로 `startedAt` 이 없는 것이 정확한 동작입니다.

## 못 세는 것을 못 센다고 말합니다

```
비용 = 시간 × 그 offering 의 시간당 단가 × parallelism
```

**단가를 모르는 기계는 0 이 아니라 "못 셈" 으로 셉니다.** 0 으로 처리하면 합계가 완전해
보이면서 낮습니다. 그래서 `unpriced_jobs` 를 따로 세고 `note` 로 말합니다.

`parallelism` 이 곱해지는 것은 pod 마다 자기 기계를 잡기 때문입니다. 위 표에서 bob 의 1 시간
job 이 $7.92 인 것이 pod 8 개짜리라서입니다.

## 켜는 순서

**둘 다 기본값이 꺼짐이라 지금 도는 job 에는 영향이 없습니다.** 켤 때는 순서가 있습니다.

```
1. terraform 으로 tenant 를 만든다   (-target 필수, 아래 참고)
2. 출력이 찍어 주는 kubectl 명령 셋을 tenant 마다 실행
3. 마지막에 operator 의 PACSRUN_RESULT_PREFIX_TEMPLATE 를 켠다
```

**3 번이 마지막인 이유는, 자물쇠를 먼저 켜면 아직 없는 prefix 를 요구해서 모든 job 이
거부되기 때문입니다.**

## terraform 을 그냥 apply 하면 안 됩니다

`pacsrun_tenants` 를 비운 채로 plan 을 돌려도 이렇게 나옵니다.

```
Plan: 6 to add, 3 to change, 2 to destroy.

  # aws_iam_role.aws_driver[0] will be destroyed
  # (because index [0] is out of range for count)
```

**tenant 자원은 그 목록에 0 건입니다.** 전부 이전부터 있던 drift 이고, 첫 줄은 살아 있는
driver role 이 사라지는 것입니다. 그래서 대상을 찍어서 적용합니다.

```
terraform plan -out=/tmp/tenants.tfplan \
  -target=aws_iam_role.tenant \
  -target=aws_iam_role_policy.tenant \
  -target=aws_eks_pod_identity_association.tenant
```

## 확인 안 된 것

1. **실제 tenant 를 만들어 본 적이 없습니다.** `pacsrun_tenants` 가 아직 비어 있습니다.
2. **`startedAt` 을 실물로 못 봤습니다.** Running 까지 가려면 GPU 를 빌려야 합니다.
   `finishedAt` 은 compare job 으로 확인했습니다.
3. **RunPod Secret 을 namespace 마다 복사하는 것을 자동화하지 않았습니다.** driver pod 이
   `LocalObjectReference` 로 읽어서 자기 namespace 만 봅니다. 지금은 출력이 명령을 찍어 줄
   뿐입니다.

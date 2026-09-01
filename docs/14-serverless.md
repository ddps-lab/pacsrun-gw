# 서버를 pod 에서 Lambda 로, 화면을 S3 와 CloudFront 로 (2026-09-01)

`08-plan.md` 미결 항목 8 과 12 를 함께 닫는 결정입니다. 지금까지 만든 것은 EKS 안의 pod 으로
도는 FastAPI 서버였고, 그 앞에 무엇을 둘지가 정해지지 않아 아무도 쓸 수 없는 상태였습니다.

## 결정

```
API      Lambda 함수 하나 + Function URL
화면     S3 에 정적 파일, CloudFront 로 배포
인증     Lambda 실행 role 을 EKS access entry 에 등록
로그     streaming 을 버리고 polling 으로 바꾼다
```

## 왜 바꾸는가 — 값이 아니라 세울 것의 수 때문입니다

pod 으로 두면 밖에서 닿게 하는 일이 남습니다. 그것을 하려면 이만큼을 세우고 유지해야 합니다.

```
Gateway API CRD          kubernetes-sigs 가 배포하는 yaml
AWS Load Balancer Controller  + IAM 정책 + Pod Identity
GatewayClass, Gateway    -> 여기서 ALB 가 생긴다
HTTPRoute                우리 Service 를 붙인다
ACM 인증서               도메인 소유 확인
Route53 레코드
```

**Lambda Function URL 은 HTTPS 주소가 함수에 딸려 나옵니다.** 위 여섯 줄이 전부 없어집니다.

값도 줄지만 그쪽이 결정적이지는 않습니다.

| | 월 |
|---|---|
| ALB | $16.43 + LCU 약 $5.84 = **$22.27** |
| Lambda + Function URL | 무료 등급 안. 요청 100 만 건, 400,000 GB-초 |

요청 1 만 건을 512 MB 에서 200 ms 로 잡으면 1,000 GB-초입니다. 무료 등급의 0.25% 입니다.

## 코드는 라우트도 모델도 안 바뀝니다

FastAPI 가 하는 일은 "URL 을 함수에 연결하고 본문을 선언한 형식과 대조하는 것" 이고, 그것은
서버 프로세스가 있어야 되는 일이 아닙니다. 어댑터를 하나 얹으면 그대로 돕니다.

```python
# pod
uvicorn ddpsrun_server.main:app --port 8080

# Lambda
from mangum import Mangum
handler = Mangum(app)
```

`main.py` 의 라우트, `models.py` 의 요청 형식, 생성되는 OpenAPI 문서가 전부 그대로입니다.

## 배포는 zip 으로 됩니다. 이미지가 필요 없습니다

Lambda 에 코드를 올리는 방법이 둘입니다.

| | 크기 한도 | 우리에게 |
|---|---|---|
| zip | 압축 해제 **250 MB** | 들어갑니다 |
| 컨테이너 이미지 | 10 GB | 필요 없습니다 |

재 봤습니다.

```
kubernetes                74.0 MB
pydantic_core              4.3 MB
pydantic                   3.5 MB
anyio                      1.7 MB
fastapi                    1.3 MB
...
합계                        92.4 MB
```

**92.4 MB 로 250 MB 한도의 37% 입니다.** `kubernetes` 하나가 74 MB 로 대부분을 차지하는데,
그것마저 넣고도 여유가 있습니다.

**그래서 어제 만든 ECR 저장소는 이 경로에서 쓰이지 않습니다.** `ddpsrun/gateway` 는 pod 으로
띄우는 전제로 만든 것이었습니다. 지우지는 않습니다 — pod 으로 되돌릴 판단이 남아 있고,
저장 비용이 이미지 20 개 기준 월 $0.40 입니다.

## 인증이 바뀌는 자리 — 여기가 유일하게 실질적인 변경입니다

지금 서버는 cluster **안**의 pod 이라 kubelet 이 넣어 준 ServiceAccount token 을 씁니다.

```
# kubectl -n pacsrun-system exec deploy/pacsrun-operator -- ls /var/run/secrets/kubernetes.io/serviceaccount/
ca.crt
namespace
token

# 그 token 의 payload
iss : https://oidc.eks.us-west-2.amazonaws.com/id/<CLUSTER_OIDC_ID>
sub : system:serviceaccount:pacsrun-system:pacsrun-operator
aud : ['https://kubernetes.default.svc']
```

**Lambda 는 cluster 밖이라 그 파일이 없습니다.** 대신 실행 role 이 IAM 신원이므로, 사람이
kubectl 로 붙는 것과 같은 길을 씁니다.

```
지금     서버 pod  --ServiceAccount token (JWT)-->      apiserver
Lambda   함수      --EKS token (presigned STS URL)-->   apiserver
```

- Lambda 실행 role 을 **access entry 에 등록**하고 `kubernetesGroups` 에 group 하나를 넣습니다.
- 지금 써 둔 ClusterRole 을 그 group 에 걸면 **RBAC 은 한 줄도 안 바뀝니다.**
- cluster endpoint 가 public 이라 **Lambda 를 VPC 에 붙일 필요가 없습니다.** NAT gateway 도
  안 생깁니다.
- 사용자 token 파일은 Secret 마운트가 없으니 Secrets Manager 나 Parameter Store 로 옮기고
  cold start 마다 한 번 읽습니다.

## 화면은 S3 와 CloudFront 로

연구실이 이미 쓰는 방식입니다.

```
# aws cloudfront list-distributions
ddps.cloud            <- s3-website.ap-northeast-2.amazonaws.com
spotlake.ddps.cloud   <- s3-website-us-west-2.amazonaws.com
profet.ddps.cloud     <- s3-website-us-west-2.amazonaws.com
```

```
[브라우저] --HTML, CSS, JS--> [CloudFront] --> [S3]        화면 파일
     │
     └-------- fetch() ------> [Lambda Function URL]        데이터
```

화면 파일은 정적입니다. 서버가 HTML 을 그리지 않고, 브라우저의 JavaScript 가 `fetch()` 로
데이터를 받아 그립니다.

### 이 선택이 만드는 문제 하나 — CORS

브라우저에는 **어떤 주소에서 받아 온 페이지는 다른 주소로 요청을 보낼 수 없다**는 규칙이
있습니다. 동일 출처 정책입니다.

```
페이지를 받은 곳   https://run.ddps.cloud        (CloudFront)
요청을 보낼 곳     https://<id>.lambda-url…      (Lambda)
                   주소가 다르다 -> 브라우저가 막는다
```

받는 쪽이 허용 헤더를 붙이면 통과합니다.

```
Access-Control-Allow-Origin: https://run.ddps.cloud
Access-Control-Allow-Headers: Authorization, Content-Type
Access-Control-Allow-Methods: GET, POST
```

Function URL 에 그 설정 칸이 있습니다. 다만 **요청 전에 브라우저가 `OPTIONS` 로 한 번 더
물어보는 왕복이 생깁니다.** preflight 라고 합니다.

**서버가 HTML 까지 그리는 방식이었다면 이 문제가 없었습니다.** 페이지와 API 가 같은 주소이기
때문입니다. 정적 분리를 고른 대가가 이것이고, 설정 한 번으로 끝나는 종류입니다.

## 로그를 streaming 에서 polling 으로

**이것이 이번 결정에서 유일하게 사용자에게 보이는 변화입니다.**

Lambda 는 한 번 실행이 최대 15 분입니다. 30 시간짜리 학습 로그를 한 연결로 흘려보낼 수
없습니다.

### 세 가지를 먼저 구분합니다

| | 연결 | 누가 먼저 말하나 | 이 시스템의 예 |
|---|---|---|---|
| streaming | 하나를 계속 붙듦 | 서버가 새 줄마다 밀어 줌 | 지금의 `logs --follow` |
| polling | 요청마다 열고 닫음 | 클라이언트가 반복해 물어봄 | `watch`, 그리고 앞으로의 `logs` |
| watch | 하나를 계속 붙듦 | apiserver 가 객체 변화를 밀어 줌 | operator 가 PacsJob 을 보는 방식 |

**Lambda 는 아무것도 반복하지 않습니다.** 반복하는 것은 클라이언트이고, Lambda 는 요청 하나에
apiserver 호출 하나를 하고 사라집니다. 지금의 FastAPI 서버도 요청당 호출 하나인 것은 같고,
켜져 있다는 것만 다릅니다.

### 중복을 어떻게 거르는가

요청에 `timestamps=True` 를 넣으면 apiserver 가 줄마다 시각을 붙여 줍니다. **클라이언트는
마지막으로 본 시각 하나만 기억하면 됩니다.**

```python
fresh = [l for l in lines if l.split(" ", 1)[0] > seen_until]
if lines:
    seen_until = lines[-1].split(" ", 1)[0]
```

**서버는 아무것도 기억하지 않습니다.** Lambda 가 매번 새로 깨어나도 되는 이유가 이것입니다.

### 실측

2 초마다 한 줄을 찍는 pod 을 만들고 6 초 간격으로 세 번 물어봤습니다.

```
[1회차] 응답 413ms | 창 안의 줄 15개 | 새 줄 15개
           2026-09-01T05:16:13.280  line 11
           ...
           2026-09-01T05:16:41.296  line 25
[2회차] 응답 137ms | 창 안의 줄 15개 | 새 줄 3개
           2026-09-01T05:16:43.298  line 26
           2026-09-01T05:16:45.299  line 27
           2026-09-01T05:16:47.300  line 28
[3회차] 응답 141ms | 창 안의 줄 15개 | 새 줄 3개
           2026-09-01T05:16:49.301  line 29
           2026-09-01T05:16:51.302  line 30
           2026-09-01T05:16:53.304  line 31
```

- **창 안의 줄이 매번 15 개로 같습니다.** 30 초 창에 2 초당 한 줄이니 항상 15 줄이고, 매번
  겹치는 것을 받습니다.
- **새 줄은 6 초 동안 생긴 3 개뿐입니다.** 나머지 12 줄은 버립니다.
- 응답이 137ms 입니다. 붙들고 있는 연결이 없습니다.

### 창 크기

```
좁으면 (30초)   겹침이 적어 가볍다.  대신 그 시간 안에 못 물어보면 놓친다
넓으면 (5분)    안 놓친다.           대신 매번 열 배를 받는다
```

**물어보는 간격의 서너 배로 잡습니다.** 6 초 간격이면 창은 30 초이고, 위 실측이 그 조합입니다.

### 요청 수

30 시간짜리 job 을 6 초 간격으로 보면 18,000 건입니다. **Lambda 무료 등급이 월 100 만 건이라
문제가 되지 않습니다.**

## cold start 를 쟀습니다 (2026-09-01). 4.1 초입니다

우리 의존성을 그대로 담은 Lambda 를 하나 만들어 재고 지웠습니다. 설정을 바꾸면 다음 호출이
반드시 새 컨테이너에서 도는 것을 이용했습니다.

```
압축 해제 105.5 MB, memory 512MB

  회차 종류       Init(ms)     Duration(ms) import(s)
  1      cold         4138.86      1.74         4.056
  1      warm         -            1.53         -
  2      cold         4136.22      1.99         4.05
  2      warm         -            1.49         -
  3      cold         4044.78      1.70         3.948
  3      warm         -            1.69         -
```

**Init 의 거의 전부가 import 입니다.** 실행 자체는 1.7ms 이고 warm 은 1.5ms 입니다. 즉 함수가
하는 일이 느린 것이 아니라 **짐을 푸는 데 4 초가 걸립니다.**

### 원인은 패키지 크기입니다

`kubernetes` client 를 빼고 다시 쟀습니다.

```
kubernetes 있음   105.5 MB   Init 4138 / 4136 / 4044 ms
kubernetes 없음    36.9 MB   Init 1307 / 1532 / 1619 ms
```

**4.1 초가 1.5 초가 됩니다.** 그리고 메모리를 올려도 안 줄어듭니다.

```
memory 1024MB     Init 1587 / 1554 ms
```

Lambda 는 메모리에 비례해 CPU 를 주므로, **메모리를 두 배로 줘도 그대로라는 것은 연산이 아니라
파일을 읽는 시간이라는 뜻입니다.** 크기가 지배합니다.

참고로 로컬에서 각 라이브러리의 import 시간만 재면 다 합쳐 530ms 입니다.

```
fastapi           199 ms
kubernetes        288 ms
mangum             30 ms
pydantic           15 ms
```

**4 초와 530ms 의 차이가 Lambda 의 파일 읽기입니다.**

### 그래서 무엇을 할 것인가 — 아직 정하지 않았습니다

우리가 `kubernetes` client 에서 실제로 쓰는 것은 다섯 개뿐이고 전부 평범한 REST 호출입니다.

```
self._custom.create_namespaced_custom_object
self._custom.get_namespaced_custom_object
self._custom.list_namespaced_custom_object
self._core.list_namespaced_pod
self._core.read_namespaced_pod_log
```

**74 MB 를 이 다섯 줄 때문에 들고 있는 셈입니다.** `requests` 로 직접 부르면 됩니다.

다만 그 client 가 대신 해 주던 일이 남습니다.

```
kubeconfig 읽기        Lambda 에는 kubeconfig 가 없으므로 어차피 우리가 만든다
EKS token 만들기       STS presigned URL 을 우리가 서명해야 한다
CA 인증서 처리         eks:DescribeCluster 로 받아 임시 파일에 쓴다
응답 역직렬화          dict 그대로 쓰면 된다.  우리는 이미 그렇게 쓴다
```

**세 번째까지가 실제 작업입니다.** 1.5 초를 위해 그것을 직접 짤지는 별도 판단이라 미결로
둡니다 (미결 17).

## 남는 제약 하나

**15 분 상한.** 로그를 polling 으로 바꾸면 이 상한에 닿는 라우트가 없어지지만, 앞으로 오래
걸리는 일을 라우트로 만들면 다시 걸립니다. 그런 일은 Lambda 에 두지 않는다는 규칙으로 둡니다.

## node 는 이 결정과 무관합니다

Lambda 로 옮겨도 EKS node 는 그대로 필요합니다. driver pod 때문이 아니라 **항상 떠 있어야 하는
Deployment 셋** 때문입니다.

| | 왜 항상 있어야 하나 |
|---|---|
| `pacsrun-operator` | PacsJob 을 reconcile 합니다. 없으면 제출해도 객체만 저장됩니다 |
| `karpenter` | GPU node 를 사 옵니다 |
| `coredns` | cluster 안의 이름 해석입니다 |

driver pod 은 원인이 아닙니다. 재본 값이 있습니다.

```
requests: cpu 100m / memory 256Mi, nvidia.com/gpu 없음
기존 CPU node 에 앉고 NodeClaim 은 0
8 분짜리 job 에서 $0.00069, job 총액의 0.6%
```

node 값은 따로 줄입니다. 지금 550 milli / 652 MiB 를 쓰는데 2 vCPU / 8 GB 를 사고 있습니다.

```
m5.large 상시   $0.103/hr × 730 = 월 $75.19    ($0.096 + gp3 $0.002 + IPv4 $0.005)
t3.small 상시   $0.0278/hr × 730 = 월 $20.29
```

**instance type 만 바꿔도 월 $54.90 입니다.** 그 위에 scale-to-zero 를 얹으면 하루 4 시간
기준으로 월 $17 을 더 아끼는데, 대가가 EventBridge 와 coredns 자동 삭제와 첫 제출 대기입니다.
coredns 는 우리가 이미 겪은 것이라 저절로 풀리지 않습니다.

> scale-to-zero 하면 drain 이 coredns 2 개 중 하나만 evict 하고, 대체 pod 이 앉을 node 가 없어
> Pending 이 되면서 ALLOWED DISRUPTIONS 가 0 으로 떨어져 나머지 하나가 영원히 안 나간다.

**instance type 을 먼저 바꾸고, scale-to-zero 는 나중에 판단합니다.**

다만 그 전에 하나 고쳐야 합니다. karpenter 가 resource requests 를 선언하지 않았습니다.

```
kube-system/karpenter    replicas=1  requests={}
```

## 만드는 순서

```
1. ~~CLI 의 logs 를 polling 으로~~   2026-09-01 완료.  아래 참고
2. Lambda 함수와 Function URL       mangum 어댑터, zip 배포, CORS 설정
3. 실행 role 을 access entry 에      kubernetesGroups 로 기존 ClusterRole 재사용
4. token 을 Secrets Manager 로
5. 정적 화면을 S3 에, CloudFront 배포
6. karpenter requests 를 채우고 t3.small 로
```

### 1 번은 끝났습니다 (2026-09-01)

`GET /v1/jobs/{id}/logs` 가 stream 이 아니라 **창 하나를 JSON 으로** 돌려줍니다.

```
GET /v1/jobs/<id>/logs?since=<지난번 last_timestamp>&window_seconds=30&max_lines=2000

{ "lines": ["2026-09-01T00:00:01.000Z {'loss': 0.85}", ...],
  "last_timestamp": "2026-09-01T00:00:06.000Z",
  "window_seconds": 30 }
```

- `since` 보다 뒤엣것만 돌아옵니다. **서버는 그것을 기억하지 않습니다.** 요청에 실려 옵니다.
- 비교는 그냥 문자열 비교입니다. apiserver 가 찍는 RFC 3339 는 소수 자리가 고정이라 **문자열
  순서가 곧 시간 순서**입니다.
- `window_seconds` 와 `max_lines` 에 상한이 있습니다. 없으면 요청 하나가 서른 시간짜리 로그를
  몇 초마다 읽습니다.

CLI 는 `--interval` 마다 다시 묻고, 창은 그 다섯 배로 잡습니다. 왕복이 한 번 느려도 놓치지
않기 위해서입니다. 실제로 돌려 보면 이렇습니다.

```
$ ddpsrun logs job-0a2f9c2fc625 --follow --interval 6
  (3회 요청 뒤 Ctrl-C)
{'loss': 0.85}
{'loss': 0.80}
{'loss': 0.75}
{'loss': 0.70}
{'loss': 0.65}
{'loss': 0.60}
```

**여섯 줄이 한 번씩만 나옵니다.** 세 번 다 15 줄짜리 창을 받았는데 중복이 없고,
`PACSRUN_KEEPALIVE` 도 걸러졌습니다. 시각은 다음 요청용 기록이라 화면에는 안 찍습니다.

1, 4 는 이 저장소 안의 코드이고 2, 3, 5, 6 은 terraform 입니다. CI 는 이미지를 굽는 대신
zip 을 만들어 올리는 형태로 바꿉니다.

## Lambda 신원으로 apiserver 에 붙는 것을 확인했습니다 (2026-09-01)

Lambda 를 실제로 만들지 않고, **Lambda 가 쓸 것과 같은 IAM role** 을 만들어 그 신원으로
확인했습니다. 끝나고 전부 지웠습니다.

```
=== 4. role 을 맡는다 ===
  지금 신원: arn:aws:sts::<ACCOUNT_ID>:assumed-role/ddpsrun-gw-lambda-probe/lambda-probe

=== 5. 그 신원으로 apiserver 를 부른다 ===
Username   arn:aws:sts::<ACCOUNT_ID>:assumed-role/ddpsrun-gw-lambda-probe/lambda-probe
Groups     [ddpsrun-gw-probe-group system:authenticated]

  can-i list pacsjobs    : no
  실제 조회:
Error from server (Forbidden): pacsjobs.pacsrun.io is forbidden: User ".../lambda-probe"
cannot list resource "pacsjobs" in API group "pacsrun.io" at the cluster scope
```

**`Forbidden` 이 성공입니다.** 읽는 법이 이렇습니다.

- `Username` 이 그 role 입니다. apiserver 가 신원을 알아봤다는 뜻이고 **인증이 통과했습니다.**
- `Groups` 에 access entry 에 적어 넣은 `ddpsrun-gw-probe-group` 이 들어 있습니다.
  **`kubernetesGroups` 가 실제로 kubernetes group 으로 전달됩니다.** 이것이 확인하려던 것입니다.
- 막힌 것은 인가입니다. 붙여 둔 `view` ClusterRole 에 CRD 인 `pacsjobs` 가 없습니다.
  **인증이 막혔다면 `Unauthorized` 가 나옵니다.**

실제로 쓸 때는 `view` 대신 `config/deploy/rbac.yaml` 의 ClusterRole 을 그 group 에 겁니다.

### 그 과정에서 알게 된 것 둘

**IAM role 을 만든 직후에는 access entry 에 등록되지 않습니다.**

```
=== 2. access entry 등록.  IAM 전파를 기다리며 재시도 ===
  1회차 대기 (IAM 전파)
  2회차 대기 (IAM 전파)
  3회차에 성공
```

5 초 간격으로 재시도해서 **3 회차, 약 10 초 뒤에** 붙었습니다. 첫 시도는 이것 때문에
`InvalidParameterException: The specified principalArn is invalid` 로 실패했습니다.
terraform 에서는 `depends_on` 이나 재시도가 필요합니다.

**access entry 의 `username` 에 세션 이름이 들어갑니다.**

```
"user": "arn:aws:sts::<ACCOUNT_ID>:assumed-role/ddpsrun-gw-lambda-probe/{{SessionName}}"
```

세션 이름은 실행마다 달라집니다. **그래서 RBAC 을 username 이 아니라 group 에 걸어야 합니다.**
`kubernetesGroups` 를 쓰기로 한 것이 결과적으로 맞았습니다.

### 첫 시도가 왜 틀렸는지도 적어 둡니다

실패했는데 **출력이 성공처럼 보였습니다.**

```
=== 2. access entry 등록 ===
An error occurred (InvalidParameterException) ... invalid principal.

=== 4. role 을 맡아서 ... ===
  맡은 신원: arn:aws:iam::<ACCOUNT_ID>:user/<IAM_USER>
  can-i list pacsjobs : yes
```

`assume-role` 도 조용히 실패해서 **뒤 단계가 원래 신원의 admin 권한으로 돌았습니다.** 그 `yes`
는 probe role 의 답이 아니었습니다. 두 번째 시도에서는 단계마다 신원을 확인하고 아니면
중단하게 고쳤습니다. **확인 script 는 각 단계가 실제로 무엇으로 돌고 있는지를 찍어야 합니다.**

## 확인 안 된 것

1. **polling 으로 바꾼 `logs` 를 긴 job 에 붙여 본 적이 없습니다.** 확인은 40 줄짜리 pod 과
   가짜 cluster 로 했습니다. 서른 시간짜리 job 에서 창 크기와 간격이 맞는지는 안 봤습니다.
2. **cold start 를 줄이는 쪽은 재기만 했습니다.** `kubernetes` client 를 빼면 1.5 초가 되는
   것은 확인했지만, 그것 없이 apiserver 를 부르는 코드는 아직 없습니다.

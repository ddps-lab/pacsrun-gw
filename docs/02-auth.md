# 인증 — 두 구간으로 나뉜다 (2026-08-31)

사용자에서 클러스터까지 가는 길이 두 구간이고, **구간마다 인증 방식이 완전히 다르다.**

```
[브라우저 UI]  또는  [ddps CLI]
      │
      │  구간 1   Authorization: Bearer <ddpsrun token>
      │           우리가 발급한다. AWS 도 kubernetes 도 모른다
      ▼
[pacsrun-gw 서버]   IAM role 을 여기가 든다
      │
      │  구간 2   Authorization: Bearer k8s-aws-v1.<base64 서명 URL>
      │           AWS 방식. 서버가 자기 IAM 으로 만든다
      ▼
[kube-apiserver]
```

**서버가 신원 경계다.** 그 앞은 우리 token, 그 뒤는 AWS IAM 이다.

---

## 구간 2 를 먼저 — EKS 가 사람을 어떻게 알아보는가

이쪽이 이미 정해져 있으므로 먼저 적는다. 우리가 설계할 것은 구간 1 이다.

### kubeconfig 에는 비밀이 없다

```
=== cluster 항목 ===
  server: https://<CLUSTER_HOST>....eks.amazonaws.com
  certificate-authority-data: LS0tLS1CRUdJ...(1476자)

=== user 항목 ===
  exec.command: aws
  exec.args: ['--region', 'us-west-2', 'eks', 'get-token', '--cluster-name', 'pacsrun', ...]

  client-certificate-data 가 있나 -> False
  client-key-data 가 있나         -> False
```

- **client 인증서가 없다.** EKS 는 mTLS 를 쓰지 않는다. bearer token 방식이다.
- `certificate-authority-data` 는 **서버 인증서를 검증하는 CA** 다. 내가 나를 증명하는
  인증서가 아니다. 방향이 반대다. 풀어 보면 이렇다.

```
  PEM 머리: -----BEGIN CERTIFICATE-----
  subject=CN=kubernetes
  issuer=CN=kubernetes
              Public-Key: (2048 bit)
                  CA:TRUE
  PRIVATE KEY 문자열: 0건
```

- `user` 항목은 **"이 명령을 실행해 token 을 받아라"** 만 적혀 있다. 비밀은
  `~/.aws/credentials` 에 따로 있다.

### token 은 JWT 가 아니라 서명된 URL 이다

`aws eks get-token` 이 주는 것을 풀면 이렇다.

```
1단계  k8s-aws-v1.aHR0cHM6Ly9zdHMudXMtd2VzdC0yLmFtYXpvbmF3cy5jb20v...  (493자)
2단계  앞: k8s-aws-v1        뒤: base64 482자
3단계  뒤를 풀면
       https://sts.us-west-2.amazonaws.com/?Action=GetCallerIdentity&...&X-Amz-Signature=233afd...
4단계  Authorization: Bearer k8s-aws-v1.aHR0cHM6...
```

query string 의 조각들이다.

```
  Action                : GetCallerIdentity
  X-Amz-Algorithm       : AWS4-HMAC-SHA256
  X-Amz-Credential      : <AKID>.../20260831/us-west-2/sts/aws4_request
  X-Amz-Date            : 20260831T022555Z
  X-Amz-Expires         : 60
  X-Amz-Signature       : 233afdbc68c95ec0...(64자 hex)
  X-Amz-SignedHeaders   : host;x-k8s-aws-id
```

**층이 세 겹이다.** 서명은 URL 의 query string 에 있고, 그 URL 전체가 base64 로 싸여 header
안에 있다.

### 세 쪽이 아는 것이 다르다

| | secret key 를 아는가 | 하는 일 |
|---|---|---|
| client | **안다** | 로컬에서 서명한다. 네트워크를 안 쓴다 |
| apiserver | **모른다** | base64 를 풀어 그 URL 을 STS 에 부친다 |
| STS | **안다** | 서명을 다시 계산해 맞춰 보고 이름을 답한다 |

- **apiserver 가 서명을 "푸는" 일은 없다.** base64 디코딩은 암호 해제가 아니라 포장 풀기다.
- STS 가 답하는 것은 자격증명이 아니라 **신원 문자열**이다.
  `arn:aws:iam::<ACCOUNT_ID>:user/<IAM_USER>` 같은 것이다.
- `x-k8s-aws-id` 가 서명 대상에 들어 있어 **다른 cluster 로 재사용할 수 없다.**

### 그 ARN 이 kubernetes 권한이 된다

```
STS 가 답한 ARN  ->  access entry 에서 찾음  ->  kubernetes 그룹  ->  RBAC 검사
```

지금 등록된 목록이다.

```
    "arn:aws:iam::<ACCOUNT_ID>:role/<KARPENTER_ROLE>",
    "arn:aws:iam::<ACCOUNT_ID>:role/<NODE_GROUP_ROLE>",
    "arn:aws:iam::<ACCOUNT_ID>:role/<GITHUB_ACTIONS_ROLE>",
    "arn:aws:iam::<ACCOUNT_ID>:user/<IAM_USER>"
```

**목록에 없는 ARN 은 인증돼도 아무것도 못 한다.** 같은 AWS 계정의 다른 IAM user 도 마찬가지다.

### TLS 는 별개다

```
  Protocol: TLSv1.3
  Cipher is TLS_AES_128_GCM_SHA256
  Peer signature type: rsa_pss_rsae_sha256
```

TLS 1.3 이라 **서버의 RSA 키는 암호화가 아니라 서명에만 쓴다.** 세션 키는 ECDHE 로 양쪽이
각자 계산하고 네트워크에 안 나간다. 개인키가 나중에 새도 과거 통신은 못 푼다.

| | 무엇으로 | 무엇을 위해 |
|---|---|---|
| 암호화 | ECDHE 임시 세션 키 | 내용을 가림 |
| 인증 | 서버 RSA 인증서와 CA | 상대가 진짜인지 확인 |

---

## 구간 1 — 우리가 설계할 것

### 왜 EKS token 을 사용자에게 줄 수 없는가

1. **목표 사용자에게 AWS 신원이 없다.** EKS token 은 IAM 자격증명으로 서명해야 만든다.
2. **있어도 주면 안 된다.** 만들 수 있다는 것은 secret key 를 갖고 있다는 뜻이고,
   브라우저에 두면 사용자가 그대로 본다.
3. **우리가 알아야 할 것이 다르다.** EKS token 은 "어느 IAM 인가" 만 답한다. 우리는
   "어느 namespace 인가" 를 알아야 한다.

### ddpsrun token 이 담는 것

```
sub          사용자 식별자
namespace    이 사용자의 namespace          <- 서버가 resultPath 와 조회 범위를 여기서 정한다
exp          만료
scope        submit, read 등
```

- **요청 본문에 namespace 를 받지 않는다.** token 에서 유도한다. 남의 namespace 를 적을 수
  없다.

### 발급자는 Cognito 로 정했다

사용자가 수십 명이 되고 UI 를 여럿이 쓸 예정이므로, 로그인 체계를 우리가 만들지 않는다.

우리가 안 만들어도 되는 것이 이만큼이다.

```
가입, 로그인, 비밀번호 저장(해시), 재설정, 세션 만료, 실효 처리, MFA, 소셜 로그인
```

| | 우리가 token 발급 | **Cognito** |
|---|---|---|
| 만들 것 | 로그인 체계 전부 | 없음 |
| 비용 | $0 | 월 활성 사용자 50,000 명까지 무료 |
| 사용자 경험 | 우리가 token 을 건네줌 | Google 로그인 등 |
| AWS 종속 | 없음 | **있음** |
| CLI 에서 | 파일에 token 한 줄 | **브라우저 로그인 흐름이 필요** |

**마지막 줄이 이 결정의 대가다.** CLI 에서 Cognito 로 로그인하려면 브라우저를 띄우고
돌아오는 흐름을 만들어야 한다. `aws sso login` 이 하는 것과 같다. 미결 4 번이다.

### Cognito 와 OIDC 는 층이 다르다

혼동하기 쉬워 적어 둔다.

| | OIDC | Cognito |
|---|---|---|
| 성격 | **규격.** 문서 | **서비스.** AWS 가 돌려 준다 |
| 하는 일 | 신원 증명 방법을 정한다 | 사용자 목록 관리, 가입, 로그인, MFA |

**Cognito 자신이 OIDC 신원 제공자 노릇도 한다.** 그래서 Cognito 를 쓰는 것이 OIDC 를 쓰는
것이기도 하다.

### CI 에는 Cognito 를 쓰지 않는다

GitHub Actions 에는 로그인할 사람이 없다. 기계이고, **GitHub 이 이미 그 기계의 신원을
무료로 증명해 준다.** Cognito 를 억지로 넣으면 Cognito 자격증명을 GitHub Secrets 에
저장하게 되어, 만료 없는 비밀이 다시 생긴다.

그리고 GitHub Actions 의 OIDC 는 **driver pod 이 STS 에서 자격증명을 받는 것과 같은
방식**이다. 신원 제공자만 다르다.

| | 신원 제공자 | `sub` 값 |
|---|---|---|
| driver pod | EKS cluster | `system:serviceaccount:default:pacsjob-writer` |
| GitHub Actions | GitHub | `repo:ddps-lab/pacsrun-gw:*` |

### 사용자가 보는 것

```
~/.ddpsrun/config.yaml
  server: https://run.ddps-lab.example
  token: dr_a1b2c3d4e5f6...
```

**AWS 도 kubernetes 도 kubeconfig 도 나오지 않는다.**

### 서버는 무엇으로 클러스터에 붙는가

자기 IAM role 이다. 두 곳 중 하나에서 온다.

| 서버 위치 | 자격증명 출처 |
|---|---|
| EKS 안의 Deployment | EKS Pod Identity 또는 IRSA |
| EKS 밖 EC2 | instance profile |

**어느 쪽이든 파일에 secret 을 두지 않는다.** 그리고 그 role 의 ARN 을 access entry 에
등록해야 한다. terraform 작업이다.

# 서버 1단계 — 무엇을 만들었고 어떻게 돌리는가 (2026-08-31)

`08-plan.md` 의 1단계다. **라우트 넷이 전부이고 판단은 하나도 없다.**

## 만든 것

```
server/
  pyproject.toml            ddpsrun-server. fastapi, pydantic, kubernetes, uvicorn
  Dockerfile                2 단계 빌드, non-root, uvicorn 8080
  ddpsrun_server/
    config.py               DDPSRUN-CONFIG      환경변수를 한 번만 읽는다
    auth.py                 DDPSRUN-AUTH        token -> (user, namespace)
    naming.py               DDPSRUN-JOBID       job_id <-> 객체 이름
    models.py               DDPSRUN-SERVER-FILLS  요청 -> PacsJob, PacsJob -> 응답
    k8s.py                  DDPSRUN-K8S         kube-apiserver 와 로그 중계
    main.py                 DDPSRUN-ROUTES      라우트 넷
  tests/                    65 개. cluster 없이 돈다
config/deploy/
  rbac.yaml                 ClusterRole 하나 + tenant namespace 마다 RoleBinding
  server.yaml               Namespace, Deployment, Service
```

## 라우트

| 메서드 | 경로 | 하는 일 |
|---|---|---|
| GET | `/healthz` | 살아 있는지. token 불필요 |
| POST | `/v1/jobs` | 제출. `job_id` 와 `result_path` 를 돌려준다 |
| GET | `/v1/jobs/{job_id}` | phase, message, GPU, recovery 횟수 |
| GET | `/v1/jobs/{job_id}/logs?follow=true` | 로그 |
| GET | `/v1/explain` | 이 서비스가 무엇인지. token 불필요 (2단계에서 추가) |
| GET | `/v1/schema` | 제출 본문의 JSON Schema. token 불필요 (2단계에서 추가) |

`03-api.md` 의 나머지 여섯 개는 아직 없다. `/validate` 와 `/estimate` 는 3단계이고,
`/uploads` 와 `/artifacts` 는 그 다음이다.

## 제출 한 번이 지나가는 길

```
POST /v1/jobs   Authorization: Bearer <token>
  │
  ├─ 1. auth.py       token 을 sha256 으로 해싱해서 (user, namespace) 를 얻는다
  ├─ 2. naming.py     job_id 를 만든다        job-a8acdef80a07
  ├─ 3. models.py     본문 + namespace + ServiceAccount + resultPath -> PacsJob
  ├─ 4. k8s.py        그 namespace 에 PacsJob 을 create
  └─ 5. 응답          { job_id, name, result_path }
                              │
                              ▼
                       PACSrun controller 가 집어 간다 (여기부터 우리 소관 아님)
```

## 서버가 채우는 것 넷

**사용자가 못 보내는 값이고, 전부 token 에서 나온다.**

| 필드 | 무엇으로 |
|---|---|
| `metadata.namespace` | token 이 가리키는 namespace |
| `spec.serviceAccountName` | `DDPSRUN_SERVICE_ACCOUNT` |
| `spec.resultPath` | `s3://<bucket>/<prefix><namespace>/<이름>-<hex>/` |
| `spec.parallelism` | 1 고정 |

`resultPath` 를 서버가 쓰기 때문에 사용자는 남의 폴더를 적을 수 없다. **그리고 PACSrun 이
같은 접두어를 cluster 쪽에서 한 번 더 검사한다** (`PACSRUN-RESULT-TENANCY`). 두 값이 어긋나면
job 이 전부 admission 에서 거부되므로 `DDPSRUN_RESULT_PREFIX` 와
`PACSRUN_RESULT_PREFIX_TEMPLATE` 는 같은 것을 가리켜야 한다.

## 판단을 하나도 안 넣은 자리

`spec.placement` 를 **아예 안 쓴다.** region, capacityType, vendor 를 서버가 정하지 않으므로
PACSrun 이 자기 기본값을 그대로 적용한다. `expected_hours` 는 받아서
`ddpsrun.io/expected-hours` annotation 에 적어만 둔다. 3단계에서 `/estimate` 의 예측과
실제 소요를 대조할 때 쓸 자료다. **지금은 아무도 읽지 않는다.**

## 인증

`02-auth.md` 가 정한 끝 모습은 Cognito 다. 지금은 그 앞 단계로 **static token 파일**을 쓴다.
`08-plan.md` 미결 항목 4(CLI 로그인 흐름)가 안 풀렸고, 브라우저를 띄울 CLI 자체가 2단계라
지금 Cognito 를 넣으면 쓸 수단이 없다.

파일 형식이다. **token 이 아니라 token 의 sha256 을 적는다.** 이 파일은 Secret 으로 마운트되고
`kubectl get secret -o yaml` 과 백업에 그대로 나온다.

```json
{
  "tokens": [
    {"sha256": "<64 hex>", "user": "alice", "namespace": "lab-alice"}
  ]
}
```

token 하나 만들기다.

```bash
# 사용자에게 줄 값
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# 파일에 적을 값
python3 -c "import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" <TOKEN>

# Secret 으로 올리기
kubectl create secret generic ddpsrun-tokens -n ddpsrun-system \
  --from-file=tokens.json=./tokens.json
```

`auth.py` 는 시작할 때 파일을 검증하고 틀리면 **프로세스를 죽인다.** hash 자리에 token 을
붙여 넣는 실수는 그대로 동작해 버리기 때문에 형식 검사에서 잡는다.

## 응답에서 지우는 것

`03-api.md` 응답 규칙 첫째다. `status` 에 있는 `blamedNodes`, `excludedOfferings`,
`currentOffering.region`, `currentOffering.zone`, `serviceAccountName` 은 나가지 않는다.
나가는 것은 `phase`, `message`, `recoveryCount`, `currentOffering.instanceType`,
`currentOffering.vendor` 다.

로그도 거른다. driver 가 사용자 출력과 같은 stdout 에 자기 것을 찍기 때문이다.

- `PACSRUN_KEEPALIVE` 는 30초마다 한 줄씩 나오므로 **줄째로 버린다.**
- 나머지 `PACSRUN_*` 는 그 토큰만 `<internal>` 로 바꾸고 **줄은 남긴다.** 사용자 출력 뒤에
  붙어 나오는 경우가 있어서 줄을 버리면 필요한 것까지 사라진다.

## 예외 하나 — namespace 이름은 `result_path` 에 나온다

`result_path` 가 `s3://<bucket>/pacsrun/lab-alice/...` 이므로 namespace 이름이 응답에 들어간다.
**1단계에는 결과를 가져갈 다른 경로가 없어서 남겨 둔 것이다.** `/v1/jobs/{id}/artifacts` 가
내려받기 URL 을 주기 시작하면 이 필드는 없앤다. `test_models.py` 의
`test_the_response_drops_the_internal_fields` 가 이 예외가 딱 한 군데인지 확인한다.

## RBAC — 안 준 권한이 요점

ClusterRole 하나에 verb 를 모으고 **tenant namespace 마다 RoleBinding 을 따로 만든다.**
ClusterRoleBinding 이면 객체는 하나로 끝나지만 kube-system 의 pod 까지 읽게 된다.

| 자원 | verb |
|---|---|
| `pacsrun.io/pacsjobs` | create, get, list |
| `pods` | list (이름이 아니라 label 로 찾는다) |
| `pods/log` | get |

**`secrets` 는 없다.** 서버는 `secretKeyRef` 를 적어 넣고 값 읽기는 kubelet 이 한다. 이 pod 을
장악해도 GitHub token 은 못 읽는다.

## 돌려 보기

```bash
cd server
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q          # 65 passed

# 로컬에서 띄우려면 kubeconfig 가 있어야 한다 (Cluster.connect 가 fallback 한다)
DDPSRUN_RESULT_BUCKET=<RESULT_BUCKET> \
DDPSRUN_TOKENS_PATH=./tokens.json \
.venv/bin/uvicorn ddpsrun_server.main:app --port 8080
```

## 실물로 확인 안 된 것

**cluster 에 한 번도 안 올렸다.** test 65 개는 kube-apiserver 를 흉내 낸 것으로 돈다. 그래서
아직 모르는 것이 이렇다.

1. ~~**우리가 만든 PacsJob 을 CRD 가 받는지.**~~ **2026-08-31 에 닫혔다.**
   `kubectl apply --dry-run=server` 로 다섯 형태를 넣어 전부 통과했다. 로그는
   `10-cli.md` 에 있다.
2. **`follow=true` 로그가 몇 시간짜리 job 에서 끊기지 않는지.** 중간의 load balancer 가
   idle timeout 으로 끊을 수 있다.
3. **image 가 registry 어디로 가는지.** CI 는 빌드만 하고 push 하지 않는다.

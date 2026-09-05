# 16. Cognito + Google 로그인

DDPSRUN-LOGIN

`02-auth.md` 가 발급자를 Cognito 로 정했고, `08-plan.md` 의 미결 4번("CLI 로그인 흐름")을
남겨 두었습니다. 이 문서는 그 미결을 닫고, 남은 결정 셋을 확정합니다.

지금은 사람이 token 문자열을 손으로 받아서 `~/.config/ddpsrun/config.json` 과 화면의 입력칸에
넣습니다. 사용자가 늘면 그 token 을 누가 만들고 누가 건네주고 누가 폐기하는지가 전부 사람의
일이 됩니다. Cognito 는 그 일을 없애기 위한 것입니다.

---

## 16.1 용어

- **id_token** — Cognito 가 로그인 성공 뒤에 주는 JWT 입니다. **누구인지**를 담습니다
  (`email`, `sub`, `exp`). 우리가 Bearer 로 받는 것이 이것입니다.
- **access_token** — 같이 나오지만 **무엇을 할 수 있는지**를 담고, Cognito 의 기본 설정에서는
  `email` 이 들어 있지 않습니다. 그래서 우리는 쓰지 않습니다.
- **refresh_token** — id_token 이 만료되면(기본 1시간) 새것을 받아 오는 데 씁니다.
- **JWKS** — Cognito 가 공개하는 서명 검증용 공개키 목록입니다. 주소는
  `https://cognito-idp.<region>.amazonaws.com/<pool-id>/.well-known/jwks.json` 입니다.
- **Hosted UI** — Cognito 가 제공하는 로그인 화면입니다. 우리가 만들지 않습니다.
- **PKCE** — 브라우저처럼 비밀을 못 숨기는 곳에서 authorization code 를 안전하게 교환하는
  방법입니다. 16.4 에서 구체적으로 설명합니다.

---

## 16.2 결정 1 — namespace 는 Cognito 가 아니라 우리 파일이 정한다

**이것이 이 문서에서 가장 중요한 결정입니다.**

Cognito 의 id_token 은 `email` 을 담지만 우리 `namespace` 는 담지 않습니다. 그러면 그 사람의
namespace 를 누가 정하느냐가 문제입니다. 후보가 셋이었습니다.

| 방법 | 문제 |
|---|---|
| Cognito 사용자 속성 `custom:namespace` 에 넣는다 | **클러스터에 없는 namespace 를 줄 수 있습니다.** namespace 는 `terraform/cluster/tenants.tf` 가 만듭니다. Cognito 에서 오타가 나도 아무도 못 잡습니다 |
| email 의 앞부분에서 만든다 (`a@b.com` -> `lab-a`) | 아무나 로그인하면 namespace 가 생깁니다. 그런데 **Lambda 에는 namespace 를 만들 권한이 없습니다** (ClusterRole 에 `create namespaces` 가 없습니다). 없는 namespace 에 제출하면 404 가 납니다 |
| **우리 token 파일이 email 로 정한다** | 새 사람이 들어올 때 운영자가 파일 한 줄과 terraform 한 줄을 같이 고쳐야 합니다 |

**세 번째를 고릅니다.** 이유는 마지막 칸이 단점이 아니라 장점이기 때문입니다. namespace 를
만드는 곳(terraform)과 namespace 를 배정하는 곳(token 파일)이 **같은 사람의 같은 작업**이
됩니다. 둘이 어긋날 수 없습니다.

그래서 token 파일의 항목이 이렇게 바뀝니다.

```json
{
  "tokens": [
    {"sha256": "<64 hex>", "user": "alice",
     "namespace": "lab-alice", "team": "lab",
     "email": "alice@example.com"}
  ]
}
```

`sha256` 과 `email` 은 **둘 다 그 사람을 가리키는 열쇠**입니다. `sha256` 은 CLI 와 CI 가
쓰는 정적 token 이고, `email` 은 Cognito 로 로그인한 사람이 쓰는 열쇠입니다. 한 사람이 둘 다
가질 수 있습니다.

**등록되지 않은 email 은 403 입니다.** 401 이 아닙니다. Cognito 가 신원은 증명했으므로 "누구인지
모르겠다"가 아니라 "누구인지는 알지만 이 서비스를 쓸 자격이 없다"입니다. 응답에 그 사실과
운영자에게 무엇을 요청해야 하는지를 적습니다.

---

## 16.3 결정 2 — 정적 token 을 없애지 않는다

Cognito 를 넣어도 정적 token 은 남습니다. **브라우저가 없는 호출자가 있기 때문입니다.**

| 호출자 | 무엇을 쓰는가 | 왜 |
|---|---|---|
| 화면 | Cognito id_token | 사람이 브라우저 앞에 있습니다 |
| CLI (사람) | Cognito id_token | `ddpsrun login` 이 브라우저를 띄웁니다 |
| CLI (스크립트, CI) | **정적 token** | 브라우저를 띄울 사람이 없습니다 |
| agent skill | **정적 token** | 같은 이유입니다 |

그래서 `auth.py` 의 `principal_for` 가 **두 갈래**가 됩니다. 이것은 `auth.py:22` 의 주석이
처음부터 예고해 둔 그 이음매입니다.

```
Authorization: Bearer <credential>
        |
        +-- 점 두 개로 나뉘고 첫 조각이 base64url 로 풀리는가?
        |     예 -> Cognito 검증 (16.5)
        |     아니오 -> sha256 해시로 파일에서 찾기 (지금 그대로)
```

**모양으로 가르는 것이지 그것으로 통과시키는 것이 아닙니다.** JWT 처럼 생겼다는 이유로
아무것도 허용되지 않습니다. 갈라진 다음에 서명, 발급자, 대상, 만료를 전부 확인합니다.

---

## 16.4 결정 3 — 브라우저와 CLI 가 같은 방법을 쓴다 (Authorization Code + PKCE)

### 왜 PKCE 인가

Cognito 에서 token 을 받으려면 원래 두 가지가 필요합니다. authorization code 와 client
secret 입니다. **그런데 우리 화면은 정적 파일이고 CLI 는 사용자 컴퓨터에서 돕니다. 둘 다
secret 을 숨길 곳이 없습니다.** 파일에 적으면 누구나 읽습니다.

PKCE 는 secret 없이 같은 것을 합니다.

1. 시작할 때 **난수 하나**를 만듭니다. 이것이 `code_verifier` 입니다. 밖에 안 나갑니다.
2. 그것의 SHA-256 을 `code_challenge` 로 Cognito 에 **미리** 보냅니다.
3. 로그인이 끝나고 code 를 받습니다.
4. code 를 교환할 때 `code_verifier` 원본을 같이 보냅니다.
5. Cognito 가 그것을 SHA-256 해서 2번의 값과 같은지 봅니다.

**code 를 가로챈 사람은 4번을 못 합니다.** 난수 원본이 없기 때문입니다.

### 화면의 흐름

```
1. 사용자가 [Sign in with Google] 을 누른다
2. 난수를 만들어 sessionStorage 에 두고, Hosted UI 로 보낸다
     https://<domain>.auth.<region>.amazoncognito.com/oauth2/authorize
       ?client_id=...&response_type=code&scope=openid+email
       &redirect_uri=https://<cloudfront>/&code_challenge=...&code_challenge_method=S256
3. Cognito 가 Google 로 보내고, 사용자가 Google 에서 로그인한다
4. Cognito 가 CloudFront 로 되돌린다:  https://<cloudfront>/?code=abc123
5. 화면이 code 와 난수 원본을 /oauth2/token 으로 보내 id_token 을 받는다
6. id_token 을 localStorage 에 두고, 이후 모든 요청에 Bearer 로 붙인다
7. 만료(1시간) 되면 refresh_token 으로 새것을 받는다
```

### CLI 의 흐름 — 왜 로컬 포트를 여는가

사용자가 전에 물으셨던 것입니다. 답은 **Cognito 가 code 를 사람 손에 주지 않고 주소로
보내기 때문**입니다.

브라우저에서는 그 주소가 CloudFront 입니다. CLI 에는 그런 주소가 없습니다. 그래서 CLI 가
**자기 컴퓨터에 잠깐 웹 서버를 하나 띄우고 그 주소를 redirect_uri 로 씁니다.**

```
1. ddpsrun login
2. 빈 포트 하나를 잡는다 (예: 51234). 그 포트에서 한 번만 받는 HTTP 서버를 띄운다
3. 브라우저를 연다:  ...&redirect_uri=http://localhost:51234/callback
4. 사용자가 브라우저에서 Google 로 로그인한다
5. Cognito 가 브라우저를 http://localhost:51234/callback?code=abc123 으로 보낸다
6. 2번의 서버가 그 요청을 받아 code 를 꺼내고, 브라우저에 "닫으셔도 됩니다"를 그린 뒤 죽는다
7. CLI 가 code 를 교환해 id_token 과 refresh_token 을 받아 config.json 에 저장한다
```

**포트를 여는 이유가 여기 있습니다.** code 를 받을 주소가 필요하고, 그 주소는 브라우저가
갈 수 있는 곳이어야 합니다. 사용자 컴퓨터의 `localhost` 가 그 조건을 만족하는 유일한 곳입니다.
`aws sso login` 과 `gcloud auth login` 이 같은 일을 합니다.

포트는 **고정하지 않고 매번 빈 것을 잡습니다.** 다만 Cognito 의 app client 에 redirect_uri 를
미리 등록해야 하므로, `http://localhost:51234/callback` 처럼 몇 개를 등록해 두고 그중 빈 것을
씁니다.

---

## 16.5 서버가 id_token 을 어떻게 믿는가

**Cognito 에 물어보지 않습니다.** 물어보면 요청마다 왕복이 하나 늘고, Lambda 의 응답 시간이
0.4 초에서 그만큼 늘어납니다. 대신 서명을 직접 검증합니다.

확인하는 것이 여섯입니다. **하나라도 어긋나면 401 입니다.**

| 확인 | 무엇을 보나 | 어긋나면 무슨 공격을 막는 것인가 |
|---|---|---|
| 서명 | JWKS 의 공개키로 RS256 검증 | 아무나 JWT 를 지어내는 것 |
| `iss` | `https://cognito-idp.<region>.amazonaws.com/<pool-id>` | **다른 Cognito user pool 에서 받은 token 을 들고 오는 것** |
| `aud` | 우리 app client id | 같은 pool 의 다른 앱 token 을 쓰는 것 |
| `token_use` | `"id"` | access_token 을 id_token 자리에 넣는 것 |
| `exp` | 지금보다 뒤인가 | 만료된 token 재사용 |
| `email_verified` | `true` | **확인 안 된 email 로 남의 자리를 차지하는 것** |

마지막 것이 특히 중요합니다. 16.2 에서 email 을 열쇠로 쓰기로 했으므로, 확인되지 않은 email
을 받으면 **아무나 남의 email 을 주장할 수 있게 됩니다.** Google 로 로그인하면 항상 `true`
지만, Cognito 에 직접 만든 사용자는 아닐 수 있습니다.

**JWKS 는 캐시합니다.** 모듈 수준 변수에 담아서 따뜻한 Lambda 가 다시 안 받게 합니다. 키가
바뀌는 일은 드물지만, 모르는 `kid` 가 오면 한 번 다시 받습니다.

---

## 16.6 제가 만들 수 없는 것 하나

**Google OAuth client 는 사용자가 Google Cloud Console 에서 직접 만드셔야 합니다.**

Cognito 에 Google 을 붙이려면 Google 이 발급하는 client ID 와 client secret 이 필요한데,
그것은 Google 계정으로 콘솔에 로그인해야 만들 수 있습니다. 그리고 그 secret 은 저장소 어느
파일에도 들어가면 안 됩니다.

만드실 때 넣는 값은 이렇습니다.

```
승인된 자바스크립트 원본:  https://<domain>.auth.<region>.amazoncognito.com
승인된 리디렉션 URI:      https://<domain>.auth.<region>.amazoncognito.com/oauth2/idpresponse
```

`<domain>` 은 우리가 terraform 으로 정하는 Cognito 도메인 접두사입니다. **그래서 순서가
있습니다.** 먼저 terraform 으로 user pool 과 도메인을 만들고, 그 도메인을 Google 콘솔에
넣고, 받은 두 값을 `terraform.tfvars` (gitignore 됨)에 적어 다시 apply 합니다.

**Google 없이도 됩니다.** Cognito 자체 사용자(email + 비밀번호)는 Google 없이 바로 씁니다.
Google 은 나중에 붙여도 되고, 붙일 때 화면과 CLI 는 하나도 안 바뀝니다.

---

## 16.7 요금

Cognito 는 **월 활성 사용자 50,000 명까지 무료**입니다. 연구실 인원 수십 명은 그 안에
한참 들어갑니다.

붙는 비용은 셋입니다.

1. **Lambda 패키지가 커집니다.** JWT 검증에 `cryptography` 가 필요하고 wheel 이 3.8 MB
   입니다. 지금 푼 크기가 106.5 MB / 상한 250 MB 이므로 여유는 충분하지만, 차가운 시작이
   4.6 초에서 조금 늘어납니다. **패키지를 읽는 시간이 차가운 시작의 대부분이기 때문입니다**
   (`terraform/lambda/variables.tf` 의 `memory_mb` 설명에 512 MB 와 1024 MB 실측이 있습니다).
2. **JWKS 를 처음 한 번 받습니다.** 차가운 시작마다 한 번, 수십 밀리초입니다.
3. **Cognito 도메인은 무료**입니다. 우리 도메인을 쓰면 ACM 인증서가 필요하지만, Cognito 가
   주는 `<prefix>.auth.<region>.amazoncognito.com` 을 쓰면 그것도 없습니다.

---

## 16.8 만드는 순서

1. `terraform/cognito` — user pool, app client, 도메인. **Google 없이 먼저 만듭니다.**
2. 서버 — `cognito.py` 검증 모듈, `auth.py` 의 두 번째 분기, `config.py` 의 새 설정.
3. 화면 — 로그인 단추, code 교환, refresh.
4. CLI — `ddpsrun login` 이 브라우저를 띄우는 흐름.
5. **사용자가 Google client 를 만들고** 두 값을 주시면, terraform 에 넣고 다시 apply.

1번부터 4번까지는 Google 없이 다 됩니다. 5번만 사용자를 기다립니다.

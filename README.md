# pacsrun-gw

PACSrun 앞단. kubectl 도 AWS IAM 도 없는 사용자가 job 을 제출하고 결과를 받는 경로다.

- `server/` API 서버. 유일하게 클러스터에 붙는 쪽이고 판단 로직을 전부 갖는다
- `cli/`    명령줄. 서버 라우트를 부른다
- `ui/`     브라우저. 제출 form 과 monitoring
- `agent/`  code agent 용 skill 과 참조 문서
- `docs/`   설계

사용자가 보는 이름은 `ddpsrun` 이다. `pacsrun` 과 `kubepacs` 는 노출하지 않는다.

# 기술 변경 기록

이 파일은 **배포 여부와 관계없이 작업을 기록하는 기준 패치노트**입니다.
날짜별 작업 기록을 먼저 보고, 아래의 버전별·주제별 상세 기록을 함께 확인하세요.
로컬 수정, 커밋, 설치, 테스트 통과, GitHub 공개, 정식 릴리스는 서로 다른 상태입니다.
`Unreleased`는 미배포 작업이며, 버전 제목이 있다고 공개 릴리스가 만들어진 것은 아닙니다.

<!-- dated-work-log:start -->
## 날짜별 작업 기록 (KST)

### 2026-09-11 — `/goal` 지속 진행 표시 + host/Control Plane 안정화

- Discord `/goal`은 ChatGPT 예약 작업이 아니라 서버의 `board-goal-driver.timer`가 직전 oneshot 종료 후 약 2분 간격으로 이어가는 durable loop입니다. active child task가 있을 때 manager driver가 `goal_progress`를 내보내고 `board_notify.py`가 goal/task/status/assignee만 담은 진행 heartbeat를 약 5분 cooldown으로 `일반` 채널에 게시하도록 보강했습니다. prompt/response/result 본문은 계속 서버 원장에만 남깁니다. focused verification: goal driver `14 passed`, Discord notify `17 passed`.
- Project Control HTTP watchdog이 단 한 번의 3초 local health miss에도 adapter를 재시작해 정상 요청까지 끊을 수 있던 false-positive 경로를 완화했습니다. `/run/project-control-http-watchdog.failures`에 연속 실패 횟수를 기록하고 기본 3회 연속 실패일 때만 restart하며, 정상 probe나 성공한 restart에서 streak를 0으로 되돌립니다. focused verification: `3 passed`.
- provider 실행이 0개일 때만 동작하는 `cleanup` profile을 추가해 stale `goal-task-*` Oracle 프로세스를 종료하고 관리형 Oracle Chrome을 재시작합니다. 실제 정리에서 swap free가 약 110MB에서 1.31GB로 회복되고 load가 크게 내려갔으며, active provider run이 있으면 provider lock + durable DB check로 fail-closed 합니다.

### 2026-09-10 — Project Control break-glass SSH + non-Snap ChatGPT runtime

- Project Control MCP의 `project_tick`이 장시간 provider 실행을 RPC 안에서 동기 대기해 클라이언트 timeout과 `provider_busy`를 연쇄시키던 구조를 분리했습니다. MCP stdio 서버에서는 tick을 detached background child로 dispatch하고 즉시 반환하며, Python 쪽은 별도 `project-tick.lock`으로 중복 dispatch를 직렬화합니다. `project_goal_status`는 최근 goal-task run의 bounded/redacted 오류 excerpt를 함께 반환해 Commander가 끊겨도 provider 원인을 Project Control만으로 진단할 수 있게 했습니다. focused verification `3 passed`.
- Project Control HTTP 연결 자체를 systemd 관리 대상으로 올리고 `Restart=always` + 무제한 start-retry로 강화했습니다. 별도 30초 watchdog timer가 `127.0.0.1:7677/healthz`를 확인해 프로세스는 살아 있지만 응답이 멎은 경우에도 adapter를 재시작하며, root break-glass helper allowlist에도 HTTP adapter/watchdog 서비스만 제한적으로 추가했습니다. 관련 배포 파일과 watchdog은 install manifest에 포함했습니다.
- Gemini manager 프롬프트에 `chatgpt` 실행은 ordinary ChatGPT web chat/browser session만 사용하고 ChatGPT Worker/Codex Worker/codex exec를 구현 경로로 선택하지 못하도록 명시했습니다. Codex/Claude는 구현 대체 경로가 아니라 별도 검증 역할로만 남깁니다.

- Project Control에 등록 host alias만 대상으로 하는 `project_ssh_hosts`, `project_ssh_status`, `project_ssh_exec`를 추가했습니다. 기본 `agent-box`는 SSH 키 장애와 무관하게 복구 가능한 local break-glass 경로이며, 추가 원격 서버는 `PROJECT_CONTROL_SSH_HOSTS_JSON` 또는 `--ssh-route ID=user@host[:port]`로만 등록합니다. 임의 host 입력은 허용하지 않고 command 길이/timeout/output을 제한하며 SSH는 BatchMode + strict host key checking을 사용합니다.
- ChatGPT goal recovery가 `MODEL_DID_NOT_COMPLETE`로 반복된 원인을 `/snap/bin/npx`/Snap Node 실행 경로로 좁혔습니다. repo-local ignored `runtime/node-current` portable Node를 기본 launcher로 사용하고, goal worker PATH에서도 `/snap/bin` 우선순위를 제거했습니다. systemd 배포 템플릿도 같은 portable runtime 경로를 사용하도록 변경했습니다.
- Project Control focused + goal worker/bus verification: `33 passed`; 외부 HTTPS MCP에서 새 SSH 도구 3개 discovery 및 `project_ssh_status(agent-box)` 실제 호출 성공.
- 복구 정책을 다시 조정했습니다. provider 실패 횟수 소진만으로 `user_decision_required`에 올리지 않고 ChatGPT operator-repair `blocked` 상태로 보존하며, recovery task는 Codex/Claude로 넘기지 않고 ChatGPT만 사용합니다. 실제 사용자 승인·비가역 외부행동이 필요한 경우만 `user_decision_required`를 유지합니다. recovery가 만든 대기 상태만 안전하게 되살리는 `project_goal_requeue`도 추가했습니다. 관련 focused verification은 `25 passed`입니다.
- Project Control에 제한형 root 서비스 복구 계층을 추가했습니다. `project_ssh_services`는 허용된 서비스/동작만 노출하고, `project_ssh_service`는 `status/start/restart/reset-failed` 중 허용된 동작만 수행합니다. root 권한은 `/usr/local/sbin/project-control-ops` helper 하나에만 위임하고 helper 내부에서 서비스 allowlist를 다시 검증합니다. 임의 `sudo`, 임의 root shell, 임의 systemd unit은 허용하지 않습니다.
- 서버 ChatGPT bus/goal worker는 provider lock으로 직렬화되는 관리형 실행이므로 매 run마다 `--copy-profile`로 새 동적 CDP 브라우저를 만들지 않습니다. 관리형 Chrome `127.0.0.1:9222`의 `/json/version`에서 현재 browser WebSocket endpoint를 읽어 `~/.config/oracle-attach/DevToolsActivePort`를 원자적으로 갱신한 뒤 `--browser-attach-running --remote-chrome 127.0.0.1:9222`를 사용합니다. 이 경로는 Chrome 152에서 `socket hang up`을 일으키는 target-id 기반 HTTP attach/new 경로를 우회하고 browser WebSocket + Target.attachToTarget 경로를 사용합니다. `chatgpt-server-worker@` unit 템플릿도 `oracle-browser@`를 Requires/After/Wants에 포함합니다.
- recovery가 `goal_task_runs.error_ref`를 일반 backlog artifact resolver로 읽으면서 실제 provider 오류 본문을 항상 잃던 결함을 수정했습니다. exact run에 이미 결합된 `error_ref`만 직접 읽고, `ECONNREFUSED 127.0.0.1:9222`는 environment-recoverable로 분류합니다. 이 경우 제한형 root helper로 `oracle-browser@board.service`를 한 번 복구하고 CDP readiness를 확인한 뒤 원 task를 안전하게 재개하며, helper 자체가 실패하면 LLM recovery 반복 없이 operator-repair로 보존합니다.
- Discord recovery 알림의 과거 `/6` 하드코딩을 제거해 실제 `max_attempts`를 표시합니다. Project Control MCP의 자유 텍스트 인자는 `--query=...`, `--command=...` 형태로 전달해 값 자체가 `-`로 시작해도 argparse가 옵션으로 오인하지 않도록 보강했습니다. 이 hardening 묶음의 focused verification은 `48 passed`입니다.
- fast gate가 호출 계정의 시스템 Node를 상속해 Oracle의 검증된 Node 24 런타임과 어긋나던 문제를 수정해 repo-local portable Node를 우선합니다. POSIX browser-temp alias 검증도 공유 `/tmp/Codex`에 테스트 계정이 쓰기 가능하다고 가정하지 않도록 alias root override를 지원해 다중 사용자/ACL 환경에서도 격리됩니다. 전체 fast gate는 `exit=0`, `35.93s/100s`로 통과했습니다.
- Project Control 연결 단절 재발 방지용 HTTP adapter 운영 계층을 추가했습니다. `project-control-http.service`는 adapter 프로세스를 `Restart=always`로 관리하고, 30초 주기의 `project-control-http-watchdog.timer`가 `/healthz` 지연/실패를 감지해 adapter를 재시작합니다. 제한형 root helper allowlist에도 adapter/watchdog을 추가했습니다. 또한 Gemini manager prompt는 `chatgpt` 구현 작업을 ordinary ChatGPT web chat/browser session에만 라우팅하고 ChatGPT Worker/Codex Worker/codex exec를 구현 경로로 선택하지 않도록 고정했습니다. focused verification은 watchdog/helper `5 passed`, manager routing `1 passed`입니다.

### 2026-09-10 — durable goal self-healing

- `attention_required` goal task가 단순 중단점이 아니라 durable recovery 진입점이 되도록 `server_goal_recovery.py`를 추가했습니다. 실패를 pre-execution/environment/partial/uncertain으로 분류하고, 불확실한 원 실행은 재실행하지 않은 채 별도 recovery task가 현재 상태를 검사·복구한 뒤 원 task를 재개합니다.
- 초기 구현은 최대 6회의 cross-provider recovery를 사용했으나, 이후 복구 책임을 ChatGPT operator로 단일화했습니다. provider 실패 소진은 사용자 결정으로 간주하지 않고 `blocked/operator-repair`로 보존하며, 실제 `user_decision_required`/비가역 외부 결정만 사용자 경계로 남깁니다.
- Project Control의 Git 호출은 등록된 exact repo에 한해서 `safe.directory`를 명시해, ACL로 위임된 Desktop Commander 사용자에서도 repo ownership을 완화하지 않고 status/diff/commit을 수행할 수 있게 했습니다.
- focused verification: recovery/goal worker/manager/Discord notify/Project Control 관련 `66 passed`.

### 2026-09-09 — agent-box Remote Desktop Commander 재발 방지

- `desktop-commander-remote.service`가 active여도 실제 장치가 offline일 수 있으므로 process/network/remote-registration/E2E ping을 분리해 판정하는 복구 계약을 추가했습니다.
- Desktop Commander 0.2.48에서 `refreshSession()`으로 회전된 refresh token이 `device.json`에 재영속되지 않아 재시작 시 `Invalid Refresh Token: Already Used`가 발생할 수 있는 결함을 exact-build SHA-256 guard로 보완했습니다.
- `bin/desktop_commander_compat.py`는 테스트한 0.2.48 빌드만 허용하고, refresh token 회전 시 serialized + atomic config 저장을 수행하며 다른 버전/unknown build에는 fail closed합니다. 비밀값은 출력하지 않습니다.
- `docs/AGENT_BOX_RECOVERY.md`에 DevSpace 우선, Commander break-glass, 노트북 OFF 독립성, 양쪽 장애 시 cloud-provider OOB 복구 요구사항을 기록했습니다.
- focused verification: `tests/test_desktop_commander_compat.py` 4 passed. 실제 설치본에 exact-build 패치를 적용했고, Supabase/Cloudflare 429 쿨다운 후 exact device `pong` 성공, 이어서 원격 graceful shutdown → systemd 자동 재기동 → **사람 재승인 없이 다시 `pong` 성공**까지 E2E 검증했습니다.
- Project Control MVP를 추가했습니다. `bin/project_repo_registry.py`가 `automation`/`stock` alias registry의 단일 소스가 되고, durable `goal_tasks`에 `repo_id`를 저장합니다. 새 task는 등록된 repo id가 필수이며 기존 DB task는 `legacy-unassigned`로 마이그레이션되어 본문 문자열 추측으로 실행되지 않습니다.
- `mcp_servers/project-control/server.mjs`는 raw shell/filesystem을 노출하지 않고 repo 목록, backlog/status, goal/task 생성, bounded tick만 제공합니다. Discord bridge와 Control MCP가 같은 registry를 사용하며 기존 provider lock/no-replay/explicit completion 정책을 그대로 통과합니다.
- Control Plane focused verification: goal/backlog/worker/driver/Project Control/Desktop Commander/board LLM seat 관련 `59 passed`; MCP stdio EOF lifecycle 결함도 함께 수정했습니다.
- `bin/board_llm_seat.py`에 구독 기반 `agy` CLI provider를 추가했습니다. 임시 sandbox 작업공간에서만 호출하고 repository를 workspace로 주지 않으며 permission prompt를 자동 승인하지 않아 회의 좌석이 임의 도구 실행으로 확장되지 않게 했습니다.
- 전체 fast gate도 `exit=0`으로 통과했습니다 (`17` jobs, 실행 시간 약 `28.6s`, budget `100s`; 모든 실행 batch 통과).
- Project Control에 ChatGPT 직접 개발용 제한형 도구를 추가했습니다: bounded UTF-8 `read`, literal `search`, SHA-256 bound atomic `patch/create`, allowlisted `test`, `git status/diff`, explicit-path local `commit`. 등록 repo 밖 경로, symlink, `.git`, stale hash, 임의 pytest 경로, raw shell/process, push/reset/restore/checkout/branch/worktree는 허용하지 않습니다.
- 역할 계약도 갱신했습니다: Gemini는 durable goal/task 관리자, ChatGPT는 Project Control을 통한 주 구현자, Codex와 Claude는 독립 교차검증자로 사용합니다. Desktop Commander는 계속 Dev/Control Plane 장애 복구용 break-glass 경로입니다.
- 직접 개발 표면 focused test `tests/test_project_control.py`는 `11 passed`; 실제 MCP 단발 `tools/list`와 비동기 `tools/call`이 모두 응답 후 정상 종료하는 것을 확인했습니다. 최종 전체 fast gate는 모든 batch가 통과해 `exit=0`이었으나 서버 부하로 약 `185.1s`가 걸려 100초 성능 budget은 초과했습니다 (`17` jobs, 기능 실패 없음).
- 목표 작업이 `attention_required`로 멈출 때 Discord에 error code만 보이던 문제를 보완했습니다. 원문 stderr/stdout `detail`은 서버 원장에만 남기고, provider/timeout/start/schema 등 비밀정보 없는 `public_reason`을 별도로 생성해 알림에 `원인:`으로 표시합니다.
- Gemini manager의 durable execution self-assignment를 금지했습니다. 저장소 조회/구현/디버깅/테스트/문서/통합 작업은 ChatGPT에 배정하고, Codex와 Claude는 구현 후 독립 리뷰/교차검증에만 사용하도록 manager contract와 parser guard를 함께 강화했습니다. 이로써 headless Gemini 작업이 `read_file` permission prompt를 띄우지 못해 중단되는 경로를 차단했습니다.

기록 보완일: **2026-09-05**. 대조 범위는 현재 Git 이력에서 커밋일이
2026-09-02 이후인 비병합 변경 커밋 **22개**, 마지막 기준은 `aecfd5a`입니다.
아래 날짜는 Git 작성일을 기준으로 하며, 현재 이력의 커밋일과 다르면 함께 표시합니다.
과거 작성 변경을 나중에 반영한 경우도 빠뜨리지 않습니다. 시간대는 KST(UTC+09:00)입니다.
이 기록은 작업·검증 이력이지 공개 배포나 실제 웹 실행의 성공 선언이 아닙니다.

### 2026-09-05 — 복구 보완, 독립 토론, 공개 조사 회의, 성능 개선

| 변경 | 내용과 영향 | 근거 커밋 |
|---|---|---|
| 제출 전 복구의 정산 가능성 보존 | 프로세스 생존 확인을 공통 플랫폼별 검사로 통일. 제출 전 종료 뒤 임시 runtime profile 디렉터리가 이미 정리된 경우를 허용하되, 부모·경로·링크와 나머지 미제출 증거 검증은 유지. 프로세스 생존 회귀 테스트 추가. | `e9ab6f9` |
| 독립 Oracle debate | 독립 초안 → 원문 교차 검토 → 별도 Judge → 최종 종합. 1~3라운드, 호출 예산, 제출 전 영속 예약, 원문 해시, 고유 대화와 완료 증거 검사. 시간 초과·불확실한 작업 뒤 새 제출을 막고, 합의하지 못하면 미결론으로 보존. | `6180ec0` |
| 공개 조사 research meeting | analyst/researcher/scout/skeptic의 독립 참여와 별도 synthesizer. 에이전트가 반론·수정·추가 조사를 선택하고, 승인된 공개 topic의 중복 요청을 묶어 조사 결과를 회의에 반환. 반론 작성자만 철회 가능. plan/run/view CLI, 해시 연결 이벤트와 읽기 전용 터미널 뷰어 추가. | `0e7a9d0` |
| 초기 공개 조사 누락 판정 | researcher/scout가 조사를 못 하고 pass한 경우를 `pending_initial_research`로 유지. 나중에 전원 agree해도 초기 출처 기반 조사가 빠졌으면 inconclusive이며, synthesizer에도 남은 조사를 전달. | `721fac9` |
| 반복 파일 열기 비용 감소 | 실행별로 제한된 수의 unbuffered 읽기 핸들을 재사용. 매번 실제 바이트와 해시·파일 동일성·경로를 다시 검사하며, fsync·덮어쓰기 방지 유지. 같은 크기/시각의 변조, 파일 교체, 재분석 지점, 핸들 제한과 실패 시 정리 회귀 추가. | `aecfd5a` |

`6180ec0`은 숨김 Windows 테스트 자식의 stdout/stderr를 명시적으로 보존하고,
fast gate의 전체 시간에 임시 파일 정리도 포함했습니다. `aecfd5a`는 timeout 테스트를
첫 제출 전/후 시나리오로 구분하여 실제 순서를 고정했습니다. 0.15초 timeout,
이후 제출 차단, 100초 gate 예산은 유지하며 테스트 삭제·새 skip으로 통과시키지 않았습니다.

연구 회의 기본값은 검토 2라운드, 전체 호출 24회, 추가 조사 3건, 동시 호출 2개입니다.
각 live turn은 별도의 regular Oracle 세션을 사용하며 같은 대화를 재사용하지 않습니다.
이것은 제한된 호출 라운드의 회의 컨트롤러이지 상시 자율 사고나 데스크톱 채팅 앱이 아닙니다.
공개/비공개 분리는 미션 지침과 데이터 최소화이지 강제 네트워크 격리가 아닙니다.
출처 카드는 에이전트 보고값이고, `web_search_verified=false` 및
`solution_verified=false`를 합의만으로 바꾸지 않습니다.

#### 2026-09-05 검증·설치 관측 — 문서 보완 직전 작업

| 구분 | 관측 결과 | 판정 |
|---|---|---|
| 관련 회귀 7개 파일 | `146 passed`, 실패/skip/경고 0, pytest 132.10초, 관찰 wrapper 134.487초 | 해당 범위 기능 통과 |
| fast gate 전체 선택 대상 | `618 passed`, 실패 0, `9 skipped`, `1 deselected`, `1 warning`; pytest 364.15초 | 실행한 테스트 통과. skip/deselection은 기존 항목이며 전체 저장소 테스트와 동일하지 않음 |
| fast gate 성능 | 테스트 자식 367.30초 + 정리 22.61초, 전체 389.92초, wrapper 390.578초, `exit_code=3` | **100초 예산 미충족**. 내부 테스트 종료 0을 gate 성공으로 계산하지 않음 |
| 남은 경고 | `test_scoped_package_tree_rejects_directory_links_and_junctions`의 subprocess UTF-8 디코딩 경고 | 미해결; 수정 전후 관측 |
| 공식 설치 | 210개 파일, 종료 0. 소스/설치본/영수증 해시 일치. WAL COMPLETE, 미완료 WAL 0. doctor PASS, issues 비어 있음 | 당시 설치 파일 동기화·무결성 확인 |
| 설치 제한 | 기존 LEGACY_AGBROWSE_MISSING 경고, optional local_multi_gpt 비활성화 유지 | 신규 기능 실웹 검증과 별개 |
| 실웹 | 이 작업의 실제 웹 제출 0건 | 기존 admission 제한과 세부 공개 조사 승인/빈 public workspace 등 미충족. live debate/research meeting 성공 증거 없음 |
| 커밋·공개 | 로컬 `aecfd5a` 생성, push/tag push/공개 release 없음 | **Unreleased** |

위 결과는 외부 미커밋 변경을 보존한 당시 공유 checkout의 관측입니다.
관련 회귀와 최종 gate는 실행 중 소스 변경이 없었고, 검사한 소스 바이트의 해시가
커밋 후에도 일치했습니다. 미커밋 작업 전체의 독립 검증 완료를 의미하지 않습니다.
수정 전 gate는 608 passed/1 failed와 전체 1266.49초였지만,
호스트 부하를 통제하지 않았으므로 시간 감소 전부를 이번 최적화의 인과 효과로 단정하지 않습니다.

#### 2026-09-05 패치노트 기록 보완

- 날짜 없는 최근 Unreleased 항목과 누락 커밋을 날짜별 한국어 기록으로 보완하고,
  기존 상세 기록을 보존했습니다. 9월 3일 작성/9월 4일 커밋도 별도 표시했습니다.
- 한국어/영어 README 상단에 날짜별 패치노트 링크와 미배포 작업 안내를 추가했습니다.
- 22개 과거 커밋의 기록 누락과 당시 성능/실웹 제한 소실을 막는 문서 회귀 검사를 추가했습니다.
  이 검사는 고정된 과거 범위를 보존하며, 미래 작업의 의미적 누락까지 자동 판정하지는 않습니다.
- 이번 문서 보완 검증: `tests/test_docs_contract.py` 4 passed, `scripts/check_docs.py`
  PASS. 위의 146/618건은 앞선 코드 작업의 역사적 관측이며 이번에 재실행한 수치가 아닙니다.
  변경한 문서·테스트 4개는 설치 manifest 대상이 아니어서 재설치하지 않았습니다.

### 2026-09-04 — 다중 실행 안정화, 인코딩, OAuth, 모델 선택, 지원 문서

| 변경 | 내용과 영향 | 근거 커밋 |
|---|---|---|
| 시작 복구 UTF-8 | PowerShell 시작 복구에서 Python UTF-8 환경/출력 인코딩을 명시하여 비ASCII 경로 손상을 방지. Git 작성일은 2026-08-29, 현재 이력의 커밋일은 2026-09-04. | `076585e` |
| 다중 실행 사전 검사·성공 판정 | exact-root qualification 사전 검사, 빈 산출물/부분 실패의 성공 오판 방지, Windows 원자적 교체의 일시 오류 재시도, 워커 시작 시차로 경합 완화. 소스 버전 1.20.16. | `fcf03ac` |
| 설치 JSON 인코딩 | 설치/업데이트/진단/롤백/Cloudflare bootstrap의 JSON 읽기 14곳에 UTF-8 명시. 비ASCII 경로 회귀와 PowerShell 출력 디코딩 테스트 보완. 소스 버전 1.20.17. | `c33f042` |
| OAuth 루트 discovery | DevSpace의 `/.well-known/oauth-protected-resource`에서 기존 자원/인증 서버/scopes 메타데이터 제공. 정확한 1.0.8 패치와 설치 목록·회귀 검사에 반영. | `10e3402` |
| 브라우저 모델 전략 전달 | multi-agent `run --model-strategy select|current|ignore`를 모든 lane manifest로 전달. 기본 select 유지, strict writer는 select만 허용. 소스 버전 1.20.18. | `9d8f53f` |
| 모델 선택 실패의 미제출 증거 | 일반 DevSpace 모델 선택기 실패를 exact metadata/미션/profile/CDP/미제출 증거에 결속. 제한된 prompt-free harvest와 명시적 사용자 확인을 거친 정산 경로 보완. 임의 잠금 해제 아님. | `d8c55ab` |
| 유료 설치 지원 안내 | PAID_SUPPORT 문서와 README 안내 추가. 지원 범위 안내이지 유료 주문·매출 발생 증거가 아님. | `5c2db3c` |
| 지원 요청 양식 | 유료 지원용 GitHub issue template 추가. | `ee2a797` |
| 지원 요청 링크 | 지원 문서에서 요청 양식으로 이동하는 링크 추가. | `fa94ddd` |

소스 버전 1.20.16~1.20.18 기록은 공개 릴리스가 아닙니다. 2026-09-05 읽기 확인 시
공개 main의 CHANGELOG 최상단은 1.20.15였으며, 최근 로컬 작업은 공개본과 달랐습니다.
이 상태를 고치기 위해 사용자 금지 조건을 무시하고 push하거나 릴리스를 만들지 않습니다.

### 2026-09-03 — 역할 기반 다중 에이전트 표면

세 변경 모두 Git 작성일은 2026-09-03, 현재 이력의 커밋일은 2026-09-04입니다.

| 변경 | 내용과 영향 | 근거 커밋 |
|---|---|---|
| Multi-agent CLI | 역할별 미션/manifest 생성과 기존 Oracle multi runner 위의 실행·결과 표면 추가. 독립 세션 검사, lane timeout/취소, wave·작업 시간 보고, 보고 경계의 민감값 마스킹. Git 출력 UTF-8 수정과 multi 테스트 gate 편입. | `b232e2e` |
| 설치 누락 수정 | multi-agent CLI를 설치 manifest에 포함하고 패키징 회귀 추가. | `59da964` |
| 읽기 전용 lane 차단 수정 | non-strict 분석 lane을 strict writer로 잘못 분류하던 provenance 광고를 수정. 실제 runner를 통과하는 dry-run과 서로 다른 미션/manifest/명령을 검사하는 smoke 추가. | `7241ef5` |

Dry-run/smoke는 브라우저 제출 전 경로를 검사하며 실제 웹 대화를 만들지 않습니다.
서로 다른 미션이나 명령이 생성됐다는 사실만으로 실제 conversation ID의 독립성이
검증됐다고 기록하지 않습니다. 당시 커밋에 적힌 테스트 수치는 그 당시 관측이며
9월 5일 현재 트리의 검사 결과로 재사용하지 않습니다.

### 2026-09-02 — 현재 대화 식별, 소유권 충돌, Pro 선택기, 절전 복구

| 변경 | 내용과 영향 | 근거 커밋 |
|---|---|---|
| 미인증 제출 전 실행 | Oracle 0.18.0의 session-not-detected 오류 문구를 인식하도록 보완. 뒤의 Login button 문장이 없어도 나머지 엄격한 미제출 증거를 검사. 현재 이력 커밋일은 2026-09-04. | `921d9b6` |
| 현재 실행 URL 구분 | qualification/canary/부모 영수증의 과거 URL을 현재 실행의 대화로 오인하지 않도록 명시적 현재 URL 필드 검사. 현재 URL 후보·충돌은 계속 미제출 판정을 막음. | `363f610` |
| 해소된 소유권 충돌 | exact owner가 해소된 제출 전 충돌을 엄격한 증거로 다시 판정. diagnose/incident에 소유권 충돌과 Pro tier 선택 실패를 구분하고, unresolved owner가 남으면 재실행 허용 금지. | `a72acf6` |
| Pro power slider | 0부터 시작하는 aria 범위와 화면의 N of M 위치를 함께 검사. 지연된 모델 행과 두 번의 안정된 관측을 확인하고 잘못된 모델/범위/중복 메뉴는 거부. 기존 1.20.15 상세 기록 보완. | `cdc148b` |
| Windows 절전 복구 | 로그인 전용 시작의 한계를 보완해 resume event 기반 작업과 숨김 Watch 경로 추가. 배터리 실행/시간 제한 설정을 보완하고 mutex로 watcher 중복 기동 방지. 현재 이력 커밋일은 2026-09-04. | `8153fda` |

같은 날짜의 병합 커밋 `a635f65`, `d70c0d9`도 이력 대조에 포함했습니다.
병합은 위 변경의 통합 기록이며 새로운 기능으로 중복 집계하지 않았습니다.
<!-- dated-work-log:end -->

<!-- pending-work:start -->
## 미커밋·미완료 작업 — 2026-09-05 확인

공유 checkout에는 아래 외부 작업자의 변경이 남아 있습니다. **기록은 남기되
우리의 완료·커밋·배포 항목으로 승격하지 않습니다.** 작업 날짜나 성공을 추정하지 않습니다.

- Oracle 실행/정산/상태 및 호환 변경: `bin/chatgpt_oracle_run.py`,
  `bin/chatgpt_oracle_state.py`, `bin/chatgpt_oracle_compat.py`와
  `tests/test_chatgpt_oracle_run.py`.
- 제출 후 conversation URL 보존 패치 2개와 대응 회귀:
  `browserIndex.await-post-submit-conversation-url.patch`,
  `conversationUrlMonitor.drain-post-submit.patch`,
  `test_chatgpt_oracle_conversation_url_persistence.py`.
- 정산 정리와 복구 불가 상태의 회귀 파일:
  `test_chatgpt_oracle_settlement_cleanup.py`,
  `test_chatgpt_oracle_submission_state_unrecoverable.py`,
  `test_chatgpt_oracle_terminal_unrecoverable.py`.
- 설치 manifest의 외부 패치 항목과 기존 개행 차이, package.json의 개행 dirty.
  이 파일들을 문서 보완 커밋에 포함하거나 정리하지 않습니다.

공식 증거가 없는 과거 실행 결과는 unknown으로 보존합니다. 기존 실행의 재정산,
상태 파일 수정, 권한 우회, 신규 replacement 제출을 패치노트 갱신에 섞지 않습니다.
<!-- pending-work:end -->

## Unreleased - Preserve fresh evidence checks without repeated file opens

- Reuse a bounded, run-local set of unbuffered read-only handles for meeting
  events and sealed turns. Re-read and hash all bytes on every check; preserve
  leaf/handle identity, ancestor/reparse checks, fsync and create-only publication.
  Close handles on every exit rather than caching bytes, mtimes or trust decisions.
- Add same-size/same-timestamp tampering, replaced/reparse identity, descriptor
  bounds, publication/child failure cleanup and no-replacement regressions.
- Make the debate timeout fixture establish its intended pre-/post-admission
  interleaving before the unchanged timeout starts. Test both zero-admission and
  already-admitted children; preserve ledger/report freezing and delayed-call bans.
- Add the I/O regressions to the existing fast gate without removing targets,
  adding skips/deselections or increasing its 100-second wall-clock budget.
  Functional verification is not a performance-budget pass or a live web run.

## Unreleased - Public research and agent-authored meeting participation

- Add the separate research-meeting controller and exact-session Oracle adapter:
  independent public/private investigation, optional agent-authored objections,
  approved-topic follow-up research, explicit final reviews and synthesis.
- Bind reviewed plans, public briefs, delivered snapshots and response provenance.
  Keep failed research pending, require the objection author's withdrawal, and
  reject closing consensus when the decision snapshot changed. Preserve missing
  initial public research even when every participant subsequently agrees.
- Keep public research missions separate from private meeting inputs. Reject
  extra public-request fields, unapproved topics and public-workspace drift.
  These are mission/data-minimization boundaries, not a connector ACL sandbox.
- Add create-only, hash-chained, atomically published events and a read-only
  terminal viewer. Preserve native ownership; never replay uncertain turns.
- Include both runtime modules, their debate safety dependency, documentation
  and synthetic regression tests in packaging and verification contracts.
  Synthetic tests and agent-reported source cards are not live web-search proof.
  No published release, live research canary or performance-budget pass is implied.

## Unreleased - Bounded independent Oracle debate

- Add `--mode debate` / `--debate-rounds {1,2,3}` with independent drafts,
  verbatim cross-review handoffs, a separate Judge, and final synthesis. The
  default three-role/two-round budget is at most 12 fresh regular Oracle
  conversations; no same-conversation reuse, automatic Pro, or legacy runner.
- Bind the built plan to the exact manifest SHA-256 and mode. Reject unsafe
  raw output paths before resolution, prior execution artifacts, replay, and
  file collisions occurring during execution instead of overwriting evidence.
- Persist launch reservations before provider calls; protect concurrent ledger
  snapshots and revoke pending launches after failure, cancellation, or timeout.
  Preserve native exact-session ownership and never manufacture settlement.
- Validate terminal/task/mission/parent/output and independent conversation
  identities. Ambiguous Judge verdicts fail closed; exhausted rounds retain
  dissent as `debate_inconclusive`, not success. Consensus is not proof that
  code executed or that the original problem was objectively solved.
- Register the debate runtime in the lifecycle manifest and its synthetic
  regression tests in the fast gate. Document preview/live distinctions and
  unresolved-run admission restrictions in `MULTI_AGENT_ANALYSIS.md`.
- Explicitly preserve stdout/stderr for the hidden fast-gate child on Windows.
  Include temporary-directory cleanup in the wall-clock budget and report
  test time separately from cleanup time. Keep the same 100-second budget,
  test targets, and existing deselection; an over-budget pass remains exit 3.
  This entry describes source changes, not a published release or live canary.

## Unreleased - Bind regular DevSpace selector failures before settlement

- 일반 `devspace`의 `Unable to locate the ChatGPT model selector button` 실패를
  Pro와 동일한 종류의 authoritative pre-submission proof로 승격했습니다. exact
  stdout envelope, Oracle metadata, `promptSubmitted=false`, ChatGPT root URL,
  requested model/profile configuration, dynamic CDP port, run-local browser profile,
  browser target, mission hashes, recovery no-live/no-URL evidence를 모두 검증합니다.
- task-bound 실행은 browser identity receipt가 아직 생성되지 않은 이 pre-submit
  실패에 대해서만 prompt-free exact-slug harvest를 허용하며, `live` recovery는 계속
  차단됩니다. 단순 terminal signature 문자열 추가로는 어떤 권한도 생기지 않습니다.
- historical `legacy-unbound` 실행은 일반 `require_current_task_owns_run()` 정책을
  그대로 유지합니다. 오직 `settle-no-submission`에서만 append-only ownership receipt가
  exact project/run/mission/slug/CDP/browser-temp tuple을 다시 증명하고 selector proof와
  recovery evidence까지 일치할 때 bounded compatibility를 허용합니다. 기존
  `user-confirmed-no-submission` 확인 토큰도 그대로 필수입니다.

## 1.20.18 - Let a multi-agent run choose its browser model strategy

- `bin/chatgpt_multi_agent.py run` accepts `--model-strategy select|current|
  ignore`, and the value travels through the Multi manifest into every lane
  manifest instead of being hardcoded to `select`. `select` stays the default,
  so existing runs are unchanged.
- This exists because Oracle's model-selector lookup fails outright when
  ChatGPT moves its model picker: the run dies before submitting with
  `Unable to locate the ChatGPT model selector button`, and Oracle itself names
  `current` and `ignore` as the escape hatches. Neither was reachable from the
  multi-agent surface.
- Strict Multi v2 still requires `select`. That path applies canonical writes,
  so it keeps proving the exact model rather than trusting whatever the browser
  already had selected. An unknown strategy is rejected when the manifest loads.
## 1.20.17 - Read lifecycle JSON as UTF-8 on non-UTF-8 ANSI locales

- **설치·업데이트·롤백·진단이 한국어 로케일에서 첫 줄부터 죽던 것을 고쳤습니다.**
  Windows PowerShell 5.1의 `Get-Content -Raw`는 BOM이 없는 파일을 콘솔 ANSI
  코드페이지(여기서는 cp949)로 읽습니다. 그런데 이 스크립트들이 쓰는 JSON 산출물
  (WAL 저널, 설치 영수증, 계약 파일)은 전부 **BOM 없는 UTF-8**이라, 경로에 든
  비ASCII 문자가 깨지면서 잘못된 디코딩이 백슬래시 하나를 삼켜 `\\`가 불법
  이스케이프 `\.`이 됩니다. `ConvertFrom-Json`이 `ArgumentException`을 던지고,
  `install.ps1`은 `Resume-PendingInstallTransactions`에서 **파일을 하나도 복사하기
  전에** 종료됩니다.
- **`Get-Content ... | ConvertFrom-Json` 14곳 전부에 `-Encoding UTF8`을 명시했습니다**
  — `install.ps1` 3곳, `update.ps1` 5곳, `doctor.ps1` 3곳, `rollback.ps1` 1곳,
  `bin/codexpro_project_cloudflare_bootstrap.ps1` 2곳. 프로세스 stdout/stderr 로그를
  읽는 `-Raw` 호출은 JSON이 아니므로 건드리지 않았습니다.
  `scripts/start_devspace_bootstrap.ps1`은 이미 올바르게 지정하고 있었습니다.
- **회귀 테스트 2건**: 모든 JSON 읽기가 `-Encoding UTF8`을 갖는지 훑는 정적 검사와,
  비ASCII 경로가 담긴 BOM 없는 UTF-8 저널이 실제로 파싱되는지 PowerShell로 확인하는
  검사. 후자는 판정을 ASCII로만 되돌려 받습니다 — PowerShell 5.1은 stdout도 콘솔
  코드페이지로 쓰기 때문에 값 자체는 돌아오는 길에 깨집니다.
- **거울상 결함도 함께 고쳤습니다**: `tests/test_codexpro_fixed_address_bootstrap.py`가
  PowerShell 출력을 엄격 UTF-8로 디코딩해서, 비ASCII 경로가 끼면 리더 스레드가
  `UnicodeDecodeError`로 죽고 `stderr`가 `None`이 됐습니다. 같은 저장소의
  `test_install_lifecycle.run_powershell`과 동일하게 `errors="replace"`를 씁니다 —
  검사하는 계약 토큰은 ASCII라 치환의 영향을 받지 않습니다.

## 1.20.16 - Harden multi-agent CLI concurrency and strict success settlement

- **Multi-Agent Preflight 게이트 추가**: `bin/chatgpt_multi_agent.py` 진입 시점에
  `PREFLIGHT.ensure_exact_root_qualified`를 호출하여 프로젝트 루트의 DevSpace 적격성과
  `worktree` 디렉터리 권한을 사전에 검증합니다. 환경 미비 시 브라우저 러너 진입 전
  `MULTI_AGENT_PREFLIGHT_FAILED`로 즉시 차단(fail-closed)하며, 디버깅을 위한
  `--skip-preflight` 플래그를 지원합니다.
- **완료 ≠ 성공 엄격 판정 적용**: 워커 프로세스가 정상 종료(exit 0)되었더라도
  실제 산출물(`output.md`)이 없거나 비어있으면 `ok: False`, `status: "failed"`로
  판정합니다. 비-strict 모드에서도 일부 워커가 실패하여 부분 완료(`partial`)된 경우
  전체 결과를 `ok: False`로 처리하고 `MULTI_AGENT_LANES_FAILED` 에러를 명시합니다.
- **Windows 자격 캐시 교체 경합 (WinError 5/32) 방어**: 다중 워커가 동시 실행될 때
  공용 JSON 파일 및 설정/캐시를 원자적 교체(`os.replace`)하는 과정에서 발생하는
  Windows 일시적 파일 잠금 오류(`ATOMIC_REPLACE_WINDOWS_TRANSIENT_ERRORS = {5, 32}`)에
  대해 지수 백오프 재시도(최대 5회)를 적용하여 프로세스 크래시를 방지합니다.
- **동시 실행 토큰 갱신 경합 완화**: `chatgpt_oracle_multi.py`의 `_run_wave` 스레드풀
  기동 시 `launch_index` 기반의 stagger 딜레이를 적용하여, 다중 워커가 밀리초 단위로
  동일 시점에 토큰 갱신 API에 몰려 발생하는 상호 토큰 무효화(401) 및 thundering herd
  경합을 방지하고 선행 워커가 갱신한 최신 캐시 토큰을 안전하게 재사용하도록 보호합니다.

## 1.20.15 - Verify the current GPT-5.6 Sol Pro power slider

- Oracle 0.18.0 now recognizes ChatGPT's current unified `Thinking effort`
  picker, where Pro is represented by the simple Power slider as `Pro, 5 of
  5` instead of a separate Heavy/Pro effort row. An already-selected Pro tier
  is accepted only when the same visible menu also proves that the checked
  model is exactly `GPT-5.6 Sol`.
- When the slider is below Pro, the adapter uses bounded ArrowRight input and
  requires two consecutive post-change proofs of both Power 5/5 and the exact
  checked model. A missing slider, changed model, unavailable control, or
  unverified result remains a pre-submit failure; no lower effort is submitted
  as Pro.
- The compatibility patch is hash-gated against the exact published Oracle
  0.18.0 package, and the regression fixture preserves the live 2026-09-02 UI
  shape whose trigger is named `Thinking effort` rather than `Pro`.
- The live slider's accessibility range is zero-based (`0..4`) even though its
  visible position is one-based (`5 of 5`). The verifier now derives the
  ordinal from `aria-valuemin`, `aria-valuemax`, and `aria-valuenow`, then
  requires it to match the visible `N of M` position before accepting Pro.
- Model rows that mount after the controlled slider fragment are observed with
  a bounded two-sample stability check. Conflicting ranges, wrong models, and
  duplicate explicit picker menus remain fail-closed, and the immediately
  preceding exact patch can be migrated through its hash-bound legacy patch.

## 1.20.14 - Select the current visible Pro effort fail-closed

- New GPT-5.6 Sol Pro and read-only Pro follow-up submissions now pass Oracle
  0.18.0's explicit `pro` thinking-time token instead of the retired `heavy`
  compatibility spelling. A missing effort on a new Pro manifest normalizes to
  `pro`, while regular non-Pro defaults remain unchanged.
- Historical `heavy` runs, immutable receipts, no-submission evidence, and
  follow-up parents remain readable and recoverable without rewriting their
  authority. Only new child submissions are normalized to `pro`; a newly
  supplied raw Pro manifest that still requests `heavy` is rejected before a
  run directory or subprocess can be created.
- If the current ChatGPT effort selector cannot prove that the visible `Pro`
  radio is selected, the run fails before submission as
  `ORACLE_PRO_TIER_NOT_SELECTED`; it may not silently continue on Extra High.
  Regression coverage binds the current five-label effort menu and preserves
  the bounded legacy Oracle 0.17.1 compatibility paths.

## 1.20.13 - Preserve exact task-bound metadata settlements

- A `v1.20.12` Oracle metadata-rename settlement created immediately before
  the final task-binding hardening remains valid when its append-only artifact
  lacks only the later `host_failure.source_thread_id` field. Revalidation
  still requires the current state, originating task, ownership block, and
  immutable ownership receipt to bind the same valid Codex task UUID, and
  every pre-existing host-failure field and hash must match exactly.
- Legacy-unbound runs, foreign-task settlement attempts, changed ownership
  receipt hashes, provider output/conversation evidence, and any other field
  drift remain fail-closed. This prevents an already settled pre-submit run
  from resurrecting its task-scoped project lock after upgrading.

## 1.20.12 - Retry transient Windows Oracle metadata replacement

- Oracle 0.18.0 session metadata now retries only transient Windows `EPERM`,
  `EACCES`, and `EBUSY` failures while atomically replacing `meta.json`. This
  prevents a pre-submit run from dying when antivirus or another short-lived
  reader briefly holds the destination, while preserving fail-closed behavior
  for every other platform and error.
- If the bounded retry still exhausts before any browser runtime or conversation
  exists, the exact task owner may use the normal explicit
  `settle-no-submission` path. The evidence binds the exact Oracle locator,
  immutable mission and ownership receipt, pending session metadata, exited
  controller, empty output/conversation/browser identity, and the exact Windows
  atomic-rename error; path, runtime, URL, output, or receipt contradictions
  remain ineligible. The state, ownership block, and append-only receipt must
  all bind the same valid Codex task UUID; legacy-unbound and foreign-task
  settlement attempts remain forbidden.

## 1.20.11 - Recover exact ordinary DevSpace prompt timeouts

- task-bound 일반 `devspace` 실행이 prompt commit timeout 뒤 browser identity
  receipt를 만들기 전에 끝나도, 동일한 Oracle zero-turn commit probe와
  profile-bound browser 메타가 정확히 일치할 때에만 `harvest --dry-run`을
  허용합니다. live recovery는 계속 receipt 없이 거부되고, harvest 뒤에도
  hash-bound recovery evidence와 명시적 사용자 no-submission 확인이 있어야만
  정산·잠금 해제가 가능합니다.
- exact harvest는 zero-turn/ownership/mission/profile/browser-config 증거를
  append-only 영수증으로 봉인합니다. 정산과 이후 잠금 판정은 그 영수증의
  SHA-256과 현재 불변 증거를 다시 대조하므로, generic recovery 로그만으로
  정산 권한을 얻을 수 없습니다.
- 모델 ID `gpt-5.6`은 실제 Oracle browser 메타의 선택 라벨 `GPT-5.6 Sol`에
  정확히 결속합니다. 다른 모델·전략·thinking time 또는 zero-turn 증거의
  모순은 계속 fail-closed 합니다.

## 1.20.10 - Persist Oracle Local network access and expose audit receipts

- Oracle 0.18.0이 띄우는 격리 Chrome에만 `--disable-session-crashed-bubble`을
  해시 결속 패치로 추가하고, Oracle seed 프로필의 `exit_type=Normal` 및
  `exited_cleanly=true`를 백업·SHA 영수증과 함께 고정합니다. 일반 Chrome의
  세션 복원 설정과 열린 탭은 변경하지 않습니다.
- 사용자 범위 Chrome 정책 ACL이 쓰기 금지인 Windows에서도 `enable`이 실패 안내로
  끝나지 않고, 닫힌 Oracle seed 프로필의 정확한 `chatgpt.com` origin에 Chrome 151+
  `local_network`와 `loopback_network` 허용을 백업·원자적 쓰기·SHA 영수증과 함께
  저장합니다. 일반 Chrome 프로필, 로그인, 쿠키, 다른 사이트 권한은 변경하지 않습니다.
- `status`는 enterprise policy와 Oracle seed 프로필을 함께 검증하므로 throwaway 실행이
  복제 전에 실제 영속 권한을 갖는지 판정합니다.
- DevSpace 1.0.8의 `open_workspace`, `read`, `read_chunk`가 서버 생성 Audit receipt ID를
  텍스트뿐 아니라 각 도구의 `structuredContent`와 output schema에도 노출해, ChatGPT 앱
  렌더러가 보조 text block을 생략하더라도 final canary challenge-response를 완결합니다.
- Oracle 실행 종료 뒤 Windows가 PID를 재사용하더라도 PID 존재만으로 과거 실행을
  살아 있다고 오판하지 않습니다. 현재 프로세스의 exact slug·run 디렉터리·격리
  브라우저 프로필 결속을 확인하며, 읽을 수 없거나 모호한 신원은 계속 fail-closed 합니다.

## 1.20.9 - Preserve registered apps across DevSpace updates

- 기존 ChatGPT 개발 앱의 이름·MCP URL·OAuth 연결을 업그레이드가 보존하고,
  도구 목록이 오래된 경우에는 앱 재생성 대신 기존 앱의 `새로 고침` 후 필요한
  경우에만 `다시 연결`하도록 한영 온보딩과 진단 메시지를 바로잡았습니다.
- DevSpace 1.0.8의 `ui://devspace/workspace-app.html` 리소스에 공개 HTTPS origin을
  hash-gated `ui.domain`으로 결속해 앱 제출 화면의 widget-domain 누락 경고를
  제거합니다. HTTP, 자격 증명이 포함된 URL, loopback origin은 fail-closed 됩니다.
- 관리형 DevSpace 복구가 healthy 401만 보고 패치 적용을 건너뛰지 않고 매번 exact
  package/native/compatibility 상태를 재검증합니다. 실제 restart marker가 있을 때만
  서비스를 한 번 재기동하며 malformed marker 보고는 재시작 없이 거부합니다.
- OAuth replay canary timeout을 구조화된 비밀 없는 오류로 남기고, Chrome의 분리된
  Local Network/loopback 정책과 기존 온보딩 상태 마이그레이션을 회귀 테스트합니다.

## 1.20.8 - Decode Tailscale status as UTF-8 during onboarding

- Windows 한국어 로케일에서도 `onboard.py start`가 `tailscale status --json`의
  비 ASCII 장치 정보를 CP949로 오해하지 않도록 stdout bytes를 UTF-8 strict로
  직접 디코딩합니다.
- 잘못된 인코딩은 기존 `TAILSCALE_HOSTNAME_UNAVAILABLE` fail-closed 오류로 유지하고,
  실제 UTF-8 비 ASCII 상태 응답과 손상된 응답을 모두 회귀 테스트합니다.

## 1.20.7 - Bind registered-app final gates to server receipts

- 새 일반 비-Pro final canary만 `registered_app_final_gate`로 명시해 실제 생성된
  Oracle `run_id`를 `open_workspace → read → read_chunk`의 동일 `auditNonce`로
  결속합니다. 세 호출은 재시도 없이 정확히 한 번씩 수행되고 세 서버 생성 영수증 ID를
  답변에 echo해야 합니다.
- `onboard.py prepare-final-gate`가 current Codex task에 결속된 exact manifest와
  dry-run/live/record 명령을 생성합니다. 다른 작업, 일반 터미널, Pro, 비정규 모델/노력은
  제출 전에 fail-closed 됩니다.
- 기존 ordinary/Pro/legacy prompt와 v1.20.6 이전 final-gate 기록은 그대로 호환됩니다.

## 1.20.6 - Explain frozen registered-app Action snapshots

- 최종 canary에서 `open_workspace`와 `read`는 성공하지만 `read_chunk` 또는
  서버 생성 Audit receipt ID가 없을 때, 로컬 DevSpace 장애로 오인하지 않고
  ChatGPT의 등록 앱 Action 스냅샷 갱신이 필요한 상태로 명확히 분류합니다.
- Enterprise/Edu의 `Action control > Refresh`와 Business/미지원 UI의 앱 재생성·게시,
  수동 Action 검토, `post-register` 1회, 새 일반 비-Pro auditNonce canary 순서를
  한영 설치 마법사와 문서에 안내합니다.
- 세 도구와 세 서버 영수증의 fail-closed 최종 gate는 완화하지 않습니다.

## 1.20.5 - Harden managed DevSpace cold-start confirmation

- Windows의 첫 `npx` DevSpace 1.0.8 기동이 20초를 넘는 경우에도 관리형
  재시작 증명이 조기 실패하지 않도록, 정확한 post-patch listener 신원 확인의
  bounded 대기를 120초로 늘렸습니다.
- 단순 포트나 `/healthz` 응답으로 완화하지 않고, 기존의 패치 해시, 정확한
  `dist/cli.js serve` 명령, 패치 이후 시작 시각 검증을 모두 유지합니다.
- 60초 지연 listener와 old/foreign listener를 함께 재현하는 회귀 테스트를
  추가해 marker가 정확한 새 서비스에서만 제거되는 것을 검증합니다.

## 1.20.4 - Validate DevSpace 1.0.8

- DevSpace `1.0.8` is the explicit current runtime for new setup and managed
  macOS launches after archive-integrity, exact patch, and compatibility-gate
  validation. DevSpace `1.0.7` is retained as the rollback LKG; neither path
  resolves a moving npm `latest` tag.
- The release portability workflow prepares only the hash-verified published
  `1.0.8` archive before running the cross-platform contract suite.
- DevSpace `1.0.8` adds an optional local-agent daemon and provider CLI
  adapters. Managed ChatGPT workspace services pin `DEVSPACE_SUBAGENTS=false`;
  enabling that separate execution surface remains an explicit user action.
- The fast pre-submit gate now runs a named cross-section of runner launch,
  ownership, app-read, completion, restart, and recovery contracts while the
  full suite retains every exhaustive contradiction permutation. This restores
  the 100-second CI budget without reducing full release coverage.

## 1.20.3 - Preserve follow-up evidence before browser launch

- A live read-only Pro `followup` now creates the exact child run directory,
  state, stdout, and stderr before the app-read, DevSpace-root, runtime-version,
  and compatibility preflights. A bounded failure therefore produces a normal
  pre-submit child and round-result receipt instead of leaving only a consumed
  parent reservation.
- Every non-dry-run follow-up appends a parent-side launch receipt before
  entering the child runner. An exception that occurs even before the child
  layout exists appends a hash-bound prelaunch-failure receipt with the exact
  error and `submission_action: none`.
- Follow-up manifests are parsed from the same verified byte buffer whose
  SHA-256 is sealed in the launch receipt and are checked again immediately
  before `Popen`. The reservation mission SHA-256 is enforced both before
  child preparation and immediately before submission. A parent-scoped
  controller mutex serializes every round key through execution, and symlinked
  parent artifact directories or manifest leaves fail closed.
- An unknown exception without child state is recorded as
  `submitted_unknown`; only the bounded pre-layout manifest/mission/path
  failures may claim `submission_action: none`.
- `--dry-run` remains side-effect free. Historical reservation-only round keys
  are immutable and stay consumed; operators must preserve them and, only
  after proving the detached controller ended, choose a new round key rather
  than deleting or replaying the old reservation.

## 1.20.2 - Prove exact app reads and close onboarding gaps

- New read-only Pro runs are blocked before browser creation unless a recent
  regular non-Pro final gate proves `open_workspace`, `read`, and `read_chunk`
  for the exact requested root through the configured app. A receipt for a
  different allowed root cannot authorize the run.
- Oracle state can now append the Pro app-read gate and provider-session
  evidence without inventing a status transition. Only exact-bound upstream
  `completed` metadata confirms provider terminal state; a local/browser
  `error` with `completedAt` remains nonterminal evidence.
- The Oracle `0.17.1` LKG and current `0.18.0` compatibility contracts now warn
  when a previously detected thinking label disappears for five minutes, while
  keeping the independent terminal watchdog active. Exact legacy-patch
  migration and behavior tests cover both the original never-detected case and
  the detected-then-missing case.
- Onboarding now labels Oracle login and ChatGPT app registration as user
  attestations until the functional final read gate succeeds. The Korean and
  English quick paths list the same nine-stage order and explain that a pasted
  repository URL is checked out by the coding agent before the local wizard
  starts. Its initial status says setup is in progress instead of claiming the
  program install is already complete before the install receipt exists.
- Ultra Economy activation no longer implies Pro authority. Its mandatory
  read-only Pro design stage requires a separate explicit user authorization
  plus `allow_pro: true`; otherwise the profile fails closed before submission.
- GitHub workflows pin checkout and Python setup actions to exact commits, and
  the drift watcher refuses an unmanaged or duplicate exact-title issue instead
  of creating a second `Upstream runtime drift` issue. Release publication now
  also requires a final-head independent-review receipt on the merged validation
  PR plus successful three-OS portability CI for the exact main commit.
- Atomic Oracle state writes use a short same-directory temporary basename so
  deep but valid Windows settlement paths do not cross `MAX_PATH` before the
  final atomic replace. The current and legacy thinking-status patch files are
  both included in lifecycle installs.

## 1.20.1 - Assign and gate upstream runtime promotion

- The six-hour watcher remains strictly read-only, but its stable drift issue
  now carries the scheduled Codex maintainer promotion/validation owner, 24-hour validation-start and
  48-hour all-gates promotion targets, exact candidate integrity, a dedicated
  label, and the complete machine-readable gate checklist. A routine stable
  patch/minor candidate has standing approval only after every gate passes;
  breaking, permission/OAuth, patch-conflict, failed-canary, and ambiguous
  cases still require explicit user approval.
- Runtime policy schema v2 makes the reporter, promotion owner, test owner,
  approval split, timing, and closed evidence set mandatory instead of relying
  on the vague instruction to promote promptly.
- Reporter permissions and maintainer permissions are now separate fields: the
  reporter cannot mutate runtime state, while the scheduled maintainer may
  promote, publish, install, and perform one safe-window restart after all
  routine gates pass. This closes the previous policy gap where a candidate
  could be detected without naming who actually deploys it.
- The watcher resolves only one exact-title drift issue and fails closed on
  duplicates; it creates a missing label without overwriting existing label
  metadata.
- DevSpace promotion and post-repair verification now require a separate
  mission-file `read` and `read_chunk` through the exact workspace ID returned
  by `open_workspace`. The gate verifies the server-returned complete SHA-256
  against the locally bound mission bytes, so matching self-authored workspace
  ID markers cannot pass. HTTP 401 health, workspace-open success, or bundled
  instructions alone cannot hide an intermittent `mcp_network_error` read path,
  and Pro stays blocked until a fresh regular non-Pro canary succeeds.
- The audit nonce now makes `open_workspace` the first workspace/process/
  mutation call in the opaque OpenAI session scope, disables every workspace
  mutation surface including `download_artifact`, and server-numbers the exact
  three receipt steps. Each tool response returns an unpredictable server receipt
  ID; the exact terminal Oracle conversation must echo all three IDs, binding the
  opaque DevSpace scope to that public conversation. The final gate rejects
  duplicate-key JSON, mixed workspaces/scopes, partial/tail chunks, missing
  challenge responses, and a receipt digest that does not match the exact mission.
- The host maintainer heartbeat is now represented by a checked-in exact contract
  and a verifier that compares the active Codex automation TOML. Downstream
  installs receive the audit contract but never auto-register the maintainer task.
- Read-only diagnosis now gives the bounded signature
  `registered-app-read-network-failure-after-workspace-open` when durable
  terminal evidence proves that workspace open succeeded but a same-connector
  file read failed with `mcp_network_error: Connection failed`.

## 1.20.0 - Follow validated upstream stable runtimes

- Oracle `0.18.0` and DevSpace `1.0.7` are now the defaults for new work after
  published-integrity, exact-patch, syntax, compatibility, and cross-platform
  validation. Oracle `0.17.1` and DevSpace `1.0.4` remain rollback LKG and exact
  historical-recovery contracts rather than continuing as stale defaults.
- A strict machine-readable runtime registry and six-hour read-only GitHub
  drift watcher compare current versions with official npm `latest`. The
  watcher maintains one issue and cannot promote, install, restart services,
  open ChatGPT, or modify a project.
- Current Oracle keeps task-scoped ownership, no-duplicate/prompt-not-observed
  fail-closed behavior, dynamic CDP binding, saved-output recovery, and bounded
  terminal-marker detection while inheriting upstream UI/model/cookie fixes.
- Current DevSpace keeps the existing allowed-root, OAuth replay, write/delete,
  large-read, and workspace-context safety canaries while inheriting upstream
  restart-safe conversation/workspace reuse and actionable workspace errors.
- The first-install and update wizard can now stop an exact running DevSpace
  `1.0.4` LKG service while upgrading to `1.0.7`. The stop authority remains
  limited to the resolved current/LKG `dist/cli.js serve` identity; arbitrary
  package versions and unrelated listeners still fail closed.

## 1.19.7 - settle direct DevSpace model-option misses safely

- Ordinary `devspace` runs that fail before submission because Oracle 0.17.1
  cannot find the requested model option can now enter the existing explicit
  user-confirmed no-submission settlement path instead of retaining a permanent
  task-scoped project lock.
- Admission binds the exact 13-line Oracle launcher/error transcript, requested
  model and browser profile, mission hashes, prompt-free no-tab/no-URL recovery,
  and strict duplicate-free Oracle metadata proving `execute-browser`,
  `promptSubmitted=false`, and the ChatGPT root URL. A conversation, output,
  changed model/profile/research settings, metadata drift, symlink, duplicate
  key, or foreign transport remains fail-closed.
- The change integrates the narrow intent of PR #20 onto the current Oracle
  ownership, follow-up, terminal-watchdog, and saved-output identity code rather
  than replacing those newer lifecycle guarantees with its older base.
- Live and durable terminal classifiers now accept up to 32 bounded provider
  reference-backlink rows that begin with an exact file-like citation and may
  include rendered section labels, quoted annotations, or semicolon-separated
  paths before the final `↩`. Ordinary prose, duplicate markers, oversized
  footers, and rows without a file-like citation remain fail-closed.

## 1.19.6 - preserve follow-up authority after saved-output reconciliation

- A saved-output reconciliation can now seal a separate v2 browser identity
  receipt when the runner's reserved CDP port differs from Oracle's completed
  runtime port. The original expected port remains bound to the ownership and
  follow-up receipts; the observed port is recorded separately and never
  rewrites historical authority.
- The owner-only `seal-saved-output-browser-identity` command migrates an
  already reconciled v1.19.5 run without opening Chrome, attaching CDP, sending
  a prompt, or changing the conversation. New mismatched-port reconciliations
  seal the same receipt automatically, and interrupted sealing remains safely
  repeatable.
- Follow-up admission revalidates the saved-output settlement, output, stdout,
  transcript, completed Oracle metadata, conversation, target, run-local
  profile, immutable ownership/follow-up receipts, and stopped run-owned PIDs.
  Foreign tasks, symlinks, drift, active processes, matching-port widening, and
  conflicting receipts continue to fail closed.
- Portable test processes now use a PID outside normal Windows/Linux ranges so
  Ubuntu CI cannot mistake an unrelated live runner process for a fixture's
  Oracle child.

## 1.19.5 - reconcile Oracle-saved terminal output

- Added an owner-only `settle-saved-output` lifecycle command for the narrow
  crash boundary where Oracle has already saved official terminal output and
  strict completed session metadata, but the outer runner did not commit the
  final state transition. It never sends, recovers, retries, or edits a foreign
  task's run.
- Reconciliation is hash-bound to the prior state, official output, stdout,
  Oracle metadata, immutable ownership receipt, and exact follow-up binding.
  It also requires one canonical saved-output log record, the unchanged parent
  conversation and isolated profile, a valid terminal outcome/Pro schema,
  empty stderr, and stopped observer/controller/Chrome processes.
- The command writes an append-only receipt before marking the exact run
  `complete / terminal / terminal_harvested`. Existing browser-receipt recovery
  remains unchanged; path drift, symlinks, live PIDs, foreign ownership,
  conversation mismatch, and ambiguous output continue to fail closed.

## 1.19.4 - bound follow-up browser and terminal detection

- Oracle follow-up runs now keep the child's newly reserved CDP port instead
  of silently inheriting the parent conversation's old port. The exact child
  state, ownership receipt, Chrome runtime, and durable browser identity remain
  bound to one port; a mismatch is recorded as a non-authoritative diagnostic
  and never accepted as recovery authority.
- The v1 terminal watchdog now accepts exactly one `TASK_OUTCOME` marker when
  ChatGPT renders only a bounded set of reference backlinks after it. Ordinary
  trailing prose, malformed footers, more than 32 backlinks, duplicate or
  conflicting markers, visible Stop controls, and active thinking still fail
  closed. The durable result classifier uses the same bounded grammar, so a
  watchdog-terminal answer cannot later regress to an unknown task outcome.
- Each run persists whether the runner actually enabled the child-only v1
  terminal-watchdog environment. An exact v1 run fails before Oracle launch if
  that environment contract cannot be enabled; no user- or machine-global
  environment variable is required.

## 1.19.3 - task-targeted operational reports

- Oracle incident packets now record the exact run owner, the task from which
  the evidence was evaluated, and a run/slug-bound operational instruction.
  Unresolved-owner checks use the run owner's task scope instead of an
  unqualified project-wide view.
- A foreign evaluator receives only `route-to-owner-task`; it never receives
  executable recover, harvest, settle, stop, or retry authority. Reports must
  be rendered separately for each target task instead of broadcasting an
  owner's next action to sibling tasks sharing the same project root.
- Exact runs already proven terminal and harvested emit `action=none`, even
  when the local status remains `attention_required`. Incident v1 packets stay
  validation-compatible, while new packets use the closed v2 routing fields.

## 1.19.2 - action-bar-independent Oracle completion

- Oracle 0.17.1 can now finish an exact v1 task-outcome run when ChatGPT omits
  the transient thinking/streaming label and completion action bar. The bounded
  fallback requires the same conversation and new assistant turn already
  enforced by Oracle, no Stop control, no active strong-thinking signal, an
  unchanged full response across two observations for at least five seconds,
  and exactly one final `TASK_OUTCOME` marker.
- The fallback is enabled only for the runner's explicit v1 answer contract.
  Legacy and generic Oracle runs scrub any inherited opt-in variable and retain
  upstream behavior. Live/harvest recovery inherits the persisted contract
  without creating a new prompt or conversation.
- A distinct warning is emitted after five minutes when no thinking status has
  ever been detected, while the independent terminal watchdog continues. This
  distinguishes a missing UI label from ordinary visible streaming without
  treating elapsed time as terminal.

## 1.19.1 - structured follow-up pre-composer settlement

- Follow-up failures are no longer eligible for no-submission settlement only
  because an error sentence appears in a growing text whitelist. An exact
  task/run/mission/round binding can now use Oracle's structured
  `resume-conversation` error, absent browser runtime and identity receipt,
  matching stdout/transcript, empty stderr, absent output, and exited observer
  as bounded pre-composer evidence. Explicit owner confirmation and inactive
  exact processes remain mandatory before releasing the task-scoped lock;
  stale `process-exited` state is rejected while the recorded PID is alive or
  its termination cannot be proven.
- Resumed-conversation hydration receives one bounded second observation window
  on the same exact conversation. The retry verifies the conversation identity
  before waiting again and never falls back to a fresh chat or submits a prompt
  while prior turns remain unsettled.
- Portable Windows doctor checks now accept the active `python.exe` runtime
  when the POSIX-style `python3` command name is unavailable.

## 1.19.0 - unify Ultra GPT and closed workflow auditing

- `strict-ultra` is no longer presented as a separate mode. New workflows use
  `workflow_profile: ultra-gpt` and add an explicit `closed_audit` contract
  only when machine-verifiable provenance is required.
- The optional audit reuses the existing Ultra GPT scheduler and adds the
  bound dependency, authority, advisory Research Governor, identity ledger,
  local-gate receipt, and final workflow audit without changing ordinary Ultra
  GPT behavior.
- Legacy `workflow_profile: strict-ultra` manifests, frozen
  `codex.chatgpt.strict-ultra-*/v1` artifacts, receipts, and recovery identities
  remain accepted without rewriting. Dry-run reports the old profile name as a
  deprecated compatibility alias.
- README, English README, repository/global policy, Ultra GPT skill, and docs
  now expose one Ultra GPT mode with an optional closed-audit capability.

## 1.18.6 - follow-up no-submission settlement continuity

- An archived-parent follow-up that failed before the composer now goes
  directly to explicit user-confirmed no-submission settlement. Recovery and
  harvest are rejected with
  `FOLLOWUP_ARCHIVED_PARENT_HARVEST_NOT_APPLICABLE`, because reopening the
  already-known parent cannot prove a child submission.
- A v1.18.5 run that already followed the earlier harvest guidance remains
  settleable only when the exact owned slug produced one strict no-live-tab /
  no-recoverable-URL log pair, no recovery state, no child conversation URL,
  no output, no nonempty candidate, and no additional recovery artifacts.
  Partial, linked, changed, symlinked, or ambiguous evidence still fails closed.
- Historical official follow-up settlement receipts created under the v1
  eligibility label are revalidated against today's stricter raw-artifact
  predicate without rewriting the receipt. Compatibility is limited to the
  original textarea-absent evidence class; all hashes, task/run/mission/parent,
  round, recovery, and Oracle metadata bindings must remain exact.

## 1.18.5 - durable read-only Pro follow-up parents

- New `pro-devspace-readonly` manifests normalize the default `archive=auto`
  to `archive=never`, so a successful parent remains available for the next
  task-bound round. Explicit `archive=always` remains a deliberate single-turn
  choice, and historical archived parents keep bounded compatibility restore.
- Oracle 0.17.1 archived-parent restore now recognizes the direct page restore
  control as well as menu/dialog controls, uses pointer-compatible clicks and
  bounded polling, and seals a structured before-composer failure receipt with
  exact parent URL and unchanged turn counts.
- The exact `unarchive-menu-not-found` child from v1.18.4 can enter the official
  user-confirmed no-submission path without reopening the old parent. The gate
  remains owner/binding/hash bound and additionally requires the latest exact
  v1.18.4 lifecycle receipt to predate the immutable ownership receipt. It
  rejects URL drift or any click/submission ambiguity, requires all exact
  run-owned processes to be stopped, and never releases ownership without
  explicit user confirmation.
- Known v1.18.4 Oracle patch hashes migrate through exact reverse patches, so
  global upgrades remain deterministic even when the pristine npm backup is
  unavailable.

## 1.18.4 - archived Pro follow-up conversation restoration

- A task-bound read-only Pro follow-up now detects the exact parent's durable
  archive state. If the bound conversation was archived, Oracle 0.17.1 restores
  only that exact `chatgpt.com/c/<id>` conversation before composer readiness
  and re-archives it after the round completes.
- Local and remote browser resume paths fail closed on URL drift, missing or
  ambiguous restore controls, or an unverified final archive state. They never
  create a replacement conversation.
- Follow-up reservations now seal an append-only child binding before browser
  launch, including task, parent, round, mission, exact conversation, and CDP
  identity.
- A pre-composer `Prompt textarea did not appear` child can use one prompt-free
  exact harvest and explicit owner confirmation only after its parent/round
  reservation, error metadata, empty runtime, artifacts, and exact parent URL
  are revalidated. The consumed round key remains non-reusable.

## 1.18.3 - task-bound Pro 프롬프트 미관찰 교착 수리

- `pro-devspace-readonly` 실행이 프롬프트 commit 확인 전에 실패해 conversation
  URL 기반 browser identity receipt를 만들지 못한 경우에도, 같은 Codex task가
  exact slug에 대해 prompt-free `harvest` 한 번을 수행할 수 있게 했습니다.
- 이 예외는 서명된 task/run/mission ownership receipt, GPT-5.6 Sol Pro 읽기 전용
  프로필, Oracle 0.17.1의 `submit-prompt/prompt-commit-timeout`, 0개 turn과 모두
  false인 commit probe, ChatGPT 루트 composer, 출력·대화 URL 부재, exact 동적
  CDP port·격리 profile·target 결속이 모두 맞을 때만 열립니다. `live`, 새 prompt,
  외부 task, 일반 Chrome, 모순된 URL·probe·port·profile·target은 계속 거부됩니다.
- task-bound run의 프로젝트 mission 파일이 실행 뒤 합법적으로 수정돼도, immutable
  run mission 사본과 ownership receipt가 같은 원래 mission hash를 봉인하면 수확과
  사용자 확인 정산을 재검증할 수 있습니다. legacy-unbound run은 기존처럼 현재 source
  bytes 일치를 요구합니다.
- 수확은 소유권을 자동 해제하지 않습니다. exact no-tab/no-URL recovery 증거가 생성된
  뒤에도 사용자의 명시적 `user-confirmed-no-submission` 정산이 있어야만 프로젝트 lock이
  해제됩니다.

## 1.18.2 - 종결 Pro 후속 대화 신원 검증 수정

- Oracle이 실행 종료 과정에서 `meta.json`에 archive와 prompt 상태를 추가해도
  task-bound `pro-devspace-readonly` 부모의 후속 라운드가 잘못
  `FOLLOWUP_PARENT_IDENTITY_INVALID`로 거부되지 않게 했습니다.
- 영수증의 `oracle_meta_sha256`은 캡처 시점 전체 메타데이터의 감사 증거로
  보존하되, 권한 검증은 task/run/mission/slug와 Chrome PID·부모 PID·격리
  profile·동적 CDP port·target·conversation URL의 불변 결속을 사용합니다.
- 종료 후 비신원 메타데이터 변경은 허용하지만, 브라우저 target·profile·port·
  대화 또는 영수증 자체가 달라지면 계속 실패 폐쇄됩니다. 기존 v1.18.1
  append-only browser receipt도 같은 불변 튜플로 호환 검증합니다.
- Windows observer가 동시에 상태를 읽는 짧은 구간에 `state.json` 원자 교체가
  공유 위반(오류 5/32)을 만나는 경우만 제한적으로 재시도합니다. 지속 오류와
  그 밖의 파일 오류는 계속 즉시 실패 폐쇄됩니다.

## 1.18.1 - Pro 읽기 전용 정책 복원

- 모든 신규 `GPT-5.6 Sol / Pro` DevSpace 실행을 읽기 전용 설계·자문·검토
  단계로 제한합니다. Pro는 프로젝트 파일을 생성·수정·삭제하거나 명령을
  실행하지 않습니다.
- 쓰기 또는 명령 실행이 필요한 작업은 별도의 일반 `GPT-5.6` 최고 비-Pro
  사고 단계(`extra-high`)가 exact-root DevSpace에서 수행합니다.
- 이미 저장된 과거 `pro-devspace` 읽기·쓰기 실행은 exact recovery 시 원래
  권한 의미를 보존하며, 새 실행만 `pro-devspace-readonly`로 생성됩니다.
- 명시적 `pro-attachment`는 불변·외부 증거를 위한 별도 읽기 전용 경로로
  유지하며 DevSpace 실패의 자동 fallback으로 사용하지 않습니다.
- Oracle 소유권을 프로젝트 폴더가 아니라 Codex task와 exact run에 결속합니다.
  같은 project root의 서로 다른 task는 별도 mutex, slug, 브라우저 프로필,
  동적 CDP port와 대화를 소유해 동시에 실행할 수 있고, 같은 task의 미해결
  실행만 중복 제출을 막습니다. 다른 task의 실행은 `FOREIGN_TASK_SESSION`으로
  표시하되 recover/harvest/stop하지 않습니다.
- 제출 직후 conversation URL과 Chrome/controller PID, profile, CDP port,
  target identity를 append-only browser receipt에 기록해 프로세스 종료 뒤에도
  어느 task/run의 대화인지 재검증할 수 있게 했습니다.
- task-bound terminal `pro-devspace-readonly` 대화에는 내부 전용 `followup`
  명령으로만 후속 라운드를 보낼 수 있습니다. 각 라운드는 같은 ChatGPT
  conversation을 증명하면서 새 Oracle run/slug와 append-only 예약·결과 영수증에
  mission/state/output/transcript hash를 남깁니다. raw follow-up 옵션, foreign/legacy
  owner, 대화 변경, 중복 round는 계속 실패 폐쇄됩니다.
- 최초 설치 마법사는 기존 DevSpace root를 병합 보존하고 손상 config를 거부하며,
  Local Multi-GPT 선택을 실제 doctor와 결속하고, Chrome Local Network 변경 전
  명시적 동의를 요구합니다. ngrok 임시 주소를 차단하고 provider별 재부팅 안내를
  분리했으며, 설치 질문과 단계 안내를 환경에 따라 한국어/영어로 표시합니다.
- 최종 설치 gate는 임의 설명이 아니라 exact 일반 비-Pro Oracle run, root/app,
  GPT-5.6 extra-high, conversation URL, terminal outcome, output/listing SHA를
  재검증합니다. 한국어와 영어 전체 설치 가이드를 함께 제공합니다.

## 1.18.0 - WebJjonku Oracle 0.18 후속 실행 timeout 호환성

- 일반 comprehensive 자동화는 계속 검증된 Oracle 0.17.1만 허용하고,
  WebJjonku Linux 배포가 명시적으로 `webjjonku-linux` profile을 선택한 경우에만
  Oracle 0.18.0의 후속 실행 timeout 전달 패치를 적용합니다.
- 0.18.0 패치는 pristine·patched SHA-256과 npm integrity를 모두 확인하고,
  명시한 `--browser-timeout`만 child follow-up에 전달합니다. profile 누락,
  알 수 없는 버전, 해시 불일치는 브라우저 실행 전에 실패 폐쇄됩니다.
- 범위 제한 프로필은 버전·설치 루트·archive를 모두 명시해야 하며,
  Windows junction/reparse point와 archive 경로 탈출을 거부합니다. 공개
  portability CI는 Windows·macOS·Ubuntu에서 실제 0.18.0 archive를 검증합니다.
- runtime archive 검증도 CI extractor와 같이 대소문자 충돌 경로를 거부하고,
  새로 추가한 CI action은 변경 가능한 tag 대신 commit SHA로 고정했습니다.

## 1.17.1 - 미인증 브라우저 pre-submit 정산

- Oracle 전용 브라우저 프로필의 ChatGPT 로그인이 만료되면 컴포저 이전 단계에서
  종료되어 대화가 생성되지 않습니다. 그런데 이 조합이 `settle-no-submission`의
  인정 유형에 없어 제출 부재가 실증됐는데도 정산이 거부되고 프로젝트 락이
  영구히 유지됐습니다. `oracle-browser-session-absent-pre-submit/v1` 유형을
  추가했습니다.
- 판별은 좁게 유지합니다. stdout이 세션 미검출과 쿠키 미적용을 함께 기록하고,
  output이 없고, stdout과 모든 recovery 로그에 `chatgpt.com/c/` 대화 URL이 없고,
  mission 해시·경로·프로젝트 루트가 일치할 때만 인정합니다. 대화 URL이 있거나
  harvest가 실제 탭을 찾은 run은 그대로 거부됩니다.
- 정산이 `transport_status`와 `session_authority`를 다시 쓰기 때문에 기록 시점
  값만 인정하면 기록된 정산을 재검증할 수 없어 락이 풀리지 않았습니다. 정산 후
  상태도 함께 인정해 기록·재검증·소유자 판정 세 경로가 같은 결론을 냅니다.

## 1.17.0 - 재개 가능한 최초 설치 마법사와 커넥터 신원 가드

- `onboard.py`에 `start`, `next`, `resume`, `confirm`, `record-final-gate`를
  추가해 최초 설치를 중단·재개 가능한 상태 기계로 만들었습니다. 상태는
  `~/.codex/state/codex-web-gpt-automation/onboarding/state.json`에 저장되며
  암호·token·cookie·OAuth secret을 담지 않도록 저장 시점에 검사합니다.
- `next`는 완료 단계를 다시 실행하거나 다음 단계로 건너뛰지 않고 현재 단계
  하나만 반환합니다. 사용자 소유 단계의 `confirm`은 실제 증거로 재검증되며,
  증거가 없으면 `STAGE_CONFIRMATION_NOT_PROVEN_BY_EVIDENCE`로 거부합니다.
- 완료 표시를 프로그램 설치 완료, ChatGPT 연결 대기, 앱 등록 완료·검증 대기,
  전체 설치 및 실제 프로젝트 연결 검증 완료로 분리했습니다. `08_final_gate`는
  일반 비-Pro Oracle exact-root 읽기 증거를 함께 요구하고, exact allowed root가
  아닌 경로는 거부합니다.
- 앱 등록 단계에서 계정별 `플러그인`과 `앱` UI 경로를 모두 안내하고, 생성
  버튼이 없을 때의 확인 순서를 제공합니다. 요금제는 마지막 가설로만 다룹니다.
- 저장소 주소만 받은 에이전트를 위해 `docs/INSTALL_AGENT.md` 설치 계약을 추가하고
  `AGENTS.md`, README, 수명주기 설치 manifest에 연결했습니다.
- `start`, `next`, `resume`이 JSON 대신 읽기 쉬운 단계 요약을 출력합니다. 셸
  로케일에 따라 한국어와 영어를 자동 선택하고 `--lang`으로 고정할 수 있으며,
  기계 판독용 원본은 `--json`으로 얻습니다.
- 마법사 회귀 테스트가 늘어나 fast gate wall-clock 예산을 60초에서 100초로
  조정했습니다. 테스트 대상과 실패 판정 기준은 그대로입니다.
- 단계는 `06b_local_network_access`를 포함한 9개입니다.
- 진행 중인 유효한 상태에서 `start`를 다시 실행하면
  `ONBOARDING_ALREADY_STARTED`로 멈추고 `resume`을 안내합니다. 기존 진행 상태를 버릴
  때만 `start --reset`으로 새 상태를 기록합니다.
- `--lang`, `--json`은 모든 하위 명령 앞에서 받습니다. `next`와 `resume`은 명령 뒤에서도
  두 플래그를 받고, `confirm`은 명령 뒤에서 `--lang`만 받습니다.
- `confirm`은 앞선 단계가 미검증이면 `accepted: false`와
  `STAGE_OUT_OF_ORDER_EARLIER_STAGE_PENDING`, 막힌 단계 ID를 반환합니다.
- 여러 ChatGPT 플러그인이 `open_workspace`, `read` 같은 동일한 도구 이름을
  노출하면 `@앱이름` 멘션만으로는 커넥터가 고정되지 않았습니다. Oracle composer
  프롬프트에 `connector_identity_guard`를 추가해 등록된 앱의 도구만 사용하고,
  미션을 읽기 전에 어느 앱의 도구를 호출해 어떤 workspace id를 받았는지 한 줄로
  밝히도록 요구합니다.
- 첫 workspace 호출이 실패해도 자체 도구 배선을 조사하거나 웹을 검색하거나 다른
  커넥터로 대체하지 않고, 같은 루트를 한 번만 재시도한 뒤 구체적 blocker를 보고하고
  멈추도록 명시합니다.
- incident classifier에 `foreign-workspace-connector-substituted` 시그니처를
  추가했습니다. 플러그인 검색 흔적과 빈 결과 또는 workspace 미발급 흔적이 함께
  있을 때만 분류하며 기존 자기관찰·OAuth 503 시그니처가 우선합니다.
- `record-final-gate`는 `--root`, `--evidence`, 반복 가능한 `--listing`을 요구합니다.
  증거 요약이 너무 짧거나 목록이 없으면 `FINAL_GATE_EVIDENCE_INSUFFICIENT`로, 일반
  비-Pro Oracle 이외의 transport면
  `FINAL_GATE_TRANSPORT_MUST_BE_REGULAR_NON_PRO_ORACLE`로 거부합니다.
- 온보딩 상태 구조가 맞지 않으면 `ONBOARDING_STATE_CORRUPT`로 실패 폐쇄합니다.

## 1.16.1 - Strict Ultra 설치 문서 동기화

- `strict-ultra` 전역 skill이 참조하는 `docs/STRICT_ULTRA.md`를 수명주기
  설치 manifest에 포함하고 설치본 경로를 명확히 했습니다.

## 1.16.0 - Strict Ultra 감사와 안전한 DevSpace 파일 제거

- 기존 Oracle Multi v2 스케줄러를 그대로 사용하는 선택형
  `strict-ultra` comprehensive 프로필을 추가했습니다. dependency,
  authority, advisory Research Governor, identity ledger, local gate, 최종
  workflow audit가 닫힌 JSON keyset과 SHA-256으로 결속됩니다.
- strict Multi 결과가 실제 wave schedule, all-lanes barrier, audited apply,
  merger를 최상위 감사 자료로 노출합니다. 5개 lane/동시성 3은 안정적인
  3+2 wave로 기록됩니다.
- DevSpace 1.0.4 호환 패치에 일반 파일 전용 `delete_file`과 복구 가능한
  `trash_file`을 추가했습니다. 절대경로·경로이탈·reparse point·Git 및
  trash 내부 대상은 실패 폐쇄하며 trash 이동은 바이트 수와 SHA-256을
  재검증합니다.
- 신규 계약은 명시적으로 선택한 경우에만 적용되며 기존 standard,
  ultra-economy, ultra-gpt, legacy 경로는 그대로 유지됩니다.

## 1.15.12 - Luna Max CLI 및 Oracle 버전 해석 복구

- Local Multi-GPT 등록이 Codex Desktop 업데이트로 사라진 구버전 CLI를
  가리키면, exact server ownership을 확인한 뒤 최신 CLI로 원자 갱신합니다.
  setup과 runtime 모두 `gpt-5.6-luna` / `max` no-run 구성 canary를 통과해야
  하며, 지원하지 않는 CLI에서는 child나 job을 만들기 전에 실패 폐쇄합니다.
- pinned Oracle 0.17.1의 `npx --version`이 일시 실패해도 정확한 로컬 npx
  캐시 package version을 확인해 브라우저 생성 전 버전 해석을 복구합니다.
- exact `ORACLE_VERSION_FAILED` pre-submit 상태는 빈 stdout/output, 대화 URL
  부재, pinned command 및 lifecycle을 모두 검증한 경우에만 공식
  no-submission 정산 대상이 됩니다. 유사 오류와 모순 증거는 거부합니다.

## 1.15.11 - DevSpace restart pre-submit 공식 정산

- direct Oracle의 exact `DEVSPACE_SERVICE_RESTART_REQUIRED` 오류를 출력·대화
  URL·Oracle 실행이 모두 없는 bounded pre-submit host failure로 분류합니다.
- 기존 `settle-no-submission` 명령이 이 exact pre-submit 증거를 mission copy,
  stderr/transcript 및 locator 해시에 결속한 append-only receipt로 정산합니다.
  유사 오류, 출력 존재, URL 존재 또는 다른 lifecycle 상태는 계속 거부합니다.

## 1.15.10 - DevSpace 테스트 restart marker 격리

- DevSpace compatibility 테스트의 restart-marker state를 각 테스트의 격리된
  임시 디렉터리로 강제했습니다. synthetic package patch가
  `%USERPROFILE%\.codex\state\devspace-compat\1.0.4\restart-required.json`을
  남겨 이후 실제 Oracle run을 제출 전에 잘못 차단할 수 없습니다.

## 1.15.9 - Oracle 재귀 자기관찰 차단

- regular direct Oracle와 comprehensive stage prompt에 exact run ID/slug를
  결속한 no-self-observation/no-nested-Oracle guard를 추가했습니다. 웹 단계는
  자신의 Oracle state/output/transcript/recovery/observer를 읽거나 기다리지
  않고 요청된 미션을 직접 수행해야 합니다.
- terminal `BLOCKED`가 exact 자기 run ID와 slug, `running`, `pending`, output
  부재, `continue-observing-same-exact-session`을 모두 포함할 때만
  `post-submit-recursive-self-observation`으로 분류합니다. 일반 BLOCKED와 단순
  식별자 언급은 기존 분류를 유지합니다.
- comprehensive stage의 해당 결함은 자동 재시도 없이 terminal BLOCKED로
  종결하여 scope를 해제합니다. fresh run은 exact state/output/transcript 해시와
  명시적 사용자 권한을 append-only receipt로 결속한 뒤에만 허용됩니다.

## 1.15.8 - Ultra review FAIL 종결 수리

- hash-bound review receipt가 `FAIL`, `ready_for_next=false`, `next_stage=null`,
  비어 있지 않은 blocker와 유효한 critical finding 결속을 제공하면 workflow를
  `BLOCKED / REVIEW_FAILED`로 즉시 종결하고 comprehensive scope를 해제합니다.
- `PASS`와 `PASS_WITH_NOTES`만 계속 `web-multi`로 진행해야 합니다. 불완전하거나
  모순된 FAIL receipt는 계속 실패 폐쇄되며, 기존 Oracle run·output·receipt는
  수정하지 않습니다.
- terminal review 상태에는 receipt SHA-256과 critical finding 집합의 해시·개수만
  보존하여 다음 workflow가 이전 의미 내용을 상속하지 않고도 정산을 감사할 수
  있습니다.

## 1.15.7 - DevSpace read bridge 사전검증 수리

- DevSpace의 50KB 초과 단일행 `read_chunk` 사전검증이 전체 MCP 서버
  모듈 그래프 import에서 멈추던 문제를 수정했습니다. 설치된
  `server.js`에서 해시 게이트된 정확한 함수 본문만 분리해 최소 Node
  프로세스에서 검증하므로 Oracle 제출 전 버전 판정이 timeout으로
  오인 실패하지 않습니다.
- 정확히 결속된 `pre_submit` bridge-timeout run은 명시적
  `user-confirmed-pre-submit-workflow-cancel` 권한으로 workflow를
  `CANCELED`로 정산하고 scope를 해제할 수 있습니다. stdout/output/
  conversation 흔적이나 다른 오류는 계속 fail-closed이며 Oracle run state는
  변경하지 않습니다.
- 동일한 정산 계약은 패치 적용 후 서비스 재시작이 필요하다는
  정확한 `DEVSPACE_SERVICE_RESTART_REQUIRED` pre-submit 오류도 구분하여
  결속합니다. 서비스 재시작은 별도 managed setup 절차로 수행하며
  정산 명령은 서비스나 prompt를 조작하지 않습니다.

## 1.15.6 - comprehensive 사용자 중지 정산

- 사용자가 provider UI에서 응답을 명시적으로 중지하고 workflow 종료를
  요청한 경우, terminal-harvested Oracle run과 exact workflow/scope/run state의
  사전 SHA-256을 요구하는 공식 `--cancel-user-stopped` 경로를 추가했습니다.
- 정산은 Oracle run state를 수정하거나 새 prompt/recovery를 만들지 않습니다.
  user authority receipt, `CANCELED` workflow, released scope, completion receipt를
  원자 기록하며 중단 후 재실행은 동일 결속에서만 idempotent하게 마무리합니다.
- scope는 `canceled`를 terminal 상태로 인정해 새 workflow가 같은 exact scope를
  청구할 수 있지만, 기존 canceled workflow 자체는 다시 활성화하지 않습니다.

## 1.15.5 - 읽기 전용 웹 표면용 Ultra host bridge

- regular comprehensive planner/reviewer가 DevSpace의 변경 도구를 받지 못해도
  hash-bound stage envelope를 반환하면 host가 workflow 소유 output, next mission,
  receipt를 동일 계약으로 materialize합니다.
- Ultra GPT strict writer는 직접 쓰기 대신 parent/lane/source-mission에 결속된
  닫힌 writeset을 반환할 수 있습니다. host는 격리 worktree의 선언된
  `owned_paths`에만 원자 적용하고 file/byte/symlink/reparse/Git delta 경계를
  검증하며 직접 delta와 writeset 혼용을 거부합니다.
- DevSpace `read_chunk`를 추가해 50KB를 넘는 단일 UTF-8 행도 24KiB 이하의
  연속 byte chunk, 전체 파일 SHA-256, EOF 결속으로 shell 없이 완전 복원합니다.
- write/edit/bash의 MCP 안전 annotation은 완화하거나 읽기 전용으로 위장하지
  않습니다.

## 1.15.4 - Oracle 0.17.1 exact-session live recovery 보강

- Oracle 0.17.1의 복구된 Pro 대화 준비 대기를 고정 60초가 아니라 host가 전달한
  `ORACLE_LIVE_TERMINAL_TIMEOUT_MS` 전체 기한까지 유지합니다.
- 느린 대화 로딩이나 장기 tool-result 대기 중 같은 exact slug/profile/tab을 보존하고,
  readiness timeout 때문에 recovery browser를 반복 생성하지 않습니다.
- published Oracle 0.17.1 byte hash, patch 결과 hash, Node 구문을 회귀 테스트로
  fail-closed 검증합니다.

## 1.15.3 - Chrome Local Network Access 최초 설치 보강

- Windows 최초 설치에서 `chatgpt.com` 정확한 origin만 Chrome의 공식
  `LocalNetworkAccessAllowedForUrls` 사용자 정책에 추가하는 receipt-backed
  helper를 제공합니다. 기존 정책 항목은 덮어쓰지 않습니다. 정책 ACL이 쓰기를
  거부하면 traceback 대신 정확한 수동 seed-profile 1회 허용 절차로 전환합니다.
- 온보딩 상태는 영속 정책 또는 전용 Oracle seed profile의 실제 Local network
  허용을 확인하며, 로그인만 된 상태를 더 이상 준비 완료로 오판하지 않습니다.
  macOS 등 비-Windows 환경은 전용 seed profile에서 한 번 직접 허용한 뒤 Chrome을
  완전히 종료하도록 안내합니다.

## 1.15.2 - 터미널 복구 후 observer 자동 정리

- exact-slug recovery가 durable output과 terminal authority를 확정하면 원래
  runner가 자신이 시작한 Oracle 프로세스 트리만 즉시 종료해 프로젝트 submit
  mutex를 반환합니다. 새 prompt나 replacement run은 만들지 않습니다.
- recovery가 먼저 끝난 뒤 80분 caution audit가 도착하더라도 이미 확정된
  `complete / terminal / terminal_harvested` 상태를 `running`으로 되돌리지 않도록
  단조성 회귀 검사를 추가했습니다. 불완전하거나 모순된 상태에는 자동 정리가
  작동하지 않습니다.

## 1.15.1 - 모델 선택 전 미제출 정산 결속

- Oracle 0.17.1이 ChatGPT 홈에서 모델 선택 버튼을 찾지 못해 prompt 전송 전에
  종료된 qualified Pro run을 사용자 확인 정산 후보로 추가했습니다. 정확한
  selector 오류만으로는 해제하지 않으며 exact slug, 버전, copied profile,
  `execute-browser` stage, `promptSubmitted=false`, 홈 URL, output·대화 URL 부재,
  recovery-binding 불가 증거와 모든 관련 SHA-256이 함께 일치해야 합니다.
- prompt 제출, 대화 URL, output, 다른 오류·stage, 누락되거나 변경된 Oracle
  session ledger 중 하나라도 있으면 기존 잠금이 유지됩니다. 정산 receipt가
  만들어진 뒤 meta가 바뀌어도 재검증이 실패해 해당 run이 다시 unresolved로
  취급됩니다.

## 1.15.0 - 울트라 GPT 모드

- setup 문서와 실제 기본 runner app name의 불일치를 제거해 기본값을 수동
  등록 권장 이름인 `codex`로 통일했습니다. 명시적 host override와 기존 custom
  app name은 계속 지원합니다.
- Codex Ultra/Multi-agent의 bounded role 분해를 독립 Oracle 웹 GPT 세션으로
  치환하는 선택형 `ultra-gpt` comprehensive 프로필을 추가했습니다. 로컬
  Codex는 native semantic subagent를 만들지 않고 exact root, mission/receipt
  해시, session lifecycle, 결정론적 gate, Git/CI/Release만 관리합니다.
- regular web planner와 별도 reviewer가 계획을 확정하고, reviewer가 2~5개의
  병렬 `worktree-write` Web Multi 구현 lane으로 분할합니다. 동시 실행은 최대
  3개이며 각 lane은 같은 Git HEAD의 별도 사전 생성 worktree에서 실행됩니다.
  host는 project-relative `owned_paths`의 동일·상위·하위 겹침과 실제 범위 밖
  변경, Git metadata 변경을 거부합니다. 모든 lane이 통과한 뒤에만 canonical
  결과에 적용하고 merger와 별도 final verifier가 순차 검증합니다.
- Pro는 프로필 내부에서 선택할 수 없습니다. 사용자가 별도로 명시 승인하고
  설계 불확실성이 실제로 있을 때만 workflow 전 사전 자문 1회를 허용합니다.
  initial stage, stage budget, Web Multi 전환, lane access와 concurrency를 모두
  제출 전 실패 폐쇄합니다.

## 1.14.7 - Oracle stale observer exact recovery

- CDP가 끊긴 원래 Oracle observer가 프로젝트 submission mutex를 계속 보유해도
  동일 run/slug의 prompt 없는 `live`/`harvest` 복구는 exact-run 전용 mutex로
  직렬화됩니다. unresolved run 상태가 새 제출을 계속 차단하므로 중복 prompt를
  허용하지 않으면서 provider-terminal 응답을 안전하게 수확할 수 있습니다.
- exact recovery가 프로젝트 mutex를 다시 기다리지 않는 회귀 테스트와 동시
  recovery writer 직렬화 경계를 추가했습니다. 기존 terminal authority의 단조성,
  exact URL/slug 결속, output/transcript 원자 저장 규칙은 그대로 유지합니다.
  늦게 종료된 원래 observer도 이미 수확된 terminal state를 덮어쓰지 못합니다.

## 1.14.6 - DevSpace OAuth 장시간 세션 안정화

- DevSpace 1.0.4의 회전형 refresh token을 여러 도구 호출이 동시에 갱신할
  때 한 요청만 성공하고 나머지가 `OAuth token request failed 503`으로
  끊기는 경쟁을 보완했습니다. 이미 소비된 token을 영구 재허용하지 않고,
  동일 client·scope·resource 요청에만 30초 동안 같은 회전 결과를 최대 32개
  메모리에서 재생합니다. 만료·불일치·revoke는 계속 fail-closed입니다.
- 호환 패치는 upstream 버전과 pristine/patched SHA-256으로 고정되며, 실제
  DevSpace 모듈과 격리 SQLite DB를 사용하는 무네트워크 replay/revoke/expiry
  검사까지 통과해야 Oracle 실행 전 호환성 확인이 성공합니다.
- 장애 복구 뒤에는 config·OAuth DB·ChatGPT 앱 설정을 변경하지 않고 관리
  서비스만 한 번 재기동한 뒤 regular non-Pro canary로 exact root의 읽기와
  no-op 명령 반환을 검증합니다. Pro는 이 canary가 성공하기 전까지 막습니다.

## 1.14.5 - DevSpace exact-root 응답 경로 보강

- Oracle의 regular·Pro DevSpace composer가 미션 경로보다 먼저 exact project
  root를 명시해 checkout으로 열도록 변경했습니다. 미션 디렉터리, 상위·하위
  폴더, 현재 활성 workspace를 exact root 대신 선택하지 못하게 해 미션과 명령
  결과가 다른 workspace session으로 분리되는 재발을 줄입니다.
- DevSpace 장애 복구 뒤 첫 검증은 계속 quota 없는 regular Oracle canary로
  수행합니다. 로컬/public doctor만으로 registered ChatGPT app 응답 성공을
  주장하거나 Pro 제출을 연결 확인용으로 소비하지 않습니다.

## 1.14.4 - Oracle 미제출 정산 잠금 호환성

- 버전 파일만 갱신하고 GitHub Release를 빠뜨리는 일을 막기 위해 annotated
  `v*` 태그 push 시 버전·태그 형식을 검증하고 Release를 자동 발행합니다.
  유지보수 스킬은 exact CI, peeled remote tag, `releases/latest`, 설치 영수증과
  source/install parity를 모두 확인하기 전에는 발행 완료로 보고하지 않습니다.
- 사용자 확인으로 정산된 Pro attachment run은 정산 뒤 프로젝트의 비미션
  첨부파일이 정상 변경돼도 당시 state와 해시 영수증에 결속된 원본 identity를
  유지합니다. 미션·운송 사본·로그·복구 증거·출력·대화 URL·영수증이 달라지면
  기존처럼 fail-closed로 잠금을 유지합니다.
- pre-submit run의 exact Oracle session 부재는 복구 로그 바이트와 locator를
  재검증한 경우에만 진단·incident packet에서 명시적으로 분류합니다. 검증된
  미제출 run만 fresh run 안전 판정에 참여합니다.

## 1.14.3 - GitHub 요청과 복구 경계 정비

- 설치 오류 뒤 정상 rollback이 완료되면 WAL을 terminal 상태로 기록하고,
  이미 backup 바이트로 복원된 항목은 재실행 때 멱등적으로 인정합니다. 실제
  외부 수정이나 누락 파일은 계속 fail-closed로 차단합니다.
- exact Oracle recovery가 provider terminal 결과를 수확했는데 로컬 observer만
  `running`으로 남은 경우 동일 run의 terminal 증거로 정합화합니다. OAuth 503과
  stale observer는 doctor가 별도 원인으로 분류합니다.
- macOS Funnel LaunchAgent에 Homebrew 우선 PATH를 명시해 headless Tailscale
  CLI를 안정적으로 선택하고 App Store GUI 번들 경로를 서비스 탐색에서 배제합니다.
  이 수정은 PR #13의 핵심 제안을 현행 main에 맞춰 반영한 것입니다.
- GitHub 메인 화면의 깨진 release badge를 tag 기반 badge로 교체하고, 첫 설치·
  진단·기여 경로를 README 상단에서 바로 찾을 수 있게 재정렬했습니다. CI는
  수동 `workflow_dispatch` 실행도 지원합니다.

## 1.14.2 - DevSpace 상주 복구

- Windows DevSpace 부트스트랩을 로그인 시 한 번 실행하고 종료하는 방식에서
  5분 간격의 숨김 per-user 감시 방식으로 변경했습니다. DevSpace 프로세스가
  로그인 이후 종료돼도 현재 `~/.devspace/config.json`의 전체 root와 정확한
  Tailscale Funnel을 자동 복구합니다.
- `setup --apply`가 감시 명령을 등록하고 즉시 시작합니다. Owner 암호, OAuth
  클라이언트·refresh token, ChatGPT 설정은 변경하거나 기록하지 않습니다.
- 설치 manifest의 새 Pro transport 표기를 실제 정책과 같은 명시적
  `pro-devspace` 읽기·쓰기 계약으로 정정했습니다. 기존 read-only run의 복구
  의미는 그대로 유지합니다.

## 1.14.1 - Pro 첨부 무전송 정산

- Oracle 0.17.1이 프롬프트 전송 전에 정확한 첨부 업로드 타임아웃을
  보고한 경우에만 사용할 수 있는 fail-closed 사용자 확인 정산 경로를
  추가했습니다.
- 정산 영수증은 run/project, 원본·운송 mission 해시, 모든 첨부파일의
  경로·크기·SHA-256, Oracle 버전·exact locator, 업로드 타임아웃 marker,
  stdout/transcript, recovery 바이트와 출력·대화 URL 부재를 결속합니다.
- 사용자 확인 token 누락, 첨부 변경, 출력·URL·live recovery, 미지원 Oracle
  버전, locator 불일치 또는 다른 오류가 하나라도 있으면 잠금을 유지하고
  replacement 제출을 금지합니다.

## 1.14.0 - 명시적 Pro 읽기·쓰기 정책

- 일반 웹 작업은 `gpt-5.6`의 최고 지원 비-Pro 추론 강도 `extra-high`를
  기본으로 사용하며 Pro로 자동 승격하지 않습니다.
- Pro는 사용자의 명시 요청에만 선택됩니다. 표준 종합 워크플로는
  `allow_pro: true`가 없으면 plan의 Pro 전환을 제출 전에 차단합니다.
- 새 qualified Pro 실행은 `pro-devspace` transport를 사용하며 exact root
  안에서 미션이 허용한 파일 쓰기와 명령 실행을 지원합니다. 기존
  `pro-devspace-readonly` 실행 기록은 복구 호환용 의미를 그대로 보존합니다.
- README, 전역 정책, 라우팅·아키텍처·설치 문서와 Pro 관련 스킬을 같은
  explicit-only/read-write 계약으로 재정렬했습니다.

## 1.13.1 - Oracle 장기 실행 상태 점검 안전성

- 80분을 종료·실패·소유권 해제 시점이 아닌 caution/status-audit 임계값으로
  정정했습니다. 동일 프로세스의 생존과 출력 진행을 기록한 뒤 계속 기다립니다.
- 브라우저 관찰 프로세스가 응답 타임아웃으로 반환해도 동일 exact slug의 live
  회수를 자동으로 이어가며, 시간만으로 새 제출이나 replacement를 만들지 않습니다.
- 종합 모드와 legacy canary에도 같은 no-time-based-termination 계약을 적용했습니다.

## 1.13.0 - 첫 설치와 DevSpace 진단 완결

- 기존 DevSpace 설정의 root 병합, Windows 재부팅 root 영속성, Unicode root의
  PowerShell 5.1 안전 직렬화를 하나의 source-of-truth 계약으로 통합했습니다.
- 첫 `devspace init`을 현재 터미널에 표시하고, 생성 Owner 암호 유지 또는 강한
  custom 암호 선택을 TTY 전용·숨김 입력으로 안내합니다.
- Funnel public endpoint에 bounded propagation retry를 추가하고 마지막 redacted
  probe를 오류에 포함합니다.
- Tailscale status JSON은 Windows ANSI locale과 무관하게 UTF-8로 읽어 Unicode
  장치명이 있어도 setup doctor가 중단되지 않습니다.
- DevSpace 시작과 lifecycle doctor가 active Node에서 `better-sqlite3` 메모리 DB를
  실제로 열어 npm 12 install-script 차단을 사전에 발견합니다.
- onboarding plan/status/configure가 기본 `codex`뿐 아니라 검증된 임의 ChatGPT
  app name을 일관되게 지원합니다.
- 초절약모드는 새 Codex 작업의 최초 요청에서만 Luna/Max 선택을 한 번 안내하고,
  사용자 확인 뒤에는 런타임 모델을 읽거나 작업 중간에 다시 묻지 않습니다.

## 1.12.1 - Oracle 사전제출 CDP 복구

- Oracle 0.17.1의 정확한 CDP 연결 해제 오류와 외부 session ledger의
  `promptSubmitted=false`가 함께 증명될 때만 qualified Pro run을
  `pre_submit / not_executed`로 안전 정산합니다.
- 출력, 대화 URL, 제출 플래그, 모델·프로필·버전 또는 오류 형태가
  조금이라도 모순되면 기존 `submitted_unknown` 잠금을 유지합니다.
- exact-slug recovery가 이 증거를 감지하면 Oracle을 다시 호출하지 않고
  프로젝트 소유권을 해제하는 standalone Pro 회귀 테스트를 추가했습니다.
- 기존 DevSpace 설정은 백업 후 전체 `allowedRoots`를 원자적으로 병합하며,
  bootstrap JSON은 진단용 mirror로만 동기화합니다.
- Windows 로그인 복구 wrapper는 매 실행마다 live
  `%USERPROFILE%\.devspace\config.json`에서 root를 읽으므로, 재부팅 시 오래된
  bootstrap 배열이 새 프로젝트를 제거하지 않습니다.
- Unicode root가 있는 설정은 ASCII-safe JSON escape로 원자 저장해, BOM 없는
  UTF-8을 ANSI로 읽는 Windows PowerShell 5.1 기본 `Get-Content`에서도 손상
  없이 파싱됩니다.

## 1.12.0 - 브랜드와 릴리스 체계

- 포털·코드 괄호·연결 노드를 결합한 프로젝트 로고, README 배너, GitHub
  소셜 프리뷰와 사용 규칙을 추가했습니다.
- 한국어·영어 README를 동일한 정보 구조로 재작성하고 최초 설치, 모드 선택,
  안전 계약과 문서 지도를 한 화면에서 찾을 수 있게 정리했습니다.
- 현행 아키텍처, 문서 인덱스, 기여 가이드, 브랜드 가이드와 SemVer 정책을
  추가하고 legacy 문서를 현재 실행 경로와 명확히 분리했습니다.
- GitHub 이슈·기능 제안·Pull Request 템플릿과 저장소 주제/설명을 정비했습니다.
- `package.json`, `package-lock.json`, `install-manifest.json`, Git 태그와 GitHub
  Release가 하나의 버전을 가리키는 릴리스 계약을 도입했습니다.

## 1.11.3 - standalone Pro 전송 불확실성 정산

- Oracle 0.17.1의 정확한 prompt-not-observed 오류와 no-live-tab/no-URL
  harvest가 함께 있을 때 standalone qualified Pro도 사용자 확인 기반의
  `settle-no-submission` 정산을 사용할 수 있습니다.
- 출력, 대화 URL, 상충 recovery 상태, 다른 Oracle 버전, 다른 transport,
  변경된 미션 바이트가 있으면 프로젝트 잠금을 계속 유지합니다.

## 1.11.2 - stale Funnel 등록 후 복구

- `post-register`가 로컬 status상 동일한 매핑이라도 외부 relay에서 닫힌
  exclusive HTTPS 슬롯을 scoped `off` 후 동일 target으로 다시 수립합니다.
- 전체 `tailscale funnel reset`은 사용하지 않으며, 같은 포트에 다른 path
  handler가 있으면 이를 보존하고 비파괴 확인만 수행합니다.

## 1.11.1 - 드라이브 루트 위생 정책

- 전역 AGENTS 정책에서 테스트·임시·로그·다운로드·dependency checkout을
  `C:\` 또는 `D:\` 바로 아래에 만들지 못하게 했습니다.
- 기본 임시 위치는 OS temp의 task별 Codex 하위 폴더이며, 짧은 경로가 꼭
  필요하면 저장소의 gitignored `.codex-tmp`를 사용합니다. 외부 소스 checkout은
  `%LOCALAPPDATA%\Codex\Sources`에 둡니다.
- 기존 루트 정리는 소유권과 실행 참조를 먼저 확인하고, 확실한 자동화 산출물만
  복구 가능한 archive로 이동하도록 명시했습니다.

## 1.11.0 - 격리된 macOS Cloudflare DevSpace 터널

- Tailscale Funnel이 OpenAI 연결 제한을 넘는 환경을 위해 별도 Named Tunnel과
  전용 LaunchAgent를 추가했습니다. 기존 Cloudflare 터널과 `com.openclaw.*`
  서비스를 재사용하거나 수정하지 않습니다.
- 설치·재시작 실패 시 기존 관리 파일과 서비스를 복구하고, doctor는 macOS에서
  실제 loaded 상태까지 검사하며, exact managed artifact만 제거하는 uninstall을
  제공합니다.

## 1.10.0 - 초절약모드

- 로컬 지휘관과 모든 네이티브 서브에이전트를 `gpt-5.6-luna` / `max`로
  제한하고, Pro 설계와 regular 웹 검토·구현·최종 검증을 분리하는 선택형
  `ultra-economy` comprehensive 프로필을 추가했습니다.
- 최초 구현은 task-bound rollout runtime evidence로 Luna Max를 검증했으나,
  1.13.0부터는 화면·런타임 판독 오류를 피하기 위해 새 작업 최초 1회 사용자
  안내·확인 계약으로 대체했습니다. 전역 `config.toml`은 자동 변경하지 않습니다.
- Pro-first와 최소 4단계 계약은 코드와 회귀 테스트로 fail-closed 고정했습니다.

## 1.9.1 - ChatGPT 앱 등록 후 연결 안정화

- 수동 ChatGPT 앱 등록·재연결 직후 기존 DevSpace 설정, Owner 자격, OAuth DB,
  허용 루트와 Funnel 주소를 보존하면서 관리 서비스를 한 번 재순환하는 명시적
  `post-register` 단계를 추가했습니다.
- 실제 등록 앱 검증은 일반(non-Pro) Oracle `@codex` 읽기 검사로 분리했습니다.
  Codex Desktop의 동명 DevSpace 플러그인은 다른 연결이므로 등록 검증에 사용하지
  않고, Pro 세션을 최초 연결 검사로 소비하지 않습니다.
- public endpoint가 정상인 상태의 앱 호출 실패가 무조건 재등록을 요구하지 않고,
  한 번의 post-register 복구 후 외부 앱 경계를 보고하도록 진단 안내를 수정했습니다.

## 1.9.0 - 선택형 Local Multi-GPT

- 첫 대화형 설치에서 `Local Multi-GPT도 설치할까요? [y/N]`를 묻고 기본값은
  아니오로 둡니다. 무인 설치는 `-EnableLocalMultiGpt` 또는
  `--enable-local-multi-gpt`를 명시해야 합니다.
- 선택하면 스킬, 서버, `multi_gpt` MCP 등록을 한 구성요소로 설치하고 하위
  단계가 사용할 호환 Codex CLI 경로를 영수증에 기록합니다.
- Multi-GPT는 PATH의 오래된 CLI보다 등록 시 검증한 Codex CLI를 우선하며,
  Planner 실패 시 stderr 진단을 보존합니다.

README는 현재 제품의 목적과 사용법만 설명합니다. 구현 변경, 호환 패치,
레거시 이전 기록은 이 문서에서 관리합니다.

## 1.8.0 — Codex Web GPT Automation

- 공개 제품명과 저장소명을 Pro 전용으로 오해되지 않는
  `Codex Web GPT Automation` / `codex-web-gpt-automation`으로 변경했습니다.
  기존 `codexpro-*` 상태, 영수증, 스키마와 복구 파일은 하위 호환 ID로
  유지합니다.
- 설치부터 고정 HTTPS endpoint, DevSpace Owner 승인, 재부팅 복구, Oracle
  전용 브라우저 로그인, ChatGPT 앱 `codex` 등록까지 순서가 고정된 최초 설치
  가이드와 fail-closed onboarding 점검기를 추가했습니다.
- Tailscale Funnel을 자동화·재부팅 검증 경로로 유지하면서 Cloudflare named
  tunnel, ngrok 고정 도메인, custom HTTPS proxy의 안전한 합류 지점을
  문서화했습니다. 임시 URL은 완료 상태로 인정하지 않습니다.
- Oracle 0.17.1 manual-login profile 미초기화가 제출 전에 발생한 경우의 안전한
  잠금 정산과, `TASK_OUTCOME` 뒤의 제한된 Markdown reference footer 분류를
  회귀 테스트로 고정했습니다.

## 1.7.0 — macOS Ultrawork

- macOS arm64에서 공통 Python `install/update/doctor/rollback/uninstall` lifecycle과
  영수증/WAL/충돌 보존을 지원합니다. PowerShell 진입점은 Windows 호환 경로로
  유지합니다.
- OMO Codex Light, 로컬 CodexPro hook marketplace, GJC식 brownfield 인터뷰와
  합산 동시 실행 상한 5를 추가했습니다.
- `RUNNING → CHECKPOINT_DUE(75분) → HANDOFF_PENDING(80분)` 상태 머신과
  exact Oracle 회수, 동일 Codex session resume, launchd 감독기를 추가했습니다.
- DevSpace 1.0.4를 macOS에서 직접 실행하고 MagicDNS 자동 탐지 및 Tailscale
  Funnel `443 → 127.0.0.1:7676` 복구 경로를 추가했습니다. Funnel 엣지가
  OpenAI 연결 제한을 넘길 때 사용할 격리된 Cloudflare Named Tunnel
  LaunchAgent도 제공합니다.
- GitHub Actions는 `windows-latest`와 `macos-14`를 모두 검증합니다.

### Oracle + DevSpace 단일 실행 경로

- 일반 GPT, 계획, 검토, 수정, 지휘, 심층 리서치, 종합모드와 Web
  Multi-GPT를 Oracle + DevSpace로 통일했습니다.
- Pro는 기본적으로 Oracle + 읽기 전용 DevSpace를 사용하며, 명시적인
  `pro-attachment`만 고정 외부 증거에 사용합니다.
- CodexPro와 agbrowse 신규 제출 경로는 동결했습니다.

### Windows 브라우저 실행 격리

- 실행마다 로그인 프로필의 throwaway 복사본을 사용합니다.
- Windows에서는 Node 내장 복사로 프로필을 만들며 rsync를 요구하지 않습니다.
- 각 Oracle 실행이 소유한 숨김 Chrome만 정리합니다.

### 장기 작업과 복구

- 웹 작업은 기본 70분 이내 episode로 분할합니다.
- 75분에는 새 fan-out을 막고 80분에는 durable handoff와 정확한 owner 상태를
  평가합니다.
- CDP 호출이 멈춰도 host watchdog이 30초 grace 뒤 동일 세션을 보존한 채
  `attention_required`로 반환합니다.
- 제출 후 로컬 종료·브라우저 연결 끊김은 `attention_required`로 보존합니다.
- 복구는 저장된 정확한 slug와 대화 URL만 사용하고 새 질문을 보내지 않습니다.
- terminal 상태는 이후 관찰에서 live로 되돌아가지 않습니다.

### 종합모드

- plan → optional Pro/Web Multi → review → implementation → final web gate
  → local deterministic gate 순서를 사용합니다.
- 각 단계는 다음 미션과 workflow/stage/attempt/input-SHA 결합 영수증을
  직접 작성합니다.
- review 단계가 수정 가능한 계획 결함을 직접 고치고 구현 미션을 확정합니다.
- Pro 증거 파일은 `[PRO_ATTACHMENT_CONTRACT]`에 선언된 파일만 첨부합니다.
- 손상된 Pro JSON은 신원이 정확히 일치하는 제한된 경우에만 감사 기록과
  함께 복구합니다.

### Web Multi-GPT

- 독립 Oracle solver 2~25개를 최대 5개씩 wave로 실행합니다.
- Windows lane마다 별도 프로필을 사용합니다.
- 각 solver는 짧은 handoff 파일을 만들고 merger 하나가 안정된 순서로
  결과를 병합합니다.

### 설치와 릴리스

- 설치 전 파일을 백업하고 durable 영수증을 남깁니다.
- 기본 설치는 동결된 agbrowse/CodexPro 의존성을 설치하거나 갱신하지 않습니다.
- portability, fast gate, golden-path, v3/v4 계약 테스트를 Windows와 macOS
  CI에서 실행합니다.

## 레거시 기록

과거 CodexPro·agbrowse 기반 v1~v4 실행기와 goal supervisor는 새 작업을
만들 수 없습니다. 이미 저장된 실행을 원래 신원으로 복구할 때만 사용합니다.
자세한 목록은 [FROZEN_LEGACY.md](FROZEN_LEGACY.md)에 있습니다.

세부 커밋 단위 변경은 Git 로그와 GitHub Releases/Actions를 권위 기록으로
사용합니다.

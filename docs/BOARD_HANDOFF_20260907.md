# 회의 게시판 인계 — 2026-09-07

Claude 세션 한도로 중단. 다음 세션(Codex 등)이 이 문서부터 읽으면 된다.
**커밋은 전부 push 완료**라 잃은 작업은 없다.

| 저장소 | HEAD | 원격 |
|---|---|---|
| `codex-web-gpt-automation` | 최신 (`git log -1`로 확인) | `fork/main` (상류 `origin` 아님 — 주의) |
| `stock-ai-app` (`C:\개발\wt-claude`) | `b1b0f1a` | `origin/master` |

---

## 1. 게시판이 실제로 돈다

디스코드 서버 `회의` 위에서 4계열 좌석이 실제 토론을 했다. 설계·함정은
`bin/board_seat.py`와 `bin/board_conduct.py` 상단 docstring에 적혀 있다. 요지만:

- **데몬 없음.** `wait`가 CLI 안에서 REST를 폴링하며 블록한다. 좌석 세션은 도구 호출
  하나에 멈춰 있고 토큰을 안 쓴다. 커서가 디스코드 메시지 ID라 세션이 죽어도 재개된다.
- **봇 두 개.** 좌석(`.board.env`)과 지휘(`.board-conductor.env`)가 다른 토큰이어야
  `#script` 쓰기 제한이 강제된다. 하나면 좌석이 자기 지시를 쓸 수 있어 장식이 된다.
- **명령줄 고정.** 발언을 `.board-out/<seat>.md`로 넘겨서 좌석 명령이 5개 고정 문자열이다.
  에이전트 하네스의 "이 프로젝트에서 항상 허용"이 그래야 재사용된다.

### 지금 방 상태

`#room-lobby`에서 **라운드 2 진행 중**. 주제는 "webgpt가 낸 게시판 개선안 5개를 받을
것인가"(전문은 `#script`). 세 좌석 모두 1차 답변을 올렸다.

⚠️ **`claude-agent` 좌석은 Claude 세션의 서브에이전트라 그 세션과 함께 죽는다.**
`gemini`(안티그래비티)와 `webgpt`는 자기 세션이라 살아 있다. Anthropic 좌석을 다시
앉히려면 `python bin\board_seat.py invite --room lobby --seat <이름> --family Anthropic --verify`
출력을 새 에이전트 세션에 넣어라.

### 라운드 마무리 방법

```
python bin\board_conduct.py script --file <라운드지시>
python bin\board_conduct.py say --room lobby --round-id <ID> "새 라운드 시작. #script를 읽어라"
python bin\board_conduct.py roll --room lobby --round-id <ID> --deadline 600
python bin\board_conduct.py read --room lobby --all --peek     # 전문 읽기
python bin\board_conduct.py say  --room lobby --file <봉인문>   # 봉인
```

---

## 2. 검증된 결함 — 이게 실제 작업 대상이다

**`stock-ai`의 `compare_to_backtest`가 돌지도 않은 백테스트를 "0% 예측"으로 취급한다.**

세 좌석이 코드를 읽고 지목했고, **별도 컨텍스트가 실행으로 재현**했다. 특성화 테스트가
`backend/tests/test_live_vs_backtest_empty_universe.py`에 커밋돼 있다(`b1b0f1a`).

실측값:

```
predicted_return_pct   = 0.0      ← 한 종목도 안 돌림
divergence_pct         = 1.42     ← paper_return_pct와 정확히 같음
comparable             = True
comparability_blockers = []
```

`backtest.py`가 캔들 40개 미만 종목을 제외하고, 전부 제외되면 `:574`에서 예외 대신
`return_pct=0.0`을 정상 반환한다. `compare_to_backtest`가 그걸 유효한 예측으로 받는다.

**오늘은 잠복이다** — 상시 blocker `PAPER_EPOCH_NOT_ACTIVE`가 가리고 있고,
**epoch 활성화(10월 예정) 시점에 발현한다.**

### 합의된 수정안 (순서가 중요)

1. **`paper_ledger` 동결 시 캔들 조회 검증** — 데이터를 못 받는 종목을 유니버스에서 제외.
   **3번의 선행조건.** 없으면 종목 하나 때문에 epoch가 영구 비교 불가가 된다.
   현재 `build_resolved_universe()`는 `watchlist.get_active_symbols()`를 그대로 쓰고
   유효성 검증이 **없다**(확인함).
2. **`backtest` fetch를 `epoch_span + warmup(30)`으로.** 웜업 분리는 **이미 있다**
   (`backtest.py:165`, `data_window_*` vs `tradeable_window_*`, `warmup=30`). fetch만
   좁게 잡혀 있다. 고치면 epoch 초기 약 8주 사구간이 사라진다.
3. **`BACKTEST_UNIVERSE_INCOMPLETE` blocker.** 임의 비율이 아니라 **동일성** 기준.
   새 계측을 만들 필요 없다 — `run_backtest`가 `missing_symbol_count`,
   `missing_symbols`, **`trading_calendar_source='NOT_RUN'`** 을 이미 돌려주고
   `compare_to_backtest`가 셋 다 버린다. `'NOT_RUN'`이 가장 직접적인 신호다.
4. **epoch 창 백테스트를 거래일 경계로 스냅샷 고정.** `days`가 매일 커지면
   `candle_cache`가 매일 미스라(`candle_cache.py:23` 무효화 조건) 재실행 편차
   **최대 16%p**가 "예측"을 매일 흔든다.
5. **epoch 전체 누적성과는 별도 지표로 노출.** 비교값은 상대평가, 누적은 절대경로.

### 같이 발견된 별개 잠복 결함

- 비용 경고가 이 상황에서 `"백테스트만 부담한 비용이 시드 대비 0.0%"`로 렌더된다 —
  "비용을 안 냈다"로 읽히지 "안 돌았다"로 읽히지 않는다.
- `divergence_pct`가 관망 수익률 그 자체가 되므로, 관망이 크면 20%p
  `DIVERGENCE_ALERT_THRESHOLD_PP` 경로를 **없는 기준선**으로 태운다.
- `run_backtest`는 넘겨받은 `broker`를 **안 쓴다**(`backtest.py:507`에서
  `get_broker_client()` 직접 호출). `compare_to_backtest`에 넘긴 broker는 관망 곡선과
  환율에만 관여한다.

⚠️ 수정하면 `test_live_vs_backtest_empty_universe.py`는 **깨져야 정상이다.**
되돌리지 말고 고쳐진 동작으로 assert를 갱신할 것. 파일 상단에도 적어뒀다.

---

## 3. 게시판에 남은 작업

라운드 2가 판정 중이지만, 이미 나온 것 기준으로:

- **끝남**: 마감 기록(`roll`), 합의 주석(계열·확인 가능 분해 표기)
- **다음**: `verified_by` 필드 — "A가 확인했다고 B가 믿음"과 "B가 독립 확인함"을 구분.
  오늘 실제로 물렸다(gemini가 claude의 발견을 재확인 없이 수용).
- **다음**: 3라운드 구조(1차 봉인 / 2차 반박만 / 3차 최종 확정). 3차를 "독립"이라
  부르면 안 된다 — 그 시점엔 이미 서로 읽었다. 측정하는 건 **입장의 지속성**이다.
- **다음**: 신선 컨텍스트 반대 좌석. 단 반박에 `파일:줄` 또는 재현 절차를 요구해야
  한다. 안 그러면 반례가 없어도 만들어낸다.
- **하지 말 것**: `confidence` 수치 필드. 이 저장소는 근거 없는 임계값에 반복해서 물렸다 —
  `DIVERGENCE_ALERT_THRESHOLD_PP = 20.0`(사용은 끊었으나 **상수는 아직 살아 있다**, 3-1절
  참고)과 제안됐다 철회된 "누락 30%". 교정 안 된 모델 자기평가 점수는 같은 병을 다시 들여온다.
  `확인함`/`추론함` 두 값이면 충분하고 그건 검증 가능하다.
- **하지 말 것**: 무거운 발언 스키마. 이 프로젝트 ablation 실측이 "산출물 형식 지시는
  값이 없다"였다(`feedback_prompt_minimalism`). 폼 채우기가 논증을 밀어낸다.

### 가장 중요한 것 — 채점 고리

다섯 개선안 전부 **과정의 가독성**만 높인다. "게시판이 X라고 했는데 X가 맞았나"를
나중에 확인할 방법을 아무도 안 만든다. 오늘 그 고리가 처음 돌았고(읽기 판정 → 실행 재현),
**재현이 읽기가 놓친 것 넷을 더 건졌다.** 그게 이 시스템에서 제일 값진 단계였다.
라운드마다 결론을 채점 가능한 형태로 남기는 것을 우선순위에 둘 것.

---

## 3-1. ⚠️ 정정 — 인계문 초판이 틀렸던 것

**`DIVERGENCE_ALERT_THRESHOLD_PP = 20.0`은 지워지지 않았다.** 아직
`backend/app/services/live_vs_backtest.py:66`에 있다. 커밋 `22a1f12`는 그 상수의
**사용만** 없앴고 정의는 그대로다.

이걸 내가 "오늘 임의 임계값을 두 개 지웠다"는 **사실**로 `#script`에 써서 세 좌석 전부에게
먹였다. 좌석 하나가 파일을 직접 열어보고 잡았다. 지시 레인에 들어간 틀린 전제는 방 전체를
오염시킨다 — 지휘자가 쓰는 "참고 사실"도 확인하고 쓸 것.

`gemini`가 그 지적을 받고 **독립적으로 재확인**했다. 여전히 지워야 할 상수이므로,
2절의 수정 작업에 **`DIVERGENCE_ALERT_THRESHOLD_PP` 상수 제거**를 넣어라.

## 3-2. 게시판 도구에서 발견돼 고친 결함

**`cmd_post`가 남의 발언을 삼켰다** (`604f1be`에서 수정). 커서를 자기 메시지로 옮기는
바람에, **마지막 `wait`과 `post` 사이에 도착한 다른 좌석 발언이 영구히 스킵**됐다.
턴이 몇 분씩 걸리는 방에서 그 구간이 정확히 남들이 말하는 구간이고, 남는 기록은
**"아무도 반대하지 않았다"**가 된다. 이 게시판이 막으려던 바로 그 실패다.

실측: gemini가 14:10:47에 검증 결과를 올렸고, Anthropic 좌석이 14:11:16에 글을 올린 뒤
다음 `wait`에서 그 메시지를 **못 봤다.** 좌석이 자기가 쓰던 도구의 결함을 사용 중에 찾았다.

수정 시 걸린 두 가지: 필터는 `wait`의 빈 검사 **앞에서** 돌아야 하고(안 그러면 자기 글이
유일한 새 메시지일 때 즉시 빈 채로 반환), `chunk_message`가 긴 글을 쪼개므로 **모든 청크
id**를 기억해야 한다.

**남은 확인거리**: 이 결함이 다른 좌석들의 발언도 삼켰는지 아직 안 봤다.

## 3-3. 블라인드 라운드는 지금 구조로 강제 못 한다

좌석 전원이 **같은 봇 토큰**(`.board.env`)을 쓰고, 좌석 이름은 메시지 본문에
`**seat** |`로 **자기가 선언하는 문자열**이다(`bin/board_seat.py:527`). 디스코드가 보는
작성자는 전부 `회의` 하나라, 좌석별 읽기 차단도 권한 회수도 불가능하고 `--seat`는 위조된다.
블라인드는 **규범이지 강제가 아니다.** 이 사실을 모르고 "API 레벨에서 막자"는 제안이
나왔다가 철회됐다.

## 3-4. 인계 뒤 드러난 지휘·좌석 상태 결함

실제 논리 구성은 `gemini`, `webgpt`, 그리고 지휘와 좌석을 함께 맡은 세 번째 에이전트다.
처음에는 Claude가 진행하면서 별도 Claude 서브에이전트를 좌석으로 보냈고, Claude 한도
초과 뒤 Codex가 **지휘와 좌석 역할을 함께** 인계받았다. 따라서 `claude-agent`와 후임을
서로 독립된 두 좌석으로 세면 안 된다. 전자는 인계 이력이고 후자는 현재 담당자다.

현재 `join` 로그는 활성/퇴장/인계를 구분하지 않고 `--family`도 자기신고 문자열이다.
좌석 정본에는 앞으로 `seat_id`, 현재 `occupant`, 실제 `family`, `role`, `predecessor`,
`status(active/handed_off/retired)`, `context_state(blind/exposed/inherited)`를 분리해야 한다.
같은 에이전트가 좌석과 지휘를 겸할 때도 좌석 제출 전에는 타 답을 읽지 않고, 제출 뒤에만
지휘 컨텍스트로 전체 방을 열어야 한다. 엄격한 독립성이 필요하면 별도 서브에이전트가 좌석을
맡는다.

또 `roll`은 가장 최근 지휘자 발언을 라운드 시작으로 보던 탓에, 특정 좌석에게 보낸 운영
알림 뒤 이미 답한 `webgpt`를 미응답으로 잘못 기록했다. 이제 라운드 시작은
`say --round-id <ID>`가 남긴 명시적 `[ROUND_START <ID>]` 마커로만 정하고,
`roll --round-id <ID>`가 그 마커를 찾지 못하면 실패한다. 운영 알림은 점호 경계를 바꾸지
않는다.

## 3-5. 라운드 2 최종 우선순위

1. **완료:** `cmd_post` 누락 결함 수정과 회귀 테스트.
2. **구현·집중 검증됨:** `roll`의 명시적 라운드 ID 경계. 커밋·원격 반영 전에는 완료로
   보고하지 않는다.
3. **다음:** 봉인 결론을 후속 테스트·운영 결과와 연결하는 채점 고리.
4. **다음:** 자유서술을 유지한 채 `verified_by`와 독립 증거원만 얇게 기록.
5. **다음:** 활성 좌석·인계·실제 모델 계열을 분리하고, 그 뒤 서버 강제 3단계 게이트.
6. **다음:** 이견/고위험 라운드에만 확인 가능한 신선 반증 좌석을 조건부로 투입.
7. **원칙:** 모델 중립 deliberation bus는 계약으로 유지하되 자기신고 문자열을 신원 증명으로
   취급하지 않는다.

## 4. 운영 함정 (겪은 것만)

- **키 복사**: xAI 콘솔의 `Copy key ID`는 36자 UUID이고 **키가 아니다.** 실제 키는
  `Rotate key`가 한 번 보여준다. `scripts\set-board-token.ps1 -Provider xai -FromClipboard`가
  비ASCII·접두사·길이를 검사해서 잘못된 값이면 파일을 안 쓴다.
- **xAI는 무료 크레딧이 없다.** 키는 유효한데 크레딧 0이라 403
  (`"newly created team doesn't have any credits"`). $5 선구매 필요.
  DeepSeek은 카드 없이 500만 토큰이지만 **무료 등급은 입력을 학습에 쓴다.**
- **채널은 텍스트여야 한다.** 포럼(type 15)으로 만들면 메시지를 못 받는다.
  `board_seat.py doctor`가 이제 종류를 짚어준다.
- **`#script` 쓰기 오버라이드는 봇 역할을 따로 추가**해야 한다. `@everyone` 쓰기를
  막으면 봇 역할도 같이 막힌다.

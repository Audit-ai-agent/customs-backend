# Frontend 목업 UX 대비 Backend API v1 공백

Frontend(`customs-frontend`)를 通審 인터랙션 명세 v1.3의 UX 플로우로 재구성하면서
확인된 **Backend API v1에 대응물이 없는 화면 요구사항**을 기록한다.

- 출처 목업: `trade-doc-auditor-origin/통심_인터랙션_명세.html` (Interaction Spec v1.3),
  `src/screens/*` (Dashboard, Workspace, V1~V4, TraceRail, DocViewer, Done)
- 기준 계약: `customs-frontend/contracts/backend-openapi-v1.json`
- 작성 시점: 2026-08-06

Frontend는 근거 없는 값을 화면에 표시하지 않는 경계를 지킨다. 따라서 아래 항목은
임의의 고정값으로 채우지 않고 화면에서 **"Backend 미지원"** 상태로 명시한다.
각 항목의 "제안 계약"은 Frontend가 필요로 하는 최소 형태를 적은 것이며 Backend가
확정한 설계가 아니다.

## 요약

| # | 공백 | 목업 위치 | Frontend 현재 처리 |
| --- | --- | --- | --- |
| 1 | ~~HSK 자동분류·후보 선택(HITL)~~ | FRAME 02 심사 Step1 | **구현됨** — `GET/POST /review-runs/{id}/hs-classification` |
| 2 | ~~관세 메리트 진단(세율·절감)~~ | FRAME 05 심사 Step4 | **구현됨(1차)** — `GET /review-runs/{id}/duty-assessment` (+ `origin-verification`) |
| 3 | 요건 서류 첨부·요건 충족 판정 | FRAME 02 / 04 | 서류 레일에 미지원 안내 |
| 4 | 심사 확정·대외 전송(UNI-PASS) | FRAME 05 / 07 | 확정 단계에 미지원 안내 |
| 5 | 리포트 문서 생성(PDF·서식) | FRAME 06 | 버튼 미노출 |
| 6 | 대시보드 운영 지표 | FRAME 01 | Case 상태 카운트만 표시 |
| 7 | Case 목록 업무 컬럼(품목·원산지·HSK) | FRAME 01 | 보유 필드만 컬럼 구성 |
| 8 | 심사 진행 실시간 트레이스 | 워크스페이스 우측 레일 | 완료된 실행의 정적 트레이스만 |

---

## 1. HSK 자동분류와 후보 선택 (HITL)

**목업 동작** — 서류 구조화 후 Agent가 HSK 후보를 신뢰도와 함께 제시하고, 관세사가
후보 하나를 수동 확정한다. 확정을 바꾸면 세율·요건이 재평가되고, 추천이 아닌 후보를
고르면 경고가 뜬다. (인터랙션 명세 `step1 / HSK 후보 선택`)

**필요 데이터** — 심사 실행 결과에 HSK 후보 목록(코드, 품명, 신뢰도, 추천 여부)과
현재 확정 코드. 확정 변경을 기록하는 쓰기 경로.

**제안 계약**

- `ReviewRun.result_detail`에 `hs_classification` 추가
  - `candidates[]`: `{ code, description, confidence, recommended, evidence_ids[] }`
  - `selected_code`, `selected_by`, `selected_at`
- `POST /api/v1/review-runs/{review_run_id}/hs-classification`
  - 요청 `{ code, reason }` → 기존 CorrectionProposal과 같은 승인 경계를 따를지,
    즉시 확정인지 결정 필요

**현재 상태 — 구현됨.** Backend가 저장된 실행의 문서 사실에서 후보를 파생한다
(`trade_features.py`): 후보군은 `hs_code`/`hs_codes` 사실의 10자리 코드, 신뢰도는
문서 합치 비율, 품목명은 패키징된 관세청 HS부호 참조표(`reference/hsk_names_20260101.json`).
확정은 `POST /api/v1/review-runs/{id}/hs-classification` `{code, reason}` — finding
decision과 같은 즉시 확정 + 이력 보존 방식이며 Agent 결과 해시는 건드리지 않는다.

## 2. 관세 메리트 진단 (세율 사다리 · 절감 시뮬레이터)

**목업 동작** — 적용 가능한 세율(기본/협정/할당)을 사다리로 비교하고, 수량·단가·환율·
인코텀즈 입력과 부가세·TRQ 토글에 따라 과세가격·관세·절감액을 즉시 재계산한다.
(인터랙션 명세 `step4`)

**필요 데이터** — 세율 후보와 각 세율의 적용 요건, 과세가격 산정 규칙.

**제안 계약**

- `ReviewRun.result_detail.duty_assessment`
  - `rates[]`: `{ kind: "basic"|"fta"|"trq", rate, legal_basis_evidence_ids[], requirements[] }`
  - `dutiable_value`: `{ amount, currency, incoterms, basis }`
- 재계산은 Browser 계산이 아니라 Backend 산식이어야 한다면
  `POST /api/v1/review-runs/{review_run_id}/duty-simulation`

**현재 상태 — 1차 구현됨.** `GET /api/v1/review-runs/{id}/duty-assessment`가 세율
사다리(기본/FTA)·요건 충족 여부·서버 계산 세액(관세+부가세)·절감액을 반환한다.
세율은 출처·기준일을 명기한 패키징 스냅샷(`reference/duty_rates_20260101.json`)이며
스냅샷에 없는 코드는 `rates_unavailable`로 fail-closed다. 과세가격은 송장 통화 기준
(`total_amount` 사실)이고 환율·인코텀즈 반영과 TRQ, 시뮬레이터 입력은 아직 없다.
원산지 검증은 `GET /api/v1/review-runs/{id}/origin-verification`가 CO 문서와 타 서류의
번호·원산지·HS 코드를 대조해 제공한다.

> 남은 것: 실환율 반영, 인코텀즈 가산, TRQ, 세율 스냅샷의 운영자 갱신 절차.

## 3. 요건 서류 첨부와 요건 충족 판정

**목업 동작** — "수입식품 신고확인증" 같은 세관장확인 요건 서류가 미첨부면 finding이
열려 있고, 첨부하면 요건이 충족되어 판정이 갱신된다. (인터랙션 명세 `step1 / step3`)

**필요 데이터** — 요건 서류의 종류 목록, 각 요건의 충족 여부, 첨부와 판정 갱신의 연결.

**제안 계약**

- `ReviewRun.result_detail.requirements[]`
  - `{ requirement_code, law_name, satisfied, satisfied_by_document_id, finding_id }`
- 첨부는 기존 `POST /api/v1/cases/{case_id}/documents`를 재사용하되
  `requirement_code`를 함께 받아 어떤 요건을 충족시키는 문서인지 결박

**현재 상태** — 문서 등록은 되지만 "요건" 개념이 없어 첨부가 판정을 바꾸지 못한다.
현재 `DocumentType`은 `CI/PL/BL/CO/UNKNOWN` 5종뿐이다.

## 4. 심사 확정과 대외 전송

**목업 동작** — 심사를 확정하면 판정(적합/검토필요/부적합)이 결정되고 UNI-PASS로
전송한다. 완료 화면에서 판정별 후속 액션이 갈린다. (인터랙션 명세 `step4 / done`)

**필요 데이터** — Case를 종결 상태로 넘기는 쓰기 경로와 그 결과 판정.

**제안 계약**

- `POST /api/v1/cases/{case_id}/closure` → `{ disposition, reason }`
- 외부 전송(UNI-PASS 등)은 Backend 밖 연계이므로 별도 승인 경계가 필요하다.
  Frontend는 전송 자체를 호출하지 않고 상태만 표시하는 편이 안전하다.

**현재 상태** — `review_disposition`은 항상 `human_review_required`로 고정이고
Case 상태를 종결로 바꾸는 경로가 없다. 목업의 "적합(CLEARED)" 전환이 불가능하다.

## 5. 리포트 문서 생성

**목업 동작** — 절감 근거 리포트, 협정관세 적용신청서, 심사 리포트를 모달로 열고
PDF로 내보낸다. (인터랙션 명세 `step4 / report`)

**제안 계약** — `POST /api/v1/review-runs/{review_run_id}/reports`
→ `{ report_id, kind, download_url }` (동기 생성이면 202 + Job 재사용)

**현재 상태** — 문서 생성·다운로드 경로가 없다. Frontend는 버튼 자체를 만들지 않는다.

## 6. 대시보드 운영 지표

**목업 동작** — 오늘 접수(전일 대비), 검토 필요, 이달 절감 관세, 평균 처리시간을
KPI 카드 4장으로 보여준다. (인터랙션 명세 `dash / 통계 카드 ×4`)

**필요 데이터** — 기간 집계.

**제안 계약** — `GET /api/v1/metrics/summary?window=today|month`
→ `{ received_count, delta_vs_previous, needs_review_count, average_handling_seconds }`
(절감 관세는 항목 2가 선행되어야 한다.)

**현재 상태** — `GET /api/v1/cases` 응답으로 **상태별 건수**는 셀 수 있다. Frontend는
지금 이 네 가지만 표시한다: 전체 / 진행 전 / 사람 검토 중 / 종료. 접수 추이·절감액·
처리시간은 표시하지 않는다.

## 7. Case 목록의 업무 컬럼

**목업 동작** — 목록 표 컬럼이 건번호·품목·원산지·HSK·상태·절감 여지이고,
건번호와 절감액으로 정렬한다. (인터랙션 명세 `dash / 정렬 헤더`)

**현재 상태** — `CaseSummary`가 가진 것은 `case_id`, `title`, `external_reference`,
`status`, `revision`, `updated_at`뿐이다. 품목·원산지·HSK·절감액 컬럼을 만들 수 없다.

**제안 계약** — `CaseSummary`에 심사 결과에서 파생된 요약 필드를 추가하거나,
`GET /api/v1/cases`에 `include=latest_run_summary` 옵션 제공.
최소한 `document_count`, `finding_count`, `latest_review_run_id`가 있으면
목록 표의 정보 밀도가 목업에 가까워진다.

## 8. 심사 진행 실시간 트레이스

**목업 동작** — 우측 레일에서 Agent 실행 트레이스가 스트리밍으로 쌓이고,
도구 호출·자기수정·에스컬레이션 건수와 진행률이 실시간으로 갱신된다.
(`src/screens/views/TraceRail.jsx`)

**현재 상태** — 완료된 실행의 `workflow.stages`와 `workflow.planner.actions`로
**정적 트레이스**는 그릴 수 있다. 진행 중 스트리밍은 `GET /api/v1/jobs/{job_id}/events/stream`
(SSE)이 계약에 있으나, 이벤트 payload가 목업의 actor/도구/자기수정 분류를 담는지
확인이 필요하다.

**필요 확인** — Job 이벤트 스키마에 `stage`, `actor`, `tool`, `event_kind`
(`tool_call` / `self_correction` / `escalation`), `summary`가 포함되는지.

---

## 부록: 로컬 Backend 계약 드리프트

2026-08-06 기준 로컬 `customs-backend` HEAD는 `6c71aa6`이고, 이 커밋의
`docs/openapi-v1.json`은 Frontend가 보관한 `contracts/backend-openapi-v1.json`과
**byte 불일치**다. Frontend 기준으로 다음 경로가 이 Backend 커밋에 없다.

- `POST /api/v1/dev/session` — 무비밀 로컬 개발 세션 시작점. 없으면 Frontend
  `dev:local` 모드가 첫 요청에서 실패한다.
- AI 도우미 대화 계열(`assistant conversations` / `messages`)
- 정정 제안 계열(`correction proposals` / 승인·반려 / 자식 심사 실행)

재현:

```powershell
Set-Location ..\customs-frontend
$env:BACKEND_OPENAPI_FILE = "..\customs-backend\docs\openapi-v1.json"
npm run contract:check
# Backend OpenAPI byte drift; run contract:generate deliberately.
```

이 공백 문서의 항목을 착수하기 전에 두 저장소의 API v1 세대를 먼저 맞춰야 한다.

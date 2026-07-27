# Backend 비밀정보 스캔 운영 기준

Backend 저장소는 Gitleaks `v8.30.1`의 기본 규칙을 사용합니다. 경로, 규칙,
커밋 또는 정규식 전체를 예외 처리하지 않습니다. 확인된 오탐만
`.gitleaksignore`에 `커밋:경로:규칙:줄` fingerprint로 등록합니다. 커밋이 없는
`경로:규칙:줄` 예외는 같은 위치에 미래 실제 비밀이 들어와도 놓칠 수 있으므로
금지합니다.

## 현재 승인된 계약 잠금 파일 오탐

`src/customs_backend/agent_experience_contract.lock.json`의 24~25번째 줄은
비밀정보가 아니라 Agent 제공자의 OpenAPI 파일 무결성을 검증하는 공개
SHA-256입니다.

- `provider_openapi_lock_file_sha256`
- `provider_openapi_sha256`

Gitleaks의 `generic-api-key` 규칙이 이 두 무결성 값을 API key로 오인합니다.
TASK-0027에서는 해당 값이 처음 포함되거나 갱신된 4개 커밋의 8개 이력
fingerprint만 승인했습니다. 잠금 파일 전체, 현재 경로, 모든 SHA-256,
`generic-api-key` 규칙 전체는 예외 처리하지 않았습니다.

최종 후보는 반드시 clean commit이어야 합니다. 따라서 전체 이력 검사는 현재
후보의 새 변경까지 포함하면서, 승인되지 않은 uncommitted 파일을 완료 후보로
취급하지 않습니다. 선택적으로 실행하는 `gitleaks dir`은 공개 계약 hash를 다시
보고할 수 있으나, 이를 없애기 위해 commit 없는 예외를 추가해서는 안 됩니다.
현재 계약 lock의 두 줄은 아래의 별도 Schema 회귀에서 공개 SHA-256 필드와 JSON
값이 정확히 일치하는지 확인합니다.

## 재현 명령

PowerShell에서 저장소 루트를 현재 디렉터리로 둔 뒤 실행합니다.

```powershell
$gitleaks = "<검증된 gitleaks v8.30.1 실행 파일>"

& $gitleaks git --no-banner --no-color --redact `
  --config .gitleaks.toml `
  --gitleaks-ignore-path .gitleaksignore `
  .
```

이 명령은 CI와 동일하게 전체 Git 이력을 검사합니다. 종료 코드는 `0`이어야
합니다. 실행 전후 `git status --short`가 비어 있어야 합니다.

기본 규칙이 실제 비밀값을 계속 탐지하는지는 저장소를 수정하지 않는
표준입력 canary로 확인합니다.

```powershell
$canary = 'api_key = "' + 'gitleaks-canary-' + 'f4e84d2e0c6a4fcda13e9a8175d094d9' + '"'
$canary |
  & $gitleaks stdin --no-banner --no-color --redact `
    --config .gitleaks.toml `
    --gitleaks-ignore-path .gitleaksignore
```

이 명령은 의도적으로 종료 코드 `1`과 탐지 1건을 반환해야 합니다. canary는
실제 credential이 아니며 저장소에 기록하지 않습니다.

## 새 오탐 승인 조건

새 fingerprint는 다음 조건을 모두 확인한 뒤에만 추가합니다.

1. 검출된 정확한 커밋, 경로, 규칙, 줄을 확인합니다.
2. 해당 값이 credential이 아니라 공개 무결성 값인지 원본 생성 경로로
   확인합니다.
3. 실제 credential이면 ignore하지 않고 폐기·재발급과 이력 정제를 먼저
   수행합니다.
4. fingerprint 한 줄만 추가하고 경로·규칙·정규식 전체 예외는 만들지
   않습니다.
5. 현재 lock Schema 회귀, 전체 이력 스캔, 표준입력 canary를 모두 다시 실행합니다.
6. `tests/product/test_secret_scanning_policy.py` 회귀를 통과시킵니다.

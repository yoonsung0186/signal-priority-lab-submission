# 취득·생성 연결과 현재 검증 범위

## 고정 입력 취득

SIT-PET 1.1 고정 record: https://zenodo.org/records/19231310 . `reproduce/input_inventory.json`에는 네 아카이브의 원래 상대경로, 크기, SHA-256, API URL과 내부 표가 있다. 다운로드는 이번에 하지 않았다. 해당 URL에서 직접 취득해 기록된 경로에 저장하고 SHA-256을 비교한다. 기존 파일은 덮어쓰지 않는다. HTTP 429 등 응답은 제공자 Retry-After를 따르며 접근 제한을 우회하지 않는다.

`configs/pet_multiday_finish.yaml`이 pet_events.zip, objects.zip, signals.zip과 examples.zip의 scenarios만 연결한다. 데이터 자체는 ZIP에서 제외했다. `state/data_acquisition_manifest.json`은 기존 취득·권리 기록으로 포함하며, 새 취득 사실을 뜻하지 않는다. 원 라이선스는 `reproduce/sitpet/LICENSE.md`에 있다.

## 실제 계산 경로 — 이번에는 실행하지 않음

1. 원자료 → `tools/pet_bulk.py --profile configs/pet_multiday_finish.yaml --output runs/<fresh_baseline_id>` → event_ledger.csv, 날짜별 parquet shards/index, pet_qc.json, `.cache/<fresh_baseline_id>_input.sqlite`, 집계·AI 결과. **이 명령은 학습을 포함하므로 이번 패키징에서 실행 금지**다. `--qc-only`를 기존 AI 포함 완료본과 동등하다고 간주하지 않는다.
2. baseline → `tools/finish_evidence.py --pet-run runs/<fresh_baseline_id> --output runs/<fresh_evidence_id>` → 자동 수치검토·AI 비교. 독립 사람 정답 검수가 아니다.
3. 보존 baseline → `tools/r2_postprocess.py --out runs/<fresh_R2_id>` → R2 no-fit 결과. 입력은 `configs/r2_postprocess.json`과 `state/r2_20261013/baseline_reuse_manifest_v2.json`의 경로·해시로 고정된다.
4. 보존 R2 → `tools/r2_signal_quality.py --out runs/<fresh_boundary_id>` → 경계 보완. `configs/r2_signal_quality.json`의 10개 장구간·원래 R2 run·baseline ledger/cache를 읽는다. 기존 R2 시간 스트레스 표와 품질유보 표는 결합해 다른 모집단의 비율을 만들지 않는다.

기존 완료 경로는 baseline `runs/finish_final_20261013_A_pet_2`, 자동 검토 `runs/finish_final_20261013_A_evidence_2`, R2 `runs/r2_final_20261013_B_2`다. ZIP의 `review/runs/`는 선택된 검토 근거이며 이 실행 디렉터리의 대체물이 아니다. `reproduce/baseline_reuse_manifest.json`은 필요한 입력·산출물 해시 목록이고 원자료나 실제 모델은 아니다.

## 명확한 미해결 사항

- 원자료, 전체 ledger/shards, cache, frozen model과 대형 중간 결과는 미포함이다. 현재 패키지만으로 전체 실자료 재현 불가다.
- R2의 `verify_reuse`는 과거 입력·fit 로그·산출물 전체와 누계 52를 요구한다. 원래 재사용 manifest만 새 경로로 복사해도 재현되지 않는다. 누락 입력과 생성 단계는 `reproduce/dependency_status.json`에 있다.
- 경계 실행기의 엄격한 원본 source/context 검사는 139개 원래 소스·문서 바이트와 R2 전체 산출물을 요구한다. 불필요한 내부 실행 지침을 제외하고 배포 README를 바꾼 이 스냅샷은 그 원본 컨텍스트와 동일하지 않다. 내부 지침을 몰래 되넣거나 체크를 우회하지 않았다. 원본 권한 있는 프로젝트에서 원래 해시 컨텍스트를 복원해야 동일 실행이 가능하다. 별도 이식용 코드 변경·재검증은 이번 범위 밖이다.
- 계산 소스는 그대로이며 Git 메타데이터를 배포하지 않는다. 일부 실행기는 `git rev-parse HEAD`를 요구한다. 배포본을 Git 저장소로 만든 것만으로 원래 소스 commit 증거가 생기지는 않는다.
- `state/environment_manifest_r1.json` / `state/environment_manifest.json` 등 이전 환경 기록은 `reproduce/`에 보관한다. 새 실행 환경을 과거 환경으로 가장해 복원하지 않는다. 현재 환경 정보와 import 결과는 별도 검증 기록이다.
- 현재 누적 학습 로그는 `reproduce/fit_history/`에 읽기 근거로 복사했다. 실행용 `runs/`로 옮겨 새 학습 예산을 만들지 않는다. 추가 실자료 fit 0회다.

이 패키지의 실제 검증은 분리된 추출 폴더의 import, CLI 도움말, 무학습 경계/컨텍스트 시험, 파일 해시·CRC와 저장 결과 검산이다. `full_raw_data_reproduction=false`. 과거 2회 재현 증거는 해당 run의 과거 검증이지 이번 실행이 아니다.

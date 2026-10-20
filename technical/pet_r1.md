# PET_INTERACTIONS_V1 기술 정의와 재현

## 실제 범위와 원천

SIT-PET 1.1, 고정 DOI 10.5281/zenodo.19231310, CC BY 4.0. 저자 Steinmaßl, Rehrl, Vornberger (2026). 현재 자료는 공개 `examples.zip` 하루 표본이다. SHA256 `60ffaaf43c4bc1287f50111aa70c1eef311616ad72e8667ca7c09a04c5750f82`. 원자료는 인계 ZIP에서 제외한다. 파생 변경은 PET QC, 조건부 집계, 신호 맥락 결합, 점유시간·객체 크기 기반 군집 기술 분석이다. 제공자의 승인이나 공모전 적합성을 뜻하지 않는다.

원자료 1,836개 event는 8개 시나리오에서 제공자가 기록한 상호작용이다. 첫 독립 계산과 구현의 32개 scenario×threshold 셀을 대조했다. 비음수 PET 1,824건, 음수 12건, 무효 0건이었고 1초 이하 비음수 PET는 5건이었다. 이는 첫 실행 결과이며 최종 수치는 선택된 검증 run의 표가 기준이다. 객체 표 8,133행에는 7,764개 ID가 있으며 반복 ID의 역할·속성을 조용히 덮어쓰지 않는다.

추가 다일자 자료는 고정 버전으로만 시도했다. metadata/license 요청의 HTTP 504 실패를 보존했고, 동일 버전의 최신 metadata와 보존된 license hash를 확인한 후 첫 archive 요청에서 HTTP 429를 받았다. 다른 경로로 우회하지 않았다. 다일자 bootstrap·순위 안정성·heldout 일반화 평가는 미수행이다. 신호 자료에 결함이 있어도 PET 자체 유효 분모는 별도로 유지한다.

## 계산 근거와 데이터 사전

`event_ledger`: dataset/version/event_id 복합키, 원본 archive hash와 행, 원시 PET 부호, 네 UTC timestamp(ms), 충돌 좌표, 제외 사유, local date/timebin, 역할별 객체·신호 연결을 담는다. `encroaching`과 `priority`는 제공자 역할명이다. 가해·피해·법적 우선권을 뜻하지 않는다. 객체 count와 pair count는 event count와 구분하며 교통량 분모로 쓰지 않는다.

`pet_qc`: 같은 key의 같은 값은 출처를 병합하고 충돌 값은 제외한다. FK, finite 수치, 좌표 범위, 역할별 leave>=enter, 누락을 검사한다. 음수 PET는 `NEGATIVE_PET_REVIEW`로 보존하고 절댓값·0으로 바꾸지 않는다. 큰 PET는 그대로 남긴다. 예제 README의 PET 정의는 encroaching 객체가 conflict area를 떠난 뒤 priority 객체가 진입하기까지 시간이다. 원시 timestamp와 정의의 수치 일치 검사는 제공자 내부 일관성 검사이며 독립 기하학적 정확도가 아니다. 공식 논문 필터 정의는 미확인이다.

`signal_context`: scenario의 역할별 signal_group을 각 객체 자신의 conflict-area enter 시각에서 `[start,end)`와 결합한다. red-yellow를 독립 상태로 유지한다. gap은 UNKNOWN, 겹침은 SIGNAL_AMBIGUOUS이며 잘못된 신호행을 정상으로 승격하지 않는다. 대표 보행자 signal mapping은 제공자 범위를 보존한다. 정지선 통과 시각 신호·위반 판정이 아니다.

`scenario_summary`, `pet_thresholds`: 시나리오별 raw/invalid/negative/valid/known 수, PET 분위수, tau=[0.5,1,1.5,2]초에서 `count(0<=PET<=tau)/count(valid PET>=0)`를 계산한다. 분모 0은 null이다. 신호 조건부 표는 양쪽 역할 신호가 알려진 별도 분모를 표시한다. tau는 운영 비교값이며 법적·사고 기준이 아니다.

`pet_ecdf`: 정렬된 비음수 유효 PET의 누적 건수/n. 전체 큰 PET를 포함한다. `timing_sensitivity`: event의 네 시각을 동일하게 [-0.5,-0.2,0,0.2,0.5]초 이동하고 신호 clock은 고정한다. PET 값은 불변이다. 이 offset은 측정된 오차분포가 아니다. local 분석은 Europe/Vienna, 시간대 표는 사전에 고정된 4시간 bin이다.

`inspection_candidates`: 기록된 관측조건에서의 추가 확인 후보다. n>=30 및 관측일>=7 조건을 충족하지 못하면 DESCRIPTIVE_ONLY, 순위 안정성과 CI는 null이다. 다일자 코드의 date-cluster bootstrap은 B=200, seed=20261012이며 도시 전체 일반화 CI가 아니다.

`cluster_profile`, `cluster_validation`: 두 객체 점유시간 log1p와 제공자 static length/width를 사용한다. PET, 라벨, identity, raw timestamp, cross-role 시간차, scenario, 신호는 primary feature에서 제외한다. 고정 결측 정책과 train-only RobustScaler, KMeans k=2..5, n_init=20, seed=20261012를 사용한다. stability seeds는 20261012/13/14다. silhouette/ARI는 탐색 지표이며 사고 정확도·모델 개선 증거가 아니다. 하루 표본의 fit은 기술 탐색만 허용하고 heldout 결과는 null이다. 실제 fit 시도 수는 run의 fit 로그로 확인하며 R1 전체 예산은 64회다.

모든 표는 CSV와 Parquet 또는 Parquet shard/index로 제공한다. `run_manifest.json`이 byte/semantic 재현 비교 대상의 실제 목록이다. figure의 직접 원천표는 `figures_index.json`을 따른다. 비결정적 시간·실행 경로가 있는 provenance/fit 실행 로그와 그림은 canonical 수치 비교와 분리하고 선택된 run 전체 hash manifest로 무결성을 검증한다.

## 실행

새 소스 ZIP은 다른 Git 저장소 안이 아닌 독립 폴더에 푼다. 분석 CLI는 프로젝트 marker를 검사한다. 최종 패키징의 Git HEAD 기록에는 해당 독립 폴더의 로컬 Git 저장소와 commit이 필요하며 원격은 필요 없다. 새 환경 설치 후 `python tools/record_environment_r1.py`로 실제 환경 기록을 생성한다. 이 명령은 기존 환경 기록을 덮어쓰지 않는다. 현재 프로젝트에는 환경 기록이 이미 있으므로 다시 실행하지 않는다.

기존 프로젝트에서는 원자료·환경을 재설치하지 않는다. 새 환경은 소스 ZIP을 새 디렉터리에 풀고 Python 3.13 환경에 requirements.lock을 설치한다. 기술 ZIP의 reproduce/sitpet 문서를 state/research/sitpet에, validation/data_acquisition_manifest.json을 state 아래에 복원한다. 인계된 환경 manifest는 과거 실행의 참고용이며 새 환경에서는 record_environment_r1.py로 실제 기록을 만든다. history/state/amendments 자료는 해당 상대경로로 복원하여 범위·실패·runtime 기록을 유지한다. 환경을 다시 만든 경우 실제 설치 버전·경로·lock hash로 새 환경 manifest를 기록한다. 고정 원자료를 `data/raw/sitpet_v1_1/examples.zip`에 두고 SHA256이 일치하는지 확인한다. 경로나 입력을 바꾸면 profile config를 명시적으로 갱신하고 새 검증을 수행한다.

```powershell
$env:PYTHONPATH = 'src'
.venv/Scripts/python.exe -m spl pet-qc --profile configs/pet_interactions_v1.yaml --output runs/my_new_qc
.venv/Scripts/python.exe -m spl pet-analyze --profile configs/pet_interactions_v1.yaml --output runs/my_new_analysis
.venv/Scripts/python.exe tools/final_verify.py --profile pet-interactions-v1 --verification-id my_new_verify_1
.venv/Scripts/python.exe tools/final_verify.py --profile pet-interactions-v1 --verification-id my_new_verify_2
.venv/Scripts/python.exe tools/package_delivery.py --profile pet-interactions-v1 --verification state/verifications/my_new_verify_2/verification_summary.json --run-id my_new_delivery
```

각 검증은 현재 interpreter로 테스트·preflight·합성 2회·PET 2회를 실행한다. 기존 output/verification/delivery ID는 거부한다. 소스·profile config·환경·lock·원자료·rights hash가 바뀌면 이전 검증으로 패키징할 수 없다. 실패 시 기록과 출력은 보존하고 새 ID를 사용한다. source ZIP의 source_snapshot.json, 기술 ZIP의 manifest.json, 외부 CHECKSUMS.sha256으로 추적한다.

원래 straight-entry 분석은 검증된 경계·접근부 매핑 전까지 보류다. 독립 실제 정답 검수, PET 기하학적 정확도, 공모전 자격 확인, 외부 제출, 원격 push는 수행하지 않는다. 메인 Astra medium 및 두 Sol xhigh 작업자 요청과 실제 런타임 식별정보 확인 가능 여부는 별도 기록한다. 현재 세션은 실제 모델 ID 조회 수단이 없어 적용 확정을 주장하지 않는다.

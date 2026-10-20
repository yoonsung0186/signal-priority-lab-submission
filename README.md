# Signal Priority Lab — 제출용 코드·근거 스냅샷

최신 완료 범위는 SIT-PET 1.1 다일자 PET 상호작용 기본 분석, R2 관측단위·시간 스트레스 보완, 신호 품질 유보 경계 보완이다. 새 분석이나 신규 학습을 수행한 패키지가 아니다. 기존 literal 결과와 품질유보 후 부분집단은 별도 결과다.

기본 분석은 기록된 상호작용을 분모로 사용한다. 전체 교통량·사고확률·법적 위반을 측정하지 않는다. AI 추가 효익은 미입증이고 독립 정답·기하학 검증은 미수행이다. 신호 장구간은 오류 확정이 아닌 보수적 품질유보다. 원래 직진 진입 분석은 보류 상태다.

## 먼저 읽을 자료

- [버전·배포 변경 목록](PACKAGE_IDENTITY.json)
- [기본 분석 근거](review/runs/finish_multiday_20261013_01/metrics.csv)
- [R2 방법](technical/R2_METHODS.md), [R2 결과](review/runs/r2_finish_20261013_01/analysis/unit_summary.csv)
- [경계 보완 방법](technical/R2_SIGNAL_QUALITY_BOUNDARY.md), [최신 결과](review/runs/r2_quality_boundary_20261013_01/metrics.csv)
- [취득·입력·생성 경로와 재현 제약](REPRODUCTION.md)
- [출처와 권리](ATTRIBUTION.md)
- [공개 대기 사항](PUBLICATION_PENDING.md), [보고서 참고 링크 대응표](REPORT_LINK_MAP.json)

## 설치와 무학습 점검

Python 3.13.9가 기존 검증 환경이다. 새 폴더에 압축을 풀고 PowerShell에서 다음을 실행한다. 설치 명령은 안내이며 이번 패키징에서 새 설치를 수행하지 않았다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-r2.lock
$env:PYTHONPATH='src'
$env:PYTHONUTF8='1'
.\.venv\Scripts\python.exe verify_snapshot.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_r2_signal_quality.py -v
.\.venv\Scripts\python.exe -m unittest discover -s tools -p test_r2_signal_quality_context.py -v
.\.venv\Scripts\python.exe tools/pet_bulk.py --help
.\.venv\Scripts\python.exe tools/r2_postprocess.py --help
.\.venv\Scripts\python.exe tools/r2_signal_quality.py --help
```

전체 테스트 탐색이나 `finish_verify.py`를 실행하지 않는다. 원래 baseline 실행은 학습을 포함하며 이번 범위 밖이다. 위 점검은 파일·저장 수치·합성 경계시험이며 실자료 전체 재현이 아니다. 원자료·ledger·SQLite cache·모델을 포함하지 않았다. 과거 fit 로그는 읽기 전용 근거로 포함하며 누계 52를 새 예산으로 초기화하지 않는다.

ZIP 밖의 `PACKAGE_RECEIPT.json`과 `VALIDATION.json`에 실제 검사 결과가 있다. 파일 manifest는 배포 바이트를 식별하며, 원본 증거에 포함된 과거 manifest는 과거 실행의 바이트를 설명한다. 경로 개인정보 제거로 바뀐 파일은 배포 manifest의 변환 표시를 확인한다.

## 역사기록 구분

예전 루트 README의 하루 예제·AI 미수행·push 미수행은 이전 단계 설명이었다. 이 배포 README가 현재 범위를 안내한다. 원본 Git README와 완료 run은 수정하지 않았다. 개별 근거의 실행 시점·미수행 상태는 역사적 증거로 보존한다.

현재 패키지는 비공개 보존용이며 심사자 열람은 미확인이다. 공개 승인·본인 확인·개인정보·동의·서명·보고서 제출은 완료되지 않았다. READY_TO_SUBMIT 상태가 아니다.

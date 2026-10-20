# 기술 데이터 사전

`table_inventory`: table은 원본 ZIP 내부 경로, row_count는 헤더 제외 행 수, data_kind=REAL이다.

`class_sample_counts`: object_class는 제공자 원문 분류, trajectory_rows는 해당 분류의 프레임 표본 수다. 객체 수/차량 수/진입 수가 아니다. 이 표가 class_samples PNG/SVG의 직접 원천이다.

`signal_coverage`: 그룹별 interval_rows, 유효 신호 구간 합집합 union_duration_s(초), invalid_intervals, overlapping_intervals, 첫/마지막 timestamp_ms(UTC Unix 밀리초). 신호별 관측량은 접근부 노출량이 아니다. 겹치는 구간을 중복 합산하지 않는다.

`real_qc.json`: unique_object_ids는 제공자 식별자 수, tracking_status_counts는 상태별 궤적 행 수다. TRACKING 표본 중 유한하고 음수가 아닌 speed_ms의 평균을 tracking_sample_mean_speed_mps로 기록한다. 좌표는 원본 WGS84 도 단위로 범위 검사하며 미터로 임의 변환하지 않는다. 시각은 실제 Unix 밀리초다. 샘플의 첫날 이후까지 이어진 궤적도 그대로 포함한다.

실제 C/G/U/N 및 red_entry_rate, accuracy, population_ci의 null은 미수행이다. 0으로 대체하지 않는다. 실제 정지선과 신호-직진 접근부 매핑이 검증되지 않았다.

합성 실행은 SYNTHETIC으로 분리한다. C=RED_CONSISTENT, G=NON_RED_CONSISTENT, U=SIGNAL_AMBIGUOUS, N=C+G+U; EXCLUDED는 분모 밖이다. 1000*C/N 및 1000*(C+U)/N은 분류 범위이며 신뢰구간이 아니다. N=0은 null. 합성 fixture의 실제 값·제외 이유는 synthetic 실행 JSON/CSV에서 추적한다.

출처: Steinmaßl, Rehrl, Vornberger, SIT-PET 1.1, DOI 10.5281/zenodo.19231310, CC BY 4.0. 파생 변경은 기초 집계와 QC이며 원본 자료를 ZIP에 포함하지 않는다.

# 선제적 인력 예측 PoC (데모)

전략 컨설팅 데모용 Streamlit 앱. 기준연도→목표연도를 입력하면 **내부 인력데이터(SQLite·SQL) → 전처리 → 마르코프 투영**과 **외부동향 리서치 에이전트(LangGraph)**가 병렬로 돌고, 두 시나리오를 비교 시각화한 뒤 **보고서 초안**까지 생성한다.

> 핵심: 숫자 정확도가 아니라 **파이프라인이 단계별로 화면에서 도는 과정**과 **실데이터 교체 용이성**.

## 실행법

```bash
pip install -r requirements.txt
# (선택) 키 설정 — 없으면 mock 폴백으로 끝까지 실행됨
#   Windows PowerShell:  $env:ANTHROPIC_API_KEY="sk-..."
#   bash:                export ANTHROPIC_API_KEY="sk-..."
streamlit run app.py
```

- **키가 없어도 데모는 끝까지 실행됩니다** (리서치·보고서 모두 mock 폴백 내장).
- `ANTHROPIC_API_KEY` 설정 시: 리서치는 web search(real), 보고서는 LLM(real)로 동작.

## 파이프라인 (화면 단계)

| Stage | 내용 | 모듈 |
|---|---|---|
| 0 | 더미 인력 레코드(개인 단위) 생성 | `generate_dummy.py` |
| 1 | SQLite 적재 → SELECT 조회 → pandas 전처리 → 전이 집계 | `data_pipeline.py` |
| 2 | 마르코프 P 추정(라플라스 평활) → 연도별 투영 (내부 트랙) | `markov.py` |
| 3 | 리서치 에이전트: 검색→평가→재검색 루프→조정계수 추출 (외부 트랙) | `research_agent.py` |
| 4 | 조정계수로 P 재조정 → 재정규화 → 재투영 | `markov.py` |
| 5 | Baseline vs Adjusted 비교 + 델타 분해 + 목표 갭 | `viz.py` |
| 6 | 보고서 초안(LLM/목업) + .md 다운로드 | `report.py` |

## 파일 구조

```
app.py             # Streamlit 메인 (UX·오케스트레이션)
generate_dummy.py  # 더미 생성 (가정값 상수화) ← 실데이터 교체 지점
data_pipeline.py   # SQLite 적재·SQL 조회·전처리·전이 집계
markov.py          # 상태정의·P 추정·평활·투영·재조정
research_agent.py  # LangGraph 그래프 + web search + 조정계수 추출 + 목업 폴백
report.py          # 보고서 생성 (real/mock)
viz.py             # plotly 차트 모음
DATA_SPEC.md       # 데이터 명세서
```

## 교체 가능성 (Swap Points)

- **더미 → 실데이터**: `generate_dummy.py`를 실 HR 추출로 교체하고, `data_pipeline.py`의 `SELECT_*` 쿼리만 실제 테이블 스키마에 맞게 수정. 이후 파이프라인은 그대로 동작.
- **에이전트 mock ↔ real**: 환경변수 `ANTHROPIC_API_KEY` 유무로 자동 토글.
- **상태 해상도**: `markov.py`의 `STATES` 리스트만 바꾸면 직급/근속밴드 확장 (단, `generate_dummy.py` 가정 테이블도 동기화).
- **고도화 로드맵**: 데이터 규모 충분 시 **계층적 풀링/베이지안(디리클레)** 추정으로 전이확률 안정화 가능.

## 주의

- 본 산출물은 PoC 데모이며 숫자는 더미데이터 기반 계산값입니다.
- 더미데이터는 `generate_dummy.py`의 가정값에 **차장 병목/누수 스토리**가 의도적으로 심어져 있습니다.
```

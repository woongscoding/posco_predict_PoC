# 선제적 인력 예측 PoC (데모)

전략 컨설팅 데모용 Streamlit 앱. 기준연도→목표연도를 입력하면 **내부 인력데이터(SQLite·SQL) → 전처리 → 마르코프 투영**과 **외부동향 리서치 에이전트(LangGraph)**가 병렬로 돌고, 두 시나리오를 비교 시각화한 뒤 **보고서 초안**까지 생성한다.

> 핵심: 숫자 정확도가 아니라 **파이프라인이 단계별로 화면에서 도는 과정**과 **실데이터 교체 용이성**.

## ☁️ Streamlit Community Cloud 무료 배포 (시연용)

> 키는 절대 커밋하지 않는다. `.env`/`secrets.toml`은 `.gitignore`로 제외됨. 키는 Cloud의 **Secrets**에 넣는다.

1. **GitHub repo 생성** — github.com 에서 빈 repo 생성 (예: `seonje-poc`). README/gitignore 추가 안 함(이미 있음).
2. **푸시** (이 폴더에서):
   ```bash
   git remote add origin https://github.com/<내계정>/seonje-poc.git
   git branch -M main
   git push -u origin main      # 푸시 시 GitHub 로그인 창이 한 번 뜸
   ```
3. **배포** — [share.streamlit.io](https://share.streamlit.io) 로그인(GitHub 연동) → **New app** → repo/branch(main)/main file `app.py` 선택.
4. **Secrets 등록** — 앱 대시보드 → **Settings → Secrets** 에 아래 붙여넣기:
   ```toml
   ANTHROPIC_API_KEY = "sk-ant-..."
   APP_PASSWORD = "데모암호"   # (선택) 공개 링크 보호용. 빼면 게이트 없음
   ```
5. 저장하면 자동 재기동 → 발급된 `https://<앱이름>.streamlit.app` URL로 시연.

- **공개 노출 주의**: 무료 앱 URL은 기본 공개. `APP_PASSWORD`를 설정하면 진입 시 암호를 요구해 API 크레딧을 보호한다. (또는 Streamlit Cloud의 앱 private + 이메일 초대 기능 사용)
- **업데이트**: 코드 수정 후 `git push` 하면 Cloud가 자동 재배포.

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
| 3 | 리서치 에이전트: 검색→루브릭 평가→refine(쿼리 보강)→재검색 루프→조정계수 추출 (외부 트랙) | `research_agent.py` |
| 4 | 조정계수로 P 재조정 → 재정규화 → 재투영 | `markov.py` |
| 5 | Baseline vs Adjusted 비교 + 델타 분해 + 목표 갭 | `viz.py` |
| 6 | 보고서 초안(LLM/목업) + .md 다운로드 | `report.py` |

## 리서치 에이전트 그래프 (Stage 3)

LangGraph 상태 그래프로 **검색 → 평가 → (부족 시) 보강 → 재검색** 루프를 구현한다.

```
START → research → evaluate → route
        route: overall≥80 or retry≥MAX → extract → END
               부족                      → refine → research (루프백)
```

- **루브릭 평가**: 단일 점수가 아니라 `정량성·방향성·커버리지`(각 0~100) + `overall` + `missing`(부족 항목)으로 채점. 통과 기준 `overall ≥ 80`.
- **refine 노드**: 부족 판정 시 같은 키워드를 반복하지 않고, `missing` + **지금까지 사용한 모든 쿼리 이력(`queries_log`)**을 보고 겹치지 않는 **새 구체 쿼리 2~3개**를 생성해 재검색 → 점수 정체·쿼리 중복 해소.
- **best-so-far 추적**: 평가가 매 라운드 전체 코퍼스를 새로 채점해 점수가 **비단조**(올랐다 내렸다)일 수 있으므로, 최고점 라운드의 코퍼스 스냅샷(`best_results`)을 보관하고 **조정계수는 최고점 라운드 결과로 추출**(마지막 라운드가 더 낮아도 안전). 화면에 🏆로 표시.
- **중복 제거 누적**: 라운드 결과를 누적할 때 같은 내용 스니펫을 걸러 코퍼스 희석을 완화.
- **상수**(`research_agent.py` 상단): `MAX_RETRY=3`, `EVAL_PASS_THRESHOLD=80`, 평가/보강은 Sonnet, 추출 Haiku, 검색 Opus(web search).
- **화면**: 라운드별 3축 루브릭 막대 + 종합점수 추이(🏆=추출 사용 라운드) + refine 보강쿼리 테이블 + mermaid 그래프 다이어그램이 실시간 로그와 함께 표시.
- **mock 폴백**: 키가 없어도 `58→74→86`으로 점수가 오르며 2회 보강 후 통과하는 흐름을 재현(데모 중단 방지).

## 파일 구조

```
app.py             # Streamlit 메인 (UX·오케스트레이션)
generate_dummy.py  # 더미 생성 (가정값 상수화) ← 실데이터 교체 지점
data_pipeline.py   # SQLite 적재·SQL 조회·전처리·전이 집계
markov.py          # 상태정의·P 추정·평활·투영·재조정
research_agent.py  # LangGraph 그래프(refine 루프·루브릭 평가) + web search + 조정계수 추출 + 목업 폴백
report.py          # 보고서 생성 (real/mock)
viz.py             # plotly 차트 모음
DATA_SPEC.md       # 데이터 명세서
```

## 교체 가능성 (Swap Points)

- **더미 → 실데이터**: `generate_dummy.py`를 실 HR 추출로 교체하고, `data_pipeline.py`의 `SELECT_*` 쿼리만 실제 테이블 스키마에 맞게 수정. 이후 파이프라인은 그대로 동작.
- **에이전트 mock ↔ real**: 환경변수 `ANTHROPIC_API_KEY` 유무로 자동 토글.
- **상태 해상도**: `markov.py`의 `STATES`가 기준 출처. 단, 상태를 바꾸면 **함께 수정할 지점이 있다** ↓
  | # | 파일 | 수정 대상 |
  |---|---|---|
  | 1 | `markov.py` | `STATES`, `ABSORBING`, `survivors_by_rank`의 `rank_map` |
  | 2 | `generate_dummy.py` | `STATES`(자체 정의), `TRANSITION_ASSUMPTIONS`, `RANK_OF_STATE`, `TENURE_RANGE`, `INITIAL_STATE_MIX` |
  | 3 | `viz.py` | `RANK_COLORS`, 직급 루프 `["사원","대리","차장"]` |
  - `data_pipeline.py`·`research_agent.py`는 `from markov import STATES`로 자동 추종(추가 수정 불필요).
- **고도화 로드맵**: 데이터 규모 충분 시 **계층적 풀링/베이지안(디리클레)** 추정으로 전이확률 안정화 가능.

## 주의

- 본 산출물은 PoC 데모이며 숫자는 더미데이터 기반 계산값입니다.
- 더미데이터는 `generate_dummy.py`의 가정값에 **차장 병목/누수 스토리**가 의도적으로 심어져 있습니다.
```

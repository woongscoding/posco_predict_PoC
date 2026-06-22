"""
data_pipeline.py — 내부데이터 수집·전처리 (Stage 1)
==================================================
흐름:
  1) 개인 단위 더미를 SQLite(in-memory)에 적재
  2) 실제 SELECT 쿼리문을 노출하고 결과 DataFrame 미리보기
  3) pandas 전처리: 상태 라벨링, 결측/이상치 정리
  4) 전이 집계: GROUP BY 현재상태 → 다음연도_상태 카운트
  5) 산출물: 전이 카운트 행렬

⚠️ 실데이터 교체 지점:
   load_to_sqlite()에 넣는 DataFrame을 실 HR 추출로 바꾸고,
   SELECT_RAW / SELECT_TRANSITIONS 쿼리만 실제 테이블 스키마에 맞게 수정하면 된다.
"""

from __future__ import annotations
import sqlite3
import pandas as pd

from markov import STATES
from generate_dummy import get_base_year_headcount

TABLE_NAME = "hr_records"

# 화면에 그대로 노출할 SQL (시연용) — "SQL로 데이터를 끌어온다"
# 데이터는 2020~기준연도의 '인사이동 이력 패널'(관측연도별 전이)이다.
SELECT_RAW = f"""
SELECT 사번, 직급, 근속연수, 부서, 관측연도, 현재상태, 다음연도_상태
FROM {TABLE_NAME}
ORDER BY 관측연도, 현재상태
LIMIT 20;
"""

# 전이 카운트는 '모든 관측연도(2020→21 …)'를 풀링해 집계 → 표본을 늘린다.
SELECT_TRANSITIONS = f"""
SELECT 현재상태, 다음연도_상태, COUNT(*) AS 전이건수
FROM {TABLE_NAME}
GROUP BY 현재상태, 다음연도_상태
ORDER BY 현재상태, 다음연도_상태;
"""

# 관측연도별 건수 — 이력 패널이 여러 해에 걸쳐 있음을 화면에서 보여주기 위함
SELECT_BY_YEAR = f"""
SELECT 관측연도, COUNT(*) AS 전이건수
FROM {TABLE_NAME}
GROUP BY 관측연도
ORDER BY 관측연도;
"""

SELECT_HEADCOUNT = f"""
SELECT 현재상태, COUNT(*) AS 인원
FROM {TABLE_NAME}
GROUP BY 현재상태;
"""


def load_to_sqlite(df: pd.DataFrame) -> sqlite3.Connection:
    """더미 DataFrame을 in-memory SQLite에 적재하고 커넥션 반환."""
    conn = sqlite3.connect(":memory:")
    df.to_sql(TABLE_NAME, conn, index=False, if_exists="replace")
    return conn


def run_query(conn: sqlite3.Connection, sql: str) -> pd.DataFrame:
    """임의 SQL 실행 → DataFrame."""
    return pd.read_sql_query(sql, conn)


def preprocess(df: pd.DataFrame) -> pd.DataFrame:
    """
    pandas 전처리 (더미라 형식적이어도 충분):
      - 상태 라벨 유효성 검사(정의되지 않은 상태 제거)
      - 결측/이상치 정리(근속연수 음수 제거, 결측 드롭)
    """
    clean = df.copy()
    # 결측 제거
    clean = clean.dropna(subset=["현재상태", "다음연도_상태"])
    # 이상치: 근속연수 음수 제거
    if "근속연수" in clean.columns:
        clean = clean[clean["근속연수"] >= 0]
    # 정의된 상태만 유지
    clean = clean[clean["현재상태"].isin(STATES) & clean["다음연도_상태"].isin(STATES)]
    return clean.reset_index(drop=True)


def build_transition_counts(df: pd.DataFrame) -> pd.DataFrame:
    """
    전이 카운트 행렬 (행=현재상태, 열=다음연도_상태) 생성.
    모든 상태를 빠짐없이 포함하도록 reindex.
    """
    counts = (
        df.groupby(["현재상태", "다음연도_상태"]).size().unstack(fill_value=0)
    )
    # 모든 상태가 행/열에 다 있도록 정렬·보강
    counts = counts.reindex(index=STATES, columns=STATES, fill_value=0)
    return counts


def get_initial_headcount(df: pd.DataFrame, base_year: int) -> pd.Series:
    """
    기준연도(투영 시작연도) 시점의 재직 인원벡터 n(0) — 상태별 인원수.

    ⚠️ 이력 패널은 여러 해의 전이를 풀링하므로, n(0)는 전체를 세면 안 되고
       'base_year 시점 스냅샷'만 집계해야 한다. (generate_dummy.get_base_year_headcount)
    """
    return get_base_year_headcount(df, int(base_year))

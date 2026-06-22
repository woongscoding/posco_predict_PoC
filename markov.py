"""
markov.py — 마르코프 모델링 (Stage 2 / 4)
==================================================
- 상태 정의
- 전이행렬 P 추정 (전이카운트 행 정규화 + 라플라스 평활)
- 연도별 투영 n(t+1) = n(t)·P
- 외부 조정계수로 행렬 재조정 (Stage 4)

⚠️ 상태 해상도 확장 지점:
   STATES 리스트만 바꾸면 직급/근속밴드를 추가해 모델을 확장할 수 있다.
   (generate_dummy.py 의 가정 테이블도 함께 맞춰야 함)
"""

from __future__ import annotations
import numpy as np
import pandas as pd

# =============================================================
# 상태 정의 (7개) — '이탈'은 흡수상태(absorbing)
# 근속밴드를 상태에 넣어 duration-aware(준마르코프) 설계
# =============================================================
STATES = [
    "사원_0-2년",
    "사원_2년+",
    "대리_0-2년",
    "대리_2년+",
    "차장_0-2년",
    "차장_2년+",
    "이탈",
]
ABSORBING = "이탈"

# 라플라스 평활 강도. 카운트에 +ALPHA 후 정규화.
# ★ 실데이터에서 표본이 적을 때(0 관측 전이를 0%로 박제하지 않게) 작동하는 안정화 장치.
LAPLACE_ALPHA = 0.5


def estimate_transition_matrix(counts: pd.DataFrame,
                               alpha: float = LAPLACE_ALPHA) -> pd.DataFrame:
    """
    전이 카운트 행렬 → 전이확률행렬 P (행 기준 정규화).

    - 라플라스 평활: (count + alpha) / (row_sum + alpha * n_states)
      0 관측 전이도 아주 작은 확률을 갖게 해 과적합 방지.
    - 흡수상태('이탈')는 행을 [..., 1] 로 고정(이탈→이탈=1).
    """
    c = counts.reindex(index=STATES, columns=STATES, fill_value=0).astype(float)
    n = len(STATES)

    smoothed = c.values + alpha
    row_sums = smoothed.sum(axis=1, keepdims=True)
    P = smoothed / row_sums

    P = pd.DataFrame(P, index=STATES, columns=STATES)

    # 흡수상태 강제: 이탈 → 이탈 = 1
    P.loc[ABSORBING, :] = 0.0
    P.loc[ABSORBING, ABSORBING] = 1.0
    return P


def project(n0: pd.Series, P: pd.DataFrame,
            base_year: int, target_year: int) -> pd.DataFrame:
    """
    인력벡터 투영: n(t+1) = n(t) · P 를 기준→목표연도까지 연도별 반복.

    Returns
    -------
    DataFrame[index=연도, columns=상태] — 연도별 상태 인원
    """
    states = list(P.index)
    n = n0.reindex(states, fill_value=0).values.astype(float).copy()
    Pv = P.values

    rows = {base_year: n.copy()}
    for year in range(base_year + 1, target_year + 1):
        n = n @ Pv
        rows[year] = n.copy()

    out = pd.DataFrame.from_dict(rows, orient="index", columns=states)
    out.index.name = "연도"
    return out


def adjust_matrix(P: pd.DataFrame, adjustments: list[dict]) -> pd.DataFrame:
    """
    외부 조정계수를 P 사본에 적용 (Stage 4).

    adjustments: research_agent 가 추출한 매핑 리스트
        [{"from": "대리_2년+", "to": "이탈", "delta_pp": +5.0}, ...]
        delta_pp 는 '퍼센트포인트'. +5.0 이면 해당 전이확률 += 0.05.

    적용 후 흡수상태 외 각 행을 다시 정규화(합=1 유지).
    ★ 조정은 '명시적 가정(시나리오 레버)'이다 — 모델이 자동 보정한 것이 아님.
    """
    P2 = P.copy()

    for adj in adjustments:
        f, t, delta = adj["from"], adj["to"], adj["delta_pp"] / 100.0
        if f in P2.index and t in P2.columns and f != ABSORBING:
            P2.loc[f, t] = np.clip(P2.loc[f, t] + delta, 0.0, 1.0)

    # 흡수상태 외 행 재정규화 (합=1 유지)
    for s in STATES:
        if s == ABSORBING:
            P2.loc[s, :] = 0.0
            P2.loc[s, ABSORBING] = 1.0
            continue
        row_sum = P2.loc[s, :].sum()
        if row_sum > 0:
            P2.loc[s, :] = P2.loc[s, :] / row_sum
    return P2


def survivors_by_rank(projection: pd.DataFrame) -> pd.DataFrame:
    """연도별 직급 단위 합계(이탈 제외) — 직급별 추이/갭 분석용."""
    rank_map = {
        "사원_0-2년": "사원", "사원_2년+": "사원",
        "대리_0-2년": "대리", "대리_2년+": "대리",
        "차장_0-2년": "차장", "차장_2년+": "차장",
    }
    cols = [c for c in projection.columns if c != ABSORBING]
    sub = projection[cols].copy()
    grouped = {}
    for state, rank in rank_map.items():
        grouped.setdefault(rank, []).append(state)
    out = pd.DataFrame(index=projection.index)
    for rank, states in grouped.items():
        out[rank] = sub[states].sum(axis=1)
    out.index.name = "연도"
    return out


def total_active(projection: pd.DataFrame) -> pd.Series:
    """연도별 총 재직 인원(이탈 제외)."""
    cols = [c for c in projection.columns if c != ABSORBING]
    return projection[cols].sum(axis=1)

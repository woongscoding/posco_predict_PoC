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

    ★ 명목 delta_pp 보존 방식 (표시 = 적용):
       대상 셀을 (old + delta)로 '고정'하고, 행의 '나머지 셀들'만 비례 축소/확대해
       합=1을 맞춘다. 행 전체를 재정규화하면 대상 셀도 같이 줄어 명목 delta가 희석되는데
       (예: 매핑테이블 +5.0%p ↔ diff 히트맵 +4.6%p 불일치), 이 방식은 대상 전이의 변화폭이
       heatmap_diff 에 명목값 그대로 찍히게 한다 — '블랙박스 아님' 셀링포인트와 정합.
    ★ 조정은 '명시적 가정(시나리오 레버)'이다 — 모델이 자동 보정한 것이 아님.
    """
    P2 = P.copy()

    # 같은 from-행의 여러 조정을 모아 한 번에 처리 (상호 희석 방지)
    by_row: dict[str, dict[str, float]] = {}
    for adj in adjustments:
        f, t, delta = adj["from"], adj["to"], adj["delta_pp"] / 100.0
        if f in P2.index and t in P2.columns and f != ABSORBING:
            by_row.setdefault(f, {})[t] = by_row.get(f, {}).get(t, 0.0) + delta

    for f, targets in by_row.items():
        row = P2.loc[f, :].copy()
        # 대상 셀들을 old+delta 로 고정 (0~1 클립)
        for t, delta in targets.items():
            row[t] = float(np.clip(row[t] + delta, 0.0, 1.0))
        fixed_sum = sum(row[t] for t in targets)
        other_cols = [c for c in P2.columns if c not in targets]
        old_other_sum = float(P2.loc[f, other_cols].sum())
        residual = 1.0 - fixed_sum
        if old_other_sum > 1e-12 and residual >= 0.0:
            # 나머지 셀만 비례 조정 → 대상 셀의 명목 delta 그대로 보존
            scale = residual / old_other_sum
            for c in other_cols:
                row[c] = P2.loc[f, c] * scale
        else:
            # 엣지(대상 합이 1 초과 등): 안전하게 행 전체 재정규화로 폴백
            tot = row.sum()
            if tot > 0:
                row = row / tot
        P2.loc[f, :] = row

    # 흡수상태 강제: 이탈 → 이탈 = 1
    P2.loc[ABSORBING, :] = 0.0
    P2.loc[ABSORBING, ABSORBING] = 1.0
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

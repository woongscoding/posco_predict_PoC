"""
snapshots.py — M3 스냅샷 저장·비교 (순수 로직, Streamlit 비의존)
==================================================
여러 변수 조합의 시뮬 결과를 캡처해 나란히 비교하기 위한 데이터 구조·라벨·비교표.

설계 원칙:
  - Streamlit 에 의존하지 않는다. session_state 조작은 sim_app.py 담당.
  - sim_core 의 Adjustments·SimResult 를 재사용한다(중복 정의 금지).
  - 캡처 시점에 파라미터·결과를 copy.deepcopy 로 '박제'해, 이후 슬라이더를 다시
    움직여도 저장된 스냅샷이 따라 바뀌지 않게 한다(참조 얽힘 차단).

★ baseline 기준선 일관성 (보완 1):
  baseline = build_default_params(years) 를 '무조정'으로 run 한 결과이며,
  build_default_params 는 외부 상태가 없는 순수 상수 함수다 → 동일 years 에서 불변.
  따라서 각 스냅샷에 저장된 result.cum_cost_delta_vs_baseline
  (그 스냅샷의 sim 누적비용 − 같은 horizon 의 무조정 baseline 누적비용)을 그대로 쓴다.
  years 가 다르면 horizon 자체가 달라 사과-오렌지가 되므로, comparison_table 에
  '연수' 열을 노출해 서로 다른 기준선을 숨기지 않는다(순수 함수라 재계산해도 동일값).
"""
from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import uuid

import pandas as pd

import sim_core as sc

# 슬라이더 기본값 = baseline 판정 기준 (sim_app 슬라이더 기본과 일치해야 함).
#   raise 기본 0.0% 는 build_default_params(raise_rate=0.0) 와 일치.
DEFAULT_PROMO_PCT = 0
DEFAULT_ATTR_PCT = 0
DEFAULT_RAISE_PCT = 0.0


@dataclass
class Snapshot:
    snapshot_id: str                 # uuid4().hex[:8] — 삭제·복원·중복라벨 구분
    label: str                       # 자동 생성, 수정 가능
    controls: dict                   # {years, promo_pct, attr_pct, raise_rate_pct} ← 복원 소스
    adjustments: sc.Adjustments      # 레버 조합 전체 (deepcopy 박제)
    result: sc.SimResult             # 총원·인건비 시계열 + 누적 Δ (deepcopy 박제)
    # 파생 비교 지표 (Δ KPI 와 동일 정의 재사용)
    final_total: float               # 최종연도 총원
    cum_cost_delta: float            # baseline 대비 누적 인건비 Δ (KRW)
    top_share: float                 # 상위단계 비중(%) — sc.top_level_share 정의


# =============================================================
# 라벨 자동 생성 (보완 3)
# =============================================================
def make_label(years: int, promo_pct: float, attr_pct: float,
               raise_rate_pct: float) -> str:
    """baseline 과 다른 레버만 골라 라벨 생성. 전부 기본이면 'baseline'.

    - 배율 레버(승진율·퇴직률): 기본 대비 증감 %로. 예 '승진 +30%', '퇴직 -20%'.
      (부호는 파이썬 정수 포맷의 ASCII 하이픈-마이너스 '-' 사용)
    - 인건비 인상률: rate 그대로, 소수부 없으면 정수로. 예 '인상 5%' (5.0% 아님).
    - 0 또는 기본값인 레버는 라벨에서 생략.
    """
    parts: list[str] = []
    if abs(promo_pct - DEFAULT_PROMO_PCT) > 1e-9:
        parts.append(f"승진 {promo_pct:+g}%")
    if abs(attr_pct - DEFAULT_ATTR_PCT) > 1e-9:
        parts.append(f"퇴직 {attr_pct:+g}%")
    if abs(raise_rate_pct - DEFAULT_RAISE_PCT) > 1e-9:
        r = raise_rate_pct
        rtxt = str(int(round(r))) if abs(r - round(r)) < 1e-9 else f"{r:g}"
        parts.append(f"인상 {rtxt}%")
    return " / ".join(parts) if parts else "baseline"


# =============================================================
# 캡처 (deepcopy 박제 + 파생지표 계산)
# =============================================================
def capture(label: str, controls: dict,
            adjustments: sc.Adjustments, result: sc.SimResult) -> Snapshot:
    """현재 조합을 스냅샷으로 박제. 이후 원본이 바뀌어도 스냅샷은 불변."""
    frozen_adj = deepcopy(adjustments)
    frozen_res = deepcopy(result)
    end = frozen_res.headcount_by_year[-1]
    return Snapshot(
        snapshot_id=uuid.uuid4().hex[:8],
        label=label,
        controls=dict(controls),                      # 스칼라 dict — 얕은 복사로 충분
        adjustments=frozen_adj,
        result=frozen_res,
        final_total=sc.total_headcount(end),
        cum_cost_delta=frozen_res.cum_cost_delta_vs_baseline,
        top_share=sc.top_level_share(end),
    )


# =============================================================
# 비교표 (열: 라벨 / 연수 / 최종총원 / 누적Δ / 상위비중)
# =============================================================
def dedup_labels(labels: list[str]) -> list[str]:
    """동일 라벨이 겹치면 뒤에 (2),(3)… 를 붙여 표시용으로 구분."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for lbl in labels:
        if lbl in seen:
            seen[lbl] += 1
            out.append(f"{lbl} ({seen[lbl]})")
        else:
            seen[lbl] = 1
            out.append(lbl)
    return out


def comparison_table(snapshots: list[Snapshot]) -> pd.DataFrame:
    """저장된 스냅샷들을 한 표로. baseline 스냅샷(Δ≈0)의 Δ는 '—' 로 표기."""
    labels = dedup_labels([s.label for s in snapshots])
    rows = []
    for s, lbl in zip(snapshots, labels):
        # Δ<1원(=사실상 0)은 baseline 스냅샷 → '—'
        delta_txt = "—" if abs(s.cum_cost_delta) < 1.0 else f"{s.cum_cost_delta / 1e8:+,.0f}억"
        rows.append({
            "라벨": lbl,
            "연수": s.controls["years"],
            "최종연도 총원": f"{s.final_total:,.0f}명",
            "누적 인건비 Δ": delta_txt,
            "상위단계 비중": f"{s.top_share:.1f}%",
        })
    return pd.DataFrame(rows)

"""
sim_core.py — v3 결정론 시뮬레이션 코어 (M1)
==================================================
직군 4종(P/R/E/A) × 단계별 마르코프 추계 + 인건비(인상률 반영) + baseline 대비 Δ.

설계 원칙(기획안 v3):
  - 결정론 우선: 마르코프 전이·인건비 계산은 전부 가벼운 rule 함수. LLM 개입 없음.
  - 마르코프 정합성: 각 (직군 f, 단계 i)에서  stay + promotion + attrition = 1.
      stay(f,i) = 1 - attrition(f,i) - promotion(f,i)   (반드시 0 이상)
  - 최상위 단계(P4/CL6/E7/A3)는 승진율 0 (§A3).
  - 채용은 '전이 이후' 가산 (§2-4, A5): 당해 채용자는 그 해 전이 미적용, 연말 합산.

더미데이터는 아래 build_default_params() 에 인라인 하드코딩(나중에 정교화).
조정 레버(퇴직/승진/인상률)는 Adjustments 로 baseline 파라미터에 적용한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from copy import deepcopy


# =============================================================
# 직군·단계 정의 (§2-1)
# =============================================================
FAMILY_LEVELS: dict[str, list[str]] = {
    "P": ["P1", "P2", "P3", "P4"],
    "R": ["CL1", "CL2", "CL3", "CL4", "CL5", "CL6"],
    "E": ["E1", "E2", "E3", "E4", "E5", "E6", "E7"],
    "A": ["A1", "A2", "A3"],
}
FAMILY_LABEL = {"P": "Professional", "R": "Research", "E": "Engineering", "A": "Admin"}


@dataclass
class SimParams:
    """(직군, 단계) 단위 더미값 묶음. 조정 레버는 여기 값을 바꿔 만든 사본으로 표현."""
    # {직군: {단계: 값}}
    initial_headcount: dict[str, dict[str, float]]
    annual_salary: dict[str, dict[str, float]]      # 연차 0 기준 단가(KRW)
    attrition_rate: dict[str, dict[str, float]]     # 고정 가정값(조정 대상 가능)
    promotion_rate: dict[str, dict[str, float]]     # 슬라이더 조정 대상
    hiring_plan: dict[str, dict[str, list[float]]]  # {직군:{단계:[연차별 채용수]}}
    years: int = 5
    raise_rate: float = 0.0                          # 전체 일괄 인상률(폴백용 스칼라)
    # 직급별(단계별) 연 인상률. 있으면 이게 우선, 없으면 위 스칼라를 전 직급에 동일 적용.
    raise_rate_by_level: dict[str, dict[str, float]] | None = None

    @property
    def families(self) -> list[str]:
        return list(self.initial_headcount.keys())

    def levels(self, f: str) -> list[str]:
        return FAMILY_LEVELS[f]


@dataclass
class SimResult:
    # [연차][직군][단계] = 인원 (float 내부 유지, 표시 시 반올림)
    headcount_by_year: list[dict[str, dict[str, float]]]
    labor_cost_by_year: list[float]
    cum_cost_delta_vs_baseline: float = 0.0


# =============================================================
# 더미 기본 파라미터 (M1: 인라인 하드코딩, 나중에 정교화)
# =============================================================
def build_default_params(years: int = 5) -> SimParams:
    """직군·단계별 그럴듯한 플레이스홀더 값. 규모/단가/율은 데모용 예시값."""

    # 초기 인원: '모래시계형' — 실무 기반(하위)은 두껍고, 중간관리 계층(허리)이
    #   텅 비어 있으며, 고참·상위 계층은 남아 있는(하지만 기반보다는 작은) 구조.
    #   → 핵심 문제: '중간관리자 공동화'. 데모 목표는 허리를 채워 피라미드로 전환.
    #   (레버: 하위→중간 승진율↑ 로 허리 복원 시뮬레이션)
    initial = {
        "P": {"P1": 900, "P2": 480, "P3": 120, "P4": 300},
        "R": {"CL1": 420, "CL2": 240, "CL3": 70, "CL4": 60, "CL5": 130, "CL6": 90},
        "E": {"E1": 780, "E2": 520, "E3": 300, "E4": 110, "E5": 120, "E6": 260, "E7": 170},
        "A": {"A1": 320, "A2": 70, "A3": 150},
    }

    # 연봉 단가(백만원 단위가 아니라 원 단위, 연차 0 기준). 단계가 오를수록 상승.
    salary = {
        "P": {"P1": 42_000_000, "P2": 55_000_000, "P3": 72_000_000, "P4": 95_000_000},
        "R": {"CL1": 48_000_000, "CL2": 60_000_000, "CL3": 74_000_000,
              "CL4": 90_000_000, "CL5": 112_000_000, "CL6": 140_000_000},
        "E": {"E1": 40_000_000, "E2": 50_000_000, "E3": 62_000_000, "E4": 76_000_000,
              "E5": 92_000_000, "E6": 112_000_000, "E7": 138_000_000},
        "A": {"A1": 36_000_000, "A2": 46_000_000, "A3": 60_000_000},
    }

    # 퇴직률(고정 가정값) — 하위 단계에서 높고 상위로 갈수록 낮게.
    attrition = {
        "P": {"P1": 0.14, "P2": 0.11, "P3": 0.08, "P4": 0.06},
        "R": {"CL1": 0.13, "CL2": 0.11, "CL3": 0.09, "CL4": 0.07, "CL5": 0.06, "CL6": 0.05},
        "E": {"E1": 0.15, "E2": 0.12, "E3": 0.10, "E4": 0.08,
              "E5": 0.07, "E6": 0.06, "E7": 0.05},
        "A": {"A1": 0.16, "A2": 0.12, "A3": 0.09},
    }

    # 승진율 — 바로 위 단계로. 최상위(P4/CL6/E7/A3)는 0.
    promotion = {
        "P": {"P1": 0.18, "P2": 0.14, "P3": 0.10, "P4": 0.0},
        "R": {"CL1": 0.20, "CL2": 0.16, "CL3": 0.13, "CL4": 0.10, "CL5": 0.07, "CL6": 0.0},
        "E": {"E1": 0.20, "E2": 0.16, "E3": 0.13, "E4": 0.11,
              "E5": 0.08, "E6": 0.06, "E7": 0.0},
        "A": {"A1": 0.15, "A2": 0.10, "A3": 0.0},
    }

    # 채용 계획: 각 직군의 최하위 단계에 매년 일정 인원 투입(플레이스홀더).
    hiring = {
        "P": {"P1": [120] * years},
        "R": {"CL1": [40] * years},
        "E": {"E1": [100] * years},
        "A": {"A1": [30] * years},
    }

    _enforce_top_level_zero(promotion)
    return SimParams(
        initial_headcount=initial,
        annual_salary=salary,
        attrition_rate=attrition,
        promotion_rate=promotion,
        hiring_plan=hiring,
        years=years,
        raise_rate=0.0,   # baseline = '인상 0%' 기준선. 슬라이더로 올린 만큼 누적 Δ가 +로 잡힌다.
    )


def _enforce_top_level_zero(promotion: dict[str, dict[str, float]]) -> None:
    """각 직군 최상위 단계 승진율을 0으로 강제(§A3)."""
    for f, levels in FAMILY_LEVELS.items():
        if f in promotion and levels[-1] in promotion[f]:
            promotion[f][levels[-1]] = 0.0


# =============================================================
# 조정 레버 (퇴직/승진/인상률) → 파라미터 사본 생성
# =============================================================
@dataclass
class Adjustments:
    """slider 조정치. baseline 파라미터에 곱/치환으로 적용해 시뮬 파라미터를 만든다.

    - promotion_scale: 승진율 배율 (1.0 = 변화 없음)
    - attrition_scale: 퇴직률 배율 (1.0 = 변화 없음)
    - raise_rate: 인건비 인상률(절대값). None 이면 baseline 값 유지.
    - promotion_scale_by_family / attrition_scale_by_family: 직군별 세부 배율(선택).
    - attrition_scale_by_level: {직군:{단계:배율}} 셀 단위 퇴직률 배율(선택).
      직급별/나이별 퇴직률 조정은 앱에서 이 dict 로 환산해 내려보낸다.
    """
    promotion_scale: float = 1.0
    attrition_scale: float = 1.0
    raise_rate: float | None = None
    raise_rate_by_level: dict[str, dict[str, float]] | None = None  # 직급별 인상률(있으면 우선)
    promotion_scale_by_family: dict[str, float] = field(default_factory=dict)
    attrition_scale_by_family: dict[str, float] = field(default_factory=dict)
    attrition_scale_by_level: dict[str, dict[str, float]] | None = None

    def is_identity(self) -> bool:
        return (
            abs(self.promotion_scale - 1.0) < 1e-9
            and abs(self.attrition_scale - 1.0) < 1e-9
            and self.raise_rate is None
            and not self.raise_rate_by_level
            and not self.promotion_scale_by_family
            and not self.attrition_scale_by_family
            and not self.attrition_scale_by_level
        )


def apply_adjustments(base: SimParams, adj: Adjustments) -> SimParams:
    """조정 레버를 적용한 새 SimParams 반환. stay>=0 을 셀 단위로 보장(클립).

    적용 순서:
      1) 퇴직률 = clip(attrition * scale, 0, 1)
      2) 승진율 = clip(promotion * scale, 0, 1 - attrition')   # stay>=0 보장
      3) 최상위 단계 승진율 0 강제
      4) raise_rate 치환(있으면)
    """
    attrition = deepcopy(base.attrition_rate)
    promotion = deepcopy(base.promotion_rate)

    for f in base.families:
        a_scale = adj.attrition_scale * adj.attrition_scale_by_family.get(f, 1.0)
        p_scale = adj.promotion_scale * adj.promotion_scale_by_family.get(f, 1.0)
        a_by_lvl = (adj.attrition_scale_by_level or {}).get(f, {})
        for lvl in base.levels(f):
            a = _clip(attrition[f][lvl] * a_scale * a_by_lvl.get(lvl, 1.0), 0.0, 1.0)
            attrition[f][lvl] = a
            # 승진율은 재직률이 음수가 되지 않도록 (1 - attrition') 이하로 제한
            p_cap = max(0.0, 1.0 - a)
            promotion[f][lvl] = _clip(promotion[f][lvl] * p_scale, 0.0, p_cap)

    _enforce_top_level_zero(promotion)

    return replace(
        base,
        attrition_rate=attrition,
        promotion_rate=promotion,
        raise_rate=base.raise_rate if adj.raise_rate is None else adj.raise_rate,
        raise_rate_by_level=(adj.raise_rate_by_level
                             if adj.raise_rate_by_level is not None
                             else base.raise_rate_by_level),
    )


def _clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


# =============================================================
# 정합성 검증 (M1 완료 기준: 합=1, stay>=0)
# =============================================================
def validate(params: SimParams, tol: float = 1e-9) -> list[str]:
    """위반 사항 문자열 리스트 반환(빈 리스트 = 통과)."""
    problems: list[str] = []
    for f in params.families:
        for i, lvl in enumerate(params.levels(f)):
            a = params.attrition_rate[f][lvl]
            p = params.promotion_rate[f][lvl]
            stay = 1.0 - a - p
            if stay < -tol:
                problems.append(f"[{f}/{lvl}] stay<0: 1-{a:.3f}-{p:.3f}={stay:.3f}")
            if abs((stay + a + p) - 1.0) > 1e-6:
                problems.append(f"[{f}/{lvl}] 합≠1: {stay + a + p:.6f}")
            if i == len(params.levels(f)) - 1 and abs(p) > tol:
                problems.append(f"[{f}/{lvl}] 최상위 승진율≠0: {p:.3f}")
    return problems


# =============================================================
# 마르코프 추계 (직군별 독립 전이) §6
# =============================================================
def simulate_family(levels: list[str],
                    init: dict[str, float],
                    promotion: dict[str, float],
                    attrition: dict[str, float],
                    hiring_of_family: dict[str, list[float]],
                    years: int) -> list[dict[str, float]]:
    """한 직군의 연도별 단계 인원 벡터 이력 반환. [연0 .. 연N]."""
    k = len(levels)
    n = [float(init.get(lvl, 0.0)) for lvl in levels]
    history = [dict(zip(levels, n))]

    for t in range(1, years + 1):
        nxt = [0.0] * k
        for i, lvl in enumerate(levels):
            promote_i = promotion.get(lvl, 0.0) if i < k - 1 else 0.0
            stay_i = 1.0 - attrition.get(lvl, 0.0) - promote_i
            nxt[i] += n[i] * stay_i               # 재직
            if i < k - 1:
                nxt[i + 1] += n[i] * promote_i    # 승진(바로 위 단계)
            # 퇴직분(n[i]*attrition)은 이탈 → 어디에도 더하지 않음
        # 채용: ★ 전이 후 가산 (§2-4, A5)
        for lvl, plan in hiring_of_family.items():
            if lvl in levels and t - 1 < len(plan):
                nxt[levels.index(lvl)] += plan[t - 1]
        n = nxt
        history.append(dict(zip(levels, n)))
    return history


def simulate(params: SimParams) -> list[dict[str, dict[str, float]]]:
    """전 직군 추계. 반환: [연차][직군][단계] = 인원."""
    per_family = {
        f: simulate_family(
            params.levels(f),
            params.initial_headcount[f],
            params.promotion_rate[f],
            params.attrition_rate[f],
            params.hiring_plan.get(f, {}),
            params.years,
        )
        for f in params.families
    }
    # [연차] 축으로 재조립
    out: list[dict[str, dict[str, float]]] = []
    for t in range(params.years + 1):
        out.append({f: per_family[f][t] for f in params.families})
    return out


# =============================================================
# 인건비 (인상률 변수 반영) §6
# =============================================================
def _raise_for(raise_spec, f: str, lvl: str) -> float:
    """raise_spec 이 직급별 dict 면 해당 (직군,단계) 값을, 스칼라면 그 값을 반환."""
    if isinstance(raise_spec, dict):
        return raise_spec.get(f, {}).get(lvl, 0.0)
    return raise_spec


def labor_cost_of_year(headcount_year: dict[str, dict[str, float]],
                       salary: dict[str, dict[str, float]],
                       raise_spec, t: int) -> float:
    """raise_spec: float(전 직급 동일) 또는 {직군:{단계:인상률}} 직급별."""
    total = 0.0
    for f, levels in headcount_year.items():
        for lvl, headcount in levels.items():
            unit = salary[f][lvl] * (1.0 + _raise_for(raise_spec, f, lvl)) ** t   # §A8
            total += headcount * unit
    return total


def labor_cost_series(history: list[dict[str, dict[str, float]]],
                      params: SimParams) -> list[float]:
    # 직급별 인상률이 있으면 우선 적용, 없으면 스칼라 raise_rate 를 전 직급에 동일 적용.
    raise_spec = params.raise_rate_by_level or params.raise_rate
    return [
        labor_cost_of_year(history[t], params.annual_salary, raise_spec, t)
        for t in range(params.years + 1)
    ]


def cum_cost_delta(sim_series: list[float], baseline_series: list[float]) -> float:
    return sum(sim_series) - sum(baseline_series)


def run(params: SimParams, baseline_cost: list[float] | None = None) -> SimResult:
    """파라미터 → 추계 + 인건비 + (baseline 있으면) 누적 Δ."""
    history = simulate(params)
    cost = labor_cost_series(history, params)
    delta = cum_cost_delta(cost, baseline_cost) if baseline_cost is not None else 0.0
    return SimResult(headcount_by_year=history, labor_cost_by_year=cost,
                     cum_cost_delta_vs_baseline=delta)


# =============================================================
# 정년 재채용 (촉탁) — 표시용 파생 지표
# =============================================================
# 가정(더미): 각 직군 최상위 단계 이탈(attrition) 중 RETIRE_SHARE 는 '정년퇴직'이고,
#   그중 REHIRE_RATE 만큼을 촉탁(계약직)으로 재채용한다.
# 재채용 인원은 별도 촉탁 풀로 보고 본 추계 headcount 에는 합산하지 않는다(표시 전용).
RETIRE_SHARE = 0.6   # 최상위 단계 이탈 중 정년퇴직 비율(가정값)
REHIRE_RATE = 0.5    # 정년퇴직자 중 재채용 비율(가정값)


def rehire_by_year(history: list[dict[str, dict[str, float]]],
                   attrition_rate: dict[str, dict[str, float]],
                   retire_share: float = RETIRE_SHARE,
                   rehire_rate: float = REHIRE_RATE) -> list[float]:
    """연차별 정년 재채용 인원. [연0(=0), 연1, ..., 연N].
    연차 t 재채용 = Σ직군 (전년도 최상위 단계 인원 × 그 단계 퇴직률 × 정년비율 × 재채용률)."""
    out = [0.0]
    for t in range(1, len(history)):
        prev = history[t - 1]
        cnt = 0.0
        for f, levels in FAMILY_LEVELS.items():
            top = levels[-1]
            cnt += (prev.get(f, {}).get(top, 0.0)
                    * attrition_rate.get(f, {}).get(top, 0.0)
                    * retire_share * rehire_rate)
        out.append(cnt)
    return out


# =============================================================
# 표시 편의 함수
# =============================================================
def total_headcount(headcount_year: dict[str, dict[str, float]]) -> float:
    return sum(sum(levels.values()) for levels in headcount_year.values())


def headcount_by_family(headcount_year: dict[str, dict[str, float]]) -> dict[str, float]:
    return {f: sum(levels.values()) for f, levels in headcount_year.items()}


def top_level_share(headcount_year: dict[str, dict[str, float]]) -> float:
    """상위단계 비중(%). 정의(고정):
        각 직군 최상위 단계(P4·CL6·E7·A3) 인원의 합 ÷ 전체 인원 × 100.
    직군마다 단계 수(3~7)가 달라 '상위'가 모호하므로 이 정의로 못박는다."""
    top = 0.0
    for f, levels in headcount_year.items():
        top_lvl = FAMILY_LEVELS[f][-1]
        top += levels.get(top_lvl, 0.0)
    tot = total_headcount(headcount_year)
    return (top / tot * 100.0) if tot > 0 else 0.0


# =============================================================
# 자체 점검 (M1 완료 기준 확인):  python sim_core.py
# =============================================================
if __name__ == "__main__":
    base = build_default_params(years=5)
    problems = validate(base)
    assert not problems, "baseline 정합성 위반:\n" + "\n".join(problems)

    baseline = run(base)
    print("== baseline ==")
    for t, (hc, cost) in enumerate(zip(baseline.headcount_by_year,
                                       baseline.labor_cost_by_year)):
        print(f"  연차 {t}: 총원 {total_headcount(hc):8.0f}명 | "
              f"인건비 {cost/1e8:10.1f}억 | 상위비중 {top_level_share(hc):4.1f}%")

    # 조정: 승진율 +30%, 퇴직률 -20%, 인상률 5%
    adj = Adjustments(promotion_scale=1.3, attrition_scale=0.8, raise_rate=0.05)
    sim_params = apply_adjustments(base, adj)
    assert not validate(sim_params), "조정 후 정합성 위반"
    sim = run(sim_params, baseline_cost=baseline.labor_cost_by_year)

    print("\n== 시뮬(승진+30% / 퇴직-20% / 인상5%) ==")
    for t, (hc, cost) in enumerate(zip(sim.headcount_by_year, sim.labor_cost_by_year)):
        print(f"  연차 {t}: 총원 {total_headcount(hc):8.0f}명 | "
              f"인건비 {cost/1e8:10.1f}억 | 상위비중 {top_level_share(hc):4.1f}%")
    print(f"\n누적 인건비 Δ vs baseline: {sim.cum_cost_delta_vs_baseline/1e8:+.1f}억")
    print("[OK] 정합성(합=1·stay>=0·최상위 승진0) 통과")

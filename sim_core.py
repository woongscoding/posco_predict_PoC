"""
sim_core.py — v4 결정론 시뮬레이션 코어
==================================================
직원 직급 6단계(사원→대리→과장→차장→리더→부장, 임원 제외) × 조직 4개
마르코프 추계 + 인건비(직급별 인상률) + 정년퇴직·재채용 + 연도별 예측 퇴직 인원.

설계 원칙:
  - 결정론 우선: 전이·인건비 계산은 전부 가벼운 rule 함수. LLM 개입 없음.
  - 마르코프 정합성: 각 (조직 f, 직급 g)에서  stay + promotion + attrition = 1.
      stay(f,g) = 1 - attrition(f,g) - promotion(f,g)   (반드시 0 이상)
  - 최상위 직급(부장)은 승진율 0 — 임원 승진은 범위 밖(임원 미고려).
  - 채용·정년 재채용은 '전이 이후' 가산: 당해 유입 인원은 그 해 전이 미적용.
  - 정년퇴직은 퇴직률의 부분집합(나이 기인 비자발 이탈). 재채용 인원은
    같은 직급으로 복귀(촉탁 가정)해 다음 해부터 전이에 참여한다.

더미데이터는 build_default_params() 에 인라인 하드코딩(목업).
조정 레버(승진/퇴직/인상률/재채용률)는 Adjustments 로 baseline 에 적용한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from copy import deepcopy


# =============================================================
# 직급(사원→부장, 임원 제외) · 조직 정의
# =============================================================
GRADES: list[str] = ["사원", "대리", "과장", "차장", "리더", "부장"]  # 하위→상위

FAMILY_LEVELS: dict[str, list[str]] = {f: list(GRADES) for f in ("P", "R", "E", "A")}
FAMILY_LABEL = {"P": "생산", "R": "연구개발", "E": "엔지니어링", "A": "경영지원"}

# 나이대(연령 밴드) — 직급별 나이 구성비(가정값). 나이별 퇴직률 조정 레버는
# 이 구성비를 가중치로 직급별 퇴직률 배율에 환산해 적용한다(코호트 추적 없는 목업 근사).
AGE_BANDS: list[str] = ["20대", "30대", "40대", "50대+"]
AGE_MIX: dict[str, dict[str, float]] = {
    "사원": {"20대": 0.55, "30대": 0.40, "40대": 0.05, "50대+": 0.00},
    "대리": {"20대": 0.15, "30대": 0.65, "40대": 0.20, "50대+": 0.00},
    "과장": {"20대": 0.00, "30대": 0.35, "40대": 0.55, "50대+": 0.10},
    "차장": {"20대": 0.00, "30대": 0.10, "40대": 0.60, "50대+": 0.30},
    "리더": {"20대": 0.00, "30대": 0.00, "40대": 0.45, "50대+": 0.55},
    "부장": {"20대": 0.00, "30대": 0.00, "40대": 0.25, "50대+": 0.75},
}

# 정년도래율(가정값) — 매년 해당 직급 인원 중 정년에 도달하는 비율.
# 정년퇴직은 퇴직률(attrition)의 부분집합이므로 attrition >= RETIRE_RATE 가 유지돼야 한다.
RETIRE_RATE: dict[str, float] = {
    "사원": 0.0, "대리": 0.0, "과장": 0.005, "차장": 0.01, "리더": 0.03, "부장": 0.06,
}

DEFAULT_REHIRE_RATE = 0.30  # baseline 정년 재채용률(정년퇴직자 중 촉탁 재채용 비율)


@dataclass
class SimParams:
    """(조직, 직급) 단위 더미값 묶음. 조정 레버는 여기 값을 바꿔 만든 사본으로 표현."""
    # {조직: {직급: 값}}
    initial_headcount: dict[str, dict[str, float]]
    annual_salary: dict[str, dict[str, float]]      # 연차 0 기준 단가(KRW)
    attrition_rate: dict[str, dict[str, float]]     # 예측 퇴직률(정년 포함, 조정 대상)
    promotion_rate: dict[str, dict[str, float]]     # 직급별로 다름(조정 대상)
    hiring_plan: dict[str, dict[str, list[float]]]  # {조직:{직급:[연차별 채용수]}}
    years: int = 5
    raise_rate: float = 0.0                          # 전체 일괄 인상률(폴백용 스칼라)
    # 직급별 연 인상률. 있으면 이게 우선, 없으면 위 스칼라를 전 직급에 동일 적용.
    raise_rate_by_level: dict[str, dict[str, float]] | None = None
    rehire_rate: float = DEFAULT_REHIRE_RATE         # 정년 재채용률(조정 대상)

    @property
    def families(self) -> list[str]:
        return list(self.initial_headcount.keys())

    def levels(self, f: str) -> list[str]:
        return FAMILY_LEVELS[f]


@dataclass
class SimResult:
    # [연차][조직][직급] = 인원 (float 내부 유지, 표시 시 반올림)
    headcount_by_year: list[dict[str, dict[str, float]]]
    labor_cost_by_year: list[float]
    # 예측 퇴직 인원(정년 포함): [연차][조직] = 그 해 이탈 인원. 연차 0 은 0.
    attrition_heads_by_year: list[dict[str, float]] = field(default_factory=list)
    retire_heads_by_year: list[float] = field(default_factory=list)   # 연도별 정년퇴직(예상)
    rehire_heads_by_year: list[float] = field(default_factory=list)   # 연도별 정년 재채용
    cum_cost_delta_vs_baseline: float = 0.0


# =============================================================
# 더미 기본 파라미터 (인라인 하드코딩, 목업)
# =============================================================
def build_default_params(years: int = 5) -> SimParams:
    """조직·직급별 그럴듯한 플레이스홀더 값. 규모/단가/율은 데모용 예시값."""

    # 초기 인원: '모래시계형' — 사원·대리(기반)는 두껍고, 과장·차장(허리)이
    #   공동화돼 있으며, 리더·부장(고참)은 남아 있는 구조.
    #   → 핵심 문제: '중간관리자 공백'. 데모 목표는 허리를 채워 피라미드로 전환.
    initial = {
        "P": {"사원": 620, "대리": 380, "과장": 90, "차장": 80, "리더": 210, "부장": 160},
        "R": {"사원": 300, "대리": 190, "과장": 45, "차장": 40, "리더": 100, "부장": 75},
        "E": {"사원": 520, "대리": 330, "과장": 80, "차장": 70, "리더": 180, "부장": 140},
        "A": {"사원": 160, "대리": 90, "과장": 25, "차장": 20, "리더": 55, "부장": 45},
    }

    # 연봉 단가(원, 연차 0 기준). 직급이 오를수록 상승, 조직별 계수 차등.
    base_salary = {"사원": 42_000_000, "대리": 52_000_000, "과장": 65_000_000,
                   "차장": 80_000_000, "리더": 96_000_000, "부장": 115_000_000}
    fam_factor = {"P": 1.00, "R": 1.10, "E": 1.02, "A": 0.92}
    salary = {f: {g: round(base_salary[g] * k) for g in GRADES}
              for f, k in fam_factor.items()}

    # 예측 퇴직률(정년 포함) — 하위 직급에서 높고(이직), 부장은 정년 영향으로 재상승.
    base_attr = {"사원": 0.14, "대리": 0.11, "과장": 0.08,
                 "차장": 0.07, "리더": 0.07, "부장": 0.09}
    attrition = {f: dict(base_attr) for f in FAMILY_LEVELS}

    # 승진율 — 직급별로 다름(바로 위 직급으로). 최상위(부장)는 0(임원 미고려).
    base_promo = {"사원": 0.16, "대리": 0.13, "과장": 0.10,
                  "차장": 0.08, "리더": 0.05, "부장": 0.0}
    promotion = {f: dict(base_promo) for f in FAMILY_LEVELS}

    # 채용 계획: 각 조직의 사원 직급에 매년 일정 인원 투입(플레이스홀더).
    hiring = {
        "P": {"사원": [90] * years},
        "R": {"사원": [40] * years},
        "E": {"사원": [80] * years},
        "A": {"사원": [25] * years},
    }

    _enforce_top_level_zero(promotion)
    return SimParams(
        initial_headcount=initial,
        annual_salary=salary,
        attrition_rate=attrition,
        promotion_rate=promotion,
        hiring_plan=hiring,
        years=years,
        raise_rate=0.0,   # baseline = '인상 0%' 기준선. 올린 만큼 누적 Δ가 +로 잡힌다.
        rehire_rate=DEFAULT_REHIRE_RATE,
    )


def _enforce_top_level_zero(promotion: dict[str, dict[str, float]]) -> None:
    """각 조직 최상위 직급(부장) 승진율을 0으로 강제(임원 미고려)."""
    for f, levels in FAMILY_LEVELS.items():
        if f in promotion and levels[-1] in promotion[f]:
            promotion[f][levels[-1]] = 0.0


# =============================================================
# 조정 레버 (승진/퇴직/인상률/재채용률) → 파라미터 사본 생성
# =============================================================
@dataclass
class Adjustments:
    """조정치. baseline 파라미터에 곱/치환으로 적용해 시뮬 파라미터를 만든다.

    - promotion_scale / attrition_scale: 전체 배율 (1.0 = 변화 없음)
    - promotion_scale_by_level / attrition_scale_by_level: 직급별 배율(전체 배율과 곱).
      나이별 퇴직률 조정은 호출부에서 직급별 나이 구성비(AGE_MIX)로 가중 환산해
      attrition_scale_by_level 에 합성해 넘긴다.
    - raise_rate / raise_rate_by_level: 인건비 인상률(절대값 치환).
    - rehire_rate: 정년 재채용률 치환. None 이면 baseline 값 유지.
    """
    promotion_scale: float = 1.0
    attrition_scale: float = 1.0
    promotion_scale_by_level: dict[str, float] = field(default_factory=dict)
    attrition_scale_by_level: dict[str, float] = field(default_factory=dict)
    raise_rate: float | None = None
    raise_rate_by_level: dict[str, dict[str, float]] | None = None
    rehire_rate: float | None = None
    promotion_scale_by_family: dict[str, float] = field(default_factory=dict)
    attrition_scale_by_family: dict[str, float] = field(default_factory=dict)

    def is_identity(self) -> bool:
        return (
            abs(self.promotion_scale - 1.0) < 1e-9
            and abs(self.attrition_scale - 1.0) < 1e-9
            and all(abs(v - 1.0) < 1e-9 for v in self.promotion_scale_by_level.values())
            and all(abs(v - 1.0) < 1e-9 for v in self.attrition_scale_by_level.values())
            and self.raise_rate is None
            and not self.raise_rate_by_level
            and self.rehire_rate is None
            and not self.promotion_scale_by_family
            and not self.attrition_scale_by_family
        )


def apply_adjustments(base: SimParams, adj: Adjustments) -> SimParams:
    """조정 레버를 적용한 새 SimParams 반환. stay>=0 을 셀 단위로 보장(클립).

    적용 순서:
      1) 퇴직률 = clip(attrition * scale, 정년도래율, 1)   # 정년분은 배율로 못 줄임
      2) 승진율 = clip(promotion * scale, 0, 1 - attrition')   # stay>=0 보장
      3) 최상위 직급 승진율 0 강제
      4) raise_rate / rehire_rate 치환(있으면)
    """
    attrition = deepcopy(base.attrition_rate)
    promotion = deepcopy(base.promotion_rate)

    for f in base.families:
        for lvl in base.levels(f):
            a_scale = (adj.attrition_scale
                       * adj.attrition_scale_by_family.get(f, 1.0)
                       * adj.attrition_scale_by_level.get(lvl, 1.0))
            p_scale = (adj.promotion_scale
                       * adj.promotion_scale_by_family.get(f, 1.0)
                       * adj.promotion_scale_by_level.get(lvl, 1.0))
            # 정년퇴직(나이 기인)은 자발 이탈이 아니라 배율로 줄일 수 없다 → 하한.
            a = _clip(attrition[f][lvl] * a_scale, RETIRE_RATE.get(lvl, 0.0), 1.0)
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
        rehire_rate=base.rehire_rate if adj.rehire_rate is None else adj.rehire_rate,
    )


def _clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


# =============================================================
# 정합성 검증 (합=1, stay>=0, 부장 승진 0, 정년⊂퇴직)
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
            if a < RETIRE_RATE.get(lvl, 0.0) - tol:
                problems.append(f"[{f}/{lvl}] 퇴직률<정년도래율: {a:.3f}<{RETIRE_RATE[lvl]:.3f}")
    return problems


# =============================================================
# 마르코프 추계 (조직별 독립 전이)
# =============================================================
def simulate_family(levels: list[str],
                    init: dict[str, float],
                    promotion: dict[str, float],
                    attrition: dict[str, float],
                    hiring_of_family: dict[str, list[float]],
                    years: int,
                    rehire_rate: float = 0.0,
                    ) -> tuple[list[dict[str, float]], list[float], list[float], list[float]]:
    """한 조직의 연도별 직급 인원 이력 + 예측 퇴직/정년퇴직/재채용 시계열 반환.

    반환: (history[연0..연N], leavers[연차], retires[연차], rehires[연차])
      leavers  = 그 해 전체 이탈(자발 이직 + 정년퇴직)
      retires  = 그중 정년퇴직 (RETIRE_RATE, attrition 의 부분집합)
      rehires  = 정년퇴직 × 재채용률 — 같은 직급으로 복귀(촉탁), 다음 해부터 전이 참여
    """
    k = len(levels)
    n = [float(init.get(lvl, 0.0)) for lvl in levels]
    history = [dict(zip(levels, n))]
    leavers, retires, rehires = [0.0], [0.0], [0.0]

    for t in range(1, years + 1):
        nxt = [0.0] * k
        leave = ret = reh = 0.0
        for i, lvl in enumerate(levels):
            promote_i = promotion.get(lvl, 0.0) if i < k - 1 else 0.0
            a = attrition.get(lvl, 0.0)
            stay_i = 1.0 - a - promote_i
            nxt[i] += n[i] * stay_i               # 재직
            if i < k - 1:
                nxt[i + 1] += n[i] * promote_i    # 승진(바로 위 직급)
            leave += n[i] * a                     # 이탈(정년 포함)
            r = n[i] * min(RETIRE_RATE.get(lvl, 0.0), a)   # 정년퇴직(퇴직의 부분집합)
            ret += r
            reh_i = r * rehire_rate
            nxt[i] += reh_i                       # 재채용: 같은 직급으로 복귀
            reh += reh_i
        # 채용: ★ 전이 후 가산
        for lvl, plan in hiring_of_family.items():
            if lvl in levels and t - 1 < len(plan):
                nxt[levels.index(lvl)] += plan[t - 1]
        n = nxt
        history.append(dict(zip(levels, n)))
        leavers.append(leave)
        retires.append(ret)
        rehires.append(reh)
    return history, leavers, retires, rehires


def simulate(params: SimParams) -> tuple[list[dict[str, dict[str, float]]],
                                         list[dict[str, float]],
                                         list[float], list[float]]:
    """전 조직 추계. 반환: (인원[연차][조직][직급], 예측퇴직[연차][조직],
    정년퇴직[연차], 재채용[연차])."""
    per_family = {
        f: simulate_family(
            params.levels(f),
            params.initial_headcount[f],
            params.promotion_rate[f],
            params.attrition_rate[f],
            params.hiring_plan.get(f, {}),
            params.years,
            rehire_rate=params.rehire_rate,
        )
        for f in params.families
    }
    # [연차] 축으로 재조립
    hc_out: list[dict[str, dict[str, float]]] = []
    attr_out: list[dict[str, float]] = []
    retire_out: list[float] = []
    rehire_out: list[float] = []
    for t in range(params.years + 1):
        hc_out.append({f: per_family[f][0][t] for f in params.families})
        attr_out.append({f: per_family[f][1][t] for f in params.families})
        retire_out.append(sum(per_family[f][2][t] for f in params.families))
        rehire_out.append(sum(per_family[f][3][t] for f in params.families))
    return hc_out, attr_out, retire_out, rehire_out


# =============================================================
# 인건비 (직급별 인상률 반영)
# =============================================================
def _raise_for(raise_spec, f: str, lvl: str) -> float:
    """raise_spec 이 직급별 dict 면 해당 (조직,직급) 값을, 스칼라면 그 값을 반환."""
    if isinstance(raise_spec, dict):
        return raise_spec.get(f, {}).get(lvl, 0.0)
    return raise_spec


def labor_cost_of_year(headcount_year: dict[str, dict[str, float]],
                       salary: dict[str, dict[str, float]],
                       raise_spec, t: int) -> float:
    """raise_spec: float(전 직급 동일) 또는 {조직:{직급:인상률}} 직급별."""
    total = 0.0
    for f, levels in headcount_year.items():
        for lvl, headcount in levels.items():
            unit = salary[f][lvl] * (1.0 + _raise_for(raise_spec, f, lvl)) ** t
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
    """파라미터 → 추계 + 인건비 + 정년/재채용 시계열 + (baseline 있으면) 누적 Δ."""
    history, attr_heads, retire_heads, rehire_heads = simulate(params)
    cost = labor_cost_series(history, params)
    delta = cum_cost_delta(cost, baseline_cost) if baseline_cost is not None else 0.0
    return SimResult(headcount_by_year=history, labor_cost_by_year=cost,
                     attrition_heads_by_year=attr_heads,
                     retire_heads_by_year=retire_heads,
                     rehire_heads_by_year=rehire_heads,
                     cum_cost_delta_vs_baseline=delta)


# =============================================================
# 표시 편의 함수
# =============================================================
def total_headcount(headcount_year: dict[str, dict[str, float]]) -> float:
    return sum(sum(levels.values()) for levels in headcount_year.values())


def headcount_by_family(headcount_year: dict[str, dict[str, float]]) -> dict[str, float]:
    return {f: sum(levels.values()) for f, levels in headcount_year.items()}


def headcount_by_grade(headcount_year: dict[str, dict[str, float]]) -> dict[str, float]:
    """전 조직 합산 직급별 인원(사원→부장)."""
    out = {g: 0.0 for g in GRADES}
    for levels in headcount_year.values():
        for g, v in levels.items():
            out[g] = out.get(g, 0.0) + v
    return out


def top_level_share(headcount_year: dict[str, dict[str, float]]) -> float:
    """부장(최상위 직급) 비중(%). 각 조직 부장 인원 합 ÷ 전체 인원 × 100."""
    top = 0.0
    for f, levels in headcount_year.items():
        top_lvl = FAMILY_LEVELS[f][-1]
        top += levels.get(top_lvl, 0.0)
    tot = total_headcount(headcount_year)
    return (top / tot * 100.0) if tot > 0 else 0.0


# =============================================================
# 자체 점검:  python sim_core.py
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
              f"인건비 {cost/1e8:10.1f}억 | 부장비중 {top_level_share(hc):4.1f}% | "
              f"정년 {baseline.retire_heads_by_year[t]:5.1f}명 | "
              f"재채용 {baseline.rehire_heads_by_year[t]:5.1f}명")

    # 조정: 승진율 전체 +30%, 퇴직률 전체 -20%, 인상률 5%, 재채용률 50%
    adj = Adjustments(promotion_scale=1.3, attrition_scale=0.8,
                      raise_rate=0.05, rehire_rate=0.5)
    sim_params = apply_adjustments(base, adj)
    assert not validate(sim_params), "조정 후 정합성 위반"
    sim = run(sim_params, baseline_cost=baseline.labor_cost_by_year)

    print("\n== 시뮬(승진+30% / 퇴직-20% / 인상5% / 재채용50%) ==")
    for t, (hc, cost) in enumerate(zip(sim.headcount_by_year, sim.labor_cost_by_year)):
        print(f"  연차 {t}: 총원 {total_headcount(hc):8.0f}명 | "
              f"인건비 {cost/1e8:10.1f}억 | 부장비중 {top_level_share(hc):4.1f}% | "
              f"정년 {sim.retire_heads_by_year[t]:5.1f}명 | "
              f"재채용 {sim.rehire_heads_by_year[t]:5.1f}명")
    print(f"\n누적 인건비 Δ vs baseline: {sim.cum_cost_delta_vs_baseline/1e8:+.1f}억")
    print("[OK] 정합성(합=1·stay>=0·부장 승진0·정년⊂퇴직) 통과")

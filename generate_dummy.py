"""
generate_dummy.py — 개인 단위 더미 인력 '이력 패널' 생성기 (2020~기준연도)
==================================================
⚠️ 실데이터 교체 지점(SWAP POINT):
   실서비스에서는 이 파일 전체를 HR 시스템의 '인사이동 이력' 추출로 교체한다.
   산출 DataFrame 스키마(아래 컬럼)만 유지하면 이후 파이프라인은 그대로 동작한다.

산출 컬럼:
   사번, 직급, 근속연수, 부서, 관측연도, 현재상태, 다음연도_상태
   → 1행 = "특정 직원이 '관측연도' 시점에 가진 상태(현재상태)와
            그 다음 해의 상태(다음연도_상태)" = 1건의 인사이동 관측.

핵심 1 — '이력 패널'(longitudinal panel)로 생성:
   2020년 코호트를 깔고, 매년 전이를 샘플링해 개인을 추적한다.
   - 이탈자는 그 해의 전이를 기록한 뒤 패널에서 빠진다(흡수).
   - 신규채용(사원_0-2년)으로 빈자리를 채워 인력 규모를 ~n명으로 유지.
   - 근속연수는 입사연도 기준으로 매년 +1 (개인별 단조 증가 → 데이터가 자연스럽다).
   관측연도는 2020 ~ (기준연도-1) 까지. 즉 마지막 '다음연도_상태'가 기준연도가 된다.
   → 전이행렬은 여러 해(2020→21 … )를 풀링해 추정하므로 표본이 풍부하다.

핵심 2 — 랜덤이 아니라 '구조를 심는다'(데모 스토리):
   - 사원→대리 승진은 활발하게
   - 대리→차장 승진은 정체되게
   - 차장 구간 이탈률을 의도적으로 높게 → 결과 차트에서 '차장 병목/누수'가 보이도록
모든 가정값은 아래 상수에 모아두어 나중에 쉽게 바꾼다.
"""

from __future__ import annotations
import numpy as np
import pandas as pd

# =============================================================
# 0. 상태 정의 (markov.py 와 동일해야 함)
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

# 더미 이력 패널의 기본 관측 범위 (요구사항: 2020년부터)
START_YEAR = 2020

# 직급별 부서 풀 (포스코 가정 — 제조/철강 직군 위주)
DEPARTMENTS = ["제철소운영", "설비기술", "품질관리", "안전환경", "경영지원", "구매조달", "연구개발"]

# =============================================================
# 1. 전이 가정값 (★ 데모 스토리의 핵심 — 여기만 바꾸면 시나리오가 바뀐다)
#    각 현재 상태에서 '다음연도_상태'로 갈 확률. 행 합 = 1.0
#    의도:
#      - 사원_2년+ → 대리_0-2년 : 승진 활발(0.30)
#      - 대리_2년+ → 차장_0-2년 : 승진 정체(0.08, 낮음)
#      - 차장_0-2년/차장_2년+ → 이탈 : 높음(0.15 / 0.22) ← 병목/누수 스토리
# =============================================================
TRANSITION_ASSUMPTIONS = {
    "사원_0-2년": {
        "사원_0-2년": 0.25,   # 아직 근속 1년차 잔류
        "사원_2년+": 0.60,    # 근속 누적
        "대리_0-2년": 0.07,   # 조기 승진(소수)
        "이탈": 0.08,
    },
    "사원_2년+": {
        "사원_2년+": 0.58,
        "대리_0-2년": 0.30,   # 승진 활발 ★
        "이탈": 0.12,
    },
    "대리_0-2년": {
        "대리_0-2년": 0.30,
        "대리_2년+": 0.58,    # 근속 누적
        "이탈": 0.12,
    },
    "대리_2년+": {
        "대리_2년+": 0.82,    # 승진 못하고 정체 ★
        "차장_0-2년": 0.08,   # 차장 승진 정체(낮음) ★
        "이탈": 0.10,
    },
    "차장_0-2년": {
        "차장_0-2년": 0.30,
        "차장_2년+": 0.55,
        "이탈": 0.15,         # 이탈 시작 ★
    },
    "차장_2년+": {
        "차장_2년+": 0.78,
        "이탈": 0.22,         # 차장 누수/병목 ★★
    },
    "이탈": {
        "이탈": 1.0,          # 흡수상태
    },
}

# 2020년 초기 코호트의 상태 분포(개인을 어떤 상태로 깔지) — 피라미드 구조
INITIAL_STATE_MIX = {
    "사원_0-2년": 0.22,
    "사원_2년+": 0.26,
    "대리_0-2년": 0.14,
    "대리_2년+": 0.20,
    "차장_0-2년": 0.08,
    "차장_2년+": 0.10,
    # '이탈'은 현재 재직인원에 없음(미래 상태)
}

# 상태별 '초기 근속연수' 샘플 범위 (2020 코호트·신규채용 시드용)
# 이후 매년 +1 로 증가하므로 개인별로는 단조 증가한다.
TENURE_RANGE = {
    "사원_0-2년": (0, 2),
    "사원_2년+": (3, 7),
    "대리_0-2년": (3, 6),
    "대리_2년+": (6, 12),
    "차장_0-2년": (10, 14),
    "차장_2년+": (13, 25),
}

RANK_OF_STATE = {
    "사원_0-2년": "사원", "사원_2년+": "사원",
    "대리_0-2년": "대리", "대리_2년+": "대리",
    "차장_0-2년": "차장", "차장_2년+": "차장",
}

# 신규채용은 사원_0-2년으로 입직한다고 가정 (입직 게이트)
HIRE_STATE = "사원_0-2년"


def _sample_next_state(current: str, rng: np.random.Generator) -> str:
    """현재 상태에서 가정 전이확률에 따라 다음연도 상태를 1회 샘플."""
    dist = TRANSITION_ASSUMPTIONS[current]
    outcomes = list(dist.keys())
    probs = np.array(list(dist.values()), dtype=float)
    probs = probs / probs.sum()
    return rng.choice(outcomes, p=probs)


def _sample_initial_tenure(state: str, rng: np.random.Generator) -> int:
    lo, hi = TENURE_RANGE[state]
    return int(rng.integers(lo, hi + 1))


def generate_dummy(n: int = 6000,
                   start_year: int = START_YEAR,
                   end_year: int = 2026,
                   seed: int = 42) -> pd.DataFrame:
    """
    개인 단위 더미 '인사이동 이력 패널' 생성 (start_year ~ end_year).

    Parameters
    ----------
    n : 유지할 재직 인력 규모(매년 신규채용으로 보충) — 수천~1만 권장
    start_year : 이력 시작연도 (기본 2020)
    end_year   : 기준연도 = 마지막 '다음연도_상태'가 찍히는 해 (기본 2026)
                 관측연도(전이 출발연도)는 start_year ~ end_year-1 까지.
    seed : 재현성

    Returns
    -------
    DataFrame[사번, 직급, 근속연수, 부서, 관측연도, 현재상태, 다음연도_상태]
        여러 해의 전이가 세로로 쌓인 형태(panel/long format).
    """
    rng = np.random.default_rng(seed)

    # end_year 가 start_year 이하이면 단일연도 스냅샷만 생성(방어적 처리)
    if end_year <= start_year:
        end_year = start_year + 1

    init_states = list(INITIAL_STATE_MIX.keys())
    init_probs = np.array(list(INITIAL_STATE_MIX.values()), dtype=float)
    init_probs = init_probs / init_probs.sum()

    # 직원 레지스트리: emp_id -> dict(state, hire_year, dept)
    employees: dict[int, dict] = {}
    counter = 0

    def _new_employee(state: str, hire_year: int) -> None:
        nonlocal counter
        employees[counter] = {
            "state": state,
            "hire_year": hire_year,
            "dept": rng.choice(DEPARTMENTS),
        }
        counter += 1

    # ----- start_year 초기 코호트 시드 -----
    seed_states = rng.choice(init_states, size=n, p=init_probs)
    for s in seed_states:
        tenure0 = _sample_initial_tenure(s, rng)
        _new_employee(s, hire_year=start_year - tenure0)

    rows = []
    # ----- 연도별 전이 기록 (관측연도 = 전이 출발연도) -----
    for year in range(start_year, end_year):
        leavers = []
        for eid, e in employees.items():
            cur = e["state"]
            nxt = _sample_next_state(cur, rng)
            tenure = max(0, year - e["hire_year"])
            rows.append({
                "사번": f"P{e['hire_year']}{eid:05d}",
                "직급": RANK_OF_STATE[cur],
                "근속연수": int(tenure),
                "부서": e["dept"],
                "관측연도": year,
                "현재상태": cur,            # 전처리 편의용(실데이터에선 직급+근속에서 파생)
                "다음연도_상태": nxt,
            })
            if nxt == "이탈":
                leavers.append(eid)
            else:
                e["state"] = nxt           # 다음 해로 상태 갱신(근속연수는 입사연도 기준 자동 +1)

        # 이탈자 퇴장
        for eid in leavers:
            del employees[eid]

        # 신규채용 보충: 빈자리를 사원_0-2년으로 채워 규모 ~n 유지
        shortfall = n - len(employees)
        for _ in range(max(0, shortfall)):
            _new_employee(HIRE_STATE, hire_year=year + 1)

    df = pd.DataFrame(rows)
    # 보기 좋게 정렬 (관측연도 → 상태)
    df = df.sort_values(["관측연도", "현재상태", "사번"]).reset_index(drop=True)
    return df


def get_base_year_headcount(df: pd.DataFrame, base_year: int) -> pd.Series:
    """
    기준연도(=투영 시작연도) 시점의 재직 인력벡터 n(0) — 상태별 인원수.

    이력 패널에서 'base_year 시점 재직자'는
      관측연도 == base_year-1 행들의 '다음연도_상태'(이탈 제외) 분포로 정의한다.
    (= base_year-1 → base_year 전이의 도착 상태)

    base_year-1 이 데이터에 없으면(예: base_year == 시작연도) 해당 연도의
    '현재상태' 분포로 폴백한다.
    """
    prev = base_year - 1
    if prev in set(df["관측연도"].unique()):
        snap = df.loc[df["관측연도"] == prev, "다음연도_상태"]
        snap = snap[snap != "이탈"]
    elif base_year in set(df["관측연도"].unique()):
        snap = df.loc[df["관측연도"] == base_year, "현재상태"]
    else:
        # 범위 밖이면 가장 최근 연도의 도착 상태로 폴백
        latest = int(df["관측연도"].max())
        snap = df.loc[df["관측연도"] == latest, "다음연도_상태"]
        snap = snap[snap != "이탈"]

    vec = snap.value_counts().reindex(STATES, fill_value=0)
    return vec.astype(float)


if __name__ == "__main__":
    df = generate_dummy(n=6000, start_year=2020, end_year=2026)
    print(df.head(12).to_string())
    print("\n총 관측(전이) 건수:", len(df))
    print("\n관측연도별 건수:\n", df["관측연도"].value_counts().sort_index())
    print("\n현재상태 분포(전체 풀링):\n", df["현재상태"].value_counts())
    print("\n전이 예시(현재→다음) 카운트 상위:\n",
          df.groupby(["현재상태", "다음연도_상태"]).size().sort_values(ascending=False).head(15))
    print("\n2026 기준연도 재직 인력벡터 n(0):\n", get_base_year_headcount(df, 2026))

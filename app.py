"""
app.py — POSCO HR 인력운영 시뮬레이터 (v4, 배포 엔트리)
==================================================
직급별 승진율 / 직급·나이별 퇴직률 / 직급별 인상률 / 정년 재채용률을 조정하면
향후 인력 구조·인건비·정년 재채용 인원이 어떻게 변하는지 결정론 마르코프로 추계해
baseline ↔ 시뮬을 좌우로 나란히 비교하고, 변수 조합을 스냅샷으로 저장·비교한다.
LLM 인사이트 챗봇 P-GPT(선택).

  - 결정론 코어:   sim_core.py  (직급 6단계 사원→부장 × 조직 4개 전이 + 인건비 + 정년/재채용)
  - 스냅샷 로직:   snapshots.py (라벨·캡처·비교표, Streamlit 비의존)
  - 화면(본 파일): POSCO 블루 리디자인 — 인라인 헤더/KPI 타일/차트 대시보드

실행:  streamlit run app.py
"""
from __future__ import annotations

import base64
import os
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# .env 의 ANTHROPIC_API_KEY 등을 환경변수로 로드(로컬). 배포에선 .env 가 없어 그냥 통과.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import sim_core as sc
import snapshots as snap
import insight_bot

st.set_page_config(page_title="POSCO HR 시뮬레이터", layout="wide", page_icon="🔷")

# 추계 연도 라벨: 연차 0 = 올해(기준 스냅샷), 추계·조정 효과는 내년(BASE_YEAR+1)부터 반영.
BASE_YEAR = date.today().year


def year_label(t: int) -> str:
    """연차 t → 실제 연도 문자열. 기준연(t=0)은 '올해' 표기."""
    return f"{BASE_YEAR}(기준)" if t == 0 else str(BASE_YEAR + t)


GRADE_ORDER = sc.GRADES           # 사원 → 대리 → 과장 → 차장 → 리더 → 부장 (임원 제외)
AGE_BANDS = sc.AGE_BANDS          # 20대 / 30대 / 40대 / 50대+
DEFAULT_REHIRE_PCT = sc.DEFAULT_REHIRE_RATE * 100.0

# Streamlit Cloud 배포용: Secrets 에 넣은 키를 환경변수로 브리지(insight_bot 은 os.environ 을 읽음).
try:
    if "ANTHROPIC_API_KEY" in st.secrets and not os.environ.get("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = str(st.secrets["ANTHROPIC_API_KEY"])
except Exception:
    pass


# =============================================================
# 전역 CSS (POSCO 블루 · Pretendard) — 앱 시작 시 1회 주입
# =============================================================
POSCO_CSS = """
<style>
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');
/* 팔레트 — posco_2과제(정기인사 시뮬레이션 Agent) 디자인 시스템과 통일
   네이비 #003C71 · 딥네이비 #002B5B · 블루 #0072CE · 스카이 포인트 #00A0E9 */
:root{
  --navy:#003C71; --navy-dp:#002B5B; --blue:#0072CE; --sky:#00A0E9; --blue-lt:#4878A8;
  --ink:#1A2B3C; --muted:#6B7688; --line:#D6E4F0; --panel:#EEF3F8;
}
html, body, [class*="css"]{ font-family:'POSCO','Pretendard',system-ui,sans-serif; color:var(--ink); }
.stApp{ background:#FFFFFF; }

/* POSCO 마크 — 네이비 칩 (로고 로드 실패 시 폴백) */
.posco-mark{ display:inline-block; background:linear-gradient(135deg,#002B5B,#0072CE);
  border-radius:6px; padding:6px 12px; color:#fff; font-weight:800; letter-spacing:.04em; }

/* 조정 레버 상단 고정 바 — 엑셀 첫 행 '틀고정' 스타일.
   ★ sticky 는 .st-key-lever_bar(내부 블록)가 아니라 그 부모 stLayoutWrapper 에 건다. */
div[data-testid="stLayoutWrapper"]:has(> .st-key-lever_bar){
  position:sticky; top:3.75rem; z-index:99;
  background:#FFFFFF; border:1px solid var(--line); border-radius:12px;
  box-shadow:0 10px 22px rgba(0,44,91,.12); margin-bottom:8px; }
.st-key-lever_bar{ padding:12px 16px 2px; }
.st-key-lever_bar label p{ font-weight:700 !important; color:var(--navy) !important; }
.st-key-lever_bar .stNumberInput input{ font-weight:700; font-variant-numeric:tabular-nums; }

/* 인라인 헤더 */
.posco-head{ display:flex; align-items:center; gap:12px; margin:4px 0 6px; }
.posco-head h1{ font-size:24px; font-weight:800; margin:0; letter-spacing:-.01em; }
.posco-badge{ font-size:10.5px; font-weight:700; letter-spacing:.06em; color:var(--blue);
  border:1px solid #C9DBF2; border-radius:5px; padding:3px 7px; }
.posco-sub{ color:var(--muted); font-size:13px; margin:2px 2px 10px; }

/* KPI 타일 */
.kpi-row{ display:grid; grid-template-columns:repeat(3,1fr); gap:14px; margin:18px 0; }
.kpi{ border-radius:12px; padding:20px; border:1px solid var(--line); background:var(--panel); }
.kpi.fill{ background:linear-gradient(135deg,#002B5B 0%,#0072CE 100%); border:0; color:#fff; }
.kpi .label{ font-size:12px; font-weight:600; color:var(--muted); }
.kpi.fill .label{ color:#B7CDEA; }
.kpi .value{ font-size:32px; font-weight:800; letter-spacing:-.02em; margin-top:8px; }
.kpi .delta{ font-size:11.5px; font-weight:700; margin-top:6px; }
.kpi .up{ color:#1B8A5A; } .kpi .down{ color:#C33; } .kpi.fill .down{ color:#F3B4B4; }
.kpi.fill .up{ color:#9BE3C1; }

/* 적용 변수 칩 — baseline/시뮬에 어떤 변수값이 들어갔는지 한 줄 요약 */
.lever-chip{ display:inline-block; border:1px solid var(--line); border-radius:999px;
  background:var(--panel); color:var(--ink); font-size:12px; font-weight:600;
  padding:5px 12px; margin:2px 6px 2px 0; }
.lever-chip b{ color:var(--navy); }
.lever-chip.sim{ border-color:#BBD9F2; background:#EAF4FD; }

/* st.metric — KPI 타일과 톤을 맞춘 카드 (누적 인건비 Δ 강조 등) */
[data-testid="stMetric"]{ border:1px solid var(--line); border-radius:12px;
  padding:14px 16px; background:var(--panel); }
[data-testid="stMetric"] [data-testid="stMetricLabel"]{ color:var(--muted);
  font-size:12px; font-weight:600; }
[data-testid="stMetric"] [data-testid="stMetricValue"]{ color:var(--ink);
  font-weight:800; letter-spacing:-.02em; }

/* 카드/표/버튼 */
.card{ border:1px solid var(--line); border-radius:12px; padding:18px 20px; }
.stDataFrame, [data-testid="stTable"]{ border:1px solid var(--line); border-radius:12px; overflow:hidden; }
thead tr th{ background:#F4F6FA !important; color:var(--muted) !important; font-weight:600 !important; }
.stButton>button{ background:#fff; color:var(--ink); border:1px solid #D5DDE8; border-radius:9px;
  font-weight:700; padding:.5rem 1rem; }
.stButton>button:hover{ background:var(--blue); color:#fff; border-color:var(--blue); }
</style>
"""


def inject_css():
    st.markdown(POSCO_CSS, unsafe_allow_html=True)


@st.cache_data
def _posco_logo_b64() -> str:
    """공식 로고 SVG(assets/posco_logo.svg) → base64. 없으면 예외 → 텍스트 마크 폴백."""
    logo_path = Path(__file__).parent / "assets" / "posco_logo.svg"
    return base64.b64encode(logo_path.read_bytes()).decode()


def render_header():
    # posco_2과제 헤더 스타일: 공식 로고 + 스카이(#00A0E9) 세로 포인트 바 + 네이비 타이틀
    try:
        mark = (f'<img src="data:image/svg+xml;base64,{_posco_logo_b64()}" '
                f'style="height:34px;"/>')
    except Exception:
        mark = '<span class="posco-mark">POSCO</span>'
    st.markdown(
        '<div class="posco-head" style="gap:18px;">'
        f'{mark}'
        '<div style="border-left:5px solid var(--sky); padding-left:14px;">'
        '<h1 style="color:var(--navy);">HR 인력운영 시뮬레이터</h1></div>'
        '<span class="posco-badge">v4 · MOCKUP</span>'
        '</div>',
        unsafe_allow_html=True)
    st.markdown(
        '<div class="posco-sub">직급별 승진율·직급/나이별 퇴직률·인상률·정년 재채용률을 조정하면 '
        '향후 인력 구조와 총 인건비 변화를 마르코프로 추계해 baseline과 나란히 보여줍니다. '
        '직급 체계: 사원→대리→과장→차장→리더→부장 (임원 제외). 결정론 rule 계산.</div>',
        unsafe_allow_html=True)


# =============================================================
# 브랜드 팔레트 (POSCO 블루) — posco_2과제 디자인 시스템과 통일
# =============================================================
C_NAVY = "#003C71"
C_NAVY_DP = "#002B5B"
C_BLUE = "#0072CE"
C_SKY = "#00A0E9"
C_BLUE_LT = "#4878A8"
C_BLUE_XLT = "#7C9CBF"
C_BASE = "#9FB3C8"       # baseline 계열(회청 — 레퍼런스 뉴트럴)
C_GRID = "#EEF3F8"
C_INK = "#1A2B3C"
FONT_FAMILY = "Pretendard, system-ui, sans-serif"

# 조직 색상 — 레퍼런스 조직도의 4단 블루 스케일
FAMILY_COLOR = {"P": C_NAVY_DP, "R": "#1B4F8A", "E": C_BLUE_LT, "A": C_BLUE_XLT}

# 직급 색상 — 사원(연청)→부장(딥네이비) 단조 그라데이션.
#   좌(baseline)·우(시뮬) 비교 차트에서 동일 색을 써 직급별 대응이 한눈에 보이게.
GRADE_COLOR = {"사원": "#BFDCF5", "대리": "#9CC3E8", "과장": C_BLUE_XLT,
               "차장": C_BLUE_LT, "리더": C_BLUE, "부장": C_NAVY_DP}

# 위젯 key ↔ 기본값. 복원은 이 key 에 값을 써넣고 rerun.
#   승진/퇴직/인상은 직급별, 퇴직은 나이별 추가. 기본 0% = baseline과 동일(Δ 0).
SLIDER_DEFAULTS: dict[str, float | int] = {"k_years": 5, "k_rehire": DEFAULT_REHIRE_PCT}
for _g in GRADE_ORDER:
    SLIDER_DEFAULTS[f"k_promo_{_g}"] = 0.0
    SLIDER_DEFAULTS[f"k_attr_{_g}"] = 0.0
    SLIDER_DEFAULTS[f"k_raise_{_g}"] = 0.0
for _a in AGE_BANDS:
    SLIDER_DEFAULTS[f"k_attr_age_{_a}"] = 0.0

# 차트 클릭 확대/툴바 끄기 (정적 표시) — 축 fixedrange 와 함께 줌·팬 차단
PLOTLY_CONFIG = {"displayModeBar": False, "staticPlot": False, "scrollZoom": False}


def _dict_summary(name: str, d: dict[str, float], signed: bool = True) -> str | None:
    """직급/나이별 % dict → '승진 과장+2%·차장+1%' 요약(전부 0이면 None)."""
    vals = list(d.values())
    if not vals or all(abs(v) < 1e-9 for v in vals):
        return None
    fmt = "{:+g}%" if signed else "{:g}%"
    if all(abs(v - vals[0]) < 1e-9 for v in vals):
        return f"{name} {fmt.format(vals[0])}"
    nz = [f"{k} {fmt.format(v)}" for k, v in d.items() if abs(v) > 1e-9]
    return f"{name} " + "·".join(nz)


def lever_desc(promo_g: dict, attr_g: dict, attr_a: dict, raise_g: dict,
               rehire_pct: float) -> str:
    """현재 레버 조합 한 줄 요약(적용 변수 칩·챗봇 컨텍스트·스냅샷 캡션 공용)."""
    parts = [p for p in (
        _dict_summary("승진", promo_g),
        _dict_summary("퇴직(직급)", attr_g),
        _dict_summary("퇴직(연령)", attr_a),
        _dict_summary("인상", raise_g, signed=False),
    ) if p]
    if abs(rehire_pct - DEFAULT_REHIRE_PCT) > 1e-9:
        parts.append(f"재채용률 {rehire_pct:g}%")
    return " / ".join(parts) if parts else "조정 없음 (baseline 동일)"


# =============================================================
# 위젯 생성 '이전' 세션 상태 초기화 + 복원 적용
#   (Streamlit 제약: 위젯이 만들어진 뒤엔 그 key 의 session_state 를 못 바꾼다)
# =============================================================
for _k, _v in SLIDER_DEFAULTS.items():
    st.session_state.setdefault(_k, _v)
st.session_state.setdefault("snapshots", [])

_pending = st.session_state.pop("_pending_restore", None)
if _pending:
    st.session_state["k_years"] = int(_pending["years"])
    st.session_state["k_rehire"] = float(_pending.get("rehire_pct", DEFAULT_REHIRE_PCT))
    for _g in GRADE_ORDER:
        st.session_state[f"k_promo_{_g}"] = float(_pending.get("promo_by_grade", {}).get(_g, 0.0))
        st.session_state[f"k_attr_{_g}"] = float(_pending.get("attr_by_grade", {}).get(_g, 0.0))
        st.session_state[f"k_raise_{_g}"] = float(_pending.get("raise_by_grade", {}).get(_g, 0.0))
    for _a in AGE_BANDS:
        st.session_state[f"k_attr_age_{_a}"] = float(_pending.get("attr_by_age", {}).get(_a, 0.0))

inject_css()
render_header()


# =============================================================
# 조정 레버 — 상단 고정 바 (엑셀 첫 행 틀고정 스타일) + 접기 토글
#   승진율·퇴직률·인상률은 직급별(popover), 퇴직률은 나이별 추가.
#   ★ 접기: 위젯을 조건부로 없애면 Streamlit 이 위젯 상태를 지워 입력값이
#   초기화되므로, 위젯은 항상 렌더하되 CSS(display:none)로만 숨긴다.
# =============================================================
def _lever_summary_from_state() -> str:
    """접힘 헤더용 — 위젯 생성 전이므로 session_state 에서 직접 현재 값을 읽는다."""
    return lever_desc(
        {g: float(st.session_state.get(f"k_promo_{g}", 0.0)) for g in GRADE_ORDER},
        {g: float(st.session_state.get(f"k_attr_{g}", 0.0)) for g in GRADE_ORDER},
        {a: float(st.session_state.get(f"k_attr_age_{a}", 0.0)) for a in AGE_BANDS},
        {g: float(st.session_state.get(f"k_raise_{g}", 0.0)) for g in GRADE_ORDER},
        float(st.session_state.get("k_rehire", DEFAULT_REHIRE_PCT)),
    )


st.session_state.setdefault("k_lever_fold", False)

with st.container(key="lever_bar"):
    head = st.columns([6, 1.3], vertical_alignment="center")
    with head[0]:
        if st.session_state["k_lever_fold"]:
            st.caption(f"조정 레버 접힘 — 연수 {st.session_state.get('k_years', 5)}년 · "
                       f"{_lever_summary_from_state()}")
        else:
            st.caption("**조정 레버** — 스크롤을 따라오는 틀고정 바. "
                       "조정을 안 할 때는 오른쪽 [접어두기]로 접을 수 있습니다.")
    with head[1]:
        lever_folded = st.toggle("접어두기", key="k_lever_fold",
                                 help="레버 입력영역을 접어 화면 가림을 줄입니다. "
                                      "접어도 설정한 값은 그대로 유지·적용됩니다.")

    with st.container(key="lever_body"):
        bar = st.columns([1.4, 1.0, 1.0, 1.0, 0.9, 1.0], vertical_alignment="bottom")
        with bar[0]:
            years = st.slider("추계 연수", 1, 15, step=1, key="k_years",
                              help="1년(내년만)부터 가능. 가벼운 단기 시뮬은 1~2년으로.")
        with bar[1]:
            promo_by_grade: dict[str, float] = {}
            with st.popover("승진율 조정", use_container_width=True):
                st.caption("승진율은 직급별로 다르다(baseline: 사원 16%→리더 5%, 부장 0%). "
                           "직급별로 baseline 대비 배율(%)을 조정. +10 이면 그 직급 승진율 ×1.10. "
                           "재직률이 음수가 되지 않도록 (1-퇴직률) 이하로 자동 제한.")
                for _g in GRADE_ORDER[:-1]:   # 부장은 승진 대상 아님(임원 미고려)
                    promo_by_grade[_g] = st.number_input(
                        f"{_g} 승진율 조정 (%)", min_value=-50.0, max_value=100.0,
                        step=0.5, format="%.1f", key=f"k_promo_{_g}")
                promo_by_grade["부장"] = 0.0
        with bar[2]:
            attr_by_grade: dict[str, float] = {}
            attr_by_age: dict[str, float] = {}
            with st.popover("퇴직률 조정", use_container_width=True):
                st.caption("(예측) 퇴직률을 직급별 × 나이별로 조정한다. "
                           "나이별 조정은 직급별 나이 구성비(가정값)로 가중해 직급별 배율에 합성. "
                           "정년퇴직(나이 기인)분은 하한으로 보호되어 배율로 줄어들지 않음.")
                st.markdown("**직급별 조정 (%)**")
                for _g in GRADE_ORDER:
                    attr_by_grade[_g] = st.number_input(
                        f"{_g} 퇴직률 조정 (%)", min_value=-50.0, max_value=100.0,
                        step=0.5, format="%.1f", key=f"k_attr_{_g}")
                st.markdown("**나이별 조정 (%)**")
                for _a in AGE_BANDS:
                    attr_by_age[_a] = st.number_input(
                        f"{_a} 퇴직률 조정 (%)", min_value=-50.0, max_value=100.0,
                        step=0.5, format="%.1f", key=f"k_attr_age_{_a}",
                        help="예: 30대 이탈 심화 가정 → 30대 +10%. "
                             "직급별 나이 구성비를 가중치로 반영.")
        with bar[3]:
            raise_by_grade: dict[str, float] = {}
            with st.popover("직급별 인상률", use_container_width=True):
                st.caption("직급(사원→부장)별로 단가 인상률을 다르게 준다. "
                           "baseline=전 직급 0% 기준이라 올린 만큼 누적 Δ가 +로 잡힘. "
                           "매년 단가=단가×(1+인상률)^연차.")
                for _g in GRADE_ORDER:
                    raise_by_grade[_g] = st.number_input(
                        f"{_g} 인상률 (%)", min_value=0.0, max_value=10.0, step=0.05,
                        format="%.2f", key=f"k_raise_{_g}",
                        help="예: 과장·차장(허리)만 5%로 올려 이탈 방지 시뮬. 소수점 입력 가능.")
        with bar[4]:
            rehire_pct = st.number_input("정년 재채용률 (%)", min_value=0.0, max_value=100.0,
                                         step=5.0, format="%.0f", key="k_rehire",
                                         help=f"정년퇴직자 중 촉탁 재채용 비율. "
                                              f"baseline {DEFAULT_REHIRE_PCT:g}%. "
                                              f"재채용 인원은 같은 직급으로 복귀.")
        with bar[5]:
            view_mode = st.radio("결과 보기 방식", ["차트", "표(숫자)"], horizontal=True,
                                 help="차트=클릭 확대 없이 정적으로 표시 / 표=숫자만")

        st.caption(
            f"현재 조정: {lever_desc(promo_by_grade, attr_by_grade, attr_by_age, raise_by_grade, rehire_pct)}"
            f" · © POSCO HR PoC · 더미데이터 기반 목업"
        )

# 접힘 상태: 위젯 트리는 그대로 두고(값 유지) 입력영역만 시각적으로 숨긴다.
if lever_folded:
    st.markdown("<style>.st-key-lever_body{ display:none !important; }</style>",
                unsafe_allow_html=True)

SHOW_TABLE = view_mode == "표(숫자)"


# =============================================================
# 계산 — baseline(조정 없음) vs 시뮬(조정 반영)
# =============================================================
def build_attr_scale_by_grade(attr_g_pct: dict[str, float],
                              attr_a_pct: dict[str, float]) -> dict[str, float]:
    """직급별 % + 나이별 % → 직급별 최종 퇴직률 배율.
    나이별 조정은 직급별 나이 구성비(AGE_MIX)를 가중치로 환산해 곱한다."""
    out = {}
    for g in GRADE_ORDER:
        age_factor = sum(share * (1.0 + attr_a_pct.get(a, 0.0) / 100.0)
                         for a, share in sc.AGE_MIX[g].items())
        out[g] = (1.0 + attr_g_pct.get(g, 0.0) / 100.0) * age_factor
    return out


@st.cache_data(show_spinner=False)
def compute(years: int, promo_t: tuple, attr_t: tuple, attr_age_t: tuple,
            raise_t: tuple, rehire_pct: float):
    # 튜플 인자: GRADE_ORDER/AGE_BANDS 순서의 % 값. 캐시 키 안정화를 위해 튜플로 받는다.
    base_params = sc.build_default_params(years=years)
    baseline = sc.run(base_params)   # baseline = 무조정 · 인상 0% 기준선
    promo_g = dict(zip(GRADE_ORDER, promo_t))
    attr_g = dict(zip(GRADE_ORDER, attr_t))
    attr_a = dict(zip(AGE_BANDS, attr_age_t))
    raise_g = dict(zip(GRADE_ORDER, raise_t))
    adj = sc.Adjustments(
        promotion_scale_by_level={g: 1.0 + promo_g[g] / 100.0 for g in GRADE_ORDER},
        attrition_scale_by_level=build_attr_scale_by_grade(attr_g, attr_a),
        raise_rate_by_level={f: {g: raise_g[g] / 100.0 for g in GRADE_ORDER}
                             for f in sc.FAMILY_LEVELS},
        rehire_rate=rehire_pct / 100.0,
    )
    sim_params = sc.apply_adjustments(base_params, adj)
    problems = sc.validate(sim_params)
    sim = sc.run(sim_params, baseline_cost=baseline.labor_cost_by_year)
    return adj, base_params, sim_params, baseline, sim, problems


adj, base_params, sim_params, baseline, sim, problems = compute(
    years,
    tuple(promo_by_grade[g] for g in GRADE_ORDER),
    tuple(attr_by_grade[g] for g in GRADE_ORDER),
    tuple(attr_by_age[a] for a in AGE_BANDS),
    tuple(raise_by_grade[g] for g in GRADE_ORDER),
    float(rehire_pct),
)

if problems:
    st.error("정합성 위반(계산 중단):\n" + "\n".join(problems))
    st.stop()

LEVER_DESC = lever_desc(promo_by_grade, attr_by_grade, attr_by_age,
                        raise_by_grade, rehire_pct)


# =============================================================
# 차트 헬퍼 (흰 배경 · 연한 그리드 · Pretendard)
# =============================================================
def _style(fig: go.Figure, height: int, title: str | None = None) -> go.Figure:
    if title:  # title=None 을 명시로 넣으면 프런트가 'undefined' 를 그리는 케이스 방지
        fig.update_layout(title=title)
    fig.update_layout(
        height=height,
        margin=dict(t=40 if title else 12, b=10, l=10, r=10),
        plot_bgcolor="#FFFFFF", paper_bgcolor="#FFFFFF",
        font=dict(family=FONT_FAMILY, color=C_INK, size=12),
        legend=dict(orientation="h", y=-0.2),
    )
    fig.update_xaxes(fixedrange=True, gridcolor=C_GRID, zeroline=False)
    fig.update_yaxes(fixedrange=True, gridcolor=C_GRID, zeroline=False)
    return fig


def area_by_family(result: sc.SimResult, title: str, height: int = 320,
                   showlegend: bool = True) -> go.Figure:
    yrs = [year_label(t) for t in range(len(result.headcount_by_year))]
    fig = go.Figure()
    for f in sc.FAMILY_LEVELS:
        vals = [sc.headcount_by_family(hc).get(f, 0.0)
                for hc in result.headcount_by_year]
        fig.add_trace(go.Scatter(
            x=yrs, y=vals, mode="lines", stackgroup="one", name=sc.FAMILY_LABEL[f],
            line=dict(width=0.5, color=FAMILY_COLOR[f]),
            hovertemplate=f"{sc.FAMILY_LABEL[f]} %{{y:.0f}}명<extra></extra>",
        ))
    _style(fig, height, title)
    # 연도 라벨('2026(기준)','2027'…)이 숫자축으로 오인돼 기준연 포인트가 NaN 되는 것 방지.
    fig.update_xaxes(type="category")
    fig.update_layout(xaxis_title="연도", yaxis_title="인원(명)", showlegend=showlegend)
    return fig


def area_by_grade(result: sc.SimResult, title: str, height: int = 320,
                  showlegend: bool = True, y_max: float | None = None) -> go.Figure:
    """연도별 직급(사원→부장) 누적 인원. y_max 를 주면 좌우 비교 시 동일 축으로 고정."""
    yrs = [year_label(t) for t in range(len(result.headcount_by_year))]
    fig = go.Figure()
    for g in GRADE_ORDER:
        vals = [sc.headcount_by_grade(hc)[g] for hc in result.headcount_by_year]
        fig.add_trace(go.Scatter(
            x=yrs, y=vals, mode="lines", stackgroup="one", name=g,
            line=dict(width=0.5, color=GRADE_COLOR[g]),
            hovertemplate=f"{g} %{{y:.0f}}명<extra></extra>",
        ))
    _style(fig, height, title)
    fig.update_xaxes(type="category")
    if y_max is not None:
        fig.update_yaxes(range=[0, y_max])
    fig.update_layout(xaxis_title="연도", yaxis_title="인원(명)", showlegend=showlegend)
    return fig


def headcount_table(result: sc.SimResult) -> pd.DataFrame:
    """연도별 직급 인원 + 총원 표(숫자)."""
    data = {}
    for t, hc in enumerate(result.headcount_by_year):
        byg = sc.headcount_by_grade(hc)
        row = {g: round(byg.get(g, 0.0)) for g in GRADE_ORDER}
        row["총원"] = round(sc.total_headcount(hc))
        data[year_label(t)] = row
    df = pd.DataFrame.from_dict(data, orient="index")
    df.index.name = "연도"
    return df.reset_index()


def grade_delta_table(t: int) -> pd.DataFrame:
    """선택 연도의 직급별 인원 — 절대값(baseline·시뮬) + 변화값(Δ)."""
    b = sc.headcount_by_grade(baseline.headcount_by_year[t])
    s = sc.headcount_by_grade(sim.headcount_by_year[t])
    rows = [{"직급": g, "BASELINE(명)": round(b[g]), "시뮬(명)": round(s[g]),
             "Δ(명)": round(s[g] - b[g])} for g in GRADE_ORDER]
    rows.append({"직급": "합계",
                 "BASELINE(명)": round(sum(b.values())),
                 "시뮬(명)": round(sum(s.values())),
                 "Δ(명)": round(sum(s.values()) - sum(b.values()))})
    return pd.DataFrame(rows)


def cost_table(baseline: sc.SimResult, sim: sc.SimResult) -> pd.DataFrame:
    """연도별 총 인건비 표(억원): baseline / 시뮬 / Δ."""
    rows = []
    for t, (b, s) in enumerate(zip(baseline.labor_cost_by_year, sim.labor_cost_by_year)):
        rows.append({"연도": year_label(t),
                     "baseline(억)": round(b / 1e8, 1),
                     "시뮬(억)": round(s / 1e8, 1),
                     "Δ(억)": round((s - b) / 1e8, 1)})
    return pd.DataFrame(rows)


def cost_chart(baseline: sc.SimResult, sim: sc.SimResult) -> go.Figure:
    """연도별 총 인건비 — baseline vs 시뮬 그룹 막대."""
    yrs = [year_label(t) for t in range(len(baseline.labor_cost_by_year))]
    b = [c / 1e8 for c in baseline.labor_cost_by_year]
    s = [c / 1e8 for c in sim.labor_cost_by_year]
    fig = go.Figure()
    fig.add_bar(x=yrs, y=b, name="baseline", marker_color=C_BASE,
                hovertemplate="baseline %{y:.0f}억<extra></extra>")
    fig.add_bar(x=yrs, y=s, name="시뮬", marker_color=C_BLUE,
                hovertemplate="시뮬 %{y:.0f}억<extra></extra>")
    _style(fig, 320, "연도별 총 인건비 (억원)")
    fig.update_xaxes(type="category")
    fig.update_layout(barmode="group", xaxis_title="연도", yaxis_title="인건비(억원)")
    return fig


def retire_rehire_chart(result: sc.SimResult) -> go.Figure:
    """연도별 정년퇴직(예상) vs 정년 재채용 인원 — 그룹 막대. 연차 1부터."""
    n = len(result.retire_heads_by_year)
    yrs = [year_label(t) for t in range(1, n)]
    ret = result.retire_heads_by_year[1:]
    reh = result.rehire_heads_by_year[1:]
    fig = go.Figure()
    fig.add_bar(x=yrs, y=ret, name="정년퇴직(예상)", marker_color=C_BLUE_XLT,
                text=[f"{v:.0f}" for v in ret], textposition="outside",
                hovertemplate="정년퇴직 %{y:.1f}명<extra></extra>")
    fig.add_bar(x=yrs, y=reh, name="정년 재채용", marker_color=C_SKY,
                text=[f"{v:.0f}" for v in reh], textposition="outside",
                hovertemplate="재채용 %{y:.1f}명<extra></extra>")
    _style(fig, 300, None)
    fig.update_xaxes(type="category")
    fig.update_layout(barmode="group", xaxis_title="연도", yaxis_title="인원(명)")
    return fig


def retire_rehire_table(baseline: sc.SimResult, sim: sc.SimResult) -> pd.DataFrame:
    rows = []
    for t in range(1, len(sim.retire_heads_by_year)):
        rows.append({"연도": year_label(t),
                     "정년퇴직(예상)": round(sim.retire_heads_by_year[t], 1),
                     "재채용(baseline)": round(baseline.rehire_heads_by_year[t], 1),
                     "재채용(시뮬)": round(sim.rehire_heads_by_year[t], 1)})
    return pd.DataFrame(rows)


def attrition_org_chart(result: sc.SimResult) -> go.Figure:
    """연도별 (예측) 퇴직 인원 — 조직별 누적 막대. 연차 1부터(정년퇴직 포함)."""
    n = len(result.attrition_heads_by_year)
    yrs = [year_label(t) for t in range(1, n)]
    fig = go.Figure()
    for f in sc.FAMILY_LEVELS:
        vals = [result.attrition_heads_by_year[t].get(f, 0.0) for t in range(1, n)]
        fig.add_bar(x=yrs, y=vals, name=sc.FAMILY_LABEL[f], marker_color=FAMILY_COLOR[f],
                    hovertemplate=f"{sc.FAMILY_LABEL[f]} %{{y:.0f}}명<extra></extra>")
    _style(fig, 300, None)
    fig.update_xaxes(type="category")
    fig.update_layout(barmode="stack", xaxis_title="연도", yaxis_title="퇴직 인원(명)")
    return fig


def org_attrition_table(baseline: sc.SimResult, sim: sc.SimResult) -> pd.DataFrame:
    """조직별 (예측) 퇴직률 — 최종연도 예상 퇴직 인원 ÷ 전년도 인원."""
    T = len(sim.attrition_heads_by_year) - 1
    rows = []
    for f in sc.FAMILY_LEVELS:
        b_prev = sc.headcount_by_family(baseline.headcount_by_year[T - 1]).get(f, 0.0)
        s_prev = sc.headcount_by_family(sim.headcount_by_year[T - 1]).get(f, 0.0)
        b_leave = baseline.attrition_heads_by_year[T].get(f, 0.0)
        s_leave = sim.attrition_heads_by_year[T].get(f, 0.0)
        rows.append({
            "조직": sc.FAMILY_LABEL[f],
            "baseline 퇴직률": f"{(b_leave / b_prev * 100) if b_prev else 0:.1f}%",
            "시뮬 퇴직률": f"{(s_leave / s_prev * 100) if s_prev else 0:.1f}%",
            "시뮬 퇴직 인원(명)": round(s_leave),
        })
    return pd.DataFrame(rows)


def shape_silhouette(hc_year: dict[str, dict[str, float]], title: str,
                     color: str, x_max: float | None = None) -> go.Figure:
    """직급(사원→부장) 중앙정렬 실루엣.
    x_max: 좌·우를 같은 가로 스케일로 그려 폭을 직접 비교할 때 지정."""
    byg = sc.headcount_by_grade(hc_year)
    vals = [byg[g] for g in GRADE_ORDER]
    fig = go.Figure(go.Bar(
        y=GRADE_ORDER, x=vals, base=[-v / 2 for v in vals],
        orientation="h", marker_color=color, width=0.72,
        text=[f"{v:,.0f}명" for v in vals], textposition="outside",
        cliponaxis=False, hovertemplate="%{y} %{x:,.0f}명<extra></extra>",
    ))
    _style(fig, 300, title)
    _max = x_max if x_max is not None else (max(vals) if vals else 1)
    _max = _max or 1
    fig.update_layout(showlegend=False,
                      xaxis=dict(visible=False, range=[-_max * 0.72, _max * 0.72]))
    fig.update_yaxes(categoryorder="array", categoryarray=GRADE_ORDER,
                     showgrid=False, title=None)
    return fig


def mini_cost(result: sc.SimResult) -> go.Figure:
    yrs = [year_label(t) for t in range(len(result.labor_cost_by_year))]
    s = [c / 1e8 for c in result.labor_cost_by_year]
    fig = go.Figure(go.Scatter(x=yrs, y=s, mode="lines",
                               line=dict(color=C_BLUE, width=2)))
    _style(fig, 140)
    fig.update_xaxes(type="category")
    fig.update_layout(showlegend=False, xaxis_title=None, yaxis_title="억원",
                      yaxis=dict(title_font=dict(size=10)))
    return fig


# =============================================================
# ① 시뮬레이션 결과 — 가장 위에 표시 (KPI 타일)
# =============================================================
end_base = baseline.headcount_by_year[-1]
end_sim = sim.headcount_by_year[-1]
tot_base = sc.total_headcount(end_base)
tot_sim = sc.total_headcount(end_sim)
top_base = sc.top_level_share(end_base)
top_sim = sc.top_level_share(end_sim)
cum_delta = sim.cum_cost_delta_vs_baseline

head_gap = tot_sim - tot_base
top_delta = top_sim - top_base

_cost_cls = "down" if cum_delta >= 0 else "up"
_cost_arrow = "▲" if cum_delta >= 0 else "▼"
_cost_txt = "인건비 증가 방향" if cum_delta >= 0 else "인건비 감소 방향"
_head_cls = "up" if head_gap >= 0 else "down"
_head_arrow = "▲" if head_gap >= 0 else "▼"
_top_cls = "up" if top_delta >= 0 else "down"
_top_arrow = "▲" if top_delta >= 0 else "▼"

st.markdown("## 시뮬레이션 결과")
st.markdown(
    f'''
<div class="kpi-row">
  <div class="kpi fill"><div class="label">{years}년 누적 인건비 Δ (vs baseline)</div>
    <div class="value">{cum_delta/1e8:+,.0f}억</div>
    <div class="delta {_cost_cls}">{_cost_arrow} {_cost_txt}</div></div>
  <div class="kpi"><div class="label">최종연도 총원</div>
    <div class="value">{tot_sim:,.0f}명</div>
    <div class="delta {_head_cls}">{_head_arrow} {head_gap:+,.0f}명 (vs baseline {tot_base:,.0f}명)</div></div>
  <div class="kpi"><div class="label">부장 비중</div>
    <div class="value">{top_sim:.1f}%</div>
    <div class="delta {_top_cls}">{_top_arrow} {top_delta:+.1f}%p (vs baseline {top_base:.1f}%)</div></div>
</div>''',
    unsafe_allow_html=True)

if st.button("스냅샷 저장"):
    controls = {"years": int(years),
                "promo_by_grade": dict(promo_by_grade),
                "attr_by_grade": dict(attr_by_grade),
                "attr_by_age": dict(attr_by_age),
                "raise_by_grade": dict(raise_by_grade),
                "rehire_pct": float(rehire_pct)}
    label = snap.make_label(**controls)
    st.session_state["snapshots"].append(snap.capture(label, controls, adj, sim))
    st.toast(f"스냅샷 저장: {label}")


# =============================================================
# ② 좌우 비교 — baseline ↔ 시뮬 (기준 연도 슬라이더 공용)
# =============================================================
st.markdown("### baseline ↔ 시뮬 좌우 비교")
st.caption(f"기준연도 = 올해({BASE_YEAR}) 현재 인원 스냅샷. 조정 효과는 "
           f"내년({BASE_YEAR + 1})부터 {BASE_YEAR + years}년까지 추계에 반영됩니다.")
# 기준 연도 슬라이더 — 아래 '직급별 인원 상세'와 '직급 구조 실루엣'이 함께 따라간다.
# 추계 연수(years)가 바뀌면 옵션 범위가 달라지므로 key 에 years 를 포함해 리셋.
_sel_years = list(range(len(sim.headcount_by_year)))
sel_t = st.select_slider("기준 연도 (직급별 상세·실루엣 공용)", options=_sel_years,
                         value=_sel_years[-1], format_func=year_label,
                         key=f"k_sel_year_{years}",
                         help="좌우로 움직이면 아래 직급별 인원 상세와 실루엣이 해당 연도로 갱신")

# 좌우 동일 축(y) 고정 — 양쪽 최대 총원 기준으로 같은 스케일에서 비교.
_cmp_y_max = max(
    max(sc.total_headcount(hc) for hc in baseline.headcount_by_year),
    max(sc.total_headcount(hc) for hc in sim.headcount_by_year)) * 1.08
left, right = st.columns(2)
with left:
    st.markdown("#### BASELINE (조정 없음)")
    if SHOW_TABLE:
        st.caption("연도별 직급 인원 · 총원")
        st.dataframe(headcount_table(baseline), use_container_width=True, hide_index=True)
    else:
        st.plotly_chart(area_by_grade(baseline, "인력 구조 (직급 누적)", y_max=_cmp_y_max),
                        use_container_width=True, key="area_base_grade", config=PLOTLY_CONFIG)
with right:
    st.markdown("#### 시뮬레이션 (조정 반영)")
    if SHOW_TABLE:
        st.caption("연도별 직급 인원 · 총원")
        st.dataframe(headcount_table(sim), use_container_width=True, hide_index=True)
    else:
        st.plotly_chart(area_by_grade(sim, "인력 구조 (직급 누적)", y_max=_cmp_y_max),
                        use_container_width=True, key="area_sim_grade", config=PLOTLY_CONFIG)

# 선택 연도의 절대값 + 변화값(Δ) 상세 — 직급별
st.markdown(f"##### {year_label(sel_t)} 직급별 인원 — 절대값 · Δ")
st.dataframe(grade_delta_table(sel_t), use_container_width=True, hide_index=True)


# =============================================================
# ③ 적용 변수 — baseline vs 시뮬에 어떤 변수값이 들어갔는지
# =============================================================
st.markdown("### 적용 변수 (baseline vs 시뮬)")
st.markdown(
    '<span class="lever-chip"><b>BASELINE</b> 조정 없음 · 인상 0% · '
    f'재채용률 {DEFAULT_REHIRE_PCT:g}%</span>'
    f'<span class="lever-chip sim"><b>시뮬</b> {LEVER_DESC} · 재채용률 {rehire_pct:g}%</span>',
    unsafe_allow_html=True)
_var_rows = []
for g in GRADE_ORDER:
    _f0 = base_params.families[0]   # 승진·퇴직률은 조직 공통(직급별) — 첫 조직 값으로 표시
    _var_rows.append({
        "직급": g,
        "승진율 baseline": f"{base_params.promotion_rate[_f0][g] * 100:.1f}%",
        "승진율 시뮬": f"{sim_params.promotion_rate[_f0][g] * 100:.1f}%",
        "퇴직률 baseline": f"{base_params.attrition_rate[_f0][g] * 100:.1f}%",
        "퇴직률 시뮬": f"{sim_params.attrition_rate[_f0][g] * 100:.1f}%",
        "연 인상률 시뮬": f"{raise_by_grade[g]:g}%",
        "정년도래율(가정)": f"{sc.RETIRE_RATE[g] * 100:g}%",
    })
st.dataframe(pd.DataFrame(_var_rows), use_container_width=True, hide_index=True)
st.caption("승진율은 직급별로 다르며(부장=0, 임원 미고려), 퇴직률 시뮬값은 "
           "직급별 조정 × 나이별 조정(직급 나이 구성비 가중)을 합성한 결과입니다. "
           "인상률 baseline 은 전 직급 0%.")

st.divider()


# =============================================================
# ④ 총 인건비 (baseline vs 시뮬)
# =============================================================
st.markdown("#### 총 인건비 (baseline vs 시뮬)")
if SHOW_TABLE:
    st.dataframe(cost_table(baseline, sim), use_container_width=True, hide_index=True)
else:
    st.plotly_chart(cost_chart(baseline, sim), use_container_width=True,
                    key="cost_chart", config=PLOTLY_CONFIG)

# --- 추계 기간 전체 누적 인건비: baseline vs 시뮬 합산 + Δ(금액·비율) 강조 ---
cum_base = sum(baseline.labor_cost_by_year)
cum_sim = sum(sim.labor_cost_by_year)
cum_diff = cum_sim - cum_base
cum_pct = (cum_diff / cum_base * 100.0) if cum_base else 0.0
mc1, mc2, mc3 = st.columns(3)
mc1.metric(f"baseline 누적 인건비 ({years}년)", f"{cum_base / 1e8:,.0f}억")
mc2.metric(f"시뮬 누적 인건비 ({years}년)", f"{cum_sim / 1e8:,.0f}억",
           delta=f"{cum_diff / 1e8:+,.0f}억", delta_color="inverse")
mc3.metric("누적 Δ (vs baseline)", f"{cum_diff / 1e8:+,.0f}억",
           delta=f"{cum_pct:+.2f}%", delta_color="inverse")
st.caption(f"baseline 누적 {cum_base / 1e8:,.0f}억 → 시뮬 {cum_sim / 1e8:,.0f}억 "
           f"(Δ {cum_diff / 1e8:+,.0f}억, {cum_pct:+.2f}%)")


# =============================================================
# ⑤ 정년 재채용 인원 — 연도별
# =============================================================
st.markdown("#### 정년 재채용 인원 (연도별)")
st.caption(f"매년 정년도래율(가정: 과장 0.5% · 차장 1% · 리더 3% · 부장 6%)만큼 "
           f"정년퇴직이 발생하고, 그중 재채용률({rehire_pct:g}%)만큼 같은 직급으로 "
           f"촉탁 재채용됩니다. 재채용 인원은 다음 해부터 전이·인건비에 반영.")
if SHOW_TABLE:
    st.dataframe(retire_rehire_table(baseline, sim),
                 use_container_width=True, hide_index=True)
else:
    st.plotly_chart(retire_rehire_chart(sim), use_container_width=True,
                    key="retire_chart", config=PLOTLY_CONFIG)
_ret_sum = sum(sim.retire_heads_by_year)
_reh_sum = sum(sim.rehire_heads_by_year)
st.caption(f"{years}년 합계: 정년퇴직(예상) {_ret_sum:,.0f}명 · 재채용 {_reh_sum:,.0f}명")


# =============================================================
# ⑥ (예측) 퇴직 — 조직별
# =============================================================
st.markdown("#### (예측) 퇴직 — 조직별")
st.caption("연도별 예측 퇴직 인원(자발 이직 + 정년퇴직)을 조직별로 나눠 봅니다. "
           "퇴직률 조정(직급별 × 나이별)이 조직별 인력 구성 차이에 따라 다르게 반영됩니다.")
_att_l, _att_r = st.columns([3, 2])
with _att_l:
    if SHOW_TABLE:
        _att_rows = []
        for t in range(1, len(sim.attrition_heads_by_year)):
            row = {"연도": year_label(t)}
            row.update({sc.FAMILY_LABEL[f]: round(sim.attrition_heads_by_year[t].get(f, 0.0))
                        for f in sc.FAMILY_LEVELS})
            _att_rows.append(row)
        st.dataframe(pd.DataFrame(_att_rows), use_container_width=True, hide_index=True)
    else:
        st.plotly_chart(attrition_org_chart(sim), use_container_width=True,
                        key="attr_org_chart", config=PLOTLY_CONFIG)
with _att_r:
    st.markdown(f"**조직별 (예측) 퇴직률 — 최종연도({year_label(len(_sel_years) - 1)}) 기준**")
    st.dataframe(org_attrition_table(baseline, sim),
                 use_container_width=True, hide_index=True)


# =============================================================
# ⑦ 직급 구조 실루엣 — 왼쪽 패널 선택 가능 (기본 BASELINE)
# =============================================================
st.markdown("#### 직급 구조 실루엣")
st.caption("직급(사원→부장)별 인원을 중앙정렬한 실루엣. 위의 '기준 연도' 슬라이더를 "
           "움직이면 좌·우가 같은 연도로 동시에 갱신됩니다. "
           "허리(과장·차장=중간관리 계층)가 얇으면 모래시계형.")
_sil_opts = {"BASELINE (기본)": ("baseline", baseline, C_BASE),
             "시뮬레이션": ("시뮬", sim, C_BLUE)}
sel_left = st.selectbox("왼쪽 패널", list(_sil_opts.keys()), index=0,
                        key="k_sil_left",
                        help=f"기본은 BASELINE 고정. 예: 기준 연도를 {BASE_YEAR + 1}로 두면 "
                             f"BASELINE {BASE_YEAR + 1} ↔ 시뮬 {BASE_YEAR + 1} 비교.")
_l_name, _l_res, _l_color = _sil_opts[sel_left]
# 좌우 같은 가로 스케일 — 선택 연도의 양쪽 직급 최대값 기준.
_sil_max = max(
    max(sc.headcount_by_grade(_l_res.headcount_by_year[sel_t]).values()),
    max(sc.headcount_by_grade(sim.headcount_by_year[sel_t]).values()))
sil_l, sil_r = st.columns(2)
with sil_l:
    st.plotly_chart(shape_silhouette(_l_res.headcount_by_year[sel_t],
                                     f"{_l_name.upper()} · {year_label(sel_t)}", _l_color,
                                     x_max=_sil_max),
                    use_container_width=True, key="sil_left", config=PLOTLY_CONFIG)
with sil_r:
    st.plotly_chart(shape_silhouette(sim.headcount_by_year[sel_t],
                                     f"시뮬 · {year_label(sel_t)}", C_BLUE,
                                     x_max=_sil_max),
                    use_container_width=True, key="sil_right", config=PLOTLY_CONFIG)

with st.expander("최종연도 조직·직급별 인원 상세 (baseline / 시뮬 / Δ)"):
    rows = []
    for f in sc.FAMILY_LEVELS:
        for lvl in sc.FAMILY_LEVELS[f]:
            b = end_base[f].get(lvl, 0.0)
            s = end_sim[f].get(lvl, 0.0)
            rows.append({"조직": sc.FAMILY_LABEL[f], "직급": lvl,
                         "baseline": round(b), "시뮬": round(s), "Δ": round(s - b)})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# =============================================================
# ⑧ 스냅샷 저장·비교
# =============================================================
st.divider()
st.markdown("## 스냅샷 저장·비교")
snaps: list[snap.Snapshot] = st.session_state["snapshots"]

if not snaps:
    st.info("아직 저장된 스냅샷이 없습니다. 레버를 조정하고 위의 [스냅샷 저장] 을 "
            "누르면 여러 변수 조합을 나란히 비교할 수 있습니다. "
            "조정 없이 저장하면 'baseline' 기준선 스냅샷이 됩니다.")
else:
    st.markdown("#### 저장된 스냅샷")
    top_bar = st.columns([6, 1])
    with top_bar[1]:
        if st.button("전체 지우기", use_container_width=True):
            st.session_state["snapshots"] = []
            st.rerun()

    for s in snaps:
        c_label, c_info, c_restore, c_del = st.columns([4, 3, 1.2, 1.2])
        with c_label:
            s.label = st.text_input("라벨", value=s.label, key=f"label_{s.snapshot_id}",
                                    label_visibility="collapsed")
        with c_info:
            c = s.controls
            st.caption(f"연수 {c['years']} · "
                       + lever_desc(c.get("promo_by_grade", {}), c.get("attr_by_grade", {}),
                                    c.get("attr_by_age", {}), c.get("raise_by_grade", {}),
                                    c.get("rehire_pct", DEFAULT_REHIRE_PCT)))
        with c_restore:
            if st.button("복원", key=f"restore_{s.snapshot_id}", use_container_width=True):
                st.session_state["_pending_restore"] = dict(s.controls)
                st.rerun()
        with c_del:
            if st.button("삭제", key=f"del_{s.snapshot_id}", use_container_width=True):
                st.session_state["snapshots"] = [
                    x for x in snaps if x.snapshot_id != s.snapshot_id]
                st.rerun()

    st.markdown("#### 비교표")
    st.dataframe(snap.comparison_table(snaps), use_container_width=True, hide_index=True)
    st.caption("※ 누적 Δ는 각 스냅샷의 자기 horizon 무조정 baseline 대비입니다. "
               "'연수'가 다른 행은 기준 horizon 이 달라 절대 Δ를 직접 비교하지 마세요. "
               "baseline 스냅샷(조정 없음)의 Δ는 '—' 로 표기됩니다.")

    if SHOW_TABLE:
        st.markdown("#### 스냅샷별 최종연도 인원 (직급)")
        final_rows = []
        for s in snaps:
            end = s.result.headcount_by_year[-1]
            byg = sc.headcount_by_grade(end)
            row = {"라벨": s.label}
            row.update({g: round(byg.get(g, 0.0)) for g in GRADE_ORDER})
            row["총원"] = round(sc.total_headcount(end))
            final_rows.append(row)
        st.dataframe(pd.DataFrame(final_rows), use_container_width=True, hide_index=True)
    else:
        st.markdown("#### 미니차트 (스냅샷별 인력구조 · 인건비)")
        PER_ROW = 4
        for start in range(0, len(snaps), PER_ROW):
            row = snaps[start:start + PER_ROW]
            cols = st.columns(len(row))
            for s, col in zip(row, cols):
                with col:
                    st.markdown(f"**{s.label}**")
                    st.plotly_chart(area_by_family(s.result, "", height=180, showlegend=False),
                                    use_container_width=True, key=f"mini_area_{s.snapshot_id}",
                                    config=PLOTLY_CONFIG)
                    st.plotly_chart(mini_cost(s.result), use_container_width=True,
                                    key=f"mini_cost_{s.snapshot_id}", config=PLOTLY_CONFIG)


# =============================================================
# ⑨ P-GPT — 대화형 인사이트 챗봇 (Claude, 메모리 유지 / 키 없으면 rule 폴백)
# =============================================================
st.divider()
st.markdown("### P-GPT · 인사이트 챗봇")

PGPT_AVATAR = str(Path(__file__).parent / "assets" / "pgpt_avatar.svg")
if not Path(PGPT_AVATAR).exists():
    PGPT_AVATAR = "🔷"

# 목업용 프리필 대화 — 첫 로드 시 채워 넣어 데모 화면이 비어 보이지 않게.
PREFILL_CHAT = [
    {"role": "user",
     "content": "지금 인력구조에서 가장 큰 리스크가 뭐야?"},
    {"role": "assistant",
     "content": "현재 구조는 과장·차장(허리)이 얇은 **모래시계형**입니다. 사원·대리 기반은 "
                "두껍고 리더·부장 고참층도 남아 있지만, 5~10년 뒤 이 고참층이 정년으로 빠지면 "
                "승계할 중간관리자가 없어 **리더십 공백**이 옵니다.\n\n"
                "추천 시나리오: **승진율 조정**에서 사원·대리 승진율을 +10~20% 올려 "
                "허리를 채우는 효과를 확인해 보세요. 인건비 부담은 '누적 인건비 Δ' KPI로 "
                "같이 보시면 됩니다."},
    {"role": "user",
     "content": "정년퇴직으로 빠지는 인원은 어떻게 보완해?"},
    {"role": "assistant",
     "content": "두 가지 레버가 있습니다.\n\n"
                "1. **정년 재채용률** — baseline은 정년퇴직자의 30%를 촉탁 재채용하는 "
                "가정입니다. 50%로 올리면 리더·부장급 경험 인력이 더 오래 유지되지만 "
                "상위직급 인건비도 함께 늘어납니다.\n"
                "2. **퇴직률 조정(나이별)** — 50대+ 퇴직률을 낮추는 리텐션 시나리오도 "
                "가능합니다. 정년(나이 기인)분은 하한으로 보호되어 줄지 않으니, 자발 이탈 "
                "억제 효과만 반영됩니다.\n\n"
                "'정년 재채용 인원 (연도별)' 차트에서 연도별 정년퇴직 예상 인원과 재채용 "
                "인원을 확인한 뒤 레버를 조정해 보세요."},
]

insight_ctx = {
    "years": int(years),
    "lever_desc": LEVER_DESC,
    "rehire_pct": float(rehire_pct),
    "tot_base": tot_base, "tot_sim": tot_sim,
    "cum_delta_eok": cum_delta / 1e8,
    "top_base": top_base, "top_sim": top_sim,
    "retire_final": sim.retire_heads_by_year[-1],
    "rehire_final": sim.rehire_heads_by_year[-1],
    "family_end": {sc.FAMILY_LABEL[f]: round(v)
                   for f, v in sc.headcount_by_family(end_sim).items()},
    # 저장된 스냅샷 요약 — 챗봇이 여러 시나리오를 비교·언급할 수 있게 컨텍스트로 전달.
    "snapshots": [
        {
            "label": s.label,
            "years": s.controls["years"],
            "lever_desc": lever_desc(
                s.controls.get("promo_by_grade", {}), s.controls.get("attr_by_grade", {}),
                s.controls.get("attr_by_age", {}), s.controls.get("raise_by_grade", {}),
                s.controls.get("rehire_pct", DEFAULT_REHIRE_PCT)),
            "final_total": round(s.final_total),
            "cum_delta_eok": s.cum_cost_delta / 1e8,
            "top_share": s.top_share,
        }
        for s in st.session_state.get("snapshots", [])
    ],
}

if insight_bot.has_api_key():
    st.caption("P-GPT 대화 모드 — 현재 시뮬 수치를 근거로 제안·질문하며 대화합니다.")
else:
    st.caption("P-GPT rule 폴백 모드 — ANTHROPIC_API_KEY 설정 시 대화가 활성화됩니다.")

col_chat, col_clear = st.columns([6, 1])
with col_clear:
    if st.button("대화 초기화", use_container_width=True):
        st.session_state["chat"] = [dict(m) for m in PREFILL_CHAT]
        st.rerun()

if "chat" not in st.session_state:
    st.session_state["chat"] = [dict(m) for m in PREFILL_CHAT]

# 메시지는 고정 높이 박스 안에서만 스크롤. chat_input 을 메인 루트에 두면
# Streamlit 이 앱 전체를 chat 앱으로 간주해 '로드 시 맨 아래로 자동 스크롤'되므로
# (KPI·차트가 아니라 챗봇부터 보이는 문제), 컬럼 안에 넣어 인라인으로 렌더한다.
chat_box = st.container(height=380, border=True)
with chat_box:
    for m in st.session_state["chat"]:
        avatar = PGPT_AVATAR if m["role"] == "assistant" else None
        with st.chat_message(m["role"], avatar=avatar):
            st.markdown(m["content"])

prompt = st.columns(1)[0].chat_input("P-GPT에게 이 시뮬 결과에 대해 물어보세요 (예: 인건비를 줄이려면?)")
if prompt:
    st.session_state["chat"].append({"role": "user", "content": prompt})
    with chat_box:
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant", avatar=PGPT_AVATAR):
            if insight_bot.has_api_key():
                try:
                    reply = st.write_stream(
                        insight_bot.stream_reply(st.session_state["chat"], insight_ctx))
                except Exception as e:  # 키 오류·네트워크 등 → rule 폴백
                    reply = insight_bot.rule_reply(st.session_state["chat"], insight_ctx)
                    st.markdown(reply)
                    st.caption(f"(Claude 호출 실패로 rule 폴백: {type(e).__name__})")
            else:
                reply = insight_bot.rule_reply(st.session_state["chat"], insight_ctx)
                st.markdown(reply)
    st.session_state["chat"].append({"role": "assistant", "content": reply})

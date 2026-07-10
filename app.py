"""
app.py — POSCO HR 인력운영 시뮬레이터 (v3, 배포 엔트리)
==================================================
승진율 / 퇴직률 / 인건비 인상률을 조정하면 향후 인력 구조와 총 인건비가 어떻게
변하는지 결정론 마르코프로 추계해 AS-IS(무조정) ↔ 시뮬1 ↔ 시뮬2 를 나란히 비교하고,
변수 조합을 스냅샷으로 저장·비교한다. LLM 인사이트 챗봇 P-GPT(선택).

  - 결정론 코어:   sim_core.py  (직군 4종 P/R/E/A × 단계별 전이 + 인건비)
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

# ★ 스테일 모듈 자가 복구 — Streamlit(로컬·Cloud)은 소스가 바뀌면 엔트리 스크립트(app.py)만
#   다시 실행하고, 이미 import 된 모듈은 프로세스 메모리의 옛 버전을 재사용한다.
#   배포 갱신 직후 옛 sim_core 가 남아 있으면 Adjustments 에 새 필드가 없어
#   TypeError(unexpected keyword)로 죽으므로, 새 필드 부재를 감지해 강제 리로드한다.
#   (reload 는 모듈 객체를 제자리 갱신하므로 snapshots 등 다른 참조도 함께 새 코드를 본다.)
import dataclasses as _dc
import importlib as _importlib
_ADJ_FIELDS = {_f.name for _f in _dc.fields(sc.Adjustments)}
if not {"attrition_delta_by_level", "promotion_delta_by_level"} <= _ADJ_FIELDS:
    _importlib.reload(sc)
    _importlib.reload(snap)
    _importlib.reload(insight_bot)

st.set_page_config(page_title="POSCO HR 시뮬레이터", layout="wide", page_icon="🔷")

# 추계 연도 라벨: 연차 0 = 올해(기준 스냅샷), 추계·조정 효과는 내년(BASE_YEAR+1)부터 반영.
# (코어는 t=0에 전이·인상 미적용이므로 조정 효과는 이미 내년부터 시작한다. 여기선 표시만 실제 연도로.)
BASE_YEAR = date.today().year


def year_label(t: int) -> str:
    """연차 t → 실제 연도 문자열. 기준연(t=0)은 '올해' 표기."""
    return f"{BASE_YEAR}(기준)" if t == 0 else str(BASE_YEAR + t)


# =============================================================
# 직급(하위→상위) — 실루엣 표시 + 직급별 승진율·인상률·퇴직률 입력에 공용으로 쓴다.
#   직군마다 단계 수가 달라(3~7단계), 각 단계의 '상대 위치'로 6개 직급에 매핑.
#   임원은 고려하지 않는다(직원 직급만): 사원 → 대리 → 과장 → 차장 → 리더 → 부장.
# =============================================================
TIER_ORDER = ["사원", "대리", "과장", "차장", "리더", "부장"]  # 아래→위


def _tier_of(i: int, n: int) -> str:
    """직군 내 i번째 단계(0-base, 총 n단계)의 상대 위치를 6개 직급에 매핑."""
    if n <= 1:
        return TIER_ORDER[0]
    k = min(5, int(i * 5 / (n - 1) + 0.5))
    return TIER_ORDER[k]


# 직급 × 연령대 구성비 (가정 더미) — '나이별 퇴직률 조정'을 직급별 배율로 환산할 때 사용.
#   예: 20대 퇴직률을 +10% 하면, 사원 직급은 20대 비중 55% 만큼 가중돼 +5.5% 적용.
AGE_BANDS = ["20대", "30대", "40대", "50대+"]
AGE_MIX = {
    "사원": {"20대": 0.55, "30대": 0.40, "40대": 0.05, "50대+": 0.00},
    "대리": {"20대": 0.20, "30대": 0.60, "40대": 0.18, "50대+": 0.02},
    "과장": {"20대": 0.03, "30대": 0.45, "40대": 0.45, "50대+": 0.07},
    "차장": {"20대": 0.00, "30대": 0.20, "40대": 0.60, "50대+": 0.20},
    "리더": {"20대": 0.00, "30대": 0.05, "40대": 0.55, "50대+": 0.40},
    "부장": {"20대": 0.00, "30대": 0.00, "40대": 0.40, "50대+": 0.60},
}

# 시나리오 정의 — AS-IS(무조정 기준선)는 항상 고정, 시뮬1/시뮬2 는 각자 레버 세트로 조정.
SCENARIOS = [("s1", "시뮬1"), ("s2", "시뮬2")]


def build_raise_by_level(tier_raise_pct: dict[str, float]) -> dict[str, dict[str, float]]:
    """직급별 인상률(%) → {직군:{단계:인상률(소수)}}. 각 단계는 소속 직급값을 받는다."""
    out: dict[str, dict[str, float]] = {}
    for f, levels in sc.FAMILY_LEVELS.items():
        n = len(levels)
        out[f] = {lvl: tier_raise_pct.get(_tier_of(i, n), 0.0) / 100.0
                  for i, lvl in enumerate(levels)}
    return out


def raise_summary(rbt: dict[str, float]) -> str:
    """직급별 인상률(%)을 짧게 요약. 전부 같으면 'X%', 다르면 0 아닌 직급만 나열."""
    vals = [rbt.get(t, 0.0) for t in TIER_ORDER]
    if all(abs(v - vals[0]) < 1e-9 for v in vals):
        return f"{vals[0]:g}%"
    parts = [f"{t} {rbt.get(t, 0.0):g}%" for t in TIER_ORDER if abs(rbt.get(t, 0.0)) > 1e-9]
    return "직급별(" + " · ".join(parts) + ")" if parts else "0%"


def build_delta_by_level(mode: str, by_tier_pctp: dict[str, float],
                         by_age_pctp: dict[str, float] | None = None
                         ) -> dict[str, dict[str, float]] | None:
    """직급별/나이별 %p 조정 → {직군:{단계:가감(소수)}}. '전체' 모드면 None(전역 delta 사용).
    승진율(전체/직급별)·퇴직률(전체/직급별/나이별) 공용.
    ★ 조정은 배율이 아니라 %p 덧셈: 승진율 18% 에 +1 을 주면 19%.

    나이별 모드는 직급 × 연령 구성비(AGE_MIX, 가정 더미)로 가중 평균해 직급별 %p 로 환산:
      단계 가감 = Σ(연령대 구성비 × 해당 연령대 %p) / 100
    """
    if mode == "전체":
        return None
    by_age_pctp = by_age_pctp or {}
    out: dict[str, dict[str, float]] = {}
    for f, levels in sc.FAMILY_LEVELS.items():
        n = len(levels)
        d = {}
        for i, lvl in enumerate(levels):
            tier = _tier_of(i, n)
            if mode == "직급별":
                pctp = by_tier_pctp.get(tier, 0.0)
            else:  # 나이별
                pctp = sum(AGE_MIX[tier][b] * by_age_pctp.get(b, 0.0) for b in AGE_BANDS)
            d[lvl] = pctp / 100.0
        out[f] = d
    return out


def scale_summary(mode: str, pctp: float, by_tier: dict[str, float],
                  by_age: dict[str, float] | None = None) -> str:
    """승진율/퇴직률 %p 조정 설정을 짧게 요약(라벨·캡션·챗봇 컨텍스트 공용)."""
    if mode == "전체":
        return f"{pctp:+g}%p"
    if mode == "직급별":
        nz = [f"{t} {by_tier.get(t, 0.0):+g}%p" for t in TIER_ORDER
              if abs(by_tier.get(t, 0.0)) > 1e-9]
        return "직급별(" + " · ".join(nz) + ")" if nz else "0%p"
    by_age = by_age or {}
    nz = [f"{b} {by_age.get(b, 0.0):+g}%p" for b in AGE_BANDS
          if abs(by_age.get(b, 0.0)) > 1e-9]
    return "나이별(" + " · ".join(nz) + ")" if nz else "0%p"


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
   ★ sticky 는 .st-key-lever_bar(내부 블록)가 아니라 그 부모 stLayoutWrapper 에 건다.
   내부 블록은 부모 래퍼와 높이가 같아 sticky 이동 공간이 0 → 스크롤을 따라오지 못했음.
   래퍼에 걸면 본문 컬럼 전체가 이동 범위가 되어 끝까지 따라온다. */
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
.kpi-row{ display:grid; grid-template-columns:repeat(3,1fr); gap:14px; margin:10px 0; }
.kpi{ border-radius:12px; padding:20px; border:1px solid var(--line); background:var(--panel); }
.kpi.fill{ background:linear-gradient(135deg,#002B5B 0%,#0072CE 100%); border:0; color:#fff; }
.kpi .label{ font-size:12px; font-weight:600; color:var(--muted); }
.kpi.fill .label{ color:#B7CDEA; }
.kpi .value{ font-size:28px; font-weight:800; letter-spacing:-.02em; margin-top:8px; }
.kpi .delta{ font-size:11.5px; font-weight:700; margin-top:6px; }
.kpi .up{ color:#1B8A5A; } .kpi .down{ color:#C33; } .kpi.fill .down{ color:#F3B4B4; }
.kpi.fill .up{ color:#9BE3C1; }

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
        '<span class="posco-badge">v3 · MOCKUP</span>'
        '</div>',
        unsafe_allow_html=True)
    st.markdown(
        '<div class="posco-sub">승진율·퇴직률·인건비 인상률을 시나리오별(시뮬1/시뮬2)로 조정하면 '
        '향후 인력 구조와 총 인건비 변화를 마르코프로 추계해 AS-IS 와 나란히 보여줍니다. '
        '결정론 rule 계산.</div>',
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
C_BASE = "#9FB3C8"       # AS-IS(baseline) 계열(회청 — 레퍼런스 뉴트럴)
C_GRID = "#EEF3F8"
C_INK = "#1A2B3C"
FONT_FAMILY = "Pretendard, system-ui, sans-serif"

# 직군 색상 — 레퍼런스 조직도의 4단 블루 스케일
FAMILY_COLOR = {"P": C_NAVY_DP, "R": "#1B4F8A", "E": C_BLUE_LT, "A": C_BLUE_XLT}

# 직급 색상 — 사원(연청)→부장(딥네이비) 단조 그라데이션.
TIER_COLOR = {"사원": "#BFDCF5", "대리": C_BLUE_XLT, "과장": C_BLUE_LT,
              "차장": C_BLUE, "리더": C_NAVY, "부장": C_NAVY_DP}

# 시나리오 색 — AS-IS 회청 / 시뮬1 블루 / 시뮬2 네이비
SCN_COLOR = {"asis": C_BASE, "s1": C_BLUE, "s2": C_NAVY}

# 레버 key ↔ 기본값 (시나리오별 접미사 _s1/_s2). 복원은 이 key 에 값을 써넣고 rerun.
SLIDER_DEFAULTS: dict = {"k_years": 5}
for _sid, _ in SCENARIOS:
    SLIDER_DEFAULTS[f"k_promo_mode_{_sid}"] = "전체"
    SLIDER_DEFAULTS[f"k_promo_{_sid}"] = 0.0
    SLIDER_DEFAULTS[f"k_attr_mode_{_sid}"] = "전체"
    SLIDER_DEFAULTS[f"k_attr_{_sid}"] = 0.0
    for _t in TIER_ORDER:
        SLIDER_DEFAULTS[f"k_promo_t_{_t}_{_sid}"] = 0.0
        SLIDER_DEFAULTS[f"k_attr_t_{_t}_{_sid}"] = 0.0
        SLIDER_DEFAULTS[f"k_raise_{_t}_{_sid}"] = 0.0
    for _b in AGE_BANDS:
        SLIDER_DEFAULTS[f"k_attr_a_{_b}_{_sid}"] = 0.0

# 차트 클릭 확대/툴바 끄기 (정적 표시) — 축 fixedrange 와 함께 줌·팬 차단
PLOTLY_CONFIG = {"displayModeBar": False, "staticPlot": False, "scrollZoom": False}


# =============================================================
# 위젯 생성 '이전' 세션 상태 초기화 + 복원 적용
#   (Streamlit 제약: 위젯이 만들어진 뒤엔 그 key 의 session_state 를 못 바꾼다)
# =============================================================
for _k, _v in SLIDER_DEFAULTS.items():
    st.session_state.setdefault(_k, _v)
st.session_state.setdefault("snapshots", [])

# ★ 잔존값 방어 — 배포 교체 직후에도 브라우저 세션(session_state)은 살아남는다.
#   구버전 위젯이 남긴 값이 새 위젯 범위 밖이면 하한으로 튀어 보이므로,
#   범위 밖/비정상 값은 기본값(0)으로 되돌린다. 모드 radio 도 옵션 밖이면 '전체'로.
# 승진율·퇴직률은 %p 가감(±10%p), 인상률은 % 게이지(0~10%).
_LEVER_BOUNDS: dict = {}
for _sid, _ in SCENARIOS:
    _LEVER_BOUNDS[f"k_promo_{_sid}"] = (-10.0, 10.0)
    _LEVER_BOUNDS[f"k_attr_{_sid}"] = (-10.0, 10.0)
    for _t in TIER_ORDER:
        _LEVER_BOUNDS[f"k_promo_t_{_t}_{_sid}"] = (-10.0, 10.0)
        _LEVER_BOUNDS[f"k_attr_t_{_t}_{_sid}"] = (-10.0, 10.0)
        _LEVER_BOUNDS[f"k_raise_{_t}_{_sid}"] = (0.0, 10.0)
    for _b in AGE_BANDS:
        _LEVER_BOUNDS[f"k_attr_a_{_b}_{_sid}"] = (-10.0, 10.0)
for _k, (_lo, _hi) in _LEVER_BOUNDS.items():
    try:
        if not (_lo <= float(st.session_state[_k]) <= _hi):
            raise ValueError
    except (TypeError, ValueError):
        st.session_state[_k] = SLIDER_DEFAULTS[_k]
for _sid, _ in SCENARIOS:
    if st.session_state.get(f"k_promo_mode_{_sid}") not in ("전체", "직급별"):
        st.session_state[f"k_promo_mode_{_sid}"] = "전체"
    if st.session_state.get(f"k_attr_mode_{_sid}") not in ("전체", "직급별", "나이별"):
        st.session_state[f"k_attr_mode_{_sid}"] = "전체"

# 복원/초기화 — _pending_restore 는 시나리오별 컨트롤 dict 의 '리스트'.
_pending = st.session_state.pop("_pending_restore", None)
if _pending:
    for _p in _pending:
        _sid = _p.get("scenario", "s1")
        st.session_state["k_years"] = int(_p.get("years", 5))
        st.session_state[f"k_promo_mode_{_sid}"] = _p.get("promo_mode", "전체")
        st.session_state[f"k_promo_{_sid}"] = float(_p.get("promo_pct", 0.0))
        st.session_state[f"k_attr_mode_{_sid}"] = _p.get("attr_mode", "전체")
        st.session_state[f"k_attr_{_sid}"] = float(_p.get("attr_pct", 0.0))
        _pt = _p.get("promo_by_tier", {})
        _rt = _p.get("raise_by_tier", {})
        _at = _p.get("attr_by_tier", {})
        _aa = _p.get("attr_by_age", {})
        for _t in TIER_ORDER:
            st.session_state[f"k_promo_t_{_t}_{_sid}"] = float(_pt.get(_t, 0.0))
            st.session_state[f"k_raise_{_t}_{_sid}"] = float(_rt.get(_t, 0.0))
            st.session_state[f"k_attr_t_{_t}_{_sid}"] = float(_at.get(_t, 0.0))
        for _b in AGE_BANDS:
            st.session_state[f"k_attr_a_{_b}_{_sid}"] = float(_aa.get(_b, 0.0))

inject_css()
render_header()


# =============================================================
# 조정 레버 — 상단 고정 바 (엑셀 첫 행 틀고정 스타일)
#   시뮬1/시뮬2 두 시나리오의 레버 세트를 좌우로 나란히. AS-IS 는 항상 무조정.
# =============================================================
def render_scenario_levers(sid: str, name: str) -> dict:
    """한 시나리오의 승진율/퇴직률/인상률 popover 3개를 그리고 설정값을 dict 로 반환."""
    c1, c2, c3 = st.columns(3)
    promo_by_tier = {t: float(st.session_state.get(f"k_promo_t_{t}_{sid}", 0.0))
                     for t in TIER_ORDER}
    promo_pct = float(st.session_state.get(f"k_promo_{sid}", 0.0))
    attr_by_tier = {t: float(st.session_state.get(f"k_attr_t_{t}_{sid}", 0.0))
                    for t in TIER_ORDER}
    attr_by_age = {b: float(st.session_state.get(f"k_attr_a_{b}_{sid}", 0.0))
                   for b in AGE_BANDS}
    attr_pct = float(st.session_state.get(f"k_attr_{sid}", 0.0))

    with c1:
        # 승진율 조정 — %p 가감(덧셈). 전체 일괄 / 직급별(사원→리더).
        #   부장은 최상위 승진율 0 고정이라 제외.
        with st.popover("승진율", use_container_width=True):
            promo_mode = st.radio("조정 방식", ["전체", "직급별"], horizontal=True,
                                  key=f"k_promo_mode_{sid}",
                                  help="전체=전 직급 일괄 %p 가감 / 직급별=사원~리더 직급별 %p 가감")
            if promo_mode == "전체":
                promo_pct = st.number_input(
                    "전체 승진율 조정 (%p)", min_value=-10.0, max_value=10.0,
                    step=0.1, format="%.1f", key=f"k_promo_{sid}",
                    help="baseline 승진율에 그대로 더합니다. 기본 0 = 조정 없음. "
                         "예: 승진율 18% 에 +1 → 19%. "
                         "재직률이 음수가 되지 않도록 (1-퇴직률) 이하로 자동 제한.")
            else:
                st.caption("직급별 승진율 %p 가감. 기본 0 = 조정 없음. "
                           "예: 대리 +2 = 대리급 승진율 16% → 18% (허리 채우기 시뮬). "
                           "부장(최상위)은 승진율 0 고정이라 제외.")
                for _t in TIER_ORDER[:-1]:
                    promo_by_tier[_t] = st.number_input(
                        f"{_t} 승진율 조정 (%p)", min_value=-10.0, max_value=10.0,
                        step=0.1, format="%.1f", key=f"k_promo_t_{_t}_{sid}")
    with c2:
        # 퇴직률 조정 — %p 가감(덧셈). 전체 / 직급별(사원→부장) / 나이별(20대→50대+).
        #   나이별은 직급 × 연령 구성비(AGE_MIX)로 가중해 직급별 %p 로 환산된다.
        with st.popover("퇴직률", use_container_width=True):
            attr_mode = st.radio("조정 방식", ["전체", "직급별", "나이별"], horizontal=True,
                                 key=f"k_attr_mode_{sid}",
                                 help="전체=전 직급 일괄 %p 가감 / 직급별=사원~부장 직급별 %p 가감 / "
                                      "나이별=연령대별 %p 가감(직급별 연령 구성비로 가중 환산)")
            # 조정 범위 ±10%p. (0 = 조정 없음. 정합성은 코어가 보장:
            #    stay = 1 - attrition - promotion 잔차 + stay>=0 셀 단위 클립)
            if attr_mode == "전체":
                attr_pct = st.number_input(
                    "전체 퇴직률 조정 (%p)", min_value=-10.0, max_value=10.0,
                    step=0.1, format="%.1f", key=f"k_attr_{sid}",
                    help="baseline 퇴직률에 그대로 더합니다. 기본 0 = 조정 없음. "
                         "예: 퇴직률 14% 에 -1 → 13%.")
            elif attr_mode == "직급별":
                st.caption("직급별 퇴직률 %p 가감. 기본 0 = 조정 없음. "
                           "예: 과장 -1 = 과장급 퇴직률 9% → 8% (이탈 방지 시뮬).")
                for _t in TIER_ORDER:
                    attr_by_tier[_t] = st.number_input(
                        f"{_t} 퇴직률 조정 (%p)", min_value=-10.0, max_value=10.0,
                        step=0.1, format="%.1f", key=f"k_attr_t_{_t}_{sid}")
            else:
                st.caption("연령대별 %p 가감. 기본 0 = 조정 없음. 직급별 연령 구성비(가정 더미)로 "
                           "가중해 직급별 %p 로 환산.")
                for _b in AGE_BANDS:
                    attr_by_age[_b] = st.number_input(
                        f"{_b} 퇴직률 조정 (%p)", min_value=-10.0, max_value=10.0,
                        step=0.1, format="%.1f", key=f"k_attr_a_{_b}_{sid}")
    with c3:
        raise_by_tier = {}
        with st.popover("연 인상률", use_container_width=True):
            st.caption("직급(사원→부장)별 단가 인상률. baseline=전 직급 0% 기준이라 "
                       "올린 만큼 누적 Δ가 +로 잡힘. 매년 단가=단가×(1+인상률)^연차.")
            for _t in TIER_ORDER:
                raise_by_tier[_t] = st.number_input(
                    f"{_t} 인상률 (%)", min_value=0.0, max_value=10.0, step=0.05,
                    format="%.2f", key=f"k_raise_{_t}_{sid}",
                    help="예: 과장·차장(중간관리)만 5%로 올려 이탈 방지 시뮬.")

    promo_desc = scale_summary(promo_mode, promo_pct, promo_by_tier)
    attr_desc = scale_summary(attr_mode, attr_pct, attr_by_tier, attr_by_age)
    return {
        "scenario": sid, "name": name,
        "promo_mode": promo_mode, "promo_pct": promo_pct, "promo_by_tier": promo_by_tier,
        "attr_mode": attr_mode, "attr_pct": attr_pct,
        "attr_by_tier": attr_by_tier, "attr_by_age": attr_by_age,
        "raise_by_tier": raise_by_tier,
        "promo_desc": promo_desc, "attr_desc": attr_desc,
        "raise_desc": raise_summary(raise_by_tier),
    }


with st.container(key="lever_bar"):
    top_bar = st.columns([1.8, 1.0, 0.8], vertical_alignment="bottom")
    with top_bar[0]:
        years = st.slider("추계 연수", 1, 15, step=1, key="k_years",
                          help="1년(내년만)부터 가능. 가벼운 단기 시뮬은 1~2년으로. "
                               "시뮬1·시뮬2 공통.")
    with top_bar[1]:
        view_mode = st.radio("결과 보기 방식", ["차트", "표(숫자)"], horizontal=True,
                             help="차트=클릭 확대 없이 정적으로 표시 / 표=숫자만")
    with top_bar[2]:
        if st.button("레버 초기화", use_container_width=True,
                     help="시뮬1·시뮬2 의 승진율·퇴직률·인상률을 전부 기본값 0%로 되돌립니다."):
            st.session_state["_pending_restore"] = [
                {"scenario": sid, "years": int(st.session_state.get("k_years", 5))}
                for sid, _ in SCENARIOS]
            st.rerun()

    lev_cols = st.columns(2)
    levers: dict[str, dict] = {}
    for (sid, name), col in zip(SCENARIOS, lev_cols):
        with col:
            st.markdown(f"**{name} 레버**")
            levers[sid] = render_scenario_levers(sid, name)

    st.caption(
        " | ".join(
            f"{lev['name']}: 승진 {lev['promo_desc']} · 퇴직 {lev['attr_desc']} · "
            f"인상 {lev['raise_desc']}"
            for lev in levers.values())
        + " · © POSCO HR PoC · 더미데이터 기반 목업")

SHOW_TABLE = view_mode == "표(숫자)"


# =============================================================
# 계산 — AS-IS(무조정) + 시뮬1/시뮬2 (각자 레버 반영)   [결정론]
# =============================================================
@st.cache_data(show_spinner=False)
def compute_baseline(years: int):
    base_params = sc.build_default_params(years=years)
    return base_params, sc.run(base_params)   # AS-IS = 전 직급 인상 0% 기준선


@st.cache_data(show_spinner=False)
def compute_scenario(years: int,
                     promo_mode: str, promo_pct: float, promo_tier_tuple: tuple,
                     attr_mode: str, attr_pct: float,
                     attr_tier_tuple: tuple, attr_age_tuple: tuple,
                     raise_tuple: tuple):
    # *_tuple: TIER_ORDER/AGE_BANDS 순서의 조정값(%p). 캐시 키 안정화를 위해 튜플로 받는다.
    # ★ 승진율·퇴직률 조정은 %p 덧셈: 예 승진율 18% + 1%p → 19%. (인상률만 % 게이지)
    base_params, baseline = compute_baseline(years)
    adj = sc.Adjustments(
        # '전체' 모드만 전역 delta, 직급별/나이별은 셀 단위 delta dict 로 환산해 전달.
        promotion_delta=(promo_pct / 100.0) if promo_mode == "전체" else 0.0,
        promotion_delta_by_level=build_delta_by_level(
            promo_mode, dict(zip(TIER_ORDER, promo_tier_tuple))),
        attrition_delta=(attr_pct / 100.0) if attr_mode == "전체" else 0.0,
        attrition_delta_by_level=build_delta_by_level(
            attr_mode,
            dict(zip(TIER_ORDER, attr_tier_tuple)),
            dict(zip(AGE_BANDS, attr_age_tuple))),
        raise_rate_by_level=build_raise_by_level(dict(zip(TIER_ORDER, raise_tuple))),
    )
    sim_params = sc.apply_adjustments(base_params, adj)
    problems = sc.validate(sim_params)
    sim = sc.run(sim_params, baseline_cost=baseline.labor_cost_by_year)
    return adj, sim_params, sim, problems


base_params, baseline = compute_baseline(years)

scenarios: list[dict] = []
for sid, name in SCENARIOS:
    lev = levers[sid]
    adj, sim_params, sim, problems = compute_scenario(
        years,
        lev["promo_mode"], lev["promo_pct"],
        tuple(lev["promo_by_tier"][_t] for _t in TIER_ORDER),
        lev["attr_mode"], lev["attr_pct"],
        tuple(lev["attr_by_tier"][_t] for _t in TIER_ORDER),
        tuple(lev["attr_by_age"][_b] for _b in AGE_BANDS),
        tuple(lev["raise_by_tier"][_t] for _t in TIER_ORDER))
    if problems:
        st.error(f"[{name}] 정합성 위반(계산 중단):\n" + "\n".join(problems))
        st.stop()
    scenarios.append({"sid": sid, "name": name, "lev": lev, "adj": adj,
                      "params": sim_params, "sim": sim, "color": SCN_COLOR[sid]})


# =============================================================
# 차트 헬퍼 (흰 배경 · 연한 그리드 · Pretendard)
# =============================================================
def _style(fig: go.Figure, height: int, title: str | None = None) -> go.Figure:
    fig.update_layout(
        title=title or None, height=height,
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
            x=yrs, y=vals, mode="lines", stackgroup="one", name=f,
            line=dict(width=0.5, color=FAMILY_COLOR[f]),
            hovertemplate=f"{f} %{{y:.0f}}명<extra></extra>",
        ))
    _style(fig, height, title)
    # 연도 라벨('2026(기준)','2027'…)이 숫자축으로 오인돼 기준연 포인트가 NaN 되는 것 방지.
    fig.update_xaxes(type="category")
    fig.update_layout(xaxis_title="연도", yaxis_title="인원(명)", showlegend=showlegend)
    return fig


def area_by_tier(result: sc.SimResult, title: str, height: int = 320,
                 showlegend: bool = True, y_max: float | None = None) -> go.Figure:
    """연도별 직급(사원→부장) 누적 인원 — 직군별 단계 수 차이(3~7단계)를
    상대 위치 6직급 매핑(tier_distribution)으로 정규화해 직급 기준으로 비교.
    y_max 를 주면 나란히 비교 시 동일 축으로 고정."""
    yrs = [year_label(t) for t in range(len(result.headcount_by_year))]
    fig = go.Figure()
    for tier in TIER_ORDER:
        vals = [tier_distribution(hc)[tier] for hc in result.headcount_by_year]
        fig.add_trace(go.Scatter(
            x=yrs, y=vals, mode="lines", stackgroup="one", name=tier,
            line=dict(width=0.5, color=TIER_COLOR[tier]),
            hovertemplate=f"{tier} %{{y:.0f}}명<extra></extra>",
        ))
    _style(fig, height, title)
    fig.update_xaxes(type="category")
    if y_max is not None:
        fig.update_yaxes(range=[0, y_max])
    fig.update_layout(xaxis_title="연도", yaxis_title="인원(명)", showlegend=showlegend)
    return fig


def headcount_table(result: sc.SimResult) -> pd.DataFrame:
    """연도별 직군 인원 + 총원 표(숫자)."""
    data = {}
    for t, hc in enumerate(result.headcount_by_year):
        byf = sc.headcount_by_family(hc)
        row = {f: round(byf.get(f, 0.0)) for f in sc.FAMILY_LEVELS}
        row["총원"] = round(sc.total_headcount(hc))
        data[year_label(t)] = row
    df = pd.DataFrame.from_dict(data, orient="index")
    df.index.name = "연도"
    return df.reset_index()


def cost_table(baseline: sc.SimResult, scns: list[dict]) -> pd.DataFrame:
    """연도별 총 인건비 표(억원): AS-IS / 시뮬1 / Δ1 / 시뮬2 / Δ2."""
    rows = []
    for t, b in enumerate(baseline.labor_cost_by_year):
        row = {"연도": year_label(t), "AS-IS(억)": round(b / 1e8, 1)}
        for s in scns:
            v = s["sim"].labor_cost_by_year[t]
            row[f"{s['name']}(억)"] = round(v / 1e8, 1)
            row[f"Δ{s['name'][-1]}(억)"] = round((v - b) / 1e8, 1)
        rows.append(row)
    return pd.DataFrame(rows)


def cost_chart(baseline: sc.SimResult, scns: list[dict]) -> go.Figure:
    """연도별 총 인건비 — AS-IS vs 시뮬1 vs 시뮬2 그룹 막대."""
    yrs = [year_label(t) for t in range(len(baseline.labor_cost_by_year))]
    fig = go.Figure()
    fig.add_bar(x=yrs, y=[c / 1e8 for c in baseline.labor_cost_by_year],
                name="AS-IS", marker_color=SCN_COLOR["asis"],
                hovertemplate="AS-IS %{y:.0f}억<extra></extra>")
    for s in scns:
        fig.add_bar(x=yrs, y=[c / 1e8 for c in s["sim"].labor_cost_by_year],
                    name=s["name"], marker_color=s["color"],
                    hovertemplate=f"{s['name']} %{{y:.0f}}억<extra></extra>")
    _style(fig, 320, "연도별 총 인건비 (억원)")
    fig.update_xaxes(type="category")
    fig.update_layout(barmode="group", xaxis_title="연도", yaxis_title="인건비(억원)")
    return fig


# --- 직급 구조 실루엣 (모래시계 ↔ 피라미드) --------------------------------
def tier_distribution(hc_year: dict[str, dict[str, float]]) -> dict[str, float]:
    tiers = {t: 0.0 for t in TIER_ORDER}
    for f, levels in sc.FAMILY_LEVELS.items():
        n = len(levels)
        fam_hc = hc_year.get(f, {})
        for i, lvl in enumerate(levels):
            tiers[_tier_of(i, n)] += fam_hc.get(lvl, 0.0)
    return tiers


def shape_silhouette(hc_year: dict[str, dict[str, float]], title: str,
                     color: str, x_max: float | None = None,
                     ref: dict[str, dict[str, float]] | None = None,
                     height: int = 300) -> go.Figure:
    """x_max: 여러 실루엣을 같은 가로 스케일로 그려 폭을 직접 비교할 때 지정.
    ref: 비교 기준 연도 인원(보통 같은 연도의 AS-IS). 주면 막대 라벨이
         '절대 인원 (기준 대비 Δ)' 로 표시된다."""
    tiers = tier_distribution(hc_year)
    vals = [tiers[t] for t in TIER_ORDER]
    if ref is not None:
        ref_tiers = tier_distribution(ref)
        labels = [f"{v:,.0f}명 ({v - ref_tiers[t]:+,.0f})"
                  for t, v in zip(TIER_ORDER, vals)]
    else:
        labels = [f"{v:,.0f}명" for v in vals]
    fig = go.Figure(go.Bar(
        y=TIER_ORDER, x=vals, base=[-v / 2 for v in vals],
        orientation="h", marker_color=color, width=0.72,
        text=labels, textposition="outside",
        cliponaxis=False, hovertemplate="%{y} %{x:,.0f}명<extra></extra>",
    ))
    _style(fig, height, title)
    _max = x_max if x_max is not None else (max(vals) if vals else 1)
    _max = _max or 1
    fig.update_layout(showlegend=False,
                      xaxis=dict(visible=False, range=[-_max * 0.72, _max * 0.72]))
    fig.update_yaxes(categoryorder="array", categoryarray=TIER_ORDER,
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


def promo_rate_by_tier(params: sc.SimParams) -> dict[str, float]:
    """직급별 평균 승진율(%) — 해당 직급에 매핑된 (직군,단계)들의 단순 평균."""
    buckets: dict[str, list[float]] = {t: [] for t in TIER_ORDER}
    for f, levels in sc.FAMILY_LEVELS.items():
        n = len(levels)
        for i, lvl in enumerate(levels):
            buckets[_tier_of(i, n)].append(params.promotion_rate[f][lvl])
    return {t: (sum(v) / len(v) * 100.0 if v else 0.0) for t, v in buckets.items()}


# =============================================================
# ① 시뮬레이션 결과 (최상단) — 시나리오별 KPI 타일 + 적용 변수
# =============================================================
st.markdown("## 시뮬레이션 결과")

end_base = baseline.headcount_by_year[-1]
tot_base = sc.total_headcount(end_base)
top_base = sc.top_level_share(end_base)
cum_base = sum(baseline.labor_cost_by_year)

for s in scenarios:
    end_s = s["sim"].headcount_by_year[-1]
    s["tot"] = sc.total_headcount(end_s)
    s["top"] = sc.top_level_share(end_s)
    s["cum"] = sum(s["sim"].labor_cost_by_year)
    s["cum_delta"] = s["sim"].cum_cost_delta_vs_baseline
    s["end_hc"] = end_s

for s in scenarios:
    head_gap = s["tot"] - tot_base
    top_delta = s["top"] - top_base
    cum_delta = s["cum_delta"]
    _cost_cls = "down" if cum_delta >= 0 else "up"
    _cost_arrow = "▲" if cum_delta >= 0 else "▼"
    _cost_txt = "인건비 증가 방향" if cum_delta >= 0 else "인건비 감소 방향"
    _head_cls = "up" if head_gap >= 0 else "down"
    _head_arrow = "▲" if head_gap >= 0 else "▼"
    _top_cls = "up" if top_delta >= 0 else "down"
    _top_arrow = "▲" if top_delta >= 0 else "▼"
    st.markdown(
        f'''
<div class="kpi-row">
  <div class="kpi fill"><div class="label">{s["name"]} · {years}년 누적 인건비 (vs AS-IS)</div>
    <div class="value">{s["cum"]/1e8:,.0f}억
      <span style="font-size:18px; font-weight:700;">({cum_delta/1e8:+,.0f}억)</span></div>
    <div class="delta {_cost_cls}">{_cost_arrow} {_cost_txt} · AS-IS 누적 {cum_base/1e8:,.0f}억</div></div>
  <div class="kpi"><div class="label">{s["name"]} · 최종연도 총원</div>
    <div class="value">{s["tot"]:,.0f}명</div>
    <div class="delta {_head_cls}">{_head_arrow} {head_gap:+,.0f}명 (vs AS-IS)</div></div>
  <div class="kpi"><div class="label">{s["name"]} · 상위단계 비중</div>
    <div class="value">{s["top"]:.1f}%</div>
    <div class="delta {_top_cls}">{_top_arrow} {top_delta:+.1f}%p (vs AS-IS)</div></div>
</div>''',
        unsafe_allow_html=True)

# 적용 변수 요약 (AS-IS / 시뮬1 / 시뮬2 각각 무엇이 걸렸는지 명시)
_scn_desc = " &nbsp;|&nbsp; ".join(
    f'<b>{s["name"]}</b>: 승진 {s["lev"]["promo_desc"]} · 퇴직 {s["lev"]["attr_desc"]} · '
    f'인상 {s["lev"]["raise_desc"]}'
    for s in scenarios)
st.markdown(
    f'<div class="posco-sub">적용 변수 — <b>AS-IS</b>: 승진율·퇴직률 기본 가정표(직급별 상이) · '
    f'인상률 0% &nbsp;|&nbsp; {_scn_desc} · 추계 {years}년</div>',
    unsafe_allow_html=True)

# =============================================================
# ② 직급 구조 실루엣 (최상단 결과) — 조회 연도 + 해당 연도 절대값·Δ
# =============================================================
st.markdown("### 직급 구조 실루엣")
_sil_years = list(range(len(baseline.headcount_by_year)))
sel_t = st.select_slider("조회 연도", options=_sil_years, value=_sil_years[-1],
                         format_func=year_label, key=f"k_sel_year_{years}",
                         help="좌우로 움직이면 연도별 지표와 직급 구조 실루엣이 함께 갱신됩니다.")

# 선택 연도의 절대값 + 변화값 (총원 · 인건비)
_hc_b_t = sc.total_headcount(baseline.headcount_by_year[sel_t])
_ct_b_t = baseline.labor_cost_by_year[sel_t] / 1e8
hc_cols = st.columns(3)
hc_cols[0].metric(f"{year_label(sel_t)} 총원 (AS-IS)", f"{_hc_b_t:,.0f}명")
for s, col in zip(scenarios, hc_cols[1:]):
    v = sc.total_headcount(s["sim"].headcount_by_year[sel_t])
    col.metric(f"{year_label(sel_t)} 총원 ({s['name']})", f"{v:,.0f}명",
               delta=f"{v - _hc_b_t:+,.0f}명")
ct_cols = st.columns(3)
ct_cols[0].metric(f"{year_label(sel_t)} 인건비 (AS-IS)", f"{_ct_b_t:,.0f}억")
for s, col in zip(scenarios, ct_cols[1:]):
    v = s["sim"].labor_cost_by_year[sel_t] / 1e8
    col.metric(f"{year_label(sel_t)} 인건비 ({s['name']})", f"{v:,.0f}억",
               delta=f"{v - _ct_b_t:+,.0f}억", delta_color="inverse")

st.caption("직급(사원→부장) 기준 중앙정렬 실루엣. 연도는 위 [조회 연도]를 따라갑니다. "
           "왼쪽 AS-IS 는 연도를 따로 선택할 수 있어 교차 비교도 가능합니다"
           f"(예: AS-IS {BASE_YEAR + 1} ↔ 시뮬 {BASE_YEAR + years}). "
           "시뮬 막대 라벨은 '절대 인원 (같은 연도 AS-IS 대비 Δ)'. "
           "허리(과장·차장=중간관리 계층)가 얇으면 모래시계형.")
sil_cols = st.columns(3)
with sil_cols[0]:
    # AS-IS 연도 선택 — 기본은 상단 조회 연도와 동기화. key 에 sel_t 를 포함해
    # 조회 연도를 움직이면 이 선택도 함께 리셋(동기화)된다.
    base_t = st.selectbox("AS-IS 연도", _sil_years, index=sel_t,
                          format_func=year_label, key=f"k_sil_base_{years}_{sel_t}",
                          help="기본은 조회 연도와 동일. 바꾸면 서로 다른 연도끼리 교차 비교.")
_sil_max = max(
    max(tier_distribution(baseline.headcount_by_year[base_t]).values()),
    *[max(tier_distribution(s["sim"].headcount_by_year[sel_t]).values())
      for s in scenarios])
with sil_cols[0]:
    st.plotly_chart(shape_silhouette(baseline.headcount_by_year[base_t],
                                     f"AS-IS · {year_label(base_t)}", SCN_COLOR["asis"],
                                     x_max=_sil_max),
                    use_container_width=True, key="sil_base", config=PLOTLY_CONFIG)
for s, col in zip(scenarios, sil_cols[1:]):
    with col:
        st.selectbox(f"{s['name']} 연도", [sel_t], index=0, format_func=year_label,
                     key=f"k_sil_{s['sid']}_{years}_{sel_t}", disabled=True,
                     help="시뮬 쪽은 위 [조회 연도] 고정입니다.")
        st.plotly_chart(shape_silhouette(s["sim"].headcount_by_year[sel_t],
                                         f"{s['name']} · {year_label(sel_t)}", s["color"],
                                         x_max=_sil_max,
                                         ref=baseline.headcount_by_year[sel_t]),
                        use_container_width=True, key=f"sil_{s['sid']}",
                        config=PLOTLY_CONFIG)

st.divider()


# =============================================================
# ③ 나란히 비교 — AS-IS ↔ 시뮬1 ↔ 시뮬2
# =============================================================
st.markdown("### AS-IS ↔ 시뮬1 ↔ 시뮬2 비교")
st.caption(f"기준연도 = 올해({BASE_YEAR}) 현재 인원 스냅샷. 승진율·퇴직률·인상률 조정 효과는 "
           f"내년({BASE_YEAR + 1})부터 {BASE_YEAR + years}년까지 추계에 반영됩니다.")
comp_dim = st.radio(
    "구분 기준", ["직급", "직군"], horizontal=True, key="k_comp_dim",
    help="직급 = 직군별 단계(3~7개)를 상대 위치로 사원→부장 6직급에 묶어 집계 / "
         "직군 = P·R·E·A 4개 직군별 집계")
# 동일 축(y) 고정 — 3개 시나리오 최대 총원 기준으로 같은 스케일에서 비교.
_cmp_y_max = max(
    max(sc.total_headcount(hc) for hc in baseline.headcount_by_year),
    *[max(sc.total_headcount(hc) for hc in s["sim"].headcount_by_year)
      for s in scenarios]) * 1.08
cmp_cols = st.columns(3)
_cmp_panels = [("AS-IS (조정 없음)",
                "적용 변수: 승진율·퇴직률 기본 가정표 · 인상률 0% (조정 미반영 기준선)",
                baseline, "asis")] + [
    (f"{s['name']} (조정 반영)",
     f"적용 변수: 승진 {s['lev']['promo_desc']} · 퇴직 {s['lev']['attr_desc']} · "
     f"인상 {s['lev']['raise_desc']}",
     s["sim"], s["sid"])
    for s in scenarios]
for (title, cap, result, sid_), col in zip(_cmp_panels, cmp_cols):
    with col:
        st.markdown(f"#### {title}")
        st.caption(cap)
        if SHOW_TABLE:
            st.dataframe(headcount_table(result), use_container_width=True, hide_index=True)
        elif comp_dim == "직급":
            st.plotly_chart(area_by_tier(result, "인력 구조 (직급 누적)", y_max=_cmp_y_max,
                                         showlegend=(sid_ == "asis")),
                            use_container_width=True, key=f"area_tier_{sid_}",
                            config=PLOTLY_CONFIG)
        else:
            st.plotly_chart(area_by_family(result, "인력 구조 (직군 누적)",
                                           showlegend=(sid_ == "asis")),
                            use_container_width=True, key=f"area_fam_{sid_}",
                            config=PLOTLY_CONFIG)

st.markdown("#### 총 인건비 (AS-IS vs 시뮬1 vs 시뮬2)")
if SHOW_TABLE:
    st.dataframe(cost_table(baseline, scenarios), use_container_width=True, hide_index=True)
else:
    st.plotly_chart(cost_chart(baseline, scenarios), use_container_width=True,
                    key="cost_chart", config=PLOTLY_CONFIG)

# --- 추계 기간 전체 누적 인건비: AS-IS vs 시뮬1 vs 시뮬2 합산 + Δ 강조 ---
#     기준연(t=0)~최종연도 합. 인건비 증가는 부담 방향이므로 delta_color="inverse"(+가 빨강).
mc_cols = st.columns(3)
mc_cols[0].metric(f"AS-IS 누적 인건비 ({years}년)", f"{cum_base / 1e8:,.0f}억")
for s, col in zip(scenarios, mc_cols[1:]):
    diff = s["cum"] - cum_base
    pct = (diff / cum_base * 100.0) if cum_base else 0.0
    col.metric(f"{s['name']} 누적 인건비 ({years}년)", f"{s['cum'] / 1e8:,.0f}억",
               delta=f"{diff / 1e8:+,.0f}억 ({pct:+.2f}%)", delta_color="inverse")

# =============================================================
# ④ 정년퇴직 · 정년 재채용 인원 (연도별)
# =============================================================
st.markdown("#### 정년퇴직 · 정년 재채용 인원 (연도별)")
st.caption(f"가정(더미): 각 직군 최상위 단계 이탈 중 {sc.RETIRE_SHARE:.0%}가 정년퇴직이고, "
           f"그중 {sc.REHIRE_RATE:.0%}를 촉탁(계약직)으로 재채용. "
           "두 지표 모두 별도 풀로 보고 본 추계 인원에는 합산하지 않는 표시용 지표입니다.")
retire_series = {"AS-IS": sc.retirement_by_year(baseline.headcount_by_year,
                                                base_params.attrition_rate)}
rehire_series = {"AS-IS": sc.rehire_by_year(baseline.headcount_by_year,
                                            base_params.attrition_rate)}
for s in scenarios:
    retire_series[s["name"]] = sc.retirement_by_year(
        s["sim"].headcount_by_year, s["params"].attrition_rate)
    rehire_series[s["name"]] = sc.rehire_by_year(
        s["sim"].headcount_by_year, s["params"].attrition_rate)
_re_names = ["AS-IS"] + [s["name"] for s in scenarios]
_re_colors = [SCN_COLOR["asis"]] + [s["color"] for s in scenarios]
_re_yrs = [year_label(t) for t in range(1, len(retire_series["AS-IS"]))]
if SHOW_TABLE:
    _re_rows = []
    for t in range(1, len(retire_series["AS-IS"])):
        row = {"연도": year_label(t)}
        for nm in _re_names:
            row[f"정년퇴직 {nm}(명)"] = round(retire_series[nm][t], 1)
        for nm in _re_names:
            row[f"재채용 {nm}(명)"] = round(rehire_series[nm][t], 1)
        _re_rows.append(row)
    st.dataframe(pd.DataFrame(_re_rows), use_container_width=True, hide_index=True)
else:
    re_l, re_r = st.columns(2)
    _ret_fig = go.Figure()
    for nm, cl in zip(_re_names, _re_colors):
        _ret_fig.add_bar(x=_re_yrs, y=[round(v, 1) for v in retire_series[nm][1:]],
                         name=nm, marker_color=cl,
                         hovertemplate=f"{nm} %{{y:.1f}}명<extra></extra>")
    _style(_ret_fig, 260, "연도별 정년퇴직 인원 (명)")
    _ret_fig.update_xaxes(type="category")
    _ret_fig.update_layout(barmode="group", xaxis_title="연도", yaxis_title="정년퇴직(명)")
    re_l.plotly_chart(_ret_fig, use_container_width=True, key="retire_chart",
                      config=PLOTLY_CONFIG)
    _reh_fig = go.Figure()
    for nm, cl in zip(_re_names, _re_colors):
        _reh_fig.add_bar(x=_re_yrs, y=[round(v, 1) for v in rehire_series[nm][1:]],
                         name=nm, marker_color=cl,
                         hovertemplate=f"{nm} %{{y:.1f}}명<extra></extra>")
    _style(_reh_fig, 260, "연도별 정년 재채용 인원 (명)")
    _reh_fig.update_xaxes(type="category")
    _reh_fig.update_layout(barmode="group", xaxis_title="연도", yaxis_title="재채용(명)")
    re_r.plotly_chart(_reh_fig, use_container_width=True, key="rehire_chart",
                      config=PLOTLY_CONFIG)


# =============================================================
# ⑤ 직급별 승진율 시각화 + 승진율·퇴직률 상세 표
# =============================================================
st.markdown("#### 직급별 승진율 (AS-IS vs 시뮬1 vs 시뮬2)")
st.caption("직급(사원→리더)별 평균 승진율. 직급별 승진율 레버를 조정하면 해당 직급 막대만 "
           "움직입니다. 부장(최상위)은 승진율 0 고정이라 제외.")
_pr_fig = go.Figure()
_pr_tiers = TIER_ORDER[:-1]
_pr_sets = [("AS-IS", promo_rate_by_tier(base_params), SCN_COLOR["asis"])] + [
    (s["name"], promo_rate_by_tier(s["params"]), s["color"]) for s in scenarios]
for nm, rates, cl in _pr_sets:
    _pr_fig.add_bar(x=_pr_tiers, y=[round(rates[t], 2) for t in _pr_tiers],
                    name=nm, marker_color=cl,
                    hovertemplate=f"{nm} %{{y:.1f}}%<extra></extra>")
_style(_pr_fig, 280)
_pr_fig.update_xaxes(type="category")
_pr_fig.update_layout(barmode="group", xaxis_title="직급", yaxis_title="평균 승진율(%)")
st.plotly_chart(_pr_fig, use_container_width=True, key="promo_chart", config=PLOTLY_CONFIG)

st.markdown("#### 직군·단계별 승진율·퇴직률 상세")
st.caption("승진율·퇴직률은 직군·단계별로 전부 다릅니다. 조정 레버는 이 기본표에 %p 로 "
           "가감되며(예: 18% + 1%p → 19%), 시뮬 열이 실제 계산에 들어간 값입니다. "
           "(최상위 단계 승진율은 0 고정)")
_rate_rows = []
for _f in sc.FAMILY_LEVELS:
    _lvls = sc.FAMILY_LEVELS[_f]
    for _i, _lvl in enumerate(_lvls):
        row = {
            "직군": _f,
            "단계": _lvl,
            "직급": _tier_of(_i, len(_lvls)),
            "승진율 AS-IS": f"{base_params.promotion_rate[_f][_lvl] * 100:.1f}%",
        }
        for s in scenarios:
            row[f"승진율 {s['name']}"] = f"{s['params'].promotion_rate[_f][_lvl] * 100:.1f}%"
        row["퇴직률 AS-IS"] = f"{base_params.attrition_rate[_f][_lvl] * 100:.1f}%"
        for s in scenarios:
            row[f"퇴직률 {s['name']}"] = f"{s['params'].attrition_rate[_f][_lvl] * 100:.1f}%"
        _rate_rows.append(row)
st.dataframe(pd.DataFrame(_rate_rows), use_container_width=True, hide_index=True,
             height=280)

with st.expander("최종연도 직군·단계별 인원 상세 (AS-IS / 시뮬1 / 시뮬2 / Δ)"):
    rows = []
    for f in sc.FAMILY_LEVELS:
        for lvl in sc.FAMILY_LEVELS[f]:
            b = end_base[f].get(lvl, 0.0)
            row = {"직군": f, "단계": lvl, "AS-IS": round(b)}
            for s in scenarios:
                v = s["end_hc"][f].get(lvl, 0.0)
                row[s["name"]] = round(v)
                row[f"Δ{s['name'][-1]}"] = round(v - b)
            rows.append(row)
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# =============================================================
# 스냅샷 저장·비교 (M3)
# =============================================================
st.divider()
st.markdown("## 스냅샷 저장·비교")

save_cols = st.columns(len(SCENARIOS) + 2)
for s, col in zip(scenarios, save_cols):
    with col:
        if st.button(f"{s['name']} 스냅샷 저장", use_container_width=True):
            lev = s["lev"]
            controls = {
                "scenario": s["sid"], "years": int(years),
                "promo_mode": lev["promo_mode"], "promo_pct": float(lev["promo_pct"]),
                "promo_by_tier": dict(lev["promo_by_tier"]),
                "attr_mode": lev["attr_mode"], "attr_pct": float(lev["attr_pct"]),
                "attr_by_tier": dict(lev["attr_by_tier"]),
                "attr_by_age": dict(lev["attr_by_age"]),
                "raise_by_tier": dict(lev["raise_by_tier"]),
                "desc": (f"승진 {lev['promo_desc']} · 퇴직 {lev['attr_desc']} · "
                         f"인상 {lev['raise_desc']}"),
            }
            label = snap.make_label(
                years=int(years), promo_pct=float(lev["promo_pct"]),
                attr_pct=float(lev["attr_pct"]),
                raise_by_tier=dict(lev["raise_by_tier"]),
                promo_desc=lev["promo_desc"], attr_desc=lev["attr_desc"])
            st.session_state["snapshots"].append(
                snap.capture(label, controls, s["adj"], s["sim"]))
            st.toast(f"스냅샷 저장: {label}")

snaps: list[snap.Snapshot] = st.session_state["snapshots"]

if not snaps:
    st.info("아직 저장된 스냅샷이 없습니다. 레버를 조정하고 [시뮬1/시뮬2 스냅샷 저장] 을 "
            "누르면 여러 변수 조합을 나란히 비교할 수 있습니다. "
            "조정 없이 저장하면 'baseline' 기준선 스냅샷이 됩니다.")
else:
    st.markdown("#### 저장된 스냅샷")
    top_row = st.columns([6, 1])
    with top_row[1]:
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
            desc = c.get("desc") or (
                f"승진 {c.get('promo_pct', 0):+g}% · "
                f"퇴직 {scale_summary(c.get('attr_mode', '전체'), c.get('attr_pct', 0.0), c.get('attr_by_tier', {}), c.get('attr_by_age', {}))} · "
                f"인상 {raise_summary(c.get('raise_by_tier', {}))}")
            st.caption(f"연수 {c['years']} · {desc}")
        with c_restore:
            if st.button("복원", key=f"restore_{s.snapshot_id}", use_container_width=True,
                         help="이 스냅샷의 레버 조합을 원래 시나리오(시뮬1/시뮬2)로 복원"):
                st.session_state["_pending_restore"] = [dict(s.controls)]
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
        st.markdown("#### 스냅샷별 최종연도 인원 (직군)")
        final_rows = []
        for s in snaps:
            end = s.result.headcount_by_year[-1]
            byf = sc.headcount_by_family(end)
            row = {"라벨": s.label}
            row.update({f: round(byf.get(f, 0.0)) for f in sc.FAMILY_LEVELS})
            row["총원"] = round(sc.total_headcount(end))
            final_rows.append(row)
        st.dataframe(pd.DataFrame(final_rows), use_container_width=True, hide_index=True)
    else:
        # 스냅샷 비교의 기준 뷰 = 직급 구조 실루엣.
        #   각 스냅샷의 최종연도 구조를 같은 가로 스케일로 나란히 — 어떤 레버 조합이
        #   허리(과장·차장)를 얼마나 채우는지 실루엣 폭으로 직접 대조한다.
        @st.cache_data(show_spinner=False)
        def _baseline_end_hc(years_: int):
            """동일 horizon 무조정 baseline 의 최종연도 인원(실루엣 Δ 라벨 기준).
            build_default_params 는 순수 상수 함수라 동일 years 에서 항상 같은 값."""
            return sc.run(sc.build_default_params(years=years_)).headcount_by_year[-1]

        st.markdown("#### 실루엣 비교 (스냅샷별 최종연도 직급 구조 · 인건비)")
        st.caption("막대 라벨은 절대 인원 (같은 연수의 무조정 baseline 대비 Δ). "
                   "가로 스케일은 전 스냅샷 공통이라 실루엣 폭을 그대로 비교할 수 있습니다.")
        _snap_max = max(
            max(tier_distribution(s.result.headcount_by_year[-1]).values())
            for s in snaps)
        PER_ROW = 4
        for start in range(0, len(snaps), PER_ROW):
            row = snaps[start:start + PER_ROW]
            cols = st.columns(len(row))
            for s, col in zip(row, cols):
                with col:
                    st.markdown(f"**{s.label}**")
                    st.plotly_chart(
                        shape_silhouette(s.result.headcount_by_year[-1],
                                         f"{year_label(s.controls['years'])} · {s.controls['years']}년 후",
                                         C_BLUE, x_max=_snap_max,
                                         ref=_baseline_end_hc(s.controls["years"]),
                                         height=230),
                        use_container_width=True, key=f"mini_sil_{s.snapshot_id}",
                        config=PLOTLY_CONFIG)
                    st.plotly_chart(mini_cost(s.result), use_container_width=True,
                                    key=f"mini_cost_{s.snapshot_id}", config=PLOTLY_CONFIG)


# =============================================================
# P-GPT — 대화형 인사이트 챗봇 (Claude, 메모리 유지 / 키 없으면 rule 폴백)
#   POSCO AI 브랜드명 'P-GPT', 어시스턴트 프로필은 'P' 아바타.
# =============================================================
st.divider()
PGPT_AVATAR = str(Path(__file__).parent / "assets" / "pgpt_avatar.svg")
if not Path(PGPT_AVATAR).exists():
    PGPT_AVATAR = None  # 아바타 파일 없으면 기본 아이콘 폴백
st.markdown('<div class="posco-head" style="margin-top:4px;">'
            '<h1 style="font-size:20px; color:var(--navy);">P-GPT</h1>'
            '<span class="posco-badge">POSCO AI · 인사이트 챗봇</span></div>',
            unsafe_allow_html=True)

insight_ctx = {
    "years": int(years),
    "tot_base": tot_base, "top_base": top_base,
    "cum_base_eok": cum_base / 1e8,
    # 시나리오별 요약 (레버 + 결과)
    "scenarios": [
        {
            "name": s["name"],
            "promo_desc": s["lev"]["promo_desc"],
            "attr_desc": s["lev"]["attr_desc"],
            "raise_desc": s["lev"]["raise_desc"],
            "tot": s["tot"],
            "cum_delta_eok": s["cum_delta"] / 1e8,
            "top_share": s["top"],
            "family_end": {f: round(v)
                           for f, v in sc.headcount_by_family(s["end_hc"]).items()},
        }
        for s in scenarios
    ],
    # 저장된 스냅샷 요약 — 챗봇이 여러 시나리오를 비교·언급할 수 있게 컨텍스트로 전달.
    "snapshots": [
        {
            "label": s.label,
            "years": s.controls["years"],
            "desc": s.controls.get("desc", ""),
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


def _prefill_chat() -> list[dict]:
    """첫 진입 시 채워 넣는 목업 대화 — 빈 챗봇 대신 사용 예시가 보이게(시연용).
    수치는 현재 baseline 값으로 채워 그럴듯하게 만든다."""
    return [
        {"role": "user",
         "content": "지금 인력 구조에서 가장 큰 리스크가 뭐야?"},
        {"role": "assistant",
         "content": (
             f"현재 총원 약 **{tot_base:,.0f}명** 기준으로 보면, 구조가 전형적인 "
             "**모래시계형**입니다. 사원·대리급 실무 기반은 두꺼운데 **과장·차장(중간관리) "
             "허리가 비어 있고**, 리더·부장급 고참은 남아 있어요.\n\n"
             "이대로 두면 몇 년 안에 실무 리더 승계가 끊기는 게 가장 큰 리스크입니다. "
             "시뮬1 레버에서 승진율을 올려 허리를 채우는 시나리오부터 확인해 보시길 권합니다.")},
        {"role": "user",
         "content": "그럼 승진율을 올리면 인건비는 얼마나 늘어?"},
        {"role": "assistant",
         "content": (
             "시뮬1 [승진율]에서 **전체 +1~+3%p**, 또는 직급별 모드로 **대리·과장만** 올려 "
             "보세요(예: 승진율 16% → 18%). 상위 직급 단가가 높아 누적 인건비 Δ가 KPI 타일에 "
             "바로 잡힙니다. 인건비 부담이 크면 시뮬2 에는 **과장·차장 인상률만 올리는 "
             "절충안**을 걸어 두 시나리오를 나란히 비교할 수 있어요. 조합별 결과는 "
             f"[스냅샷 저장]으로 쌓아서 {years}년 horizon 으로 대조하는 걸 추천합니다.")},
    ]


col_chat, col_clear = st.columns([6, 1])
with col_clear:
    if st.button("대화 초기화", use_container_width=True):
        st.session_state["chat"] = _prefill_chat()
        st.rerun()

if "chat" not in st.session_state:
    st.session_state["chat"] = _prefill_chat()


def _chat_message(role: str):
    """P-GPT 아바타(어시스턴트=P 로고) 적용한 chat_message 컨텍스트."""
    return st.chat_message(role, avatar=PGPT_AVATAR if role == "assistant" else None)


# 메시지는 고정 높이 박스 안에서만 스크롤. chat_input 을 메인 루트에 두면
# Streamlit 이 앱 전체를 chat 앱으로 간주해 '로드 시 맨 아래로 자동 스크롤'되므로
# (KPI·차트가 아니라 챗봇부터 보이는 문제), 컬럼 안에 넣어 인라인으로 렌더한다.
chat_box = st.container(height=380, border=True)
with chat_box:
    for m in st.session_state["chat"]:
        with _chat_message(m["role"]):
            st.markdown(m["content"])

prompt = st.columns(1)[0].chat_input("P-GPT에게 이 시뮬 결과에 대해 물어보세요 (예: 인건비를 줄이려면?)")
if prompt:
    st.session_state["chat"].append({"role": "user", "content": prompt})
    with chat_box:
        with _chat_message("user"):
            st.markdown(prompt)
        with _chat_message("assistant"):
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

"""
app.py — POSCO HR 인력운영 시뮬레이터 (v3, 배포 엔트리)
==================================================
승진율 / 퇴직률 / 인건비 인상률을 조정하면 향후 인력 구조와 총 인건비가 어떻게
변하는지 결정론 마르코프로 추계해 baseline ↔ 시뮬을 좌우로 나란히 비교하고,
변수 조합을 스냅샷으로 저장·비교한다. LLM 인사이트 챗봇(선택).

  - 결정론 코어:   sim_core.py  (직군 4종 P/R/E/A × 단계별 전이 + 인건비)
  - 스냅샷 로직:   snapshots.py (라벨·캡처·비교표, Streamlit 비의존)
  - 화면(본 파일): POSCO 블루 리디자인 — 인라인 헤더/KPI 타일/차트 대시보드

계산·데이터·키는 UI 리디자인에서 변경하지 않는다. 렌더 계층만 손댄다.
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
if not any(_f.name == "attrition_scale_by_level" for _f in _dc.fields(sc.Adjustments)):
    _importlib.reload(sc)
    _importlib.reload(snap)

st.set_page_config(page_title="POSCO HR 시뮬레이터", layout="wide", page_icon="🔷")

# 추계 연도 라벨: 연차 0 = 올해(기준 스냅샷), 추계·조정 효과는 내년(BASE_YEAR+1)부터 반영.
# (코어는 t=0에 전이·인상 미적용이므로 조정 효과는 이미 내년부터 시작한다. 여기선 표시만 실제 연도로.)
BASE_YEAR = date.today().year


def year_label(t: int) -> str:
    """연차 t → 실제 연도 문자열. 기준연(t=0)은 '올해' 표기."""
    return f"{BASE_YEAR}(기준)" if t == 0 else str(BASE_YEAR + t)


# =============================================================
# 직급(하위→상위) — 실루엣 표시 + 직급별 인상률·퇴직률 입력에 공용으로 쓴다.
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


def build_attr_scale_by_level(mode: str, by_tier_pct: dict[str, float],
                              by_age_pct: dict[str, float]) -> dict[str, dict[str, float]] | None:
    """직급별/나이별 퇴직률 조정(%) → {직군:{단계:배율}}. '전체' 모드면 None(전역 배율 사용).

    나이별 모드는 직급 × 연령 구성비(AGE_MIX, 가정 더미)로 가중 평균해 직급별 배율로 환산:
      단계 배율 = 1 + Σ(연령대 구성비 × 해당 연령대 조정%) / 100
    """
    if mode == "전체":
        return None
    out: dict[str, dict[str, float]] = {}
    for f, levels in sc.FAMILY_LEVELS.items():
        n = len(levels)
        d = {}
        for i, lvl in enumerate(levels):
            tier = _tier_of(i, n)
            if mode == "직급별":
                pct = by_tier_pct.get(tier, 0.0)
            else:  # 나이별
                pct = sum(AGE_MIX[tier][b] * by_age_pct.get(b, 0.0) for b in AGE_BANDS)
            d[lvl] = 1.0 + pct / 100.0
        out[f] = d
    return out


def attr_summary(mode: str, pct: float, by_tier: dict[str, float],
                 by_age: dict[str, float]) -> str:
    """퇴직률 조정 설정을 짧게 요약(라벨·캡션·챗봇 컨텍스트 공용)."""
    if mode == "전체":
        return f"{pct:+g}%"
    if mode == "직급별":
        nz = [f"{t} {by_tier.get(t, 0.0):+g}%" for t in TIER_ORDER
              if abs(by_tier.get(t, 0.0)) > 1e-9]
        return "직급별(" + " · ".join(nz) + ")" if nz else "0%"
    nz = [f"{b} {by_age.get(b, 0.0):+g}%" for b in AGE_BANDS
          if abs(by_age.get(b, 0.0)) > 1e-9]
    return "나이별(" + " · ".join(nz) + ")" if nz else "0%"


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
.kpi-row{ display:grid; grid-template-columns:repeat(3,1fr); gap:14px; margin:18px 0; }
.kpi{ border-radius:12px; padding:20px; border:1px solid var(--line); background:var(--panel); }
.kpi.fill{ background:linear-gradient(135deg,#002B5B 0%,#0072CE 100%); border:0; color:#fff; }
.kpi .label{ font-size:12px; font-weight:600; color:var(--muted); }
.kpi.fill .label{ color:#B7CDEA; }
.kpi .value{ font-size:32px; font-weight:800; letter-spacing:-.02em; margin-top:8px; }
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
        '<div class="posco-sub">승진율·퇴직률·인건비 인상률을 조정하면 향후 인력 구조와 '
        '총 인건비 변화를 마르코프로 추계해 baseline과 나란히 보여줍니다. 결정론 rule 계산.</div>',
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

# 직군 색상 — 레퍼런스 조직도의 4단 블루 스케일
FAMILY_COLOR = {"P": C_NAVY_DP, "R": "#1B4F8A", "E": C_BLUE_LT, "A": C_BLUE_XLT}

# 직급 색상 — 사원(연청)→부장(딥네이비) 단조 그라데이션.
#   좌(baseline)·우(시뮬) 비교 차트에서 동일 색을 써 직급별 대응이 한눈에 보이게.
TIER_COLOR = {"사원": "#BFDCF5", "대리": C_BLUE_XLT, "과장": C_BLUE_LT,
              "차장": C_BLUE, "리더": C_NAVY, "부장": C_NAVY_DP}

# 슬라이더 key ↔ 기본값. 복원은 이 key 에 값을 써넣고 rerun.
#   인상률은 직급별(사원→부장) 6개 입력. 기본 0% = baseline과 동일(Δ 0).
#   퇴직률 조정은 전체/직급별/나이별 3개 모드(기본 전체 0%).
SLIDER_DEFAULTS = {"k_years": 5, "k_promo": 0.0, "k_attr": 0.0, "k_attr_mode": "전체"}
for _t in TIER_ORDER:
    SLIDER_DEFAULTS[f"k_raise_{_t}"] = 0.0
    SLIDER_DEFAULTS[f"k_attr_t_{_t}"] = 0.0
for _b in AGE_BANDS:
    SLIDER_DEFAULTS[f"k_attr_a_{_b}"] = 0.0

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
#   구버전 위젯이 남긴 값이 새 위젯 범위 밖이면 하한(-50 등)으로 튀어 보이므로,
#   범위 밖/비정상 값은 기본값(0)으로 되돌린다.
_LEVER_BOUNDS = {"k_promo": (-50.0, 100.0), "k_attr": (-50.0, 100.0)}
for _t in TIER_ORDER:
    _LEVER_BOUNDS[f"k_raise_{_t}"] = (0.0, 10.0)
    _LEVER_BOUNDS[f"k_attr_t_{_t}"] = (-50.0, 100.0)
for _b in AGE_BANDS:
    _LEVER_BOUNDS[f"k_attr_a_{_b}"] = (-50.0, 100.0)
for _k, (_lo, _hi) in _LEVER_BOUNDS.items():
    try:
        if not (_lo <= float(st.session_state[_k]) <= _hi):
            raise ValueError
    except (TypeError, ValueError):
        st.session_state[_k] = SLIDER_DEFAULTS[_k]
if st.session_state.get("k_attr_mode") not in ("전체", "직급별", "나이별"):
    st.session_state["k_attr_mode"] = "전체"

_pending = st.session_state.pop("_pending_restore", None)
if _pending:
    st.session_state["k_years"] = int(_pending["years"])
    st.session_state["k_promo"] = float(_pending["promo_pct"])
    st.session_state["k_attr"] = float(_pending["attr_pct"])
    st.session_state["k_attr_mode"] = _pending.get("attr_mode", "전체")
    _rt = _pending.get("raise_by_tier", {})
    _at = _pending.get("attr_by_tier", {})
    _aa = _pending.get("attr_by_age", {})
    for _t in TIER_ORDER:
        st.session_state[f"k_raise_{_t}"] = float(_rt.get(_t, 0.0))
        st.session_state[f"k_attr_t_{_t}"] = float(_at.get(_t, 0.0))
    for _b in AGE_BANDS:
        st.session_state[f"k_attr_a_{_b}"] = float(_aa.get(_b, 0.0))

inject_css()
render_header()


# =============================================================
# 조정 레버 — 상단 고정 바 (엑셀 첫 행 틀고정 스타일)
#   사이드바 대신 본문 상단 sticky 컨테이너: 스크롤해도 항상 화면 위에 보인다.
#   위젯 key/값 범위/스텝은 기존 사이드바 시절과 동일(스냅샷 복원 호환).
# =============================================================
with st.container(key="lever_bar"):
    bar = st.columns([1.5, 1.0, 1.0, 1.1, 1.1], vertical_alignment="bottom")
    with bar[0]:
        years = st.slider("추계 연수", 1, 15, step=1, key="k_years",
                          help="1년(내년만)부터 가능. 가벼운 단기 시뮬은 1~2년으로.")
    with bar[1]:
        promo_pct = st.number_input("승진율 조정 (%)", min_value=-50.0, max_value=100.0,
                                    step=0.1, format="%.1f", key="k_promo",
                                    help="baseline 승진율 대비 배율. +2.2 이면 승진율 ×1.022. "
                                         "소수점 입력 가능. 재직률이 음수가 되지 않도록 "
                                         "(1-퇴직률) 이하로 자동 제한.")
    with bar[2]:
        # 퇴직률 조정 — 전체 일괄 / 직급별(사원→부장) / 나이별(20대→50대+) 3개 모드.
        #   나이별은 직급 × 연령 구성비(AGE_MIX)로 가중해 직급별 배율로 환산된다.
        attr_by_tier = {t: float(st.session_state.get(f"k_attr_t_{t}", 0.0)) for t in TIER_ORDER}
        attr_by_age = {b: float(st.session_state.get(f"k_attr_a_{b}", 0.0)) for b in AGE_BANDS}
        attr_pct = float(st.session_state.get("k_attr", 0.0))
        with st.popover("퇴직률 조정", use_container_width=True):
            attr_mode = st.radio("조정 방식", ["전체", "직급별", "나이별"], horizontal=True,
                                 key="k_attr_mode",
                                 help="전체=전 직급 일괄 배율 / 직급별=사원~부장 직급별 배율 / "
                                      "나이별=연령대별 배율(직급별 연령 구성비로 가중 환산)")
            if attr_mode == "전체":
                attr_pct = st.number_input(
                    "전체 퇴직률 조정 (%)", min_value=-50.0, max_value=100.0,
                    step=0.1, format="%.1f", key="k_attr",
                    help="baseline 퇴직률 대비 배율. -2.5 이면 퇴직률 ×0.975. 소수점 입력 가능.")
            elif attr_mode == "직급별":
                st.caption("직급별로 퇴직률 배율을 다르게 준다. 예: 과장 -10% = 과장급 이탈 방지 시뮬.")
                for _t in TIER_ORDER:
                    attr_by_tier[_t] = st.number_input(
                        f"{_t} 퇴직률 조정 (%)", min_value=-50.0, max_value=100.0,
                        step=0.5, format="%.1f", key=f"k_attr_t_{_t}")
            else:
                st.caption("연령대별 배율. 직급별 연령 구성비(가정 더미)로 가중해 직급 배율로 환산.")
                for _b in AGE_BANDS:
                    attr_by_age[_b] = st.number_input(
                        f"{_b} 퇴직률 조정 (%)", min_value=-50.0, max_value=100.0,
                        step=0.5, format="%.1f", key=f"k_attr_a_{_b}")
    with bar[3]:
        raise_by_tier = {}
        # 직급별 입력은 popover 로 접어 바 높이를 낮게 유지(모바일에서도 sticky 유지).
        with st.popover("직급별 연 인상률", use_container_width=True):
            st.caption("직급(사원→부장)별로 단가 인상률을 다르게 준다. "
                       "baseline=전 직급 0% 기준이라 올린 만큼 누적 Δ가 +로 잡힘. "
                       "매년 단가=단가×(1+인상률)^연차.")
            for _t in TIER_ORDER:
                raise_by_tier[_t] = st.number_input(
                    f"{_t} 인상률 (%)", min_value=0.0, max_value=10.0, step=0.05,
                    format="%.2f", key=f"k_raise_{_t}",
                    help="예: 과장·차장(중간관리)만 5%로 올려 이탈 방지 시뮬. "
                         "소수점 입력 가능(최대 10%).")
    with bar[4]:
        view_mode = st.radio("결과 보기 방식", ["차트", "표(숫자)"], horizontal=True,
                             help="차트=클릭 확대 없이 정적으로 표시 / 표=숫자만")
        if st.button("레버 초기화", use_container_width=True,
                     help="승진율·퇴직률·인상률을 전부 기본값 0%로 되돌립니다."):
            st.session_state["_pending_restore"] = {
                "years": int(st.session_state.get("k_years", 5)),
                "promo_pct": 0.0, "attr_pct": 0.0, "attr_mode": "전체",
                "attr_by_tier": {}, "attr_by_age": {}, "raise_by_tier": {}}
            st.rerun()

    attr_desc = attr_summary(attr_mode, attr_pct, attr_by_tier, attr_by_age)
    _r_min, _r_max = min(raise_by_tier.values()), max(raise_by_tier.values())
    _r_txt = f"{_r_min:g}%" if _r_min == _r_max else f"{_r_min:g}~{_r_max:g}%"
    st.caption(
        f"승진율 배율 ×{1 + promo_pct/100:.2f} · "
        f"퇴직률 조정 {attr_desc} · "
        f"인상률 {_r_txt} · © POSCO HR PoC · 더미데이터 기반 목업"
    )

SHOW_TABLE = view_mode == "표(숫자)"


# =============================================================
# 계산 — baseline(조정 없음) vs 시뮬(조정 반영)   [로직 불변]
# =============================================================
@st.cache_data(show_spinner=False)
def compute(years: int, promo_pct: float, attr_mode: str, attr_pct: float,
            attr_tier_tuple: tuple, attr_age_tuple: tuple, raise_tuple: tuple):
    # *_tuple: TIER_ORDER/AGE_BANDS 순서의 조정값(%). 캐시 키 안정화를 위해 튜플로 받는다.
    base_params = sc.build_default_params(years=years)
    baseline = sc.run(base_params)   # baseline = 전 직급 인상 0% 기준선
    tier_pct = dict(zip(TIER_ORDER, raise_tuple))
    adj = sc.Adjustments(
        promotion_scale=1 + promo_pct / 100.0,
        # '전체' 모드만 전역 배율, 직급별/나이별은 셀 단위 배율 dict 로 환산해 전달.
        attrition_scale=(1 + attr_pct / 100.0) if attr_mode == "전체" else 1.0,
        attrition_scale_by_level=build_attr_scale_by_level(
            attr_mode,
            dict(zip(TIER_ORDER, attr_tier_tuple)),
            dict(zip(AGE_BANDS, attr_age_tuple))),
        raise_rate_by_level=build_raise_by_level(tier_pct),
    )
    sim_params = sc.apply_adjustments(base_params, adj)
    problems = sc.validate(sim_params)
    sim = sc.run(sim_params, baseline_cost=baseline.labor_cost_by_year)
    return adj, base_params, sim_params, baseline, sim, problems


adj, base_params, sim_params, baseline, sim, problems = compute(
    years, promo_pct, attr_mode, attr_pct,
    tuple(attr_by_tier[_t] for _t in TIER_ORDER),
    tuple(attr_by_age[_b] for _b in AGE_BANDS),
    tuple(raise_by_tier[_t] for _t in TIER_ORDER))

if problems:
    st.error("정합성 위반(계산 중단):\n" + "\n".join(problems))
    st.stop()


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
    y_max 를 주면 좌우 비교 시 동일 축으로 고정."""
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


# --- 직급 구조 실루엣 (모래시계 ↔ 피라미드) --------------------------------
# TIER_ORDER / _tier_of 는 레버 바(직급별 인상률·퇴직률 입력)보다 먼저 필요해 상단에 정의됨.
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
                     ref: dict[str, dict[str, float]] | None = None) -> go.Figure:
    """x_max: 좌(baseline)·우(시뮬)를 같은 가로 스케일로 그려 폭을 직접 비교할 때 지정.
    ref: 비교 기준 연도 인원(보통 같은 연도의 baseline). 주면 막대 라벨이
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
    _style(fig, 300, title)
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


# =============================================================
# ① 시뮬레이션 결과 (최상단) — KPI 타일 + 적용 변수 + 조회 연도
# =============================================================
st.markdown("## 시뮬레이션 결과")

end_base = baseline.headcount_by_year[-1]
end_sim = sim.headcount_by_year[-1]
tot_base = sc.total_headcount(end_base)
tot_sim = sc.total_headcount(end_sim)
top_base = sc.top_level_share(end_base)
top_sim = sc.top_level_share(end_sim)
cum_delta = sim.cum_cost_delta_vs_baseline
# 추계 기간 누적 인건비 총액(기준연~최종연 합) — KPI 는 '총액 (+Δ)' 로 절대값·변화값 병기.
cum_base = sum(baseline.labor_cost_by_year)
cum_sim = sum(sim.labor_cost_by_year)

head_gap = tot_sim - tot_base
top_delta = top_sim - top_base

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
  <div class="kpi fill"><div class="label">{years}년 누적 인건비 (시뮬 · vs baseline)</div>
    <div class="value">{cum_sim/1e8:,.0f}억
      <span style="font-size:20px; font-weight:700;">({cum_delta/1e8:+,.0f}억)</span></div>
    <div class="delta {_cost_cls}">{_cost_arrow} {_cost_txt} · baseline 누적 {cum_base/1e8:,.0f}억</div></div>
  <div class="kpi"><div class="label">최종연도 총원</div>
    <div class="value">{tot_sim:,.0f}명</div>
    <div class="delta {_head_cls}">{_head_arrow} {head_gap:+,.0f}명</div></div>
  <div class="kpi"><div class="label">상위단계 비중</div>
    <div class="value">{top_sim:.1f}%</div>
    <div class="delta {_top_cls}">{_top_arrow} {top_delta:+.1f}%p</div></div>
</div>''',
    unsafe_allow_html=True)

# 적용 변수 요약 (baseline vs 시뮬에 각각 무엇이 걸렸는지 명시)
st.markdown(
    f'<div class="posco-sub">적용 변수 — <b>BASELINE</b>: 승진율·퇴직률 기본 가정표(직급별 상이) · '
    f'인상률 0% · 추계 {years}년&nbsp;&nbsp;|&nbsp;&nbsp;'
    f'<b>시뮬</b>: 승진율 {promo_pct:+g}% (×{1 + promo_pct/100:.3f}) · '
    f'퇴직률 {attr_desc} · 인상률 {raise_summary(raise_by_tier)} · 추계 {years}년</div>',
    unsafe_allow_html=True)

# =============================================================
# ② 직급 구조 실루엣 (최상단 결과) — 조회 연도 + 해당 연도 절대값·Δ
# =============================================================
st.markdown("### 직급 구조 실루엣")
_sil_years = list(range(len(sim.headcount_by_year)))
sel_t = st.select_slider("조회 연도", options=_sil_years, value=_sil_years[-1],
                         format_func=year_label, key=f"k_sel_year_{years}",
                         help="좌우로 움직이면 연도별 지표와 직급 구조 실루엣이 함께 갱신됩니다.")

# 선택 연도의 절대값 + 변화값 (총원 · 인건비)
_hc_b_t = sc.total_headcount(baseline.headcount_by_year[sel_t])
_hc_s_t = sc.total_headcount(sim.headcount_by_year[sel_t])
_ct_b_t = baseline.labor_cost_by_year[sel_t] / 1e8
_ct_s_t = sim.labor_cost_by_year[sel_t] / 1e8
y1, y2, y3, y4 = st.columns(4)
y1.metric(f"{year_label(sel_t)} 총원 (BASELINE)", f"{_hc_b_t:,.0f}명")
y2.metric(f"{year_label(sel_t)} 총원 (시뮬)", f"{_hc_s_t:,.0f}명",
          delta=f"{_hc_s_t - _hc_b_t:+,.0f}명")
y3.metric(f"{year_label(sel_t)} 인건비 (BASELINE)", f"{_ct_b_t:,.0f}억")
y4.metric(f"{year_label(sel_t)} 인건비 (시뮬)", f"{_ct_s_t:,.0f}억",
          delta=f"{_ct_s_t - _ct_b_t:+,.0f}억", delta_color="inverse")

st.caption("직급(사원→부장) 기준 중앙정렬 실루엣. 연도는 위 [조회 연도]를 따라갑니다. "
           "왼쪽 BASELINE 은 연도를 따로 선택할 수 있어 교차 비교도 가능합니다"
           f"(예: baseline {BASE_YEAR + 1} ↔ 시뮬 {BASE_YEAR + 1}, "
           f"baseline {BASE_YEAR + 1} ↔ 시뮬 {BASE_YEAR + years}). "
           "시뮬 막대 라벨은 '절대 인원 (같은 연도 baseline 대비 Δ)'. "
           "허리(과장·차장=중간관리 계층)가 얇으면 모래시계형.")
sil_l, sil_r = st.columns(2)
with sil_l:
    # BASELINE 연도 선택 — 기본은 상단 조회 연도와 동기화. key 에 sel_t 를 포함해
    # 조회 연도를 움직이면 이 선택도 함께 리셋(동기화)된다.
    base_t = st.selectbox("BASELINE 연도", _sil_years, index=sel_t,
                          format_func=year_label, key=f"k_sil_base_{years}_{sel_t}",
                          help="기본은 조회 연도와 동일. 바꾸면 서로 다른 연도끼리 교차 비교.")
with sil_r:
    st.selectbox("시뮬 연도", [sel_t], index=0, format_func=year_label,
                 key=f"k_sil_sim_{years}_{sel_t}", disabled=True,
                 help="시뮬 쪽은 위 [조회 연도] 고정입니다.")
# 좌우 같은 가로 스케일 — 각자 선택된 연도의 양쪽 직급 최대값 기준.
_sil_max = max(
    max(tier_distribution(baseline.headcount_by_year[base_t]).values()),
    max(tier_distribution(sim.headcount_by_year[sel_t]).values()))
with sil_l:
    st.plotly_chart(shape_silhouette(baseline.headcount_by_year[base_t],
                                     f"BASELINE · {year_label(base_t)}", C_BASE,
                                     x_max=_sil_max),
                    use_container_width=True, key="sil_base", config=PLOTLY_CONFIG)
with sil_r:
    st.plotly_chart(shape_silhouette(sim.headcount_by_year[sel_t],
                                     f"시뮬 · {year_label(sel_t)}", C_BLUE,
                                     x_max=_sil_max,
                                     ref=baseline.headcount_by_year[sel_t]),
                    use_container_width=True, key="sil_sim", config=PLOTLY_CONFIG)

st.divider()


# =============================================================
# ③ 좌우 비교 — baseline ↔ 시뮬
# =============================================================
st.markdown("### baseline ↔ 시뮬 좌우 비교")
st.caption(f"기준연도 = 올해({BASE_YEAR}) 현재 인원 스냅샷. 승진율·퇴직률·인상률 조정 효과는 "
           f"내년({BASE_YEAR + 1})부터 {BASE_YEAR + years}년까지 추계에 반영됩니다.")
comp_dim = st.radio(
    "구분 기준", ["직급", "직군"], horizontal=True, key="k_comp_dim",
    help="직급 = 직군별 단계(3~7개)를 상대 위치로 사원→부장 6직급에 묶어 집계 / "
         "직군 = P·R·E·A 4개 직군별 집계")
# 좌우 동일 축(y) 고정 — 양쪽 최대 총원 기준으로 같은 스케일에서 비교.
_cmp_y_max = max(
    max(sc.total_headcount(hc) for hc in baseline.headcount_by_year),
    max(sc.total_headcount(hc) for hc in sim.headcount_by_year)) * 1.08
left, right = st.columns(2)
with left:
    st.markdown("#### BASELINE (조정 없음)")
    st.caption("적용 변수: 승진율·퇴직률 기본 가정표 · 인상률 0% (조정 미반영 기준선)")
    if SHOW_TABLE:
        st.caption("연도별 직군 인원 · 총원")
        st.dataframe(headcount_table(baseline), use_container_width=True, hide_index=True)
    elif comp_dim == "직급":
        st.plotly_chart(area_by_tier(baseline, "인력 구조 (직급 누적)", y_max=_cmp_y_max),
                        use_container_width=True, key="area_base_tier", config=PLOTLY_CONFIG)
    else:
        st.plotly_chart(area_by_family(baseline, "인력 구조 (직군 누적)"),
                        use_container_width=True, key="area_base", config=PLOTLY_CONFIG)
with right:
    st.markdown("#### 시뮬레이션 (조정 반영)")
    st.caption(f"적용 변수: 승진율 {promo_pct:+g}% · 퇴직률 {attr_desc} · "
               f"인상률 {raise_summary(raise_by_tier)}")
    if SHOW_TABLE:
        st.caption("연도별 직군 인원 · 총원")
        st.dataframe(headcount_table(sim), use_container_width=True, hide_index=True)
    elif comp_dim == "직급":
        st.plotly_chart(area_by_tier(sim, "인력 구조 (직급 누적)", y_max=_cmp_y_max),
                        use_container_width=True, key="area_sim_tier", config=PLOTLY_CONFIG)
    else:
        st.plotly_chart(area_by_family(sim, "인력 구조 (직군 누적)"),
                        use_container_width=True, key="area_sim", config=PLOTLY_CONFIG)

st.markdown("#### 총 인건비 (baseline vs 시뮬)")
if SHOW_TABLE:
    st.dataframe(cost_table(baseline, sim), use_container_width=True, hide_index=True)
else:
    st.plotly_chart(cost_chart(baseline, sim), use_container_width=True,
                    key="cost_chart", config=PLOTLY_CONFIG)

# --- 추계 기간 전체 누적 인건비: baseline vs 시뮬 합산 + Δ(금액·비율) 강조 ---
#     기준연(t=0)~최종연도 합(cum_base/cum_sim 은 KPI 블록에서 계산).
#     인건비 증가는 부담 방향이므로 delta_color="inverse"(+가 빨강).
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
# ③ 정년 재채용 인원 (연도별)
# =============================================================
st.markdown("#### 정년 재채용 인원 (연도별)")
st.caption(f"가정(더미): 각 직군 최상위 단계 이탈 중 {sc.RETIRE_SHARE:.0%}가 정년퇴직이고, "
           f"그중 {sc.REHIRE_RATE:.0%}를 촉탁(계약직)으로 재채용. "
           "재채용 인원은 별도 촉탁 풀로 보고 본 추계 인원에는 합산하지 않는 표시용 지표입니다.")
rehire_b = sc.rehire_by_year(baseline.headcount_by_year, base_params.attrition_rate)
rehire_s = sc.rehire_by_year(sim.headcount_by_year, sim_params.attrition_rate)
if SHOW_TABLE:
    _re_rows = [{"연도": year_label(t),
                 "baseline(명)": round(rehire_b[t], 1),
                 "시뮬(명)": round(rehire_s[t], 1),
                 "Δ(명)": round(rehire_s[t] - rehire_b[t], 1)}
                for t in range(1, len(rehire_b))]
    st.dataframe(pd.DataFrame(_re_rows), use_container_width=True, hide_index=True)
else:
    _re_yrs = [year_label(t) for t in range(1, len(rehire_b))]
    _re_fig = go.Figure()
    _re_fig.add_bar(x=_re_yrs, y=[round(v, 1) for v in rehire_b[1:]], name="baseline",
                    marker_color=C_BASE,
                    hovertemplate="baseline %{y:.1f}명<extra></extra>")
    _re_fig.add_bar(x=_re_yrs, y=[round(v, 1) for v in rehire_s[1:]], name="시뮬",
                    marker_color=C_SKY,
                    hovertemplate="시뮬 %{y:.1f}명<extra></extra>")
    _style(_re_fig, 260, "연도별 정년 재채용 인원 (명)")
    _re_fig.update_xaxes(type="category")
    _re_fig.update_layout(barmode="group", xaxis_title="연도", yaxis_title="재채용(명)")
    st.plotly_chart(_re_fig, use_container_width=True, key="rehire_chart",
                    config=PLOTLY_CONFIG)


# =============================================================
# ④ 직급별 승진율·퇴직률 상세 — 승진율은 직급(단계)마다 다르므로 표로 노출
# =============================================================
st.markdown("#### 직급별 승진율·퇴직률 (baseline vs 시뮬)")
st.caption("승진율·퇴직률은 직군·단계별로 전부 다릅니다. 조정 레버는 이 기본표에 배율로 "
           "적용되며, 시뮬 열이 실제 계산에 들어간 값입니다. (최상위 단계 승진율은 0 고정)")
_rate_rows = []
for _f in sc.FAMILY_LEVELS:
    _lvls = sc.FAMILY_LEVELS[_f]
    for _i, _lvl in enumerate(_lvls):
        _rate_rows.append({
            "직군": f"{_f} ({sc.FAMILY_LABEL[_f]})",
            "단계": _lvl,
            "직급": _tier_of(_i, len(_lvls)),
            "승진율 baseline": f"{base_params.promotion_rate[_f][_lvl] * 100:.1f}%",
            "승진율 시뮬": f"{sim_params.promotion_rate[_f][_lvl] * 100:.1f}%",
            "퇴직률 baseline": f"{base_params.attrition_rate[_f][_lvl] * 100:.1f}%",
            "퇴직률 시뮬": f"{sim_params.attrition_rate[_f][_lvl] * 100:.1f}%",
        })
st.dataframe(pd.DataFrame(_rate_rows), use_container_width=True, hide_index=True,
             height=280)

with st.expander("최종연도 직군·단계별 인원 상세 (baseline / 시뮬 / Δ)"):
    rows = []
    for f in sc.FAMILY_LEVELS:
        for lvl in sc.FAMILY_LEVELS[f]:
            b = end_base[f].get(lvl, 0.0)
            s = end_sim[f].get(lvl, 0.0)
            rows.append({"직군": f, "단계": lvl,
                         "baseline": round(b), "시뮬": round(s), "Δ": round(s - b)})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# =============================================================
# 스냅샷 저장·비교 (M3)
# =============================================================
st.divider()
st.markdown("## 스냅샷 저장·비교")

if st.button("현재 조합 스냅샷 저장"):
    controls = {"years": int(years), "promo_pct": float(promo_pct),
                "attr_pct": float(attr_pct), "attr_mode": attr_mode,
                "attr_by_tier": dict(attr_by_tier), "attr_by_age": dict(attr_by_age),
                "raise_by_tier": dict(raise_by_tier)}
    label = snap.make_label(attr_desc=attr_desc, **controls)
    st.session_state["snapshots"].append(snap.capture(label, controls, adj, sim))
    st.toast(f"스냅샷 저장: {label}")

snaps: list[snap.Snapshot] = st.session_state["snapshots"]

if not snaps:
    st.info("아직 저장된 스냅샷이 없습니다. 레버를 조정하고 [현재 조합 스냅샷 저장] 을 "
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
            _c_attr = attr_summary(c.get("attr_mode", "전체"), c.get("attr_pct", 0.0),
                                   c.get("attr_by_tier", {}), c.get("attr_by_age", {}))
            st.caption(f"연수 {c['years']} · 승진 {c['promo_pct']:+g}% · "
                       f"퇴직 {_c_attr} · 인상 {raise_summary(c.get('raise_by_tier', {}))}")
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
    "years": int(years), "promo_pct": float(promo_pct), "attr_pct": float(attr_pct),
    "attr_desc": attr_desc,
    "raise_desc": raise_summary(raise_by_tier),
    "raise_by_tier": {t: raise_by_tier[t] for t in TIER_ORDER},
    "tot_base": tot_base, "tot_sim": tot_sim,
    "cum_delta_eok": cum_delta / 1e8,
    "top_base": top_base, "top_sim": top_sim,
    "family_end": {f: round(v) for f, v in sc.headcount_by_family(end_sim).items()},
    # 저장된 스냅샷 요약 — 챗봇이 여러 시나리오를 비교·언급할 수 있게 컨텍스트로 전달.
    "snapshots": [
        {
            "label": s.label,
            "years": s.controls["years"],
            "promo_pct": s.controls["promo_pct"],
            "attr_desc": attr_summary(s.controls.get("attr_mode", "전체"),
                                      s.controls.get("attr_pct", 0.0),
                                      s.controls.get("attr_by_tier", {}),
                                      s.controls.get("attr_by_age", {})),
            "raise_desc": raise_summary(s.controls.get("raise_by_tier", {})),
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
             "승진율 레버를 올려 허리를 채우는 시나리오부터 확인해 보시길 권합니다.")},
        {"role": "user",
         "content": "그럼 승진율을 올리면 인건비는 얼마나 늘어?"},
        {"role": "assistant",
         "content": (
             "상단 레버에서 **승진율 조정을 +10~+30%** 로 올려 보세요. 상위 직급 단가가 "
             "높아 누적 인건비 Δ가 KPI 타일에 바로 잡힙니다. 인건비 부담이 크면 "
             "**직급별 연 인상률**에서 과장·차장만 올리고 나머지는 0%로 두는 절충안도 "
             f"비교해 볼 수 있어요. 조합별 결과는 [현재 조합 스냅샷 저장]으로 쌓아서 "
             f"{years}년 horizon 으로 나란히 비교하는 걸 추천합니다.")},
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

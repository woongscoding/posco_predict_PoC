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

import os
from datetime import date

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
# (코어는 t=0에 전이·인상 미적용이므로 조정 효과는 이미 내년부터 시작한다. 여기선 표시만 실제 연도로.)
BASE_YEAR = date.today().year


def year_label(t: int) -> str:
    """연차 t → 실제 연도 문자열. 기준연(t=0)은 '올해' 표기."""
    return f"{BASE_YEAR}(기준)" if t == 0 else str(BASE_YEAR + t)

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
:root{
  --navy:#002C5F; --blue:#0057B8; --blue-lt:#3E8FE0;
  --ink:#10233F; --muted:#6B7688; --line:#E1E7F0; --panel:#F4F7FB;
}
html, body, [class*="css"]{ font-family:'POSCO','Pretendard',system-ui,sans-serif; color:var(--ink); }
.stApp{ background:#FFFFFF; }

/* 사이드바 — 네이비 */
section[data-testid="stSidebar"]{ background:linear-gradient(180deg,#0A2A52,#0E2038); border-right:0; }
section[data-testid="stSidebar"] *{ color:#C7D5E8; }
section[data-testid="stSidebar"] label, section[data-testid="stSidebar"] h1,
section[data-testid="stSidebar"] h2, section[data-testid="stSidebar"] h3{ color:#fff !important; font-weight:700; }
section[data-testid="stSidebar"] .stNumberInput input, section[data-testid="stSidebar"] .stTextInput input{
  background:#FFFFFF !important; color:#0A2A52 !important; border:1px solid rgba(255,255,255,.35);
  border-radius:9px; font-weight:700; font-variant-numeric:tabular-nums;
  -webkit-text-fill-color:#0A2A52 !important; }
/* number_input +/- 스텝 버튼: 흰 배경 위 진한 아이콘 */
section[data-testid="stSidebar"] .stNumberInput button{ background:#FFFFFF !important; color:#0A2A52 !important; }
section[data-testid="stSidebar"] .stNumberInput button svg{ fill:#0A2A52 !important; }
section[data-testid="stSidebar"] .stSlider [role="slider"]{ background:var(--blue-lt); }
.posco-mark{ display:inline-block; border:1.5px solid rgba(255,255,255,.4); border-radius:6px;
  padding:6px 12px; color:#fff; font-weight:800; letter-spacing:.04em; }

/* 인라인 헤더 */
.posco-head{ display:flex; align-items:center; gap:12px; margin:4px 0 6px; }
.posco-head h1{ font-size:24px; font-weight:800; margin:0; letter-spacing:-.01em; }
.posco-badge{ font-size:10.5px; font-weight:700; letter-spacing:.06em; color:var(--blue);
  border:1px solid #C9DBF2; border-radius:5px; padding:3px 7px; }
.posco-sub{ color:var(--muted); font-size:13px; margin:2px 2px 10px; }

/* KPI 타일 */
.kpi-row{ display:grid; grid-template-columns:repeat(3,1fr); gap:14px; margin:18px 0; }
.kpi{ border-radius:12px; padding:20px; border:1px solid var(--line); background:var(--panel); }
.kpi.fill{ background:linear-gradient(135deg,#002C5F,#0A4C97); border:0; color:#fff; }
.kpi .label{ font-size:12px; font-weight:600; color:var(--muted); }
.kpi.fill .label{ color:#B7CDEA; }
.kpi .value{ font-size:32px; font-weight:800; letter-spacing:-.02em; margin-top:8px; }
.kpi .delta{ font-size:11.5px; font-weight:700; margin-top:6px; }
.kpi .up{ color:#1B8A5A; } .kpi .down{ color:#C33; } .kpi.fill .down{ color:#F3B4B4; }
.kpi.fill .up{ color:#9BE3C1; }

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


def render_header():
    st.markdown(
        '<div class="posco-head">'
        '<span class="posco-mark">POSCO</span>'
        '<h1>HR 인력운영 시뮬레이터</h1>'
        '<span class="posco-badge">v3 · MOCKUP</span>'
        '</div>',
        unsafe_allow_html=True)
    st.markdown(
        '<div class="posco-sub">승진율·퇴직률·인건비 인상률을 조정하면 향후 인력 구조와 '
        '총 인건비 변화를 마르코프로 추계해 baseline과 나란히 보여줍니다. 결정론 rule 계산.</div>',
        unsafe_allow_html=True)


# =============================================================
# 브랜드 팔레트 (POSCO 블루)
# =============================================================
C_NAVY = "#002C5F"
C_BLUE = "#0057B8"
C_BLUE_LT = "#3E8FE0"
C_BLUE_XLT = "#8FBEEE"
C_BASE = "#B7C2D2"       # baseline 계열(연한 회청)
C_GRID = "#F2F4F8"
C_INK = "#10233F"
FONT_FAMILY = "Pretendard, system-ui, sans-serif"

# 직군 색상 — 명시 지정
FAMILY_COLOR = {"P": C_NAVY, "R": C_BLUE, "E": C_BLUE_LT, "A": C_BLUE_XLT}

# 슬라이더 key ↔ 기본값. 복원은 이 key 에 값을 써넣고 rerun.
SLIDER_DEFAULTS = {"k_years": 5, "k_promo": 0, "k_attr": 0, "k_raise": 3.0}

# 차트 클릭 확대/툴바 끄기 (정적 표시) — 축 fixedrange 와 함께 줌·팬 차단
PLOTLY_CONFIG = {"displayModeBar": False, "staticPlot": False, "scrollZoom": False}


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
    st.session_state["k_promo"] = int(_pending["promo_pct"])
    st.session_state["k_attr"] = int(_pending["attr_pct"])
    st.session_state["k_raise"] = float(_pending["raise_rate_pct"])

inject_css()
render_header()


# =============================================================
# 사이드바 — 조정 레버 (위젯 key/값/스텝 불변)
# =============================================================
with st.sidebar:
    st.markdown('<span class="posco-mark">POSCO</span>', unsafe_allow_html=True)
    st.header("조정 레버")
    years = st.slider("추계 연수", 3, 15, step=1, key="k_years")

    st.divider()
    st.subheader("승진율")
    promo_pct = st.number_input("승진율 조정 (%, baseline 대비)", min_value=-50, max_value=100,
                                step=1, key="k_promo",
                                help="baseline 승진율 대비 배율. +30 이면 승진율 ×1.3. "
                                     "재직률이 음수가 되지 않도록 (1-퇴직률) 이하로 자동 제한.")

    st.subheader("퇴직률")
    attr_pct = st.number_input("퇴직률 조정 (%, baseline 대비)", min_value=-50, max_value=100,
                               step=1, key="k_attr",
                               help="baseline 퇴직률 대비 배율. -20 이면 퇴직률 ×0.8.")

    st.subheader("인건비 인상률")
    raise_rate = st.number_input("연 인상률 (%)", min_value=0.0, max_value=10.0, step=0.05,
                                 format="%.2f", key="k_raise",
                                 help="직접 기입(예: 3.15). 매년 단가 = 단가×(1+인상률)^연차. "
                                      "인상률은 민감하니 소수점까지 입력하세요(최대 10%).")

    st.divider()
    view_mode = st.radio("결과 보기 방식", ["차트", "표(숫자)"], horizontal=True,
                         help="차트=클릭 확대 없이 정적으로 표시 / 표=숫자만")

    st.divider()
    st.caption(
        f"승진율 배율 ×{1 + promo_pct/100:.2f} · "
        f"퇴직률 배율 ×{1 + attr_pct/100:.2f} · "
        f"인상률 {raise_rate:.2f}%"
    )
    st.caption("© POSCO HR PoC · 더미데이터 기반 목업")

SHOW_TABLE = view_mode == "표(숫자)"


# =============================================================
# 계산 — baseline(조정 없음) vs 시뮬(조정 반영)   [로직 불변]
# =============================================================
@st.cache_data(show_spinner=False)
def compute(years: int, promo_pct: int, attr_pct: int, raise_rate: float):
    base_params = sc.build_default_params(years=years)
    baseline = sc.run(base_params)
    adj = sc.Adjustments(
        promotion_scale=1 + promo_pct / 100.0,
        attrition_scale=1 + attr_pct / 100.0,
        raise_rate=raise_rate / 100.0,
    )
    sim_params = sc.apply_adjustments(base_params, adj)
    problems = sc.validate(sim_params)
    sim = sc.run(sim_params, baseline_cost=baseline.labor_cost_by_year)
    return adj, baseline, sim, problems


adj, baseline, sim, problems = compute(years, promo_pct, attr_pct, raise_rate)

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
    fig.update_layout(barmode="group", xaxis_title="연도", yaxis_title="인건비(억원)")
    return fig


# --- 직급 구조 실루엣 (모래시계 ↔ 피라미드) --------------------------------
# 직군마다 단계 수가 달라(3~7단계) 직접 겹칠 수 없으므로, 각 단계의 '상대 위치'로
# 5개 티어(하위→상위)에 합산한다. 중앙정렬 가로막대로 그리면 폭의 넓고좁음이
# 그대로 조직 실루엣이 된다 — 허리가 얇으면 모래시계, 위로 갈수록 좁아지면 피라미드.
TIER_ORDER = ["하위", "중하", "중위", "중상", "상위"]  # 아래→위


def _tier_of(i: int, n: int) -> str:
    p = i / (n - 1) if n > 1 else 0.5
    if p < 0.2:
        return "하위"
    if p < 0.4:
        return "중하"
    if p < 0.6:
        return "중위"
    if p < 0.8:
        return "중상"
    return "상위"


def tier_distribution(hc_year: dict[str, dict[str, float]]) -> dict[str, float]:
    tiers = {t: 0.0 for t in TIER_ORDER}
    for f, levels in sc.FAMILY_LEVELS.items():
        n = len(levels)
        fam_hc = hc_year.get(f, {})
        for i, lvl in enumerate(levels):
            tiers[_tier_of(i, n)] += fam_hc.get(lvl, 0.0)
    return tiers


def shape_silhouette(hc_year: dict[str, dict[str, float]], title: str,
                     color: str) -> go.Figure:
    tiers = tier_distribution(hc_year)
    vals = [tiers[t] for t in TIER_ORDER]
    fig = go.Figure(go.Bar(
        y=TIER_ORDER, x=vals, base=[-v / 2 for v in vals],
        orientation="h", marker_color=color, width=0.72,
        text=[f"{v:,.0f}명" for v in vals], textposition="outside",
        cliponaxis=False, hovertemplate="%{y} %{x:,.0f}명<extra></extra>",
    ))
    _style(fig, 300, title)
    _max = max(vals) if vals else 1
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
    fig.update_layout(showlegend=False, xaxis_title=None, yaxis_title="억원",
                      yaxis=dict(title_font=dict(size=10)))
    return fig


# =============================================================
# 핵심 차이 KPI 타일 + 스냅샷 저장
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

st.markdown(
    f'''
<div class="kpi-row">
  <div class="kpi fill"><div class="label">{years}년 누적 인건비 Δ (vs baseline)</div>
    <div class="value">{cum_delta/1e8:+,.0f}억</div>
    <div class="delta {_cost_cls}">{_cost_arrow} {_cost_txt}</div></div>
  <div class="kpi"><div class="label">최종연도 총원</div>
    <div class="value">{tot_sim:,.0f}명</div>
    <div class="delta {_head_cls}">{_head_arrow} {head_gap:+,.0f}명</div></div>
  <div class="kpi"><div class="label">상위단계 비중</div>
    <div class="value">{top_sim:.1f}%</div>
    <div class="delta {_top_cls}">{_top_arrow} {top_delta:+.1f}%p</div></div>
</div>''',
    unsafe_allow_html=True)

if st.button("스냅샷 저장"):
    controls = {"years": int(years), "promo_pct": int(promo_pct),
                "attr_pct": int(attr_pct), "raise_rate_pct": float(raise_rate)}
    label = snap.make_label(**controls)
    st.session_state["snapshots"].append(snap.capture(label, controls, adj, sim))
    st.toast(f"스냅샷 저장: {label}")

st.divider()


# =============================================================
# 좌우 비교 — baseline ↔ 시뮬
# =============================================================
st.markdown("### baseline ↔ 시뮬 좌우 비교")
st.caption(f"기준연도 = 올해({BASE_YEAR}) 현재 인원 스냅샷. 승진율·퇴직률·인상률 조정 효과는 "
           f"내년({BASE_YEAR + 1})부터 {BASE_YEAR + years}년까지 추계에 반영됩니다.")
left, right = st.columns(2)
with left:
    st.markdown("#### BASELINE (조정 없음)")
    if SHOW_TABLE:
        st.caption("연도별 직군 인원 · 총원")
        st.dataframe(headcount_table(baseline), use_container_width=True, hide_index=True)
    else:
        st.plotly_chart(area_by_family(baseline, "인력 구조 (직군 누적)"),
                        use_container_width=True, key="area_base", config=PLOTLY_CONFIG)
with right:
    st.markdown("#### 시뮬레이션 (조정 반영)")
    if SHOW_TABLE:
        st.caption("연도별 직군 인원 · 총원")
        st.dataframe(headcount_table(sim), use_container_width=True, hide_index=True)
    else:
        st.plotly_chart(area_by_family(sim, "인력 구조 (직군 누적)"),
                        use_container_width=True, key="area_sim", config=PLOTLY_CONFIG)

st.markdown("#### 총 인건비 (baseline vs 시뮬)")
if SHOW_TABLE:
    st.dataframe(cost_table(baseline, sim), use_container_width=True, hide_index=True)
else:
    st.plotly_chart(cost_chart(baseline, sim), use_container_width=True,
                    key="cost_chart", config=PLOTLY_CONFIG)

st.markdown("#### 직급 구조 실루엣 — 모래시계 ↔ 피라미드")
st.caption(f"직급을 상대 위치로 5개 티어(하위→상위)에 묶어 중앙정렬한 실루엣. "
           f"좌=baseline 현재({BASE_YEAR})는 허리가 얇은 **모래시계형**, "
           f"우=시뮬 최종연도({BASE_YEAR + years})는 레버 조정 후 형태 변화를 보여줍니다.")
sil_l, sil_r = st.columns(2)
with sil_l:
    st.plotly_chart(shape_silhouette(baseline.headcount_by_year[0],
                                     f"BASELINE · {BASE_YEAR}(현재)", C_BASE),
                    use_container_width=True, key="sil_base", config=PLOTLY_CONFIG)
with sil_r:
    st.plotly_chart(shape_silhouette(sim.headcount_by_year[-1],
                                     f"시뮬 · {BASE_YEAR + years}(최종연도)", C_BLUE),
                    use_container_width=True, key="sil_sim", config=PLOTLY_CONFIG)

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
            st.caption(f"연수 {c['years']} · 승진 {c['promo_pct']:+d}% · "
                       f"퇴직 {c['attr_pct']:+d}% · 인상 {c['raise_rate_pct']:g}%")
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
# 대화형 인사이트 챗봇 (Claude, 메모리 유지 / 키 없으면 rule 폴백)
# =============================================================
st.divider()
st.markdown("### 인사이트 챗봇")

insight_ctx = {
    "years": int(years), "promo_pct": int(promo_pct), "attr_pct": int(attr_pct),
    "raise_rate": float(raise_rate),
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
            "attr_pct": s.controls["attr_pct"],
            "raise_pct": s.controls["raise_rate_pct"],
            "final_total": round(s.final_total),
            "cum_delta_eok": s.cum_cost_delta / 1e8,
            "top_share": s.top_share,
        }
        for s in st.session_state.get("snapshots", [])
    ],
}

if insight_bot.has_api_key():
    st.caption("Claude 대화 모드 — 현재 시뮬 수치를 근거로 제안·질문하며 대화합니다.")
else:
    st.caption("rule 폴백 모드 — ANTHROPIC_API_KEY 설정 시 Claude 대화가 활성화됩니다.")

col_chat, col_clear = st.columns([6, 1])
with col_clear:
    if st.button("대화 초기화", use_container_width=True):
        st.session_state["chat"] = []
        st.rerun()

st.session_state.setdefault("chat", [])
for m in st.session_state["chat"]:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])

prompt = st.chat_input("이 시뮬 결과에 대해 물어보세요 (예: 인건비를 줄이려면?)")
if prompt:
    st.session_state["chat"].append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
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

"""
app.py — POSCO HR 인력운영 시뮬레이터 (v3, 배포 엔트리)
==================================================
승진율 / 퇴직률 / 인건비 인상률을 조정하면 향후 인력 구조와 총 인건비가 어떻게
변하는지 결정론 마르코프로 추계해 baseline ↔ 시뮬을 좌우로 나란히 비교하고,
변수 조합을 스냅샷으로 저장·비교한다. LLM·리서치 없음(순수 rule).

  - 결정론 코어:   sim_core.py  (직군 4종 P/R/E/A × 단계별 전이 + 인건비)
  - 스냅샷 로직:   snapshots.py (라벨·캡처·비교표, Streamlit 비의존)
  - 화면(본 파일): 좌우 비교 + 스냅샷 + POSCO 블루 브랜딩

실행:  streamlit run app.py
※ POSCO 로고: assets/posco_logo.(png|svg|jpg) 파일이 있으면 그걸 헤더에 사용하고,
  없으면 텍스트 워드마크(플레이스홀더)로 대체한다. 공식 로고는 그 경로에 넣으면 된다.
"""
from __future__ import annotations

import os

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

st.set_page_config(page_title="POSCO 인력운영 시뮬레이터", layout="wide", page_icon="🔷")

# Streamlit Cloud 배포용: Secrets 에 넣은 키를 환경변수로 브리지(insight_bot 은 os.environ 을 읽음).
try:
    if "ANTHROPIC_API_KEY" in st.secrets and not os.environ.get("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = str(st.secrets["ANTHROPIC_API_KEY"])
except Exception:
    pass

# =============================================================
# 브랜드 팔레트 (푸른색 계열)
# =============================================================
POSCO_NAVY = "#003A70"
POSCO_BLUE = "#0072CE"
POSCO_CYAN = "#00A3E0"
STEEL_BLUE = "#5B7FA6"
GRID_GRAY = "#94A3B8"

# 직군 색상 — 전부 블루 계열이면서 서로 구분되게
FAMILY_COLOR = {"P": POSCO_NAVY, "R": POSCO_BLUE, "E": POSCO_CYAN, "A": STEEL_BLUE}

# 슬라이더 key ↔ 기본값. 복원은 이 key 에 값을 써넣고 rerun.
SLIDER_DEFAULTS = {"k_years": 5, "k_promo": 0, "k_attr": 0, "k_raise": 3.0}

# 차트 클릭 확대/툴바 끄기 (정적 표시) — 축 fixedrange 와 함께 줌·팬 차단
PLOTLY_CONFIG = {"displayModeBar": False, "staticPlot": False, "scrollZoom": False}


# =============================================================
# 브랜드 헤더 + CSS
# =============================================================
def _find_logo() -> str | None:
    for name in ("posco_logo.png", "posco_logo.svg", "posco_logo.jpg"):
        p = os.path.join("assets", name)
        if os.path.exists(p):
            return p
    return None


def render_brand_header():
    st.markdown(
        """
        <style>
        .posco-header{
            background:linear-gradient(90deg,#003A70 0%,#0072CE 100%);
            border-radius:14px; padding:18px 26px; margin:2px 0 4px 0;
            display:flex; align-items:center; gap:18px;
        }
        .posco-logo{
            font-family:Arial,Helvetica,sans-serif; font-weight:800; letter-spacing:3px;
            color:#fff; font-size:28px; line-height:1;
            border:2px solid rgba(255,255,255,.9); border-radius:8px; padding:7px 14px;
            white-space:nowrap;
        }
        .posco-title{ color:#fff; font-size:22px; font-weight:700; line-height:1.3; }
        .posco-title .ver{
            font-size:13px; font-weight:600; opacity:.85; margin-left:8px;
            background:rgba(255,255,255,.18); padding:2px 8px; border-radius:6px;
            vertical-align:middle;
        }
        .posco-sub{ color:#5b7fa6; font-size:13px; margin:6px 2px 14px 2px; }
        div[data-testid="stMetricValue"]{ color:#0072CE; font-weight:700; }
        section[data-testid="stSidebar"]{ border-right:1px solid #d6e4f5; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    logo = _find_logo()
    if logo:
        c1, c2 = st.columns([1, 6], vertical_alignment="center")
        with c1:
            st.image(logo, use_container_width=True)
        with c2:
            st.markdown(
                '<div class="posco-title">HR 인력운영 시뮬레이터'
                '<span class="ver">v3 · MOCKUP</span></div>',
                unsafe_allow_html=True)
    else:
        st.markdown(
            '<div class="posco-header">'
            '<div class="posco-logo">POSCO</div>'
            '<div class="posco-title">HR 인력운영 시뮬레이터'
            '<span class="ver">v3 · MOCKUP</span></div>'
            '</div>',
            unsafe_allow_html=True)

    st.markdown(
        '<div class="posco-sub">승진율·퇴직률·인건비 인상률을 조정하면 향후 인력 구조와 '
        '총 인건비 변화를 마르코프로 추계해 <b>baseline과 나란히</b> 보여줍니다. '
        '결정론 rule 계산만 사용 — API 호출 없음.</div>',
        unsafe_allow_html=True)


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

render_brand_header()


# =============================================================
# 사이드바 — 조정 레버 3종 (각 슬라이더에 고정 key)
# =============================================================
with st.sidebar:
    st.header("⚙️ 조정 레버")
    years = st.slider("추계 연수", 3, 15, step=1, key="k_years")

    st.divider()
    st.subheader("🔼 승진율")
    promo_pct = st.slider("승진율 조정", -50, 100, step=5, format="%d%%", key="k_promo",
                          help="baseline 승진율 대비 배율. +30%면 승진율 ×1.3. "
                               "재직률이 음수가 되지 않도록 (1-퇴직률) 이하로 자동 제한.")

    st.subheader("🔽 퇴직률")
    attr_pct = st.slider("퇴직률 조정", -50, 100, step=5, format="%d%%", key="k_attr",
                         help="baseline 퇴직률 대비 배율. -20%면 퇴직률 ×0.8.")

    st.subheader("💰 인건비 인상률")
    raise_rate = st.slider("연 인상률", 0.0, 10.0, step=0.5, format="%.1f%%", key="k_raise",
                           help="매년 단가 = 단가×(1+인상률)^연차")

    st.divider()
    view_mode = st.radio("결과 보기 방식", ["📋 표(숫자)", "📈 차트"], horizontal=True,
                         help="표=숫자만 / 차트=클릭 확대 없이 정적으로 표시")

    st.divider()
    st.caption(
        f"승진율 배율 ×{1 + promo_pct/100:.2f} · "
        f"퇴직률 배율 ×{1 + attr_pct/100:.2f} · "
        f"인상률 {raise_rate:.1f}%"
    )
    st.caption("© POSCO HR PoC · 더미데이터 기반 목업")

SHOW_TABLE = view_mode.startswith("📋")


# =============================================================
# 계산 — baseline(조정 없음) vs 시뮬(조정 반영)
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
# 차트 헬퍼
# =============================================================
def area_by_family(result: sc.SimResult, title: str, height: int = 320,
                   showlegend: bool = True) -> go.Figure:
    yrs = list(range(len(result.headcount_by_year)))
    fig = go.Figure()
    for f in sc.FAMILY_LEVELS:
        vals = [sc.headcount_by_family(hc).get(f, 0.0)
                for hc in result.headcount_by_year]
        fig.add_trace(go.Scatter(
            x=yrs, y=vals, mode="lines", stackgroup="one", name=f,
            line=dict(width=0.5, color=FAMILY_COLOR[f]),
            hovertemplate=f"{f} %{{y:.0f}}명<extra></extra>",
        ))
    fig.update_layout(title=title or None, xaxis_title="연차", yaxis_title="인원(명)",
                      height=height, margin=dict(t=40 if title else 10, b=10, l=10, r=10),
                      showlegend=showlegend, legend=dict(orientation="h", y=-0.2),
                      plot_bgcolor="rgba(0,0,0,0)")
    fig.update_xaxes(fixedrange=True)
    fig.update_yaxes(fixedrange=True)
    return fig


def headcount_table(result: sc.SimResult) -> pd.DataFrame:
    """연도별 직군 인원 + 총원 표(숫자)."""
    data = {}
    for t, hc in enumerate(result.headcount_by_year):
        byf = sc.headcount_by_family(hc)
        row = {f: round(byf.get(f, 0.0)) for f in sc.FAMILY_LEVELS}
        row["총원"] = round(sc.total_headcount(hc))
        data[t] = row
    df = pd.DataFrame.from_dict(data, orient="index")
    df.index.name = "연차"
    return df.reset_index()


def cost_table(baseline: sc.SimResult, sim: sc.SimResult) -> pd.DataFrame:
    """연도별 총 인건비 표(억원): baseline / 시뮬 / Δ."""
    rows = []
    for t, (b, s) in enumerate(zip(baseline.labor_cost_by_year, sim.labor_cost_by_year)):
        rows.append({"연차": t,
                     "baseline(억)": round(b / 1e8, 1),
                     "시뮬(억)": round(s / 1e8, 1),
                     "Δ(억)": round((s - b) / 1e8, 1)})
    return pd.DataFrame(rows)


def cost_overlay(baseline: sc.SimResult, sim: sc.SimResult) -> go.Figure:
    yrs = list(range(len(baseline.labor_cost_by_year)))
    b = [c / 1e8 for c in baseline.labor_cost_by_year]
    s = [c / 1e8 for c in sim.labor_cost_by_year]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=yrs, y=b, mode="lines+markers", name="baseline",
                             line=dict(color=GRID_GRAY, width=2)))
    fig.add_trace(go.Scatter(x=yrs, y=s, mode="lines+markers", name="시뮬",
                             line=dict(color=POSCO_BLUE, width=3, dash="dash")))
    fig.update_layout(title="총 인건비 (억원)", xaxis_title="연차", yaxis_title="인건비(억원)",
                      height=300, margin=dict(t=40, b=10, l=10, r=10),
                      legend=dict(orientation="h", y=-0.25), plot_bgcolor="rgba(0,0,0,0)")
    fig.update_xaxes(fixedrange=True)
    fig.update_yaxes(fixedrange=True)
    return fig


def mini_cost(result: sc.SimResult) -> go.Figure:
    yrs = list(range(len(result.labor_cost_by_year)))
    s = [c / 1e8 for c in result.labor_cost_by_year]
    fig = go.Figure(go.Scatter(x=yrs, y=s, mode="lines",
                               line=dict(color=POSCO_BLUE, width=2)))
    fig.update_layout(height=140, margin=dict(t=6, b=6, l=6, r=6), showlegend=False,
                      xaxis_title=None, yaxis_title="억원",
                      yaxis=dict(title_font=dict(size=10)), plot_bgcolor="rgba(0,0,0,0)")
    fig.update_xaxes(fixedrange=True)
    fig.update_yaxes(fixedrange=True)
    return fig


# =============================================================
# Δ 강조 블록 (대형 KPI) + 스냅샷 저장
# =============================================================
end_base = baseline.headcount_by_year[-1]
end_sim = sim.headcount_by_year[-1]
tot_base = sc.total_headcount(end_base)
tot_sim = sc.total_headcount(end_sim)
top_base = sc.top_level_share(end_base)
top_sim = sc.top_level_share(end_sim)
cum_delta = sim.cum_cost_delta_vs_baseline

st.markdown("### 📊 핵심 차이 (Δ vs baseline)")
k1, k2, k3 = st.columns(3)
k1.metric(f"{years}년 누적 인건비 Δ", f"{cum_delta/1e8:+,.0f}억",
          help="시뮬 누적 인건비 − baseline 누적 인건비")
k2.metric("최종연도 총원", f"{tot_sim:,.0f}명", f"{tot_sim - tot_base:+,.0f}명")
k3.metric("상위단계 비중", f"{top_sim:.1f}%", f"{top_sim - top_base:+.1f}%p")

if st.button("📸 스냅샷 저장"):
    controls = {"years": int(years), "promo_pct": int(promo_pct),
                "attr_pct": int(attr_pct), "raise_rate_pct": float(raise_rate)}
    label = snap.make_label(**controls)
    st.session_state["snapshots"].append(snap.capture(label, controls, adj, sim))
    st.toast(f"스냅샷 저장: {label}")

st.divider()


# =============================================================
# 좌우 비교 — baseline ↔ 시뮬
# =============================================================
st.markdown("### ↔️ baseline ↔ 시뮬 좌우 비교")
left, right = st.columns(2)
with left:
    st.markdown("#### ⬅️ BASELINE (조정 없음)")
    if SHOW_TABLE:
        st.caption("연도별 직군 인원 · 총원")
        st.dataframe(headcount_table(baseline), use_container_width=True, hide_index=True)
    else:
        st.plotly_chart(area_by_family(baseline, "인력 구조 (직군 누적)"),
                        use_container_width=True, key="area_base", config=PLOTLY_CONFIG)
with right:
    st.markdown("#### ➡️ 시뮬레이션 (조정 반영)")
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
    st.plotly_chart(cost_overlay(baseline, sim), use_container_width=True,
                    key="cost_overlay", config=PLOTLY_CONFIG)

with st.expander("🔍 최종연도 직군·단계별 인원 상세 (baseline / 시뮬 / Δ)"):
    rows = []
    for f in sc.FAMILY_LEVELS:
        for lvl in sc.FAMILY_LEVELS[f]:
            b = end_base[f].get(lvl, 0.0)
            s = end_sim[f].get(lvl, 0.0)
            rows.append({"직군": f, "단계": lvl,
                         "baseline": round(b), "시뮬": round(s), "Δ": round(s - b)})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# =============================================================
# 📸 스냅샷 저장·비교 (M3)
# =============================================================
st.divider()
st.markdown("## 📸 스냅샷 저장·비교")
snaps: list[snap.Snapshot] = st.session_state["snapshots"]

if not snaps:
    st.info("아직 저장된 스냅샷이 없습니다. 슬라이더를 조정하고 위의 **[📸 스냅샷 저장]** 을 "
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
    st.caption("※ 누적 Δ는 각 스냅샷의 **자기 horizon** 무조정 baseline 대비입니다. "
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
# 💬 대화형 인사이트 챗봇 (Claude, 메모리 유지 / 키 없으면 rule 폴백)
# =============================================================
st.divider()
st.markdown("### 💬 인사이트 챗봇")

# 현재 시뮬 수치를 매 턴 컨텍스트로 주입
insight_ctx = {
    "years": int(years), "promo_pct": int(promo_pct), "attr_pct": int(attr_pct),
    "raise_rate": float(raise_rate),
    "tot_base": tot_base, "tot_sim": tot_sim,
    "cum_delta_eok": cum_delta / 1e8,
    "top_base": top_base, "top_sim": top_sim,
    "family_end": {f: round(v) for f, v in sc.headcount_by_family(end_sim).items()},
}

if insight_bot.has_api_key():
    st.caption("🟢 Claude 대화 모드 — 현재 시뮬 수치를 근거로 제안·질문하며 대화합니다.")
else:
    st.caption("🟡 rule 폴백 모드 — ANTHROPIC_API_KEY 설정 시 Claude 대화가 활성화됩니다.")

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

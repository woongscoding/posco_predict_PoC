"""
sim_app.py — v3 인력운영 시뮬레이터 (M2 좌우 비교 + M3 스냅샷)
==================================================
슬라이더 3종(승진율 / 퇴직률 / 인건비 인상률)으로 결정론 마르코프 추계를 돌려
baseline ↔ 시뮬을 좌우로 나란히 비교하고, 변수 조합을 스냅샷으로 저장·비교한다.
LLM·리서치 없음. 더미데이터는 sim_core.

실행:  streamlit run sim_app.py
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import sim_core as sc
import snapshots as snap

st.set_page_config(page_title="인력운영 시뮬레이터 v3", layout="wide", page_icon="🧮")

# 직군 색상 (누적 영역/막대에서 직군 구분)
FAMILY_COLOR = {"P": "#4C78A8", "R": "#F58518", "E": "#54A24B", "A": "#B279A2"}

# 슬라이더 key ↔ 기본값. 복원은 이 key 에 값을 써넣고 rerun.
SLIDER_DEFAULTS = {"k_years": 5, "k_promo": 0, "k_attr": 0, "k_raise": 3.0}


# =============================================================
# 위젯 생성 '이전'에 처리해야 하는 세션 상태 초기화 + 복원 적용
#   (Streamlit 제약: 위젯이 만들어진 뒤엔 그 key 의 session_state 를 못 바꾼다)
# =============================================================
for _k, _v in SLIDER_DEFAULTS.items():
    st.session_state.setdefault(_k, _v)
st.session_state.setdefault("snapshots", [])

# 복원 요청이 걸려 있으면(직전 rerun 에서 예약) 슬라이더 key 에 값 주입 후 소비
_pending = st.session_state.pop("_pending_restore", None)
if _pending:
    st.session_state["k_years"] = int(_pending["years"])
    st.session_state["k_promo"] = int(_pending["promo_pct"])
    st.session_state["k_attr"] = int(_pending["attr_pct"])
    st.session_state["k_raise"] = float(_pending["raise_rate_pct"])


# =============================================================
# 사이드바 — 조정 레버 3종 (각 슬라이더에 고정 key)
# =============================================================
st.title("🧮 HR 인력운영 시뮬레이터 (v3)")
st.caption(
    "승진율·퇴직률·인건비 인상률을 조정하면 향후 인력 구조와 총 인건비가 어떻게 "
    "변하는지 마르코프로 추계해 **baseline과 나란히** 보여줍니다. "
    "_결정론 rule 계산만 사용 — API 호출 없음._"
)

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
    st.caption(
        f"승진율 배율 ×{1 + promo_pct/100:.2f} · "
        f"퇴직률 배율 ×{1 + attr_pct/100:.2f} · "
        f"인상률 {raise_rate:.1f}%"
    )


# =============================================================
# 계산 — baseline(조정 없음) vs 시뮬(조정 반영)
#   baseline 인상률은 sim_core 기본값(3%) 고정, 시뮬은 슬라이더 값.
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
    """연도별 직군 누적 영역차트(총원 구조)."""
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
                      showlegend=showlegend,
                      legend=dict(orientation="h", y=-0.2))
    return fig


def cost_overlay(baseline: sc.SimResult, sim: sc.SimResult) -> go.Figure:
    """총 인건비: baseline 실선 + 시뮬 점선 겹쳐 표시."""
    yrs = list(range(len(baseline.labor_cost_by_year)))
    b = [c / 1e8 for c in baseline.labor_cost_by_year]
    s = [c / 1e8 for c in sim.labor_cost_by_year]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=yrs, y=b, mode="lines+markers", name="baseline",
                             line=dict(color="#888", width=2)))
    fig.add_trace(go.Scatter(x=yrs, y=s, mode="lines+markers", name="시뮬",
                             line=dict(color="#E4572E", width=2, dash="dash")))
    fig.update_layout(title="총 인건비 (억원)", xaxis_title="연차",
                      yaxis_title="인건비(억원)", height=300,
                      margin=dict(t=40, b=10, l=10, r=10),
                      legend=dict(orientation="h", y=-0.25))
    return fig


def mini_cost(result: sc.SimResult) -> go.Figure:
    """스냅샷용 소형 인건비 라인(자체 시계열만)."""
    yrs = list(range(len(result.labor_cost_by_year)))
    s = [c / 1e8 for c in result.labor_cost_by_year]
    fig = go.Figure(go.Scatter(x=yrs, y=s, mode="lines",
                               line=dict(color="#E4572E", width=2)))
    fig.update_layout(height=140, margin=dict(t=6, b=6, l=6, r=6),
                      showlegend=False, xaxis_title=None, yaxis_title="억원",
                      yaxis=dict(title_font=dict(size=10)))
    return fig


# =============================================================
# Δ 강조 블록 (대형 KPI)
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

# 📸 현재 조합을 스냅샷으로 저장
if st.button("📸 스냅샷 저장", use_container_width=False):
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
    st.plotly_chart(area_by_family(baseline, "인력 구조 (직군 누적)"),
                    use_container_width=True, key="area_base")
with right:
    st.markdown("#### ➡️ 시뮬레이션 (조정 반영)")
    st.plotly_chart(area_by_family(sim, "인력 구조 (직군 누적)"),
                    use_container_width=True, key="area_sim")

st.plotly_chart(cost_overlay(baseline, sim), use_container_width=True, key="cost_overlay")


# =============================================================
# 상세 테이블 — 최종연도 직군·단계별 인원 (baseline vs 시뮬 vs Δ)
# =============================================================
with st.expander("🔍 최종연도 직군·단계별 인원 상세 (baseline / 시뮬 / Δ)"):
    rows = []
    for f in sc.FAMILY_LEVELS:
        for lvl in sc.FAMILY_LEVELS[f]:
            b = end_base[f].get(lvl, 0.0)
            s = end_sim[f].get(lvl, 0.0)
            rows.append({
                "직군": f, "단계": lvl,
                "baseline": round(b), "시뮬": round(s), "Δ": round(s - b),
            })
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
    # ── 목록: 라벨 수정 / 복원 / 삭제 ──────────────────────────
    st.markdown("#### 저장된 스냅샷")
    top_bar = st.columns([6, 1])
    with top_bar[1]:
        if st.button("전체 지우기", use_container_width=True):
            st.session_state["snapshots"] = []
            st.rerun()

    for s in snaps:
        c_label, c_info, c_restore, c_del = st.columns([4, 3, 1.2, 1.2])
        with c_label:
            new_label = st.text_input("라벨", value=s.label, key=f"label_{s.snapshot_id}",
                                      label_visibility="collapsed")
            s.label = new_label   # 저장 리스트의 객체를 직접 수정(세션에 반영)
        with c_info:
            c = s.controls
            st.caption(f"연수 {c['years']} · 승진 {c['promo_pct']:+d}% · "
                       f"퇴직 {c['attr_pct']:+d}% · 인상 {c['raise_rate_pct']:g}%")
        with c_restore:
            if st.button("복원", key=f"restore_{s.snapshot_id}", use_container_width=True):
                # 위젯 생성 이후이므로 지금 key 를 못 바꾼다 → 예약 후 rerun 하여
                # 다음 실행의 '위젯 생성 이전' 지점에서 주입한다.
                st.session_state["_pending_restore"] = dict(s.controls)
                st.rerun()
        with c_del:
            if st.button("삭제", key=f"del_{s.snapshot_id}", use_container_width=True):
                st.session_state["snapshots"] = [
                    x for x in snaps if x.snapshot_id != s.snapshot_id]
                st.rerun()

    # ── 비교표 ────────────────────────────────────────────────
    st.markdown("#### 비교표")
    st.dataframe(snap.comparison_table(snaps), use_container_width=True, hide_index=True)
    st.caption("※ 누적 Δ는 각 스냅샷의 **자기 horizon** 무조정 baseline 대비입니다. "
               "'연수'가 다른 행은 기준 horizon 이 달라 절대 Δ를 직접 비교하지 마세요. "
               "baseline 스냅샷(조정 없음)의 Δ는 '—' 로 표기됩니다.")

    # ── 미니차트: 스냅샷당 [직군 누적 영역 + 인건비 라인], 4개씩 한 줄 ──
    st.markdown("#### 미니차트 (스냅샷별 인력구조 · 인건비)")
    PER_ROW = 4
    for start in range(0, len(snaps), PER_ROW):
        row = snaps[start:start + PER_ROW]
        cols = st.columns(len(row))
        for s, col in zip(row, cols):
            with col:
                st.markdown(f"**{s.label}**")
                st.plotly_chart(
                    area_by_family(s.result, "", height=180, showlegend=False),
                    use_container_width=True, key=f"mini_area_{s.snapshot_id}")
                st.plotly_chart(mini_cost(s.result), use_container_width=True,
                                key=f"mini_cost_{s.snapshot_id}")


# =============================================================
# rule 인사이트 (템플릿 문장 — LLM 없음, §7 OFF 모드)
# =============================================================
st.divider()
st.markdown("### 💬 인사이트 (rule 템플릿)")
direction = "증가" if cum_delta >= 0 else "감소"
st.info(
    f"**{years}년 후** 총원 **{tot_sim:,.0f}명** "
    f"(baseline 대비 **{tot_sim - tot_base:+,.0f}명**). "
    f"누적 인건비는 baseline 대비 **{cum_delta/1e8:+,.0f}억** {direction}. "
    f"승진율 {promo_pct:+d}% · 퇴직률 {attr_pct:+d}% · 인상률 {raise_rate:.1f}% 조정으로 "
    f"상위단계 비중이 **{top_sim - top_base:+.1f}%p** 변동했습니다."
)
st.caption("※ 인사이트는 순수 rule 템플릿입니다(API 호출 0). LLM 서술은 추후 토글로 추가 예정.")

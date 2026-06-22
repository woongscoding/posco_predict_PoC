"""
viz.py — plotly 차트 함수 모음
==================================================
모든 차트는 인터랙티브 plotly Figure 를 반환. app.py 에서 st.plotly_chart 로 표시.
"""

from __future__ import annotations
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from markov import STATES, ABSORBING, total_active, survivors_by_rank

RANK_COLORS = {"사원": "#4C78A8", "대리": "#F58518", "차장": "#E45756"}


def heatmap_matrix(P: pd.DataFrame, title: str = "전이확률 행렬 P") -> go.Figure:
    """전이행렬 히트맵 (행=현재상태, 열=다음상태)."""
    fig = px.imshow(
        P.values,
        x=list(P.columns), y=list(P.index),
        color_continuous_scale="Blues",
        zmin=0, zmax=1,
        text_auto=".2f",
        aspect="auto",
        labels=dict(x="다음연도 상태", y="현재 상태", color="확률"),
    )
    fig.update_layout(title=title, height=460, margin=dict(l=10, r=10, t=50, b=10))
    return fig


def heatmap_diff(P_before: pd.DataFrame, P_after: pd.DataFrame,
                 title: str = "조정 전후 차이 (After − Before, %p)") -> go.Figure:
    """조정 전후 전이확률 차이 히트맵 (어디가 바뀌었나)."""
    diff = (P_after - P_before) * 100.0
    fig = px.imshow(
        diff.values,
        x=list(diff.columns), y=list(diff.index),
        color_continuous_scale="RdBu", color_continuous_midpoint=0,
        text_auto=".1f", aspect="auto",
        labels=dict(x="다음연도 상태", y="현재 상태", color="%p"),
    )
    fig.update_layout(title=title, height=460, margin=dict(l=10, r=10, t=50, b=10))
    return fig


def line_total(projection: pd.DataFrame, label: str = "총 재직인원") -> go.Figure:
    """연도별 총 재직 인원 추이 라인."""
    total = total_active(projection)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=total.index, y=total.values, mode="lines+markers",
                             name=label, line=dict(width=3)))
    fig.update_layout(title="연도별 총 재직인원 추이", xaxis_title="연도",
                      yaxis_title="인원(명)", height=380)
    return fig


def bar_by_rank(projection: pd.DataFrame) -> go.Figure:
    """직급별 인원 추이 (그룹 막대)."""
    by_rank = survivors_by_rank(projection)
    fig = go.Figure()
    for rank in ["사원", "대리", "차장"]:
        if rank in by_rank.columns:
            fig.add_trace(go.Bar(x=by_rank.index, y=by_rank[rank], name=rank,
                                 marker_color=RANK_COLORS.get(rank)))
    fig.update_layout(barmode="group", title="직급별 인원 추이",
                      xaxis_title="연도", yaxis_title="인원(명)", height=380)
    return fig


def line_states(projection: pd.DataFrame) -> go.Figure:
    """상태별(7개) 인원 추이 라인 — 상세 뷰."""
    fig = go.Figure()
    for s in STATES:
        if s == ABSORBING:
            continue
        fig.add_trace(go.Scatter(x=projection.index, y=projection[s],
                                 mode="lines+markers", name=s))
    fig.update_layout(title="상태별 인원 추이(이탈 제외)", xaxis_title="연도",
                      yaxis_title="인원(명)", height=380)
    return fig


def compare_total(baseline: pd.DataFrame, adjusted: pd.DataFrame) -> go.Figure:
    """Baseline vs Adjusted 총 인원 추이 겹쳐 그리기."""
    b, a = total_active(baseline), total_active(adjusted)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=b.index, y=b.values, mode="lines+markers",
                             name="Baseline(내부만)", line=dict(width=3)))
    fig.add_trace(go.Scatter(x=a.index, y=a.values, mode="lines+markers",
                             name="Adjusted(외부보정)", line=dict(width=3, dash="dash")))
    fig.update_layout(title="시나리오 비교 — 총 재직인원", xaxis_title="연도",
                      yaxis_title="인원(명)", height=400)
    return fig


def compare_by_rank(baseline: pd.DataFrame, adjusted: pd.DataFrame) -> go.Figure:
    """직급별 Baseline vs Adjusted 비교 (실선 vs 점선)."""
    b, a = survivors_by_rank(baseline), survivors_by_rank(adjusted)
    fig = go.Figure()
    for rank in ["사원", "대리", "차장"]:
        if rank in b.columns:
            fig.add_trace(go.Scatter(x=b.index, y=b[rank], mode="lines+markers",
                                     name=f"{rank}·Baseline",
                                     line=dict(color=RANK_COLORS[rank], width=2)))
            fig.add_trace(go.Scatter(x=a.index, y=a[rank], mode="lines+markers",
                                     name=f"{rank}·Adjusted",
                                     line=dict(color=RANK_COLORS[rank], width=2, dash="dash")))
    fig.update_layout(title="직급별 시나리오 비교", xaxis_title="연도",
                      yaxis_title="인원(명)", height=420)
    return fig


def delta_decomposition(baseline: pd.DataFrame, adjusted: pd.DataFrame) -> go.Figure:
    """
    델타(차이) 분해 뷰 — 직급별·연도별 (Adjusted − Baseline) 갭.
    ★ 인사이트의 핵심: 어느 직급/어느 시점에서 갭이 벌어지는지.
    """
    b, a = survivors_by_rank(baseline), survivors_by_rank(adjusted)
    delta = (a - b)
    fig = go.Figure()
    for rank in ["사원", "대리", "차장"]:
        if rank in delta.columns:
            fig.add_trace(go.Bar(x=delta.index, y=delta[rank], name=rank,
                                 marker_color=RANK_COLORS.get(rank)))
    fig.update_layout(barmode="relative", title="델타 분해 — 직급별 갭 (Adjusted − Baseline)",
                      xaxis_title="연도", yaxis_title="인원 차이(명)", height=400)
    fig.add_hline(y=0, line_dash="dot", line_color="gray")
    return fig


def gap_gauge(required: float, projected: float, label: str = "목표 대비 갭") -> go.Figure:
    """목표 필요인력 대비 예측 잔존인력 게이지."""
    gap = required - projected
    fig = go.Figure(go.Indicator(
        mode="number+delta",
        value=projected,
        number={"suffix": " 명"},
        delta={"reference": required, "relative": False,
               "increasing": {"color": "green"}, "decreasing": {"color": "red"}},
        title={"text": f"{label}<br><sub>필요 {required:.0f}명 대비</sub>"},
    ))
    fig.update_layout(height=260)
    return fig


def eval_score_bar(history: list[dict]) -> go.Figure:
    """리서치 평가 점수 이력 막대 (재시도별)."""
    if not history:
        return go.Figure()
    rounds = [f"{h['round']}회차" for h in history]
    scores = [h["score"] for h in history]
    colors = ["#E45756" if s < 80 else "#54A24B" for s in scores]
    fig = go.Figure(go.Bar(x=rounds, y=scores, marker_color=colors,
                           text=scores, textposition="outside"))
    fig.add_hline(y=80, line_dash="dash", line_color="gray",
                  annotation_text="충분 기준 80점")
    fig.update_layout(title="리서치 평가 점수 추이", yaxis_range=[0, 100],
                      yaxis_title="점수", height=300)
    return fig

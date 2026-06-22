"""
app.py — 선제적 인력 예측 PoC (Streamlit 메인)
==================================================
UX 흐름 (과정의 시각화가 핵심):
  입력 → [예측 실행] → 단계별 진행이 눈에 보이게:
    Stage 0 더미생성 → Stage 1 SQLite·전처리 → Stage 2 마르코프(내부 트랙)
    Stage 3 외부 리서치 에이전트(외부 트랙, 좌우 2컬럼으로 병렬 연출)
    Stage 4 행렬 재조정 → Stage 5 시나리오 비교 → Stage 6 보고서

실행:  streamlit run app.py
키 없어도 끝까지 도는 목업 폴백 포함.
"""

from __future__ import annotations
import os
import time
import pandas as pd
import streamlit as st

# .env 파일에서 ANTHROPIC_API_KEY 등을 환경변수로 로드 (로컬 모듈 import 전에 실행)
from dotenv import load_dotenv
load_dotenv()

# Streamlit Cloud 배포용: Secrets에 넣은 키를 환경변수로 브리지.
# (anthropic SDK / 우리 코드는 os.environ 을 읽으므로, st.secrets → os.environ 으로 옮겨줌)
# 로컬에선 secrets.toml 이 없어 그냥 통과(.env 로딩으로 충분).
try:
    for _k in ("ANTHROPIC_API_KEY",):
        if _k in st.secrets and not os.environ.get(_k):
            os.environ[_k] = str(st.secrets[_k])
except Exception:
    pass

import generate_dummy as gd
import data_pipeline as dp
import markov as mk
import viz
from research_agent import run_research_agent, get_graph_mermaid, _can_run_real
from report import generate_report

st.set_page_config(page_title="선제적 인력 예측 PoC", layout="wide", page_icon="📊")

STEP_PAUSE = 0.4  # 진행감을 주는 짧은 sleep


# =============================================================
# (선택) 접근 암호 게이트 — 공개 배포 시 API 크레딧 보호용.
#   Secrets/환경변수에 APP_PASSWORD 가 있으면 활성화, 없으면 그냥 통과(로컬/내부).
# =============================================================
def _check_access() -> bool:
    pw = None
    try:
        pw = st.secrets.get("APP_PASSWORD")
    except Exception:
        pw = None
    pw = pw or os.environ.get("APP_PASSWORD")
    if not pw:
        return True  # 암호 미설정 → 게이트 없음
    if st.session_state.get("_authed"):
        return True
    st.title("🔒 데모 접근 제한")
    st.caption("공개 링크 보호를 위해 접근 암호가 설정되어 있습니다.")
    entered = st.text_input("접근 암호", type="password")
    if entered == pw:
        st.session_state["_authed"] = True
        st.rerun()
    elif entered:
        st.error("암호가 일치하지 않습니다.")
    return False


if not _check_access():
    st.stop()


# =============================================================
# 사이드바 — 입력
# =============================================================
st.title("📊 선제적 인력 예측 PoC")
st.caption(
    "기준연도 → 목표연도 입력 시, 내부 인력데이터를 SQL로 끌어와 전처리하고 "
    "**마르코프 모델**로 투영하며, 동시에 **외부동향 리서치 에이전트(LangGraph)**로 "
    "조정계수를 뽑아 시나리오를 재조정하고, 두 시나리오를 비교해 **보고서 초안**까지 생성합니다. "
    "_이 데모의 핵심은 숫자 정확도가 아니라 **파이프라인이 단계별로 도는 과정**입니다._"
)

with st.sidebar:
    st.header("⚙️ 분석 조건")
    base_year = st.number_input("기준연도", min_value=2020, max_value=2035, value=2026, step=1)
    target_year = st.number_input("목표연도", min_value=2021, max_value=2045, value=2031, step=1)
    use_external = st.toggle("외부보정 시나리오 on", value=True)

    st.divider()
    st.subheader("목표 필요인력(선택)")
    use_target = st.checkbox("직급별 목표 필요인력 입력")
    target_required = None
    if use_target:
        req_사원 = st.number_input("사원 필요", 0, 10000, 2500, step=100)
        req_대리 = st.number_input("대리 필요", 0, 10000, 1500, step=100)
        req_차장 = st.number_input("차장 필요", 0, 10000, 900, step=100)
        target_required = {"사원": req_사원, "대리": req_대리, "차장": req_차장}

    st.divider()
    real_mode = _can_run_real()
    st.markdown(f"**리서치 모드:** {'🟢 real (web search)' if real_mode else '🟡 mock 폴백'}")
    st.markdown(f"**보고서 모드:** {'🟢 real (LLM)' if os.environ.get('ANTHROPIC_API_KEY') else '🟡 mock 폴백'}")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        st.info("ANTHROPIC_API_KEY 미설정 — 목업 폴백으로 끝까지 실행됩니다.")

    # ─────────────────────────────────────────────────────────
    # 데모 전용 설정 — 실데이터 연결 시 사라지는 노브.
    # 실서비스에선 인력 규모를 '입력'하지 않고 HR 테이블에서 SELECT COUNT(*)로 '조회'한다.
    # ─────────────────────────────────────────────────────────
    st.divider()
    with st.expander("🔧 데모 설정 (실데이터 연결 시 사라짐)"):
        st.caption(
            "아래 값은 진짜 HR DB가 없는 데모를 위해 **더미 인력 데이터를 몇 명 규모로 "
            "생성할지**만 정합니다. 실데이터 연결 시 인력 규모는 입력값이 아니라 "
            "실제 테이블에서 조회되는 값이므로 이 설정은 제거됩니다."
        )
        n_emp = st.slider("더미 인력 규모(명)", 1000, 10000, 6000, step=500,
                          help="실데이터 교체 시 무관 — 더미 생성 규모만 조절")

    st.divider()
    run = st.button("🚀 예측 실행", type="primary", use_container_width=True)

# 데이터 명세서 / 그래프 구조는 항상 열람 가능
with st.expander("📑 데이터 명세서 (실데이터 전환 설계도)"):
    st.markdown("""
| 항목 | 실데이터 출처(가정) | 데모 더미 생성 방식 |
|---|---|---|
| 전이 카운트 | HR 시스템 인사이동 이력 테이블 | `generate_dummy.py` 가정값(전이확률 상수) |
| 초기 인력벡터 | 재직자 현황 스냅샷 | 초기 상태 분포 상수(`INITIAL_STATE_MIX`) |
| 조정계수 | 외부 리서치 → 전문가 검토 | LangGraph 에이전트 추출 / 매핑 테이블 상수 |
| 목표 필요인력 | 사업계획·조직설계 | 사이드바 수기 입력 |

> 목적: PoC를 '장난감'이 아니라 **'실데이터 전환 설계도'**로 제시. 교체 지점은 `generate_dummy.py`/`data_pipeline.py`의 SQL.
""")

if target_year <= base_year:
    st.error("목표연도는 기준연도보다 커야 합니다.")
    st.stop()

if not run:
    st.info("👈 사이드바에서 조건을 설정하고 **[예측 실행]**을 눌러주세요.")
    st.stop()


# =============================================================
# 실행 — 단계별 진행
# =============================================================

# ---------- Stage 0: 더미 생성 ----------
with st.status("**Stage 0 — 더미 인력 '이력 패널' 생성** (개인 단위, 2020~기준연도)", expanded=True) as s0:
    st.write(f"개인 단위 인사이동 이력 생성 중… ({gd.START_YEAR}~{int(base_year)}, 실데이터에선 HR 추출로 교체)")
    df = gd.generate_dummy(n=n_emp, start_year=gd.START_YEAR, end_year=int(base_year))
    time.sleep(STEP_PAUSE)
    n_years = df["관측연도"].nunique()
    st.write(f"✅ 총 **{len(df):,}건**의 전이 관측 (재직 ~{n_emp:,}명 × {n_years}개 관측연도). "
             f"컬럼: `{', '.join(df.columns)}`")
    st.caption("1행 = 한 직원이 '관측연도'에 가진 상태(현재상태)와 그 다음 해 상태(다음연도_상태) = 인사이동 1건")
    st.dataframe(df.head(10), use_container_width=True)
    s0.update(label=f"**Stage 0 — 이력 패널 생성 완료 ({len(df):,}건 / {n_years}개 연도)**",
              state="complete", expanded=False)


# ---------- Stage 1: SQLite 적재 · SQL 조회 · 전처리 ----------
with st.status("**Stage 1 — 내부데이터 수집·전처리** (SQLite + SQL + pandas)", expanded=True) as s1:
    st.write("in-memory SQLite에 적재 중…")
    conn = dp.load_to_sqlite(df)
    time.sleep(STEP_PAUSE)

    st.markdown("**① 실제 SELECT 쿼리로 데이터 조회** (SQL로 끌어오는 과정 시연)")
    st.code(dp.SELECT_RAW.strip(), language="sql")
    st.dataframe(dp.run_query(conn, dp.SELECT_RAW), use_container_width=True)

    st.markdown("**② 관측연도별 이력 분포** (2020~기준연도 여러 해가 쌓인 패널)")
    st.code(dp.SELECT_BY_YEAR.strip(), language="sql")
    st.dataframe(dp.run_query(conn, dp.SELECT_BY_YEAR), use_container_width=True)

    st.markdown("**③ pandas 전처리** (상태 라벨링·결측/이상치 정리)")
    clean = dp.preprocess(df)
    st.write(f"전처리 후 {len(clean):,}행 (제거 {len(df)-len(clean):,}행)")

    st.markdown("**④ 전이 집계** (`GROUP BY 현재상태 → 다음연도_상태`, 전 연도 풀링)")
    st.code(dp.SELECT_TRANSITIONS.strip(), language="sql")
    counts = dp.build_transition_counts(clean)
    st.dataframe(counts, use_container_width=True)
    st.caption("↑ 전이 카운트 행렬 (행=현재상태, 열=다음연도 상태) — 여러 관측연도를 합산해 표본 확보")

    n0 = dp.get_initial_headcount(clean, int(base_year))
    s1.update(label="**Stage 1 — 수집·전처리 완료**", state="complete", expanded=False)


# ---------- 내부/외부 트랙 — 좌우 2컬럼 (병렬 연출) ----------
st.markdown("## 🔀 병렬 트랙 — 내부 모델링 ↔ 외부 리서치")
col_in, col_out = st.columns(2)

# ===== 내부 트랙: Stage 2 마르코프 =====
with col_in:
    st.markdown("### 🏢 내부 트랙 — 마르코프 모델링")
    with st.status("Stage 2 — 전이행렬 추정·투영", expanded=True) as s2:
        st.write("전이카운트 → 행 정규화 + 라플라스 평활(+0.5)로 P 추정…")
        P_base = mk.estimate_transition_matrix(counts)
        time.sleep(STEP_PAUSE)
        st.plotly_chart(viz.heatmap_matrix(P_base, "전이확률 행렬 P (Baseline)"),
                        use_container_width=True)
        st.caption("라플라스 평활: 표본 적을 때 0 관측 전이를 0%로 박제하지 않게 하는 안정화 장치.")

        st.write(f"인력벡터 투영: n(t+1)=n(t)·P 를 {int(base_year)}→{int(target_year)} 반복…")
        proj_base = mk.project(n0, P_base, int(base_year), int(target_year))
        time.sleep(STEP_PAUSE)
        st.plotly_chart(viz.line_total(proj_base), use_container_width=True)
        st.plotly_chart(viz.bar_by_rank(proj_base), use_container_width=True)
        st.caption("⚠️ 마르코프 무기억성 가정 — 실제 이탈은 근속·연령 영향. 본 모델은 근속밴드 상태로 부분 보완.")
        s2.update(label="Stage 2 — 내부 베이스라인 완료", state="complete", expanded=True)

# ===== 외부 트랙: Stage 3 리서치 에이전트 =====
with col_out:
    st.markdown("### 🌐 외부 트랙 — 리서치 에이전트 (LangGraph)")
    with st.status("Stage 3 — 검색→평가→재검색 루프→추출", expanded=True) as s3:
        with st.expander("🧩 에이전트 그래프 구조 (mermaid)"):
            st.code(get_graph_mermaid(), language="mermaid")

        st.markdown("**실시간 검증 로그**")
        log_box = st.container()
        logs: list[str] = []

        def emit(ev: dict):
            # 노드 실행마다 실시간 로그 카드 갱신
            logs.append(ev.get("message", str(ev)))
            with log_box:
                st.write(ev.get("message", str(ev)))
            time.sleep(STEP_PAUSE)

        keyword = "철강·제조 인력시장, AI 도입, 정년·신규채용 동향, 경력직 이직률"
        st.caption(f"검색 키워드: _{keyword}_")
        research = run_research_agent(keyword, use_real=None, emit=emit)

        st.markdown(f"**모드:** `{research['mode']}`")
        if research["history"]:
            # 3축 루브릭 채점(정량성/방향성/커버리지 + 종합) — 어느 축이 부족했는지 가시화
            st.plotly_chart(viz.eval_rubric_chart(research["history"]),
                            use_container_width=True)
            best_round = research.get("best_round")
            st.plotly_chart(viz.eval_score_bar(research["history"], best_round),
                            use_container_width=True)
            if best_round is not None:
                best_h = next((h for h in research["history"]
                               if h["round"] == best_round), None)
                if best_h:
                    st.caption(
                        f"🏆 최고점 **{best_round}회차({best_h['score']}점)** 결과로 "
                        f"조정계수를 추출했습니다 — 마지막 라운드가 더 낮아도 최고 라운드를 사용.")

            # refine 노드가 생성한 보강 쿼리를 라운드별로 표시
            refine_rows = [
                {"라운드": f"{h['round']}회차",
                 "부족 항목(missing)": ", ".join(h.get("missing") or []) or "—",
                 "보강 쿼리(refine)": " / ".join(h.get("refine_queries") or []) or "—"}
                for h in research["history"]
            ]
            if any(r["보강 쿼리(refine)"] != "—" for r in refine_rows):
                st.markdown("**🔧 쿼리 보강 이력 (refine 노드 산출물)**")
                st.dataframe(pd.DataFrame(refine_rows), use_container_width=True,
                             hide_index=True)

        st.markdown("**산출물 1 — 트렌드 요약 카드**")
        for t in research["trends"]:
            st.markdown(f"- **{t.get('name','')}** ({t.get('direction','')}): {t.get('desc','')}")

        st.markdown("**산출물 2 — 조정계수 매핑 테이블** (명시적 시나리오 레버, 블랙박스 아님)")
        coef_df = pd.DataFrame(research["coefficients"])
        if not coef_df.empty:
            coef_df = coef_df.rename(columns={
                "trend": "외부 트렌드", "from": "영향 전이(from)",
                "to": "영향 전이(to)", "delta_pp": "조정(%p)"})
        st.dataframe(coef_df, use_container_width=True)
        s3.update(label=f"Stage 3 — 리서치 완료 ({research['mode']})", state="complete", expanded=True)


# ---------- Stage 4: 행렬 재조정 ----------
st.markdown("## 🔧 Stage 4 — 행렬 재조정 (외부보정 시나리오)")
if use_external:
    with st.status("조정계수를 P에 적용 → 재정규화 → 재투영", expanded=True) as s4:
        P_adj = mk.adjust_matrix(P_base, research["coefficients"])
        time.sleep(STEP_PAUSE)
        c1, c2 = st.columns(2)
        with c1:
            st.plotly_chart(viz.heatmap_matrix(P_base, "조정 전 P (Baseline)"),
                            use_container_width=True)
        with c2:
            st.plotly_chart(viz.heatmap_matrix(P_adj, "조정 후 P (Adjusted)"),
                            use_container_width=True)
        st.plotly_chart(viz.heatmap_diff(P_base, P_adj), use_container_width=True)
        st.caption("↑ 어느 전이가 얼마나 바뀌었는지(%p). 적용 후 각 행 재정규화로 합=1 유지.")
        proj_adj = mk.project(n0, P_adj, int(base_year), int(target_year))
        s4.update(label="Stage 4 — 재조정·재투영 완료", state="complete", expanded=True)
else:
    st.info("외부보정 토글 OFF — Adjusted = Baseline 으로 처리합니다.")
    P_adj = P_base
    proj_adj = proj_base


# ---------- Stage 5: 시나리오 비교 (수렴) ----------
st.markdown("## 🎯 Stage 5 — 두 시나리오 비교 (수렴)")
with st.status("Baseline vs Adjusted 비교 시각화", expanded=True) as s5:
    st.plotly_chart(viz.compare_total(proj_base, proj_adj), use_container_width=True)
    st.plotly_chart(viz.compare_by_rank(proj_base, proj_adj), use_container_width=True)
    st.markdown("**델타 분해 뷰 — 어느 직급·시점에서 갭이 벌어지는가** (인사이트 핵심)")
    if use_external:
        st.plotly_chart(viz.delta_decomposition(proj_base, proj_adj), use_container_width=True)
    else:
        st.info("외부보정 토글 OFF — Adjusted = Baseline 이라 델타가 전부 0입니다. "
                "토글을 켜면 외부동향 보정에 따른 직급·시점별 갭이 이 자리에 표시됩니다.")

    if target_required:
        st.markdown("### 목표 필요인력 대비 갭")
        by_rank_end = mk.survivors_by_rank(proj_adj).iloc[-1]
        gcols = st.columns(len(target_required))
        for (rank, req), gc in zip(target_required.items(), gcols):
            proj_val = float(by_rank_end.get(rank, 0))
            with gc:
                st.plotly_chart(viz.gap_gauge(req, proj_val, f"{rank}"),
                                use_container_width=True)
                short = req - proj_val
                if short > 0:
                    st.error(f"**{rank} {short:.0f}명 부족 — 선제 대응 필요**")
                else:
                    st.success(f"{rank} 여유 {-short:.0f}명")
    s5.update(label="Stage 5 — 비교 완료", state="complete", expanded=True)


# ---------- Stage 6: 보고서 초안 ----------
st.markdown("## 📝 Stage 6 — 보고서 초안 생성")
with st.status("구조화된 산출물 → LLM(또는 목업) 보고서", expanded=True) as s6:
    report_md, rmode = generate_report(
        proj_base, proj_adj, research["trends"], research["coefficients"],
        int(base_year), int(target_year), target_required)
    st.markdown(f"**생성 모드:** `{rmode}`")
    s6.update(label=f"Stage 6 — 보고서 생성 완료 ({rmode})", state="complete", expanded=True)

st.markdown("---")
st.markdown(report_md)
st.download_button("⬇️ 보고서 다운로드 (.md)", data=report_md,
                   file_name=f"인력예측보고서_{int(base_year)}_{int(target_year)}.md",
                   mime="text/markdown")

st.success("✅ 전체 파이프라인 완료 — 더미생성 → SQL조회 → 전처리 → 마르코프 → 외부보정 → 비교 → 보고서")

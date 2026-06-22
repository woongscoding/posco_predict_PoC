"""
report.py — 보고서 초안 생성 (Stage 6)
==================================================
구조화된 산출물(숫자·트렌드·갭)을 LLM에 넘겨 한국어 컨설팅 보고서 초안 생성.
API 키 없으면 템플릿 기반 목업 초안으로 폴백 (숫자는 실제 계산값으로 채움).

섹션: ① 요약 ② 내부 전망(베이스라인) ③ 외부동향 ④ 시나리오 비교·갭 ⑤ 시사점·선제 제언
"""

from __future__ import annotations
import os
import json
import pandas as pd

from markov import total_active, survivors_by_rank

REPORT_MODEL = "claude-opus-4-8"


def _summarize_numbers(baseline: pd.DataFrame, adjusted: pd.DataFrame,
                       base_year: int, target_year: int) -> dict:
    """보고서 입력용 핵심 숫자 묶음."""
    b_total = total_active(baseline)
    a_total = total_active(adjusted)
    b_rank = survivors_by_rank(baseline)
    a_rank = survivors_by_rank(adjusted)

    return {
        "base_year": base_year,
        "target_year": target_year,
        "baseline_total_start": round(float(b_total.iloc[0])),
        "baseline_total_end": round(float(b_total.iloc[-1])),
        "adjusted_total_end": round(float(a_total.iloc[-1])),
        "total_gap_end": round(float(a_total.iloc[-1] - b_total.iloc[-1])),
        "baseline_rank_end": {k: round(float(v)) for k, v in b_rank.iloc[-1].items()},
        "adjusted_rank_end": {k: round(float(v)) for k, v in a_rank.iloc[-1].items()},
        "rank_gap_end": {k: round(float(a_rank.iloc[-1][k] - b_rank.iloc[-1][k]))
                         for k in b_rank.columns},
    }


def _build_context(nums: dict, trends: list, coefficients: list,
                   target_required: dict | None) -> str:
    """LLM 프롬프트용 컨텍스트 문자열."""
    ctx = {
        "핵심숫자": nums,
        "외부트렌드": trends,
        "조정계수": coefficients,
        "목표필요인력": target_required or "미입력",
    }
    return json.dumps(ctx, ensure_ascii=False, indent=2)


def generate_report(baseline: pd.DataFrame, adjusted: pd.DataFrame,
                    trends: list, coefficients: list,
                    base_year: int, target_year: int,
                    target_required: dict | None = None,
                    use_real: bool | None = None) -> tuple[str, str]:
    """
    보고서 초안 생성.
    Returns (markdown_text, mode)  — mode ∈ {"real", "mock"}
    """
    nums = _summarize_numbers(baseline, adjusted, base_year, target_year)

    if use_real is None:
        use_real = bool(os.environ.get("ANTHROPIC_API_KEY"))

    if use_real:
        try:
            import anthropic
            client = anthropic.Anthropic()
            context = _build_context(nums, trends, coefficients, target_required)
            resp = client.messages.create(
                model=REPORT_MODEL,
                max_tokens=2500,
                messages=[{
                    "role": "user",
                    "content": (
                        "너는 전략 컨설턴트다. 아래 구조화된 인력 분석 결과로 한국어 컨설팅 "
                        "보고서 초안을 작성하라. 마크다운으로, 다음 5개 섹션을 포함:\n"
                        "① 요약 ② 내부 인력 전망(베이스라인) ③ 외부동향 "
                        "④ 시나리오 비교·갭 ⑤ 시사점·선제 제언\n"
                        "숫자는 제공된 값을 그대로 인용하고, 차장 구간 누수/병목과 "
                        "선제 대응을 강조하라.\n\n"
                        f"[데이터]\n{context}"
                    ),
                }],
            )
            txt = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
            return txt, "real"
        except Exception as e:
            # 실패 시 목업 폴백
            mock = _mock_report(nums, trends, coefficients, target_required)
            return mock + f"\n\n> ⚠️ (real 생성 실패 → 목업 폴백: {e})", "mock"

    return _mock_report(nums, trends, coefficients, target_required), "mock"


def _mock_report(nums: dict, trends: list, coefficients: list,
                 target_required: dict | None) -> str:
    """템플릿 기반 목업 보고서 — 실제 계산값을 채워 그럴듯하게."""
    base_y, tgt_y = nums["base_year"], nums["target_year"]
    b_end, a_end = nums["baseline_total_end"], nums["adjusted_total_end"]
    gap = nums["total_gap_end"]
    b_start = nums["baseline_total_start"]

    trend_lines = "\n".join(
        f"- **{t.get('name','')}** ({t.get('direction','')}): {t.get('desc','')}"
        for t in trends
    )
    coef_lines = "\n".join(
        f"| {c.get('trend','')} | {c.get('from','')} → {c.get('to','')} | "
        f"{c.get('delta_pp',0):+.1f}%p |"
        for c in coefficients
    )
    rank_gap = nums["rank_gap_end"]
    rank_gap_lines = "\n".join(
        f"- {rank}: Baseline {nums['baseline_rank_end'].get(rank,0)}명 → "
        f"Adjusted {nums['adjusted_rank_end'].get(rank,0)}명 "
        f"(**{rank_gap.get(rank,0):+d}명**)"
        for rank in nums["baseline_rank_end"].keys()
    )

    # 가장 크게 빠지는 직급
    worst_rank = min(rank_gap, key=rank_gap.get) if rank_gap else "차장"
    worst_val = rank_gap.get(worst_rank, 0)

    gap_section = ""
    if target_required:
        gap_section = "\n### 목표 필요인력 대비 갭\n"
        for rank, req in target_required.items():
            proj = nums["adjusted_rank_end"].get(rank, 0)
            short = req - proj
            gap_section += (f"- {rank}: 필요 {req}명 vs 예측 잔존 {proj}명 "
                            f"→ **{'부족 ' + str(short) + '명, 선제 대응 필요' if short>0 else '여유 ' + str(-short) + '명'}**\n")

    return f"""# 선제적 인력 예측 보고서 초안 ({base_y} → {tgt_y})

> 본 보고서는 PoC 데모 산출물입니다. 숫자는 더미데이터 기반 파이프라인 계산값이며,
> 실데이터 교체 시 동일 구조로 재생산됩니다.

## ① 요약
- 분석 기간: **{base_y}년 → {tgt_y}년** ({tgt_y - base_y}개년 투영)
- 내부 베이스라인 기준 총 재직인원은 {b_start}명 → **{b_end}명**으로 변동 예상.
- 외부동향 보정(Adjusted) 시 목표연도 총원은 **{a_end}명**으로, 베이스라인 대비 **{gap:+d}명** 차이.
- 핵심 리스크: **차장 구간 인력 누수(병목)** 와 **대리→차장 승진 정체**가 중간관리층 공동화를 유발.

## ② 내부 인력 전망 (베이스라인)
- 마르코프 전이행렬(라플라스 평활 적용) 기반 연도별 투영.
- 사원→대리 승진은 활발하나, 대리→차장 승진이 정체되어 상위직급 충원이 지연됨.
- 차장급 이탈률이 구조적으로 높아 목표연도로 갈수록 차장 인력이 빠르게 감소.

## ③ 외부동향
{trend_lines}

**조정계수 매핑 (명시적 시나리오 레버):**

| 외부 트렌드 | 영향 대상 전이 | 조정 |
|---|---|---|
{coef_lines}

## ④ 시나리오 비교 · 갭
목표연도({tgt_y}) 직급별 Baseline vs Adjusted:
{rank_gap_lines}

- 가장 큰 변동 직급: **{worst_rank}** ({worst_val:+d}명) — 외부 환경 악화 시 최우선 노출.
{gap_section}
## ⑤ 시사점 · 선제 제언
1. **차장 병목 선제 해소**: 대리→차장 승진 적체 해소(승진 TO 확대·경로 다변화)로 중간관리층 공동화 방지.
2. **차장급 리텐션 강화**: 이탈률이 가장 높은 구간으로, 처우·보임 정책으로 잔류율 +3~5%p 확보 시 갭 상당 부분 상쇄.
3. **선제 채용 계획 연동**: 외부보정 시나리오의 갭({gap:+d}명)을 채용·전환배치 계획에 미리 반영.
4. **모니터링 체계화**: 본 파이프라인을 분기 단위로 실데이터에 연결, 전이확률 변화를 조기 감지.

---
*PoC 파이프라인: 더미생성 → SQLite 조회 → 전처리 → 마르코프 투영 → 외부 리서치 보정 → 시나리오 비교 → 보고서.*
"""

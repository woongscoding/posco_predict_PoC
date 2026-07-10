"""
insight_bot.py — P-GPT 대화형 인사이트 (Claude API, 메모리 유지)
==================================================
현재 시뮬 수치(AS-IS + 시뮬1/시뮬2)를 컨텍스트로 넣어 대화하며 인사이트를 얻는다.
대화 기록(messages)은 호출부(app.py)의 st.session_state 가 들고 있고, 매 턴 통째로 전달한다.

- 모델: claude-opus-4-8 (Anthropic 공식 SDK)
- 키: 환경변수 ANTHROPIC_API_KEY (Streamlit Cloud 는 Secrets → 환경변수 브리지)
- 키가 없으면 rule 템플릿 폴백(API 호출 0)
"""
from __future__ import annotations

import os

MODEL = "claude-opus-4-8"


def has_api_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _format_scenarios(scns: list[dict], tot_base: float) -> str:
    lines = []
    for s in scns:
        fam = " · ".join(f"{f} {v:,}명" for f, v in s.get("family_end", {}).items())
        lines.append(
            f"- {s.get('name')}: 레버(승진 {s.get('promo_desc')} · 퇴직 {s.get('attr_desc')} · "
            f"인상 {s.get('raise_desc')}) → 최종총원 {s.get('tot'):,.0f}명 "
            f"(AS-IS 대비 {s.get('tot', 0) - tot_base:+,.0f}명) · "
            f"누적 인건비 Δ {s.get('cum_delta_eok'):+,.0f}억 · "
            f"상위비중 {s.get('top_share'):.1f}% / 직군별 {fam}"
        )
    return "\n".join(lines)


def _format_snapshots(snaps: list[dict]) -> str:
    """저장된 스냅샷 요약을 프롬프트용 텍스트로. 없으면 안내 문구."""
    if not snaps:
        return "- 저장된 스냅샷: 없음 (사용자가 '스냅샷 저장'을 누르면 시나리오가 쌓입니다)\n"
    lines = ["- 저장된 스냅샷(시나리오 비교용):"]
    for i, s in enumerate(snaps, 1):
        delta = "baseline" if abs(s.get("cum_delta_eok", 0)) < 0.5 \
            else f"{s.get('cum_delta_eok', 0):+,.0f}억"
        lines.append(
            f"  {i}. '{s.get('label')}' — {s.get('desc') or '레버 무조정'} · {s.get('years')}년 / "
            f"최종총원 {s.get('final_total'):,}명 · 누적인건비Δ {delta} · "
            f"상위비중 {s.get('top_share'):.1f}%"
        )
    return "\n".join(lines) + "\n"


def build_system_prompt(ctx: dict) -> str:
    """현재 시뮬 수치를 시스템 프롬프트에 주입(매 턴 최신 값)."""
    tot_base = ctx.get("tot_base", 0)
    return (
        "당신은 POSCO 의 사내 AI 어시스턴트 'P-GPT'입니다. HR 인력운영 시뮬레이션을 "
        "함께 보는 인력계획 애널리스트 역할로, "
        "사용자와 한국어로 대화하며, 아래 '현재 시뮬 수치'를 근거로 인사이트를 제시하고 "
        "승진율·퇴직률·인건비 인상률·채용 같은 레버를 어떻게 조정하면 좋을지 구체적으로 제안하세요.\n"
        "원칙:\n"
        "- 제공된 수치 안에서 이야기하고, 없는 숫자를 지어내지 마세요. 필요하면 '레버를 이렇게 바꿔보라'고 안내하세요.\n"
        "- 시뮬1/시뮬2 두 시나리오가 있으면 서로의 장단점(구조 개선 vs 인건비 부담)을 비교해 주세요.\n"
        "- 사용자가 저장한 스냅샷들이 아래에 있으면, 그 값들을 근거로 시나리오 간 장단점을 비교해 주세요.\n"
        "- 간결하게. 한 번에 핵심 1~2개 + 다음 액션 제안. 필요하면 되묻는 질문 1개.\n"
        "- 이 데이터는 더미(가정값) 기반 목업임을 감안하세요.\n\n"
        "현재 시뮬 수치:\n"
        f"- 추계 연수: {ctx.get('years')}년\n"
        f"- AS-IS(무조정): 최종총원 {tot_base:,.0f}명 · 누적 인건비 {ctx.get('cum_base_eok', 0):,.0f}억 · "
        f"상위비중 {ctx.get('top_base', 0):.1f}%\n"
        + _format_scenarios(ctx.get("scenarios", []), tot_base) + "\n"
        f"- 초기 인력구조는 '모래시계형': 실무 기반(사원·대리)은 두껍지만 중간관리 계층"
        f"(과장·차장 허리)이 공동화돼 있고 상위 고참은 남아 있음. 핵심 문제는 '중간관리자 공백'이며, "
        f"승진율 등 레버로 허리를 채워 '피라미드형'으로 전환하는 것이 목표.\n"
        + _format_snapshots(ctx.get("snapshots", []))
    )


def stream_reply(messages: list[dict], ctx: dict):
    """Claude 응답을 스트리밍으로 yield. (호출부에서 st.write_stream 로 소비)"""
    import anthropic

    client = anthropic.Anthropic()
    with client.messages.stream(
        model=MODEL,
        max_tokens=1500,
        system=build_system_prompt(ctx),
        messages=messages,
    ) as stream:
        for text in stream.text_stream:
            yield text


def rule_reply(messages: list[dict], ctx: dict) -> str:
    """키가 없거나 호출 실패 시 rule 템플릿 폴백(API 0)."""
    tot_base = ctx.get("tot_base", 0)
    parts = []
    for s in ctx.get("scenarios", []):
        d = s.get("tot", 0) - tot_base
        cum = s.get("cum_delta_eok", 0)
        direction = "증가" if cum >= 0 else "감소"
        parts.append(
            f"**{s.get('name')}** (승진 {s.get('promo_desc')} · 퇴직 {s.get('attr_desc')} · "
            f"인상 {s.get('raise_desc')}): {ctx.get('years')}년 후 총원 "
            f"**{s.get('tot'):,.0f}명**(AS-IS 대비 {d:+,.0f}명), "
            f"누적 인건비 **{cum:+,.0f}억 {direction}**, 상위비중 {s.get('top_share'):.1f}%"
        )
    return (
        "(P-GPT rule 모드) 현재 시나리오 요약입니다.\n\n" + "\n\n".join(parts) + "\n\n"
        "더 풍부한 대화형 인사이트를 원하면 **ANTHROPIC_API_KEY** 를 설정하세요 "
        "(로컬 `.env` 또는 Streamlit Cloud Secrets). 그러면 이 값들을 근거로 제가 제안·질문하며 "
        "대화할 수 있습니다."
    )

"""
insight_bot.py — 대화형 인사이트 (Claude API, 메모리 유지)
==================================================
현재 시뮬 수치를 컨텍스트로 넣어 Claude와 대화하며 인사이트를 얻는다.
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


def build_system_prompt(ctx: dict) -> str:
    """현재 시뮬 수치를 시스템 프롬프트에 주입(매 턴 최신 값)."""
    fam = " · ".join(f"{f} {v:,}명" for f, v in ctx.get("family_end", {}).items())
    return (
        "당신은 POSCO HR 인력운영 시뮬레이션을 함께 보는 인력계획 애널리스트입니다. "
        "사용자와 한국어로 대화하며, 아래 '현재 시뮬 수치'를 근거로 인사이트를 제시하고 "
        "승진율·퇴직률·인건비 인상률·채용 같은 레버를 어떻게 조정하면 좋을지 구체적으로 제안하세요.\n"
        "원칙:\n"
        "- 제공된 수치 안에서 이야기하고, 없는 숫자를 지어내지 마세요. 필요하면 '슬라이더를 이렇게 바꿔보라'고 안내하세요.\n"
        "- 간결하게. 한 번에 핵심 1~2개 + 다음 액션 제안. 필요하면 되묻는 질문 1개.\n"
        "- 이 데이터는 더미(가정값) 기반 목업임을 감안하세요.\n\n"
        "현재 시뮬 수치:\n"
        f"- 추계 연수: {ctx.get('years')}년\n"
        f"- 조정 레버: 승진율 {ctx.get('promo_pct'):+d}% · 퇴직률 {ctx.get('attr_pct'):+d}% · "
        f"인건비 인상률 {ctx.get('raise_rate'):.2f}%\n"
        f"- 최종연도 총원: baseline {ctx.get('tot_base'):,.0f}명 → 시뮬 {ctx.get('tot_sim'):,.0f}명 "
        f"(Δ {ctx.get('tot_sim', 0) - ctx.get('tot_base', 0):+,.0f}명)\n"
        f"- {ctx.get('years')}년 누적 인건비 Δ(vs baseline): {ctx.get('cum_delta_eok'):+,.0f}억\n"
        f"- 상위단계 비중: baseline {ctx.get('top_base'):.1f}% → 시뮬 {ctx.get('top_sim'):.1f}% "
        f"(Δ {ctx.get('top_sim', 0) - ctx.get('top_base', 0):+.1f}%p)\n"
        f"- 최종연도 직군별 인원: {fam}\n"
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
    tot_d = ctx.get("tot_sim", 0) - ctx.get("tot_base", 0)
    top_d = ctx.get("top_sim", 0) - ctx.get("top_base", 0)
    cum = ctx.get("cum_delta_eok", 0)
    direction = "증가" if cum >= 0 else "감소"
    return (
        f"(rule 모드) 현재 조정(승진율 {ctx.get('promo_pct'):+d}% · 퇴직률 {ctx.get('attr_pct'):+d}% · "
        f"인상률 {ctx.get('raise_rate'):.2f}%) 기준으로 **{ctx.get('years')}년 후 총원 "
        f"{ctx.get('tot_sim'):,.0f}명**(baseline 대비 {tot_d:+,.0f}명), "
        f"누적 인건비는 baseline 대비 **{cum:+,.0f}억 {direction}**, "
        f"상위단계 비중 {top_d:+.1f}%p 변동입니다.\n\n"
        "더 풍부한 대화형 인사이트를 원하면 **ANTHROPIC_API_KEY** 를 설정하세요 "
        "(로컬 `.env` 또는 Streamlit Cloud Secrets). 그러면 이 값들을 근거로 제가 제안·질문하며 "
        "대화할 수 있습니다."
    )

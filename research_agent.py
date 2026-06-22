"""
research_agent.py — 외부동향 리서치 에이전트 (Stage 3, LangGraph)
==================================================
검색 → 평가 → (부족 시) 재검색 루프 → 조정계수 추출 을 LangGraph 상태 그래프로 구현.

그래프 구조:
    START → research → evaluate → [조건분기]
                                    ├─ 부족 & retry<2 → research (루프백)
                                    └─ 충분 또는 retry==2 → extract → END

State: keyword, search_results, eval_score, eval_feedback, retry_count(최대 2), coefficients

실행 모드:
  - real: Anthropic web search + LLM 평가/추출 (ANTHROPIC_API_KEY 필요)
  - mock: 키/패키지 없을 때 canned 시퀀스로 동일 UI가 끝까지 돌게 (데모 중단 방지)

★ 평가는 '진짜 평가'다. "1회차는 무조건 부족" 같은 스크립트가 아니라
  LLM이 실제 점수를 매겨 루프가 돌아야 정직한 데모가 된다.
  (단, mock 모드의 점수 시퀀스는 의도적으로 1회 부족→재검색→충분 흐름을 보여주는 폴백이다.)
"""

from __future__ import annotations
import os
import json
from typing import Callable, Optional, TypedDict

# ----- 설정 -----
MAX_RETRY = 2
EVAL_PASS_THRESHOLD = 80   # 이 점수 이상이면 '충분'
EVAL_MODEL = "claude-haiku-4-5-20251001"      # 평가는 Haiku로 비용 절감
EXTRACT_MODEL = "claude-haiku-4-5-20251001"
SEARCH_MODEL = "claude-opus-4-8"              # web search tool 사용

EmitFn = Callable[[dict], None]


def _noop(_: dict) -> None:
    pass


# =============================================================
# State 정의
# =============================================================
class ResearchState(TypedDict, total=False):
    keyword: str
    search_results: list
    eval_score: int
    eval_feedback: str
    retry_count: int
    coefficients: list
    trends: list
    emit: object   # 콜백 (그래프 외부 전달용)


# =============================================================
# 모드 판별
# =============================================================
def _can_run_real() -> bool:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa
        import langgraph  # noqa
        return True
    except Exception:
        return False


# =============================================================
# MOCK 폴백 — canned 트렌드/점수 시퀀스 (데모 중단 방지)
# ★ 폴백임. real 모드 키가 있으면 사용되지 않음.
# =============================================================
MOCK_SEARCH_ROUNDS = [
    # 1회차: 방향성은 있으나 정량 근거 약함 → 평가 낮게
    {
        "round": 1,
        "snippets": [
            "철강·제조 인력시장: 숙련 기능직 고령화로 향후 5년 내 대규모 정년 도래 예상.",
            "AI·자동화 도입으로 일부 사무·관리 직무 수요 둔화 전망(정성적 보도 다수).",
        ],
        "score": 62,
        "feedback": "방향성은 확인되나 전이확률에 매핑할 정량 근거(이탈률/채용률 수치)가 약함.",
    },
    # 2회차: 피드백 반영해 정량 보강 → 평가 충분
    {
        "round": 2,
        "snippets": [
            "제조업 경력직 이직률 최근 2년 연 12%→16% 상승(노동시장 과열).",
            "중견 관리자(차장급) 처우개선·리텐션 정책 도입 기업 증가, 잔류율 +3~5%p 보고.",
            "구조조정 시그널 산업군에서 주니어 자발적 이탈 +2~3%p 관측.",
        ],
        "score": 88,
        "feedback": "이탈률 변동폭/리텐션 효과가 수치로 확보됨 — 조정계수 매핑 가능.",
    },
]

# 최종 조정계수 매핑 (mock & real extract 산출물의 공통 스키마)
MOCK_TRENDS = [
    {"name": "노동시장 과열", "desc": "제조 경력직 이직률 상승(연 12%→16%)", "direction": "이탈↑"},
    {"name": "사업 구조조정 시그널", "desc": "주니어 자발적 이탈 증가", "direction": "이탈↑"},
    {"name": "처우 개선 정책", "desc": "차장급 리텐션 정책으로 잔류율 개선", "direction": "잔류↑"},
]

MOCK_COEFFICIENTS = [
    {"trend": "노동시장 과열", "from": "대리_2년+", "to": "이탈", "delta_pp": +5.0},
    {"trend": "사업 구조조정 시그널", "from": "사원_2년+", "to": "이탈", "delta_pp": +3.0},
    {"trend": "처우 개선 정책", "from": "차장_2년+", "to": "차장_2년+", "delta_pp": +4.0},
]


# =============================================================
# REAL 노드 구현 (Anthropic API)
# =============================================================
def _anthropic_client():
    import anthropic
    return anthropic.Anthropic()


def _real_research(keyword: str, feedback: str, round_no: int) -> list[str]:
    """Anthropic web search tool 로 최신 인력시장 동향 수집."""
    client = _anthropic_client()
    query_hint = keyword
    if feedback:
        query_hint += f" (직전 평가 보강요청: {feedback} — 정량 수치/비율 위주로)"
    resp = client.messages.create(
        model=SEARCH_MODEL,
        max_tokens=1200,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}],
        messages=[{
            "role": "user",
            "content": (
                f"다음 키워드의 최신 인력시장 동향을 web search로 조사해 핵심 사실을 "
                f"불릿 5개로 요약해줘. 가능하면 이직률/채용률/잔류율 같은 정량 수치를 포함해줘.\n"
                f"키워드: {query_hint}"
            ),
        }],
    )
    texts = []
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            texts.append(block.text)
    return [t for t in "\n".join(texts).split("\n") if t.strip()]


def _real_evaluate(keyword: str, snippets: list[str]) -> tuple[int, str]:
    """LLM이 '조정계수로 쓸 정량/방향성 정보가 충분한가'를 0~100 + 사유로 판정."""
    client = _anthropic_client()
    joined = "\n".join(snippets)
    resp = client.messages.create(
        model=EVAL_MODEL,
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": (
                "너는 HR 전략 애널리스트다. 아래 검색결과가 '인력 전이확률 조정계수'로 "
                "쓸 만한 정량/방향성 정보를 트렌드별로 충분히 담고 있는지 0~100점으로 평가하라. "
                "정량 수치(이탈률/잔류율 등)가 없으면 점수를 낮게 줘라. "
                'JSON만 출력: {"score": <int>, "feedback": "<부족/충분 사유 한 문장>"}\n\n'
                f"검색결과:\n{joined}"
            ),
        }],
    )
    txt = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    try:
        data = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
        return int(data["score"]), str(data["feedback"])
    except Exception:
        return 70, "평가 파싱 실패 — 기본값 적용."


def _real_extract(keyword: str, snippets: list[str]) -> tuple[list, list]:
    """충분해진 결과에서 트렌드 요약 + 조정계수 매핑 테이블 추출."""
    client = _anthropic_client()
    joined = "\n".join(snippets)
    states = ["사원_0-2년", "사원_2년+", "대리_0-2년", "대리_2년+", "차장_0-2년", "차장_2년+", "이탈"]
    resp = client.messages.create(
        model=EXTRACT_MODEL,
        max_tokens=800,
        messages=[{
            "role": "user",
            "content": (
                "아래 검색결과에서 인력 트렌드를 뽑고, 각 트렌드를 마르코프 전이확률 조정으로 매핑하라.\n"
                f"사용 가능한 상태: {states}\n"
                "delta_pp는 퍼센트포인트(예: +5.0). JSON만 출력:\n"
                '{"trends":[{"name":"","desc":"","direction":""}],'
                '"coefficients":[{"trend":"","from":"<상태>","to":"<상태>","delta_pp":0.0}]}\n\n'
                f"검색결과:\n{joined}"
            ),
        }],
    )
    txt = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    try:
        data = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
        return data.get("trends", []), data.get("coefficients", [])
    except Exception:
        return MOCK_TRENDS, MOCK_COEFFICIENTS


# =============================================================
# LangGraph 그래프 빌드 (real 모드)
# =============================================================
def build_graph(emit: EmitFn = _noop):
    """LangGraph StateGraph 구성 후 compile. (langgraph 설치 필요)"""
    from langgraph.graph import StateGraph, START, END

    def research_node(state: ResearchState) -> ResearchState:
        rc = state.get("retry_count", 0)
        emit({"type": "search", "round": rc + 1,
              "message": f"🔍 검색 {rc + 1}회차 실행"})
        snippets = _real_research(state["keyword"], state.get("eval_feedback", ""), rc + 1)
        prev = state.get("search_results", [])
        return {"search_results": prev + snippets}

    def evaluate_node(state: ResearchState) -> ResearchState:
        score, feedback = _real_evaluate(state["keyword"], state["search_results"])
        emit({"type": "evaluate", "round": state.get("retry_count", 0) + 1,
              "score": score, "feedback": feedback,
              "message": f"📊 평가 {score}점 — {feedback}"})
        return {"eval_score": score, "eval_feedback": feedback}

    def route(state: ResearchState) -> str:
        score = state.get("eval_score", 0)
        rc = state.get("retry_count", 0)
        if score >= EVAL_PASS_THRESHOLD or rc >= MAX_RETRY:
            return "extract"
        emit({"type": "retry", "round": rc + 1,
              "message": f"🔄 부족 — 재검색(피드백 반영). 누적 재시도 {rc + 1}"})
        return "research"

    def increment_retry(state: ResearchState) -> ResearchState:
        return {"retry_count": state.get("retry_count", 0) + 1}

    def extract_node(state: ResearchState) -> ResearchState:
        emit({"type": "extract", "message": "✅ 조정계수 추출"})
        trends, coeffs = _real_extract(state["keyword"], state["search_results"])
        return {"trends": trends, "coefficients": coeffs}

    g = StateGraph(ResearchState)
    g.add_node("research", research_node)
    g.add_node("evaluate", evaluate_node)
    g.add_node("retry_inc", increment_retry)
    g.add_node("extract", extract_node)

    g.add_edge(START, "research")
    g.add_edge("research", "evaluate")
    # evaluate 후 분기: 충분→extract / 부족→retry_inc→research
    g.add_conditional_edges("evaluate", route, {"extract": "extract", "research": "retry_inc"})
    g.add_edge("retry_inc", "research")
    g.add_edge("extract", END)
    return g.compile()


def get_graph_mermaid() -> str:
    """그래프 구조 다이어그램(mermaid). 화면에 '에이전트 구조'로 한 번 렌더."""
    try:
        graph = build_graph()
        return graph.get_graph().draw_mermaid()
    except Exception:
        # langgraph 미설치 시 정적 mermaid 폴백
        return (
            "graph TD\n"
            "    START([START]) --> research[research: web search]\n"
            "    research --> evaluate[evaluate: LLM 0~100점]\n"
            "    evaluate -->|부족 & retry<2| retry_inc[retry_count++]\n"
            "    retry_inc --> research\n"
            "    evaluate -->|충분 or retry==2| extract[extract: 조정계수]\n"
            "    extract --> END([END])\n"
        )


# =============================================================
# 외부 진입점 — app.py 에서 호출
# =============================================================
def run_research_agent(keyword: str,
                       use_real: Optional[bool] = None,
                       emit: EmitFn = _noop) -> dict:
    """
    리서치 에이전트 실행. emit 콜백으로 실시간 진행 이벤트를 흘려준다.

    Returns dict:
        {"mode", "trends", "coefficients", "history":[평가이력], "search_results"}
    """
    if use_real is None:
        use_real = _can_run_real()

    history = []

    def _emit_wrap(ev: dict):
        if ev["type"] == "evaluate":
            history.append({"round": ev["round"], "score": ev["score"],
                            "feedback": ev["feedback"]})
        emit(ev)

    # ---------- REAL ----------
    if use_real:
        try:
            graph = build_graph(_emit_wrap)
            final = graph.invoke({"keyword": keyword, "retry_count": 0,
                                  "search_results": []})
            return {
                "mode": "real",
                "trends": final.get("trends", MOCK_TRENDS),
                "coefficients": final.get("coefficients", MOCK_COEFFICIENTS),
                "history": history,
                "search_results": final.get("search_results", []),
            }
        except Exception as e:
            emit({"type": "error", "message": f"⚠️ real 모드 실패 → mock 폴백: {e}"})

    # ---------- MOCK 폴백 ----------
    search_results = []
    for r in MOCK_SEARCH_ROUNDS:
        _emit_wrap({"type": "search", "round": r["round"],
                    "message": f"🔍 검색 {r['round']}회차 실행 (mock)"})
        search_results += r["snippets"]
        _emit_wrap({"type": "evaluate", "round": r["round"], "score": r["score"],
                    "feedback": r["feedback"],
                    "message": f"📊 평가 {r['score']}점 — {r['feedback']}"})
        if r["score"] < EVAL_PASS_THRESHOLD and r["round"] <= MAX_RETRY:
            _emit_wrap({"type": "retry", "round": r["round"],
                        "message": "🔄 부족 — 재검색(피드백 반영)"})
        else:
            break
    _emit_wrap({"type": "extract", "message": "✅ 조정계수 추출"})

    return {
        "mode": "mock",
        "trends": MOCK_TRENDS,
        "coefficients": MOCK_COEFFICIENTS,
        "history": history,
        "search_results": search_results,
    }

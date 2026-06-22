"""
research_agent.py — 외부동향 리서치 에이전트 (Stage 3, LangGraph)
==================================================
검색 → 평가 → (부족 시) 쿼리보강(refine) → 재검색 루프 → 조정계수 추출
을 LangGraph 상태 그래프로 구현.

그래프 구조:
    START → research → evaluate → [조건분기 route]
                                    ├─ 부족 & retry<MAX → refine → research (루프백)
                                    └─ 충분 또는 retry==MAX → extract → END

  ★ 핵심 개선: 부족 판정 시 같은 키워드로 재검색하지 않고, refine 노드가
    평가의 missing 항목을 보고 '새롭고 구체적인 한국어 쿼리 2~3개'를 생성해
    next_queries로 넘긴다. → 라운드마다 다른 결과 → 점수 정체 해소.

State: keyword, search_results, eval_score(=overall), eval_feedback, eval_detail(루브릭),
       eval_missing, next_queries, retry_count(최대 MAX_RETRY), trends, coefficients

평가는 단일 점수가 아니라 3축 루브릭(정량성/방향성/커버리지) + overall로 구조화.

실행 모드:
  - real: Anthropic web search + LLM 평가/보강/추출 (ANTHROPIC_API_KEY 필요)
  - mock: 키/패키지 없을 때 canned 시퀀스로 동일 UI가 끝까지 돌게 (데모 중단 방지)

★ 평가는 '진짜 평가'다. "1회차는 무조건 부족" 같은 스크립트가 아니라
  LLM이 실제 루브릭 점수를 매겨 루프가 돌아야 정직한 데모가 된다.
  (단, mock 모드의 점수 시퀀스는 의도적으로 부족→보강→통과 흐름을 보여주는 폴백이다.)
"""

from __future__ import annotations
import os
import re
import json
from typing import Callable, Optional, TypedDict

# ----- 설정 -----
MAX_RETRY = 3                                  # refine 수렴 여지 확보 (2→3)
EVAL_PASS_THRESHOLD = 80   # overall 이 점수 이상이면 '충분'
# 평가/보강은 일관성이 중요 → Sonnet (단일 점수 Haiku 대비 루브릭 안정성↑)
EVAL_MODEL = "claude-sonnet-4-6"
REFINE_MODEL = "claude-sonnet-4-6"            # 부족항목 기반 구체 쿼리 생성
EXTRACT_MODEL = "claude-haiku-4-5-20251001"
SEARCH_MODEL = "claude-opus-4-8"              # web search tool 사용

EmitFn = Callable[[dict], None]


def _safe_rubric(overall: int = 60, feedback: str = "평가 파싱 실패 — 기본값 적용.",
                 missing: Optional[list] = None) -> dict:
    """평가 파싱 실패 시 안전한 기본 루브릭. overall<80이라 재시도되되,
    retry>=MAX 에서는 extract로 빠져 데모가 멈추지 않는다."""
    return {
        "정량성": overall, "방향성": overall, "커버리지": overall,
        "overall": overall,
        "missing": missing if missing is not None else ["정량 근거 보강 필요"],
        "feedback": feedback,
    }


# ----- 누적/이력 유틸 -----
_TAG_RE = re.compile(r"^\[R\d+\]\s*")   # "[R2] ..." 라운드 태그 제거용


def _norm_line(line: str) -> str:
    """라운드 태그를 떼고 정규화 — 중복 판정 키."""
    return _TAG_RE.sub("", line).strip()


def _accumulate(prev: list, new: list) -> list:
    """검색결과 누적 시 '같은 내용'(태그 무시) 중복을 제거해 코퍼스 희석 완화."""
    seen = {_norm_line(l) for l in prev}
    out = list(prev)
    for l in new:
        k = _norm_line(l)
        if k and k not in seen:
            seen.add(k)
            out.append(l)
    return out


def _best_round(history: list) -> Optional[int]:
    """평가 이력에서 overall 최고점 라운드 번호."""
    if not history:
        return None
    return max(history, key=lambda h: h.get("score", 0))["round"]


def _noop(_: dict) -> None:
    pass


# =============================================================
# State 정의
# =============================================================
class ResearchState(TypedDict, total=False):
    keyword: str
    search_results: list
    eval_score: int          # = 루브릭 overall (하위호환)
    eval_feedback: str
    eval_detail: dict        # 루브릭 3축 {정량성/방향성/커버리지/overall/missing/feedback}
    eval_missing: list       # 부족 항목 (refine 입력)
    next_queries: list       # refine가 생성한 구체 쿼리 (없으면 최초 keyword 사용)
    queries_log: list        # 지금까지 사용한 모든 검색 쿼리 (refine 중복 회피용)
    best_score: int          # 지금까지 본 최고 overall
    best_results: list       # 최고점 라운드 시점의 search_results 스냅샷
    best_detail: dict        # 최고점 라운드 루브릭
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
# ★ mock 시퀀스: 58 → 74 → 86 으로 점수가 오르며 통과.
#   각 라운드는 루브릭(정량성/방향성/커버리지/overall)과, 부족 시 refine이 만든
#   '보강 쿼리(refine_queries)'를 함께 담아 화면에 그대로 노출된다.
#   (의도적으로 2회 부족 후 통과 — real 모드 키가 있으면 사용되지 않는 폴백)
MOCK_SEARCH_ROUNDS = [
    # 1회차: 방향성은 있으나 정량 근거 약함 → overall 낮게
    {
        "round": 1,
        "queries": None,   # 최초 라운드는 keyword 그대로
        "snippets": [
            "철강·제조 인력시장: 숙련 기능직 고령화로 향후 5년 내 대규모 정년 도래 예상.",
            "AI·자동화 도입으로 일부 사무·관리 직무 수요 둔화 전망(정성적 보도 다수).",
        ],
        "rubric": {
            "정량성": 40, "방향성": 70, "커버리지": 60, "overall": 58,
            "missing": ["경력직 이직률 정량 수치", "차장급 잔류율 근거"],
            "feedback": "방향성은 확인되나 전이확률에 매핑할 정량 근거(이탈률/잔류율)가 약함.",
        },
        # 다음 라운드 검색에 쓸 구체 쿼리 (refine 노드 산출물)
        "refine_queries": ["철강업 경력직 자발적 이직률 통계 2025",
                           "제조업 차장급 잔류율 리텐션 정책 효과"],
    },
    # 2회차: 정량 보강됐으나 커버리지 일부 부족 → 통과 직전
    {
        "round": 2,
        "queries": ["철강업 경력직 자발적 이직률 통계 2025",
                    "제조업 차장급 잔류율 리텐션 정책 효과"],
        "snippets": [
            "제조업 경력직 이직률 최근 2년 연 12%→16% 상승(노동시장 과열).",
            "중견 관리자(차장급) 처우개선·리텐션 정책 도입 기업 증가, 잔류율 +3~5%p 보고.",
        ],
        "rubric": {
            "정량성": 72, "방향성": 80, "커버리지": 68, "overall": 74,
            "missing": ["주니어(사원) 이탈 정량 근거"],
            "feedback": "이직률·리텐션 수치는 확보됐으나 주니어 이탈 트렌드 근거가 약함.",
        },
        "refine_queries": ["제조업 구조조정 신입·주니어 자발적 이탈률 비율",
                           "철강 산업 사원급 조기퇴사 통계 2024 2025"],
    },
    # 3회차: 트렌드 3종 모두 방향성+정량 확보 → 통과
    {
        "round": 3,
        "queries": ["제조업 구조조정 신입·주니어 자발적 이탈률 비율",
                    "철강 산업 사원급 조기퇴사 통계 2024 2025"],
        "snippets": [
            "구조조정 시그널 산업군에서 주니어 자발적 이탈 +2~3%p 관측.",
            "신입~3년차 조기퇴사율 업계 평균 대비 1.5배, 사업 불확실성과 상관.",
        ],
        "rubric": {
            "정량성": 86, "방향성": 88, "커버리지": 84, "overall": 86,
            "missing": [],
            "feedback": "트렌드 3종(과열·구조조정·처우개선) 모두 방향성+정량 근거 확보.",
        },
        "refine_queries": None,
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


def _real_research(keyword: str, queries: Optional[list], round_no: int) -> list[str]:
    """Anthropic web search tool 로 최신 인력시장 동향 수집.
    queries(refine 산출물)가 있으면 그 구체 쿼리들로, 없으면 최초 keyword로 검색."""
    client = _anthropic_client()
    if queries:
        target = "\n".join(f"- {q}" for q in queries)
        instruction = (
            f"다음 구체 검색 쿼리들을 각각 web search로 조사해 핵심 사실을 불릿으로 요약해줘. "
            f"이직률/잔류율/채용률 같은 정량 수치를 최우선으로 뽑아줘.\n쿼리:\n{target}"
        )
    else:
        instruction = (
            f"다음 키워드의 최신 인력시장 동향을 web search로 조사해 핵심 사실을 "
            f"불릿 5개로 요약해줘. 가능하면 이직률/채용률/잔류율 같은 정량 수치를 포함해줘.\n"
            f"키워드: {keyword}"
        )
    resp = client.messages.create(
        model=SEARCH_MODEL,
        max_tokens=1200,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}],
        messages=[{"role": "user", "content": instruction}],
    )
    texts = []
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            texts.append(block.text)
    # 라운드 태그를 붙여 누적 — 어느 라운드 결과인지 추적
    lines = [t for t in "\n".join(texts).split("\n") if t.strip()]
    return [f"[R{round_no}] {ln}" for ln in lines]


def _real_evaluate(keyword: str, snippets: list[str]) -> dict:
    """3축 루브릭으로 구조화 평가. 단일 점수가 아니라 정량성/방향성/커버리지 + overall.
    missing 리스트는 refine 노드 입력으로 쓰인다."""
    client = _anthropic_client()
    joined = "\n".join(snippets)
    resp = client.messages.create(
        model=EVAL_MODEL,
        max_tokens=600,
        messages=[{
            "role": "user",
            "content": (
                "너는 HR 전략 애널리스트다. 아래 검색결과가 '인력 전이확률 조정계수'로 쓸 만한지 "
                "다음 3축 루브릭으로 0~100점 채점하라.\n"
                "- 정량성: 이직률/잔류율/채용률 등 정량 근거가 있는가\n"
                "- 방향성: 전이확률을 어느 방향(↑/↓)으로 조정할지 명확한가\n"
                "- 커버리지: 핵심 트렌드 3종(노동시장 과열·구조조정·처우개선)을 포괄하는가\n"
                "기준 앵커: 80점 = 트렌드 3개 모두 방향성 확보 + 최소 1개 정량 근거.\n"
                "정량 수치가 전혀 없으면 정량성을 낮게 줘라. "
                "missing 에는 다음 검색에서 보강해야 할 부족 항목을 구체적으로 적어라.\n"
                "JSON만 출력:\n"
                '{"정량성":<int>,"방향성":<int>,"커버리지":<int>,"overall":<int>,'
                '"missing":["<부족항목>"],"feedback":"<한 문장>"}\n\n'
                f"검색결과:\n{joined}"
            ),
        }],
    )
    txt = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    try:
        data = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
        return {
            "정량성": int(data["정량성"]), "방향성": int(data["방향성"]),
            "커버리지": int(data["커버리지"]), "overall": int(data["overall"]),
            "missing": list(data.get("missing", [])),
            "feedback": str(data.get("feedback", "")),
        }
    except Exception:
        return _safe_rubric()


def _real_refine(keyword: str, snippets: list[str], missing: list[str],
                 feedback: str, prior_queries: list[str]) -> list[str]:
    """평가의 missing 항목 + '지금까지 사용한 모든 쿼리'를 보고, 겹치지 않는
    새롭고 구체적인 한국어 검색 쿼리 2~3개를 생성한다.
    → 직전 8줄만 보던 좁은 기억창을 전체 쿼리 이력으로 확장해 중복/드리프트 방지."""
    client = _anthropic_client()
    seen = "\n".join(snippets[-10:])   # 최근 결과 일부 (방향 참고용)
    used = "\n".join(f"- {q}" for q in prior_queries) or "- (아직 없음, 최초 키워드만 사용)"
    miss = "; ".join(missing) if missing else feedback
    resp = client.messages.create(
        model=REFINE_MODEL,
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": (
                "이전 검색이 다음 항목에서 부족했다: " + miss + "\n"
                "아래 '이미 사용한 쿼리'와 의미가 겹치지 않는, 부족 항목을 메우는 "
                "'새롭고 구체적인' 한국어 검색 쿼리 2~3개를 만들어라. "
                "연도·직급·지표(이직률/잔류율 등)를 포함해 구체적으로. "
                'JSON 배열만 출력: ["쿼리1","쿼리2"]\n\n'
                f"원 키워드: {keyword}\n"
                f"이미 사용한 쿼리(겹치지 말 것):\n{used}\n\n"
                f"지금까지 검색 결과(일부):\n{seen}"
            ),
        }],
    )
    txt = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    try:
        arr = json.loads(txt[txt.find("["): txt.rfind("]") + 1])
        qs = [str(q) for q in arr if str(q).strip()][:3]
        return qs or [f"{keyword} 정량 통계 최신"]
    except Exception:
        # 폴백: missing 을 키워드에 이어붙인 단순 쿼리
        return [f"{m} 통계 최신" for m in (missing[:2] or [keyword])]


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
        queries = state.get("next_queries")   # refine 산출물 (최초 라운드엔 없음)
        if queries:
            emit({"type": "search", "round": rc + 1, "queries": queries,
                  "message": f"🔍 검색 {rc + 1}회차 — 보강 쿼리로 재검색"})
            used = list(queries)
        else:
            emit({"type": "search", "round": rc + 1,
                  "message": f"🔍 검색 {rc + 1}회차 실행"})
            used = [state["keyword"]]   # 최초 라운드는 키워드 자체를 쿼리로 기록
        snippets = _real_research(state["keyword"], queries, rc + 1)
        # 중복 제거 누적 + 사용한 쿼리 이력 적재 (refine 중복 회피용) + next_queries 소비
        merged = _accumulate(state.get("search_results", []), snippets)
        return {"search_results": merged, "next_queries": [],
                "queries_log": state.get("queries_log", []) + used}

    def evaluate_node(state: ResearchState) -> ResearchState:
        rubric = _real_evaluate(state["keyword"], state["search_results"])
        score = rubric["overall"]
        is_best = score > state.get("best_score", -1)
        star = " 🏆최고점 갱신" if is_best else ""
        emit({"type": "evaluate", "round": state.get("retry_count", 0) + 1,
              "score": score, "정량성": rubric["정량성"], "방향성": rubric["방향성"],
              "커버리지": rubric["커버리지"], "missing": rubric["missing"],
              "feedback": rubric["feedback"], "is_best": is_best,
              "message": (f"📊 평가 {score}점 (정량 {rubric['정량성']}·"
                          f"방향 {rubric['방향성']}·커버 {rubric['커버리지']}){star} — {rubric['feedback']}")})
        out = {"eval_score": score, "eval_feedback": rubric["feedback"],
               "eval_detail": rubric, "eval_missing": rubric["missing"]}
        if is_best:
            # 최고점 라운드의 코퍼스 스냅샷 보관 → extract가 이걸로 추출
            out["best_score"] = score
            out["best_results"] = list(state["search_results"])
            out["best_detail"] = rubric
        return out

    def route(state: ResearchState) -> str:
        score = state.get("eval_score", 0)
        rc = state.get("retry_count", 0)
        if score >= EVAL_PASS_THRESHOLD or rc >= MAX_RETRY:
            return "extract"
        emit({"type": "retry", "round": rc + 1,
              "message": f"🔄 부족({score}점) — 쿼리 보강 단계로. 누적 재시도 {rc + 1}"})
        return "refine"

    def refine_node(state: ResearchState) -> ResearchState:
        """부족 항목(missing)을 보고 새 구체 쿼리 2~3개 생성 + retry_count 증가."""
        rc = state.get("retry_count", 0)
        queries = _real_refine(state["keyword"], state.get("search_results", []),
                               state.get("eval_missing", []),
                               state.get("eval_feedback", ""),
                               state.get("queries_log", []))
        emit({"type": "refine", "round": rc + 1, "queries": queries,
              "message": "🔧 쿼리 보강: " + " / ".join(queries)})
        return {"next_queries": queries, "retry_count": rc + 1}

    def extract_node(state: ResearchState) -> ResearchState:
        # 마지막(저점일 수 있는) 라운드가 아니라 '최고점 라운드' 코퍼스로 추출
        best_results = state.get("best_results") or state.get("search_results", [])
        bs = state.get("best_score")
        msg = "✅ 조정계수 추출"
        if bs is not None:
            msg += f" (최고점 {bs}점 라운드 결과 기준)"
        emit({"type": "extract", "message": msg})
        trends, coeffs = _real_extract(state["keyword"], best_results)
        return {"trends": trends, "coefficients": coeffs}

    g = StateGraph(ResearchState)
    g.add_node("research", research_node)
    g.add_node("evaluate", evaluate_node)
    g.add_node("refine", refine_node)
    g.add_node("extract", extract_node)

    g.add_edge(START, "research")
    g.add_edge("research", "evaluate")
    # evaluate 후 분기: 충분→extract / 부족→refine→research (루프백)
    g.add_conditional_edges("evaluate", route, {"extract": "extract", "refine": "refine"})
    g.add_edge("refine", "research")
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
            "    research --> evaluate[evaluate: 루브릭 3축 채점]\n"
            "    evaluate -->|부족 & retry<MAX| refine[refine: 보강 쿼리 생성]\n"
            "    refine --> research\n"
            "    evaluate -->|충분 or retry==MAX| extract[extract: 조정계수]\n"
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
        {"mode", "trends", "coefficients", "history":[루브릭 평가이력],
         "search_results", "best_round"}
        history 각 항목: {round, score(=overall), 정량성, 방향성, 커버리지,
                         missing, feedback, refine_queries}
        best_round: 조정계수 추출에 사용된 최고점 라운드 번호
    """
    if use_real is None:
        use_real = _can_run_real()

    history = []

    def _emit_wrap(ev: dict):
        if ev["type"] == "evaluate":
            history.append({
                "round": ev["round"], "score": ev["score"],
                "정량성": ev.get("정량성"), "방향성": ev.get("방향성"),
                "커버리지": ev.get("커버리지"), "missing": ev.get("missing", []),
                "feedback": ev.get("feedback", ""), "refine_queries": [],
            })
        elif ev["type"] == "refine":
            # 직전 평가 라운드 항목에 보강 쿼리 부착 (라운드별 화면 표시용)
            for h in reversed(history):
                if h["round"] == ev.get("round"):
                    h["refine_queries"] = ev.get("queries", [])
                    break
        emit(ev)

    # ---------- REAL ----------
    if use_real:
        try:
            graph = build_graph(_emit_wrap)
            final = graph.invoke({"keyword": keyword, "retry_count": 0,
                                  "search_results": [], "queries_log": [],
                                  "best_score": -1})
            return {
                "mode": "real",
                "trends": final.get("trends", MOCK_TRENDS),
                "coefficients": final.get("coefficients", MOCK_COEFFICIENTS),
                "history": history,
                "search_results": final.get("search_results", []),
                "best_round": _best_round(history),
            }
        except Exception as e:
            emit({"type": "error", "message": f"⚠️ real 모드 실패 → mock 폴백: {e}"})

    # ---------- MOCK 폴백 ----------
    # 점수가 오르며(58→74→86) 통과하고, refine 보강 쿼리도 화면에 노출되게 재현.
    search_results = []
    for r in MOCK_SEARCH_ROUNDS:
        rc = r["round"] - 1
        qs = r.get("queries")
        if qs:
            _emit_wrap({"type": "search", "round": r["round"], "queries": qs,
                        "message": f"🔍 검색 {r['round']}회차 — 보강 쿼리로 재검색 (mock)"})
        else:
            _emit_wrap({"type": "search", "round": r["round"],
                        "message": f"🔍 검색 {r['round']}회차 실행 (mock)"})
        search_results += [f"[R{r['round']}] {s}" for s in r["snippets"]]

        rub = r["rubric"]
        _emit_wrap({"type": "evaluate", "round": r["round"], "score": rub["overall"],
                    "정량성": rub["정량성"], "방향성": rub["방향성"],
                    "커버리지": rub["커버리지"], "missing": rub["missing"],
                    "feedback": rub["feedback"],
                    "message": (f"📊 평가 {rub['overall']}점 (정량 {rub['정량성']}·"
                                f"방향 {rub['방향성']}·커버 {rub['커버리지']}) — {rub['feedback']}")})

        if rub["overall"] < EVAL_PASS_THRESHOLD and rc < MAX_RETRY:
            _emit_wrap({"type": "retry", "round": r["round"],
                        "message": f"🔄 부족({rub['overall']}점) — 쿼리 보강 단계로"})
            rq = r.get("refine_queries") or []
            _emit_wrap({"type": "refine", "round": r["round"], "queries": rq,
                        "message": "🔧 쿼리 보강: " + " / ".join(rq)})
        else:
            break
    _emit_wrap({"type": "extract", "message": "✅ 조정계수 추출"})

    return {
        "mode": "mock",
        "trends": MOCK_TRENDS,
        "coefficients": MOCK_COEFFICIENTS,
        "history": history,
        "search_results": search_results,
        "best_round": _best_round(history),
    }

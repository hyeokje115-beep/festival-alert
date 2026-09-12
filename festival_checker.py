#!/usr/bin/env python3
"""
공모사업 알림 봇 v6.6
v6.5 대비 변경점:
- 메뉴/마이페이지 링크 오탐 차단: 비가시 요소 제외, 네비게이션 URL 패턴 차단,
  메뉴 문구 정확일치 차단, 폴백 단계에서 게시글형 URL만 허용
- 같은 스크래핑 내 중복 제거(상세 URL 기준)
- 2년 이상 과거 연도 공고 제외, 용역/투표/수상작 블랙리스트 추가
- 알림을 게시글 1건 = 1알림(per_post)으로 전환 → 알림 클릭 시 해당 게시글로 정확히 이동
  (사이트별 신규 6건 초과 시 자동으로 묶음 알림 전환)
"""

import json, os, re, time, traceback
from datetime import datetime, timedelta
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright
import requests
import google.generativeai as genai

# ─── 환경변수 ───────────────────────────────────────────────
NTFY_TOPIC  = os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = "https://ntfy.sh"
KNOWN_FILE  = "known_posts.json"
GEMINI_KEY  = os.environ.get("GEMINI_API_KEY", "")

NOTIFY_MODE = "per_post"          # "per_post" | "grouped"
PER_POST_MAX_PER_SITE = 6         # 사이트당 신규가 이 수를 넘으면 묶음 알림으로 전환
NTFY_BODY_LIMIT = 3800            # 묶음 알림 1건당 본문 바이트 한도
OLD_YEAR_TOLERANCE = 1            # 제목 연도가 (올해-1)보다 오래되면 제외

# ═══════════════════════════════════════════════════════════
# 필터링 규칙
# ═══════════════════════════════════════════════════════════

CATEGORY_WORDS = [
    "공연", "전시", "체험", "박람회", "축제",
    "기획전", "전시회", "아트페어",
    "문화예술", "예술단체",
]

PURPOSE_WORDS = [
    "공모전", "공모사업", "공모", "지원사업", "지원금",
    "모집공고", r"모집\s*공고",
    "신청접수", "작품모집", "모집", "신청",
    r"참여작가",
    r"예술가?\s*모집", r"단체\s*모집", r"예술인\s*모집", r"작가\s*모집",
    r"참여\s*단체\s*모집", r"팀\s*모집",
]

BLACKLIST_PATTERNS = [
    r"결과\s*발표", r"결과\s*공개", r"결과\s*안내", r"선정\s*결과",
    r"최종\s*선정", r"심사\s*결과", r"수상\s*결과", r"합격자?\s*발표",
    r"\d+차\s*결과", r"행정심사\s*결과",
    r"모집\s*결과", r"접수\s*결과",
    r"첨부파일", r"정산\s*안내서?", r"정산\s*보고", r"사업비\s*정산", r"보조금\s*정산",
    r"사업\s*추진단", r"^\s*TF\s*$", r"^\s*위원회\s*$", r"위원\s*위촉",
    r"심사위원.{0,10}모집", r"평가위원.{0,10}모집",
    r"자문위원.{0,10}모집", r"운영위원.{0,10}모집",
    r"상설공연", r"정기공연", r"\d{1,2}월\s*공연\s*안내", r"\d{1,2}일\s*공연\s*안내",
    r"공연\s*소개", r"공연\s*일정",
    r"채용\s*공고", r"채용\s*공지", r"직원\s*모집", r"근무자\s*모집",
    r"인턴\s*모집", r"기간제\s*모집", r"계약직\s*모집",
    r"입찰\s*공고", r"용역\s*발주",
    r"서포터즈\s*모집", r"기자단\s*모집", r"모니터단?\s*모집",
    r"자원봉사자?\s*모집", r"수강생\s*모집",
    r"^\s*(더보기|바로가기|하위메뉴)\s*$", r"홍보\s*추진",
    # v6.6 — 실측 노이즈 대응
    r"운영대행\s*용역", r"용역\s*(제안서|입찰|공고)", r"용역사\s*모집",
    r"온라인\s*투표", r"대국민\s*투표", r"투표\s*안내",
    r"공모\s*수상작", r"수상작품?\s*(전시|안내|발표)",
    r"운영\s*안내$", r"취소\s*안내$", r"이용\s*안내$",
    r"교부/?변경\s*신청", r"사업비\s*카드", r"지원신청\s*(현황|취소)",
]

# 메뉴/마이페이지 문구 — 정확일치일 때만 차단(오탐 위험 최소화)
MENU_EXACT_BLACKLIST = {
    "대관신청", "대관신청(대관시스템)", "예매/신청 조회", "나의 지원신청 현황",
    "지원신청취소내역", "지원신청 취소내역", "교부/변경 신청", "교부/변경신청",
    "사업비카드발급신청", "지역의 공모, 공고", "지역의 공모,공고",
    "민주·인권·평화 공모수상작품", "민주·인권·평화 공모전",
    "지원신청", "정산/사업실적", "커뮤니티", "고객센터", "공지사항", "예약상담",
    "시스템 개선", "자료실", "게시판", "이용안내", "자주묻는질문", "회원정보",
    "로그인", "회원가입", "마이페이지", "전체메뉴", "이전", "다음", "목록",
    "검색", "맨위로", "사이트맵", "홈", "HOME", "더보기",
}

# 네비게이션/마이페이지 성격의 URL 패턴 — 게시글이 아닌 링크
NAV_HREF_NEGATIVE = re.compile(
    r'(/menu\.do|/contents\.do|quickSchedule|frs/index|memberJoin|goLogin|'
    r'memberInfo|memberBye|memberReservation|memberFind|/search\.do|'
    r'allSchedule\.do|/renewal/frm|/support/main|/etc/system|'
    r'board/reserve/list|board/system/list|board/form/list|'
    r'/mypage|/login|/join|/logout|/sitemap|/faq|/rent)',
    re.IGNORECASE
)

# 게시글 상세로 보이는 URL 힌트
DETAIL_LINK_HINT = re.compile(
    r'(view|idx=|aseq=|seq=|wr_id=|bo_idx=|no=\d|article|read|detail|nttSn=|postView|'
    r'bseq=\d|ix=\d|board_id=|bbsId=.*ntt|/notice/\d|/post/\d)',
    re.IGNORECASE
)

FALLBACK_KEYWORDS = [
    "공모사업", "지원사업 공모", "전시 공모", "공연 공모",
    "지원금 공모", "작품 공모", "전시공간 지원", "모집 공고",
]

CURRENT_YEAR = datetime.now().year


def normalize(text: str) -> str:
    text = re.sub(r'[\[\]<>【】《》「」『』\(\)\{\}·•★◆▶►≪≫～~]', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def looks_like_navigation_href(href: str) -> bool:
    return bool(href) and bool(NAV_HREF_NEGATIVE.search(href))


def is_too_old(title: str) -> bool:
    years = [int(y) for y in re.findall(r'(?<!\d)(20\d{2})(?!\d)', title)]
    if not years:
        return False
    return max(years) < CURRENT_YEAR - OLD_YEAR_TOLERANCE


def rule_filter_debug(title: str, href: str = "", category_required: bool = False) -> tuple:
    t = title.strip()
    if t in MENU_EXACT_BLACKLIST:
        return False, "메뉴/마이페이지 문구 정확일치"
    if looks_like_navigation_href(href):
        return False, "네비게이션 URL 패턴"

    raw_len = len(t)
    if raw_len < 8:
        return False, f"너무 짧음({raw_len}자)"

    if is_too_old(t):
        return False, "과거 연도 공고"

    n = normalize(title)

    for p in BLACKLIST_PATTERNS:
        if re.search(p, n):
            return False, f"블랙리스트[{p}]"

    cat_match = next((p for p in CATEGORY_WORDS if re.search(p, n)), None)
    if category_required and not cat_match:
        return False, "카테고리어 없음(종합사이트라 필수)"

    pur_match = next((p for p in PURPOSE_WORDS if re.search(p, n)), None)
    if not pur_match:
        return False, "목적어 없음(공모·지원사업·모집 없음)"

    return True, f"통과 [cat={cat_match or '사이트자체로대체'} / pur={pur_match}]"


# ═══════════════════════════════════════════════════════════
# Gemini
# ═══════════════════════════════════════════════════════════

GEMINI_MODEL_CHAIN = ["gemini-flash-latest", "gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
_GEMINI_WORKING_MODEL = {"name": None}
GEMINI_FAILURE_LOG = []

HARD_EXCLUDE_REASONS = {
    "RESULT_ANNOUNCEMENT", "SETTLEMENT_ADMIN", "COMMITTEE_OR_JUDGE",
    "GENERAL_PUBLIC_RECRUIT", "EVENT_PROMO_ONLY", "JOB_POSTING", "SITE_MENU",
}

GEMINI_RESPONSE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "is_relevant": {"type": "boolean"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "exclude_reason": {
                "type": "string",
                "enum": ["NONE", "RESULT_ANNOUNCEMENT", "SETTLEMENT_ADMIN", "COMMITTEE_OR_JUDGE",
                         "GENERAL_PUBLIC_RECRUIT", "EVENT_PROMO_ONLY", "JOB_POSTING", "SITE_MENU", "OTHER"],
            },
        },
        "required": ["id", "is_relevant", "confidence", "exclude_reason"],
    },
}

GEMINI_SYSTEM_INSTRUCTION = (
    "당신은 문화예술 분야 보조금·공모사업 심사 전문가입니다. "
    "전시·공연·체험(축제 포함) 분야에서 활동하는 '개인사업자 또는 단체'가 "
    "직접 신청서를 제출해 예산·공간·활동기회를 지원받을 수 있는 공고만 정확히 골라내는 것이 "
    "당신의 유일한 임무입니다. 판단이 애매할 경우, 기회를 하나 놓치는 손해가 "
    "노이즈를 하나 더 보내는 손해보다 훨씬 크므로 반드시 포함 쪽으로 판단하십시오. "
    "단, 홈페이지 메뉴명·마이페이지 항목처럼 게시글이 아닌 것은 확실히 제외하십시오."
)

GEMINI_PROMPT_TEMPLATE = """아래는 문화재단/기관 홈페이지에서 수집한 게시글 제목 목록입니다.
각 항목이 "문화예술 분야 사업자·단체가 신청 가능한 공모/지원사업"인지 판단하세요.

[반드시 제외 - exclude_reason]
- RESULT_ANNOUNCEMENT: 이미 끝난 심사·선정 결과, 합격자 발표, 수상작 안내, 투표 안내 (신규 신청과 무관)
- SETTLEMENT_ADMIN: 정산·보고·교부/변경·사업비카드 등 내부 행정 절차
- COMMITTEE_OR_JUDGE: 심사위원/평가위원/자문위원 위촉·모집
- GENERAL_PUBLIC_RECRUIT: 일반 시민/청소년/수강생 등 '개인 참가자' 모집(사업자 신청 아님)
- EVENT_PROMO_ONLY: 이미 확정된 행사·공연 일정 홍보(신청 절차 없음)
- JOB_POSTING: 직원 채용, 근무자/인턴 모집, 운영대행 용역사 모집 등 고용·용역 공고
- SITE_MENU: 게시글이 아닌 홈페이지 메뉴명/기능명 (예: 대관신청, 예매/신청 조회, 나의 지원신청 현황)
- OTHER: 위에 해당 안 하지만 무관함

[반드시 포함]
전시/공연/체험/축제 관련 사업자·단체·작가 대상 공모전, 지원사업, 보조금,
참가(입점)단체 모집, 대관·부스 지원, 창작지원금, 레지던시 입주작가 모집 등 "새로 신청 가능한" 공고

각 항목마다 id, is_relevant(boolean), confidence(high/medium/low), exclude_reason을
JSON 배열로만 반환하세요.

목록:
{items_text}
"""


def should_include(gemini_item: dict) -> bool:
    if gemini_item.get("is_relevant") is True:
        return True
    conf = gemini_item.get("confidence")
    reason = gemini_item.get("exclude_reason")
    if conf == "high" and reason in HARD_EXCLUDE_REASONS:
        return False
    return True


def _safe_json_loads(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
    return json.loads(text)


def _make_items_text(batch):
    return "\n".join(f"{i+1}. {it['title']}" for i, it in enumerate(batch))


def _try_gemini_call(batch, with_schema: bool, model_name: str):
    model = genai.GenerativeModel(model_name, system_instruction=GEMINI_SYSTEM_INSTRUCTION)
    if with_schema:
        config = genai.GenerationConfig(response_mime_type="application/json",
                                        response_schema=GEMINI_RESPONSE_SCHEMA, temperature=0.1)
    else:
        config = genai.GenerationConfig(response_mime_type="application/json", temperature=0.1)
    prompt = GEMINI_PROMPT_TEMPLATE.format(items_text=_make_items_text(batch))
    resp = model.generate_content(prompt, generation_config=config)
    return _safe_json_loads(resp.text)


def gemini_filter_batch(batch: list) -> list:
    model_names = [_GEMINI_WORKING_MODEL["name"]] if _GEMINI_WORKING_MODEL["name"] else GEMINI_MODEL_CHAIN
    last_err = None
    for model_name in model_names:
        for with_schema in (True, False):
            for attempt in range(2):
                try:
                    parsed = _try_gemini_call(batch, with_schema, model_name)
                    _GEMINI_WORKING_MODEL["name"] = model_name
                    id_to_item = {i + 1: it for i, it in enumerate(batch)}
                    kept = []
                    for r in parsed:
                        it = id_to_item.get(r.get("id"))
                        if it is not None and should_include(r):
                            it["gemini_conf"] = r.get("confidence", "")
                            kept.append(it)
                    print(f"     Gemini({model_name}) 응답 {len(parsed)}건 → 통과 {len(kept)}건")
                    return kept
                except Exception as e:
                    last_err = e
                    time.sleep(2 + attempt * 2)
    print(f"     [Gemini 전체 실패] {last_err} → 규칙필터 결과 그대로 통과(fail-open)")
    GEMINI_FAILURE_LOG.append(str(last_err))
    return [c for c in batch if any(w in c["title"] for w in FALLBACK_KEYWORDS)] or batch


def gemini_filter(items: list) -> list:
    if not items:
        return []
    if not GEMINI_KEY:
        print("     [Gemini 없음] fallback 키워드 필터 적용")
        return [c for c in items if any(w in c["title"] for w in FALLBACK_KEYWORDS)]
    results = []
    BATCH = 25
    for i in range(0, len(items), BATCH):
        batch = items[i:i + BATCH]
        results.extend(gemini_filter_batch(batch))
        if i + BATCH < len(items):
            time.sleep(2)
    return results


if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)
    print("✓ Gemini 연결됨 (모델 폴백체인:", GEMINI_MODEL_CHAIN, ")")
else:
    print("! Gemini 키 없음 → fallback 모드")


# ═══════════════════════════════════════════════════════════
# 사이트 목록 (type: "board" | "calendar" | "wevity")
# ═══════════════════════════════════════════════════════════

SITES = [
    {"name": "광주문화재단",         "url": "https://www.gctf.or.kr/web/board/1/postList",         "type": "board"},
    {"name": "MLDC",                "url": "https://mldc.kr/notice",                               "type": "board"},
    {"name": "미마프",               "url": "http://www.mimaf.net/xe/index.php?mid=notice",          "type": "board"},
    {"name": "한국문화예술교육진흥원", "url": "https://www.kh.or.kr/brd/board/644/L/SITES/100/menu/371?brdCodeField=SITES&brdCodeValue=100", "type": "board"},
    {"name": "리콜렉션",             "url": "https://recollection.kr/bbs/board.php?bo_table=notice&page=1", "type": "board"},
    {"name": "전주문화재단",          "url": "https://www.jge.go.kr/jgemain/na/ntt/selectNttList.do?mi=2116&bbsId=1123", "type": "board"},
    {"name": "나주문화재단",          "url": "https://www.njcf.or.kr/www/community/notices",         "type": "board"},
    {"name": "담양문화재단",          "url": "https://www.damyangcf.or.kr/user/board/lists/board_cd/4010", "type": "board"},
    {"name": "전남문화재단_타기관",    "url": "https://www.jncf.or.kr/jact/open/otherevents.do",     "type": "board"},
    {"name": "전남문화재단_협업",      "url": "https://www.jncf.or.kr/jact/open/collusion.do",       "type": "board"},
    {"name": "전남문화재단_공지",      "url": "https://www.jncf.or.kr/jact/open/notice.do",          "type": "board"},
    {"name": "광주문화재단_공지",      "url": "https://www.gjcf.or.kr/cf/news/notice.do",            "type": "board"},
    {"name": "고양문화재단",          "url": "https://www.gtcc.or.kr/bbs/board.php?bo_table=info&page=1", "type": "board"},
    {"name": "문화예술",              "url": "http://xn--9p4b13eb4bd6i.com/notice",                  "type": "board"},
    {"name": "국립아시아문화전당_공모", "url": "https://www.ncas.or.kr/board/contest/list?menuNo=&currentPageNo=1&searchCondition=", "type": "board"},
    {"name": "위비티",                "url": "https://www.wevity.com/?c=find&s=1&gub=1&cidx=&sp=&sw=&gbn=list&mode=new", "type": "wevity"},
    {"name": "국립아시아문화전당_공지", "url": "https://www.acc.go.kr/main/board/board.do?PID=0701&boardID=NOTICE", "type": "board"},
    {"name": "광주비엔날레",          "url": "https://www.gwangjubiennale.org/gb/notice.do",         "type": "board"},
    {"name": "전북문화관광재단",       "url": "https://www.jbct.or.kr/notice.php",                   "type": "board"},
    {"name": "전북문화관광재단_공모",  "url": "https://www.jbct.or.kr/c_notice.php",                 "type": "board"},
    {"name": "순천문화재단_공모캘린더","url": "https://www.cfsc.or.kr/contents/news/news0106.asp",   "type": "calendar"},
    {"name": "순천문화재단_타기관공모","url": "https://www.cfsc.or.kr/contents/open/open0501.asp",   "type": "board"},
    {"name": "순천문화재단_공모게시판","url": "https://www.cfsc.or.kr/contents/open/open0102.asp?bseq=1&cat=39&yy=", "type": "board"},
    {"name": "목포문화재단_문화도시",  "url": "https://mpcf.or.kr/bbs/board.php?bo_table=notice&sca=%EB%AC%B8%ED%99%94%EB%8F%84%EC%8B%9C", "type": "board"},
    {"name": "목포문화재단_전체",      "url": "https://mpcf.or.kr/bbs/board.php?bo_table=notice",    "type": "board"},
]

SITE_BY_NAME = {s["name"]: s for s in SITES}


# ═══════════════════════════════════════════════════════════
# 공통 유틸
# ═══════════════════════════════════════════════════════════

def clean(text: str) -> str:
    return re.sub(r'\s+', ' ', text.replace('\n', ' ')).strip()


def extract_deadline(text: str) -> str:
    m = re.search(r'[~～]\s*(\d{2,4}[.\-/]\d{1,2}[.\-/]\d{1,2})', text)
    if m: return m.group(1)
    m = re.search(r'마감\s*:?\s*(\d{2,4}[.\-]\d{1,2}[.\-]\d{1,2})', text)
    if m: return m.group(1)
    dates = re.findall(r'\d{4}[.\-]\d{1,2}[.\-]\d{1,2}', text)
    return dates[-1] if len(dates) >= 2 else (dates[0] if dates else "")


PLACEHOLDER_HREF = re.compile(r'^\s*(#.*|javascript:.*)?\s*$', re.IGNORECASE)


def resolve_href(page, raw_href: str) -> str:
    if not raw_href or PLACEHOLDER_HREF.match(raw_href.strip()):
        return ""
    try:
        return urljoin(page.url, raw_href.strip())
    except Exception:
        return ""


def is_visible(el) -> bool:
    try:
        return el.is_visible()
    except Exception:
        return True   # 판정 실패 시엔 배제하지 않음(recall 우선)


def dedup_same_batch(items: list) -> list:
    """같은 스크래핑 결과 내 중복 제거 — 상세 URL 기준(없으면 정규화 제목)."""
    seen, out = set(), []
    for it in items:
        href = (it.get("detail_url") or "").strip()
        key = href if href else f"__NOHREF__::{normalize(it['title'])}"
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


# ═══════════════════════════════════════════════════════════
# 스크래퍼: 위비티 전용
# ═══════════════════════════════════════════════════════════

WEVITY_MAX_PAGES = 3


def scrape_wevity(page, url: str, name: str) -> list:
    raw = []
    seen_ix = set()
    for gp in range(1, WEVITY_MAX_PAGES + 1):
        target = re.sub(r'([?&])gp=\d+', r'\1gp={}'.format(gp), url)
        if "gp=" not in target:
            sep = "&" if "?" in target else "?"
            target = f"{target}{sep}gp={gp}"
        try:
            page.goto(target, wait_until="networkidle", timeout=40000)
            page.wait_for_timeout(1200)
            anchors = page.query_selector_all("a[href*='gbn=view']")
            if not anchors:
                break
            found_new = False
            for a in anchors:
                try:
                    href = a.get_attribute("href") or ""
                    m = re.search(r'[?&]ix=(\d+)', href)
                    if not m or m.group(1) in seen_ix:
                        continue
                    ix = m.group(1)
                    seen_ix.add(ix)
                    found_new = True
                    title = clean(a.inner_text())
                    title = re.sub(r'(?:\s*(?:SPECIAL|IDEA|NEW|HOT))+$', '', title, flags=re.IGNORECASE).strip()
                    if len(title) < 8:
                        continue
                    deadline = ""
                    try:
                        li_text = a.evaluate(
                            "node => { const li = node.closest('li'); return li ? li.innerText : ''; }"
                        )
                        dm = re.search(r'D-(\d+)', li_text or "")
                        if dm:
                            deadline = (datetime.now() + timedelta(days=int(dm.group(1)))).strftime('%Y-%m-%d')
                        else:
                            deadline = extract_deadline(li_text or "")
                    except Exception:
                        pass
                    raw.append({
                        "name": name, "title": title[:120], "site_url": url,
                        "detail_url": urljoin(page.url, href), "deadline": deadline,
                    })
                except Exception:
                    continue
            if not found_new:
                break
        except Exception as e:
            print(f"     [{name}] {gp}페이지 오류: {e}")
            break
    return raw


# ═══════════════════════════════════════════════════════════
# 스크래퍼: 캘린더 전용 (cfsc — "공모" 라벨만 통과)
# ═══════════════════════════════════════════════════════════

CALENDAR_ALLOWED_LABELS = {"공모"}


def scrape_calendar(page, url: str, name: str) -> list:
    raw = []
    seen = set()
    today = datetime.now()
    ny, nm = today.year, today.month + 1
    if nm > 12:
        ny, nm = ny + 1, 1

    for (yy, mm) in [(today.year, today.month), (ny, nm)]:
        cal_url = f"{url}?y={yy}&m={mm:02d}"
        print(f"     캘린더 URL: {cal_url}")
        try:
            page.goto(cal_url, wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(4000)
            try:
                page.wait_for_selector("ul li a strong, ul li a b, ul li", timeout=8000)
            except Exception:
                print("     [경고] 요소 감지 실패, 계속 진행")

            for li in page.query_selector_all("ul li"):
                try:
                    li_text = clean(li.inner_text())
                    if not li_text:
                        continue
                    m = re.match(r'^\s*(공모|전시|공연|교육|축제\s*[·・]?\s*행사)\b', li_text)
                    if not m:
                        continue
                    label = m.group(1).replace(" ", "").replace("·", "").replace("・", "")
                    if label not in CALENDAR_ALLOWED_LABELS:
                        continue

                    title = li_text[len(m.group(0)):].strip()
                    period = re.search(r'(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})', title)
                    deadline = period.group(2) if period else ""
                    title = re.sub(r'_?\d{4}-\d{2}-\d{2}\s*~\s*\d{4}-\d{2}-\d{2}_?', '', title).strip()
                    title = title.strip(" _-·")
                    if len(title) < 8:
                        continue
                    key = normalize(title)
                    if key in seen:
                        continue
                    seen.add(key)

                    href = ""
                    a = li.query_selector("a")
                    if a:
                        href = resolve_href(page, a.get_attribute("href") or "")

                    raw.append({
                        "name": name, "title": title[:120], "site_url": cal_url,
                        "detail_url": href, "deadline": deadline,
                    })
                except Exception:
                    continue
        except Exception as e:
            print(f"     [{name}] {yy}-{mm:02d} 오류: {e}")
            traceback.print_exc()

    print(f"     캘린더 수집: 총 {len(raw)}개 ('공모' 라벨만)")
    return raw


# ═══════════════════════════════════════════════════════════
# 스크래퍼: 일반 게시판 (3단계 폴백 + 비가시/메뉴 링크 차단)
# ═══════════════════════════════════════════════════════════

def _accept_anchor(a, href: str, title: str, seen: set, require_detail_hint: bool) -> bool:
    if len(title) < 8 or title in MENU_EXACT_BLACKLIST or title in seen:
        return False
    if looks_like_navigation_href(href):
        return False
    if not is_visible(a):
        return False
    if require_detail_hint and not DETAIL_LINK_HINT.search(href or ""):
        return False
    return True


def scrape_board(page, url: str, name: str) -> list:
    raw = []
    try:
        page.goto(url, wait_until="networkidle", timeout=40000)
        page.wait_for_timeout(2000)

        # 1단계: 표 형태 게시판 (번호/공지 열이 있는 행만)
        rows = page.query_selector_all("table tbody tr")
        for row in rows:
            try:
                cells = row.query_selector_all("td")
                if len(cells) < 2: continue
                first = cells[0].inner_text().strip()
                if not re.match(r'^\d+$', first) and first not in ("공지", "고정", "NOTICE", "Notice"):
                    continue
                a = next((c.query_selector("a") for c in cells[1:] if c.query_selector("a")), None)
                if not a: continue
                title = clean(a.inner_text())[:100]
                href = a.get_attribute("href") or ""
                if len(title) < 8 or title in MENU_EXACT_BLACKLIST: continue
                if looks_like_navigation_href(href): continue
                raw.append({
                    "name": name, "title": title, "site_url": url,
                    "detail_url": resolve_href(page, href),
                    "deadline": extract_deadline(row.inner_text()),
                })
            except Exception:
                pass

        # 2단계: 리스트(ul/ol) 형태 게시판 — 본문 영역 우선, 게시글형 URL만
        if not raw:
            seen = set()
            scopes = ["main ul li, main ol li, #content ul li, #contents ul li, .content ul li, .board ul li, .bbs ul li",
                      "ul li, ol li"]
            for scope in scopes:
                for li in page.query_selector_all(scope):
                    try:
                        a = li.query_selector("a")
                        if not a: continue
                        title = clean(a.inner_text())[:100]
                        href = a.get_attribute("href") or ""
                        if not _accept_anchor(a, href, title, seen, require_detail_hint=True):
                            continue
                        seen.add(title)
                        raw.append({
                            "name": name, "title": title, "site_url": url,
                            "detail_url": resolve_href(page, href),
                            "deadline": extract_deadline(li.inner_text()),
                        })
                    except Exception:
                        pass
                if raw:
                    break

        # 3단계: 페이지 내 모든 링크 중 게시글형 URL만
        if not raw:
            seen = set()
            for a in page.query_selector_all("a"):
                try:
                    href = a.get_attribute("href") or ""
                    title = clean(a.inner_text())[:100]
                    if not _accept_anchor(a, href, title, seen, require_detail_hint=True):
                        continue
                    seen.add(title)
                    raw.append({
                        "name": name, "title": title, "site_url": url,
                        "detail_url": resolve_href(page, href),
                        "deadline": "",
                    })
                except Exception:
                    pass

    except Exception as e:
        print(f"     [{name}] 오류: {e}")
    return raw


# ═══════════════════════════════════════════════════════════
# 전체 수집
# ═══════════════════════════════════════════════════════════

def scrape_all() -> tuple:
    """(후보 목록, 사이트별 원시 수집 개수) 반환"""
    candidates = []
    raw_counts = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            viewport={"width": 1366, "height": 900},
        )
        page = context.new_page()
        for site in SITES:
            name, url, stype = site["name"], site["url"], site.get("type", "board")
            print(f"\n▶ [{name}] {url}")
            try:
                if stype == "wevity":
                    items = scrape_wevity(page, url, name)
                elif stype == "calendar":
                    items = scrape_calendar(page, url, name)
                else:
                    items = scrape_board(page, url, name)
            except Exception as e:
                print(f"     [{name}] 스크래핑 예외: {e}")
                items = []

            before = len(items)
            items = dedup_same_batch(items)
            raw_counts[name] = len(items)
            print(f"     원시 수집 {before}건 → 중복제거 후 {len(items)}건")

            category_required = (stype == "wevity")
            passed = 0
            for it in items:
                ok, reason = rule_filter_debug(it["title"], it.get("detail_url", ""), category_required)
                if ok:
                    it["rule_reason"] = reason
                    candidates.append(it)
                    passed += 1
                else:
                    print(f"       ✗ {it['title'][:50]}  ← {reason}")
            print(f"     규칙필터 통과 {passed}건")
        browser.close()
    return candidates, raw_counts


# ═══════════════════════════════════════════════════════════
# 중복방지 DB
# ═══════════════════════════════════════════════════════════

def make_key(item: dict) -> str:
    """신규 키: 사이트명 + 상세URL(없으면 정규화 제목)"""
    href = (item.get("detail_url") or "").strip()
    return f"{item['name']}|{href}" if href else f"{item['name']}|{normalize(item['title'])}"


def make_key_legacy(item: dict) -> str:
    """구버전 키: 사이트명 + 원제목"""
    return f"{item['name']}|{item['title']}"


def load_known() -> set:
    if not os.path.exists(KNOWN_FILE):
        return set()
    try:
        with open(KNOWN_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return set(data.keys())
        return set(data)
    except Exception:
        return set()


def save_known(known: set):
    with open(KNOWN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(known), f, ensure_ascii=False, indent=1)


# ═══════════════════════════════════════════════════════════
# ntfy 발송
# ═══════════════════════════════════════════════════════════

def _hdr(value: str) -> str:
    """ntfy 헤더는 ASCII만 허용 → 한글은 RFC 2047 방식으로 인코딩"""
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        import base64
        return "=?UTF-8?B?" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "?="


def _short(text: str, n: int) -> str:
    text = text.strip()
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def _post_ntfy(body: str, headers: dict) -> bool:
    if not NTFY_TOPIC:
        print("  ! NTFY_TOPIC 없음 → 발송 생략\n" + body)
        return False
    try:
        r = requests.post(f"{NTFY_SERVER}/{NTFY_TOPIC}", data=body.encode("utf-8"),
                          headers=headers, timeout=20)
        ok = r.status_code < 300
        if not ok:
            print(f"  ! ntfy 응답 {r.status_code}: {r.text[:200]}")
        return ok
    except Exception as e:
        print(f"  ! ntfy 발송 실패: {e}")
        return False


def _actions_header(pairs: list) -> str:
    """pairs: [(label, url), ...] 최대 3개. 라벨은 짧게 유지(버튼 정돈)."""
    acts = []
    for label, u in pairs[:3]:
        if not u:
            continue
        label = label.replace(";", " ").replace(",", " ")
        acts.append(f"view, {label}, {u}, clear=true")
    return "; ".join(acts)


def send_post_notification(item: dict) -> bool:
    """게시글 1건 = 알림 1건. 본문 클릭 → 게시글 상세로 이동."""
    site = item["name"]
    title = item["title"]
    detail = item.get("detail_url") or ""
    site_url = item.get("site_url") or ""
    click = detail or site_url
    deadline = f"\n마감: {item['deadline']}" if item.get("deadline") else ""
    conf = item.get("gemini_conf", "")
    conf_txt = {"high": "신뢰도 높음", "medium": "신뢰도 보통", "low": "확인 필요"}.get(conf, "")
    body = f"{title}{deadline}\n출처: {site}" + (f" · {conf_txt}" if conf_txt else "")
    if not detail:
        body += "\n(상세 링크 없음 → 목록으로 이동)"

    pairs = []
    if detail:
        pairs.append(("게시글 열기", detail))
    pairs.append(("목록 보기", site_url))

    headers = {
        "Title": _hdr(f"[{site}] 새 공모"),
        "Priority": "default",
        "Tags": "loudspeaker",
        "Click": click,
        "Actions": _hdr(_actions_header(pairs)),
    }
    return _post_ntfy(body, headers)


def _chunk_items_by_bytes(lines: list, limit: int) -> list:
    chunks, cur, size = [], [], 0
    for ln in lines:
        b = len(ln.encode("utf-8")) + 1
        if cur and size + b > limit:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(ln)
        size += b
    if cur:
        chunks.append(cur)
    return chunks


def send_grouped_notification(site: str, items: list):
    """사이트별 묶음 알림(분할 발송). 본문 클릭 → 사이트 목록, 버튼은 앞 3건 상세."""
    site_url = items[0].get("site_url", "")
    lines = []
    for it in items:
        dl = f" (~{it['deadline']})" if it.get("deadline") else ""
        lines.append(f"• {it['title']}{dl}")
    chunks = _chunk_items_by_bytes(lines, NTFY_BODY_LIMIT)
    total = len(chunks)
    for idx, chunk in enumerate(chunks, 1):
        suffix = f" ({idx}/{total})" if total > 1 else ""
        start = sum(len(c) for c in chunks[:idx - 1])
        part_items = items[start:start + len(chunk)]
        pairs = [(_short(it["title"], 14), it.get("detail_url") or site_url) for it in part_items[:3]]
        headers = {
            "Title": _hdr(f"[{site}] 새 공모 {len(items)}개{suffix}"),
            "Priority": "default",
            "Tags": "loudspeaker",
            "Click": site_url,
            "Actions": _hdr(_actions_header(pairs)),
        }
        _post_ntfy("\n".join(chunk), headers)
        time.sleep(0.5)


def notify_site(site: str, items: list):
    if NOTIFY_MODE == "per_post" and len(items) <= PER_POST_MAX_PER_SITE:
        for it in items:
            send_post_notification(it)
            time.sleep(0.4)
    else:
        send_grouped_notification(site, items)


def send_system_alert(title: str, body: str):
    _post_ntfy(body, {"Title": _hdr(title), "Priority": "low", "Tags": "warning"})


# ═══════════════════════════════════════════════════════════
# main
# ═══════════════════════════════════════════════════════════

def main():
    print(f"=== 공모 알림 봇 v6.6 시작 {datetime.now():%Y-%m-%d %H:%M} ===")
    known = load_known()
    first_run = len(known) == 0
    print(f"기존 등록 {len(known)}건 {'(첫 실행: 알림 없이 등록만)' if first_run else ''}")

    candidates, raw_counts = scrape_all()
    print(f"\n규칙필터 통과 후보: {len(candidates)}건")

    # 신규 판별 (신규키 / 구버전키 둘 다 확인)
    new_items = []
    for it in candidates:
        k_new, k_old = make_key(it), make_key_legacy(it)
        if k_new in known or k_old in known:
            continue
        new_items.append(it)
    print(f"신규(미등록) 후보: {len(new_items)}건")

    # Gemini 2차 필터 (신규 후보에만 적용)
    final_items = gemini_filter(new_items) if new_items else []
    print(f"Gemini 통과: {len(final_items)}건")

    # 알림 발송 (첫 실행은 등록만)
    if not first_run and final_items:
        by_site = {}
        for it in final_items:
            by_site.setdefault(it["name"], []).append(it)
        for site, items in by_site.items():
            print(f"\n📣 [{site}] 알림 {len(items)}건 발송")
            notify_site(site, items)

    # DB 갱신: 신규 후보 전체(제외된 것도) 등록 → 같은 글로 다시 판정하지 않음
    for it in new_items:
        known.add(make_key(it))
    save_known(known)
    print(f"\nDB 저장: {len(known)}건")

    # 점검 알림
    zero_sites = [n for n, c in raw_counts.items() if c == 0]
    if zero_sites:
        send_system_alert("[점검] 0건 수집 사이트",
                          "구조 변경 또는 접속 실패 가능:\n" + "\n".join(f"• {n}" for n in zero_sites))
    if GEMINI_FAILURE_LOG:
        send_system_alert("[점검] Gemini 필터 실패",
                          f"{len(GEMINI_FAILURE_LOG)}회 실패 → 규칙필터 결과로 발송됨\n{GEMINI_FAILURE_LOG[-1][:300]}")

    print("=== 종료 ===")


if __name__ == "__main__":
    main()

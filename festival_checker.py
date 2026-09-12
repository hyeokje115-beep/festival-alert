#!/usr/bin/env python3
"""
공모사업 알림 봇 v6.4
v6.2 대비 변경점:
1. 위비티 전용 스크래퍼 신설 (gbn=view 상세글만 선택, 다중페이지, 카테고리어 필수)
2. cfsc 순천 캘린더: "공모" 라벨만 통과 + 상세링크 있는 게시판(open0102.asp) 신규 추가
3. Gemini: JSON 구조화 출력 + confidence/exclude_reason + 모델 폴백체인(4단계) + recall-first 정책
4. 규칙필터: 위비티(종합사이트)만 카테고리어 필수, 나머지 22개 문화재단은 목적어+블랙리스트만
5. 상세페이지 고유 URL 추출 → ntfy click/actions에 직접 링크
6. known_posts 키를 detail_url 우선으로 변경 (신구 키 동시 확인해 마이그레이션 안전)
7. 0건 수집 사이트 / Gemini 전체 실패 감지 시 점검 알림 발송
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
    # 결과/선정
    r"결과\s*발표", r"결과\s*공개", r"결과\s*안내", r"선정\s*결과",
    r"최종\s*선정", r"심사\s*결과", r"수상\s*결과", r"합격자?\s*발표",
    r"\d+차\s*결과", r"행정심사\s*결과",
    r"모집\s*결과", r"접수\s*결과",
    # 첨부파일/정산
    r"첨부파일", r"정산\s*안내서?", r"정산\s*보고", r"사업비\s*정산", r"보조금\s*정산",
    # 행정조직/위촉/위원모집
    r"사업\s*추진단", r"^\s*TF\s*$", r"^\s*위원회\s*$", r"위원\s*위촉",
    r"심사위원.{0,10}모집", r"평가위원.{0,10}모집",
    r"자문위원.{0,10}모집", r"운영위원.{0,10}모집",
    # 상설/정기 공연
    r"상설공연", r"정기공연", r"\d{1,2}월\s*공연\s*안내", r"\d{1,2}일\s*공연\s*안내",
    r"공연\s*소개", r"공연\s*일정",
    # 채용/구인
    r"채용\s*공고", r"채용\s*공지", r"직원\s*모집", r"근무자\s*모집",
    r"인턴\s*모집", r"기간제\s*모집", r"계약직\s*모집",
    # 입찰/용역
    r"입찰\s*공고", r"용역\s*발주",
    # 일반 시민/학생 대상 모집(위비티 등 종합사이트 대비)
    r"서포터즈\s*모집", r"기자단\s*모집", r"모니터단?\s*모집",
    r"자원봉사자?\s*모집", r"수강생\s*모집",
    # UI 잔여물
    r"^\s*(더보기|바로가기|하위메뉴)\s*$", r"홍보\s*추진",
]

FALLBACK_KEYWORDS = [
    "공모사업", "지원사업 공모", "전시 공모", "공연 공모",
    "지원금 공모", "작품 공모", "전시공간 지원", "모집 공고",
]


def normalize(text: str) -> str:
    text = re.sub(r'[\[\]<>【】《》「」『』\(\)\{\}·•★◆▶►≪≫～~]', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def rule_filter_debug(title: str, category_required: bool = False) -> tuple:
    """
    category_required=False (기본, 문화재단 등 전용 사이트): 목적어+블랙리스트만 확인
    category_required=True (위비티: 전분야 종합 공모사이트): 카테고리어까지 필수
    """
    raw_len = len(title.strip())
    if raw_len < 8:
        return False, f"너무 짧음({raw_len}자)"

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
# Gemini — 구조화 출력 + 모델 폴백체인 + recall-first 정책
# ═══════════════════════════════════════════════════════════

GEMINI_MODEL_CHAIN = ["gemini-flash-latest", "gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
_GEMINI_WORKING_MODEL = {"name": None}
GEMINI_FAILURE_LOG = []

HARD_EXCLUDE_REASONS = {
    "RESULT_ANNOUNCEMENT", "SETTLEMENT_ADMIN", "COMMITTEE_OR_JUDGE",
    "GENERAL_PUBLIC_RECRUIT", "EVENT_PROMO_ONLY", "JOB_POSTING",
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
                         "GENERAL_PUBLIC_RECRUIT", "EVENT_PROMO_ONLY", "JOB_POSTING", "OTHER"],
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
    "노이즈를 하나 더 보내는 손해보다 훨씬 크므로 반드시 포함 쪽으로 판단하십시오."
)

GEMINI_PROMPT_TEMPLATE = """아래는 문화재단/기관 홈페이지에서 수집한 게시글 제목 목록입니다.
각 항목이 "문화예술 분야 사업자·단체가 신청 가능한 공모/지원사업"인지 판단하세요.

[반드시 제외 - exclude_reason]
- RESULT_ANNOUNCEMENT: 이미 끝난 심사·선정 결과, 합격자 발표 (신규 신청과 무관)
- SETTLEMENT_ADMIN: 정산·보고·내부 행정 절차
- COMMITTEE_OR_JUDGE: 심사위원/평가위원/자문위원 위촉·모집
- GENERAL_PUBLIC_RECRUIT: 일반 시민/청소년/수강생 등 '개인 참가자' 모집(사업자 신청 아님)
- EVENT_PROMO_ONLY: 이미 확정된 행사·공연 일정 홍보(신청 절차 없음)
- JOB_POSTING: 직원 채용, 근무자/인턴 모집 등 고용 공고
- OTHER: 위에 해당 안 하지만 무관함

[반드시 포함]
전시/공연/체험/축제 관련 사업자·단체·작가 대상 공모전, 지원사업, 보조금,
참가(입점)단체 모집, 대관·부스 지원, 창작지원금 등 "새로 신청 가능한" 공고

각 항목마다 id, is_relevant(boolean), confidence(high/medium/low), exclude_reason을
JSON 배열로만 반환하세요.

목록:
{items_text}
"""


def should_include(gemini_item: dict) -> bool:
    """recall-first 정책: 애매하면 포함, 명확한 근거+고신뢰일 때만 제외"""
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
                            kept.append(it)
                    print(f"     Gemini({model_name}) 응답 {len(parsed)}건 → 통과 {len(kept)}건")
                    return kept
                except Exception as e:
                    last_err = e
                    time.sleep(2 + attempt * 2)
    print(f"     [Gemini 전체 실패] {last_err} → 규칙필터 결과 그대로 통과(fail-open)")
    GEMINI_FAILURE_LOG.append(str(last_err))
    # 마지막 안전망: fallback 키워드 필터라도 적용
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
    # 순천 — 캘린더(개선) + 실제 링크 있는 게시판(신규, 이중 안전망)
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


# ═══════════════════════════════════════════════════════════
# 스크래퍼: 위비티 전용 (gbn=view 상세글만, 다중페이지)
# ═══════════════════════════════════════════════════════════

WEVITY_MAX_PAGES = 3  # 최신순 3페이지(약 45~60건) 커버 → 마감 누락 방지


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
                    title = re.sub(r'_?\d{4}-\d{2}-\d{2}\s*~\s*\d{4}-\d{2}-\d{2}_?', '', title).strip()
                    title = title[:150]
                    if len(title) < 8 or title in seen:
                        continue
                    seen.add(title)

                    dm = re.search(r'(\d{4})-(\d{2})-(\d{2})\s*~\s*(\d{4})-(\d{2})-(\d{2})', li_text)
                    deadline = f"{dm.group(4)}-{dm.group(5)}-{dm.group(6)}" if dm else extract_deadline(li_text)

                    raw.append({
                        "name": name, "title": title,
                        "site_url": cal_url,
                        "detail_url": "",  # cfsc 캘린더는 href="#!" (JS 오버레이)라 실제 링크 없음(실측 확인됨)
                        "deadline": deadline,
                    })
                except Exception:
                    continue
        except Exception as e:
            print(f"     [{name}] {yy}-{mm:02d} 오류: {e}")
            traceback.print_exc()

    print(f"     캘린더 수집: 총 {len(raw)}개 ('공모' 라벨만)")
    return raw


# ═══════════════════════════════════════════════════════════
# 스크래퍼: 일반 게시판 (상세 URL 추출 포함)
# ═══════════════════════════════════════════════════════════

DETAIL_LINK_HINT = re.compile(r'(view|idx=|aseq=|seq=|wr_id=|bo_idx=|no=\d|article|read|detail)', re.IGNORECASE)
NAV_EXACT_BLACKLIST = {"공지사항", "전체메뉴", "로그인", "회원가입", "이전", "다음", "목록",
                       "검색", "맨위로", "사이트맵", "홈", "HOME", "더보기"}


def scrape_board(page, url: str, name: str) -> list:
    raw = []
    try:
        page.goto(url, wait_until="networkidle", timeout=40000)
        page.wait_for_timeout(2000)

        # ─ 전략 1: 번호 있는 테이블 ─
        rows = page.query_selector_all("table tbody tr")
        for row in rows:
            try:
                cells = row.query_selector_all("td")
                if len(cells) < 2: continue
                first = cells[0].inner_text().strip()
                if not re.match(r'^\d+$', first) and first not in ("공지", "고정"):
                    continue
                a = next((c.query_selector("a") for c in cells[1:] if c.query_selector("a")), None)
                if not a: continue
                title = clean(a.inner_text())[:100]
                if len(title) < 8 or title in NAV_EXACT_BLACKLIST: continue
                href = a.get_attribute("href") or ""
                raw.append({
                    "name": name, "title": title, "site_url": url,
                    "detail_url": resolve_href(page, href),
                    "deadline": extract_deadline(row.inner_text()),
                })
            except Exception:
                pass

        # ─ 전략 2: ul/ol li ─
        if not raw:
            seen = set()
            for li in page.query_selector_all("ul li, ol li"):
                try:
                    a = li.query_selector("a")
                    if not a: continue
                    title = clean(a.inner_text())[:100]
                    if len(title) < 8 or title in seen or title in NAV_EXACT_BLACKLIST: continue
                    seen.add(title)
                    href = a.get_attribute("href") or ""
                    raw.append({
                        "name": name, "title": title, "site_url": url,
                        "detail_url": resolve_href(page, href),
                        "deadline": extract_deadline(li.inner_text()),
                    })
                except Exception:
                    pass

        # ─ 전략 3: href 패턴 필터가 걸린 a 태그만 (메뉴 오염 방지) ─
        if not raw:
            seen = set()
            for a in page.query_selector_all("a"):
                try:
                    href = a.get_attribute("href") or ""
                    if not DETAIL_LINK_HINT.search(href):
                        continue
                    title = clean(a.inner_text())[:100]
                    if len(title) < 8 or title in seen or title in NAV_EXACT_BLACKLIST: continue
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


SCRAPER_MAP = {"board": scrape_board, "calendar": scrape_calendar, "wevity": scrape_wevity}


# ═══════════════════════════════════════════════════════════
# 전체 파이프라인
# ═══════════════════════════════════════════════════════════

def scrape_all() -> tuple:
    all_posts = {}
    raw_counts = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ))

        for site in SITES:
            name  = site["name"]
            url   = site["url"]
            stype = site.get("type", "board")
            print(f"\n{'═'*55}")
            print(f"  [{name}]  type={stype}")

            try:
                raw = SCRAPER_MAP.get(stype, scrape_board)(page, url, name)
            except Exception as e:
                print(f"  [{name}] 치명적 오류(건너뜀): {e}")
                traceback.print_exc()
                raw = []

            raw_counts[name] = len(raw)
            print(f"  수집: {len(raw)}개")

            # ── 1차 규칙 필터 (위비티만 카테고리어 필수) ──
            category_required = (stype == "wevity")
            rule_passed = []
            for item in raw:
                ok, reason = rule_filter_debug(item["title"], category_required=category_required)
                if ok:
                    rule_passed.append(item)
                    print(f"  ✅ {item['title'][:55]}  →  {reason}")
                else:
                    print(f"  ❌ {item['title'][:55]}  →  {reason}")

            print(f"  [규칙통과] {len(rule_passed)}개")

            # ── 2차 Gemini 필터 ──
            final = gemini_filter(rule_passed)
            print(f"  [최종알림] {len(final)}개")

            for item in final:
                key = make_key(item)
                all_posts[key] = item

        browser.close()
    return all_posts, raw_counts


# ═══════════════════════════════════════════════════════════
# known_posts 관리 (신구 키 동시 확인 → 안전한 마이그레이션)
# ═══════════════════════════════════════════════════════════

def make_key(item: dict) -> str:
    if item.get("detail_url"):
        return f"{item['name']}|{item['detail_url']}"
    return f"{item['name']}|{item['title'][:80]}"


def make_key_legacy(item: dict) -> str:
    return f"{item['name']}|{item['title'][:50]}"  # v6.2 방식


def is_known(item: dict, known: dict) -> bool:
    return make_key(item) in known or make_key_legacy(item) in known


def load_known() -> dict:
    if os.path.exists(KNOWN_FILE):
        with open(KNOWN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_known(data: dict):
    with open(KNOWN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════
# ntfy 알림 전송 (직접 링크 + 액션 버튼)
# ═══════════════════════════════════════════════════════════

def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


def _truncate_bytes(s: str, max_bytes: int) -> str:
    encoded = s.encode('utf-8')
    if len(encoded) <= max_bytes:
        return s
    return encoded[:max_bytes].decode('utf-8', errors='ignore') + "…"


def send_ntfy(title: str, body: str, click_url: str = "", actions: list = None):
    if not NTFY_TOPIC:
        return
    payload = {
        "topic":    NTFY_TOPIC,
        "title":    title,
        "message":  _truncate_bytes(body, 3800),
        "priority": 4,
        "tags":     ["loudspeaker"],
    }
    if click_url:
        payload["click"] = click_url
    if actions:
        payload["actions"] = actions[:3]
    for attempt in range(2):
        try:
            r = requests.post(NTFY_SERVER, json=payload, timeout=10,
                               headers={"Content-Type": "application/json"})
            icon = "✓" if r.status_code == 200 else f"✗({r.status_code})"
            print(f"  [{icon}] ntfy: {title}")
            return
        except Exception as e:
            print(f"  [!] ntfy 오류: {e}")
            time.sleep(3)


def notify_site(site_name: str, items: list):
    if len(items) == 1:
        it = items[0]
        link = it.get("detail_url") or it["site_url"]
        dl = f"\n📅 마감: {it['deadline']}" if it.get("deadline") else ""
        send_ntfy(f"📢 [{site_name}] 새 공모", f"{it['title']}{dl}", click_url=link)
        return

    primary_link = items[0].get("detail_url") or items[0]["site_url"]
    lines = []
    for i in items[:10]:
        line = f"• {i['title']}"
        if i.get("deadline"):
            line += f" (~{i['deadline']})"
        lines.append(line)
    if len(items) > 10:
        lines.append(f"...외 {len(items) - 10}건 더")

    actions = []
    for it in items[:3]:
        link = it.get("detail_url") or it["site_url"]
        if link:
            actions.append({"action": "view", "label": _truncate(it["title"], 35), "url": link, "clear": True})

    send_ntfy(f"📢 [{site_name}] 새 공모 {len(items)}개", "\n".join(lines),
              click_url=primary_link, actions=actions)


# ═══════════════════════════════════════════════════════════
# 메인
# ═══════════════════════════════════════════════════════════

def main():
    print(f"\n공모사업 알림 봇 v6.4 | {datetime.now().strftime('%Y-%m-%d %H:%M KST')}")
    print("=" * 55)

    current, raw_counts = scrape_all()
    known = load_known()

    new_items = {k: v for k, v in current.items() if k not in known}
    print(f"\n{'═'*55}")
    print(f"전체 공모 풀: {len(current)}건 | 신규: {len(new_items)}건")

    if new_items:
        by_site: dict = {}
        for v in new_items.values():
            by_site.setdefault(v["name"], []).append(v)
        for site_name, items in by_site.items():
            notify_site(site_name, items)
    else:
        send_ntfy("✅ 신규 공모 없음", f"확인 시각: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    # ── 점검 알림: 0건 수집 사이트 / Gemini 전체 실패 ──
    warnings = []
    zero_sites = [n for n, c in raw_counts.items() if c == 0]
    if zero_sites:
        warnings.append(f"⚠️ 0건 수집(구조변경/차단 의심): {', '.join(zero_sites)}")
    if GEMINI_FAILURE_LOG:
        warnings.append(f"⚠️ Gemini 필터 실패 {len(GEMINI_FAILURE_LOG)}건 → 일부는 규칙필터 결과만 반영됐을 수 있음")
    if warnings:
        send_ntfy("🔧 festival-alert 점검 필요", "\n".join(warnings))

    # known_posts 업데이트 (신규 키 포맷으로 저장, 레거시 키도 함께 보존해 안전)
    known.update(current)
    save_known(known)
    print("\n완료.")


if __name__ == "__main__":
    main()

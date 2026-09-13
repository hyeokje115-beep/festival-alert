#!/usr/bin/env python3
"""
공모사업 알림 봇 v7.0 (B세트)
- 접속: domcontentloaded + 3단계 재시도 + 리소스 차단 + 스텔스 + Cloudflare 대기
- 노이즈: DOM 구조 기반 메뉴 제외(nav/header/footer/aside/gnb/lnb), 메뉴 문구·URL 패턴 차단, URL 기준 중복제거
- 검증: 신규 후보만 상세페이지 열어 접수기간 파싱(마감 지난 글 제외) + 애매건 본문 500자 Gemini 재판정
- Gemini: list_models()로 사용 가능 모델 자동 선택, JSON 모드(스키마 미사용), fail-open
- 알림: 게시글 1건=1알림, 클릭→상세, 버튼 [열기 / 관련없음 / 목록], 우선순위 계층(high/default/low)
- 학습: 관련없음 버튼 → 피드백 토픽 → 다음 실행에 URL 차단 + Gemini 프롬프트 예시 주입
- 운영: D-3 리마인더, 메타데이터 DB, fixtures 저장 + --replay 리플레이, 일요일 주간 제외리포트
"""
import base64, hashlib, json, os, re, sys, time, traceback
from datetime import datetime, timedelta, date
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse
import requests

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_FEEDBACK_TOPIC = os.environ.get("NTFY_FEEDBACK_TOPIC", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
RUN_MODE = os.environ.get("RUN_MODE", "full")          # full | feedback
KNOWN_FILE = "known_posts.json"
FIXTURE_DIR = "fixtures"
REPORT_DIR = "reports"

PER_POST_MAX_PER_SITE = 6
NTFY_BODY_LIMIT = 3800
OLD_YEAR_TOLERANCE = 1
DETAIL_FETCH_LIMIT = 20
REMIND_DAYS = 3
NOW = datetime.now()
TODAY = NOW.date()
CURRENT_YEAR = NOW.year

# ═════════════════════ 규칙 ═════════════════════
CATEGORY_WORDS = ["공연", "전시", "체험", "박람회", "축제", "기획전", "전시회", "아트페어", "문화예술", "예술단체"]
PURPOSE_WORDS = ["공모전", "공모사업", "공모", "지원사업", "지원금", r"모집\s*공고", "신청접수", "작품모집", "모집", "신청",
                 "참여작가", r"예술가?\s*모집", r"단체\s*모집", r"예술인\s*모집", r"작가\s*모집", r"팀\s*모집"]
BLACKLIST_PATTERNS = [
    r"결과\s*발표", r"결과\s*공개", r"결과\s*안내", r"선정\s*결과", r"최종\s*선정", r"심사\s*결과", r"수상\s*결과",
    r"합격자?\s*발표", r"\d+차\s*결과", r"행정심사\s*결과", r"모집\s*결과", r"접수\s*결과", r"결과\s*및\s*인터뷰",
    r"첨부파일", r"정산\s*안내서?", r"정산\s*보고", r"사업비\s*정산", r"보조금\s*정산",
    r"사업\s*추진단", r"^\s*TF\s*$", r"^\s*위원회\s*$", r"위원\s*위촉",
    r"심사위원.{0,10}모집", r"평가위원.{0,10}모집", r"자문위원.{0,10}모집", r"운영위원.{0,10}모집",
    r"상설공연(?!.*(공모|모집))", r"정기공연(?!.*(공모|모집))", r"\d{1,2}월\s*공연\s*안내", r"공연\s*소개", r"공연\s*일정",
    r"채용\s*공고", r"채용\s*공지", r"직원\s*모집", r"근무자\s*모집", r"인턴\s*모집", r"기간제\s*모집", r"계약직\s*모집",
    r"입찰\s*공고", r"용역\s*발주", r"운영대행\s*용역", r"용역\s*(제안서|입찰|공고)", r"용역사\s*모집",
    r"서포터즈\s*모집", r"기자단\s*모집", r"모니터단?\s*모집", r"자원봉사자?\s*모집", r"수강생\s*모집",
    r"^\s*(더보기|바로가기|하위메뉴)\s*$", r"홍보\s*추진",
    r"온라인\s*투표", r"대국민\s*투표", r"투표\s*안내", r"공모\s*수상작", r"수상작품?\s*(전시|안내|발표)",
    r"운영\s*안내$", r"취소\s*안내$", r"이용\s*안내$", r"교부/?변경\s*신청", r"사업비\s*카드", r"지원신청\s*(현황|취소)",
]
MENU_EXACT_BLACKLIST = {
    "대관신청", "대관신청(대관시스템)", "예매/신청 조회", "나의 지원신청 현황", "지원신청취소내역", "지원신청 취소내역",
    "교부/변경 신청", "교부/변경신청", "사업비카드발급신청", "지역의 공모, 공고", "지역의 공모,공고",
    "민주·인권·평화 공모수상작품", "민주·인권·평화 공모전", "지원신청", "정산/사업실적", "커뮤니티", "고객센터",
    "공지사항", "예약상담", "시스템 개선", "자료실", "게시판", "이용안내", "자주묻는질문", "회원정보", "로그인",
    "회원가입", "마이페이지", "전체메뉴", "사이트맵", "더보기", "HOME",
}
NAV_HREF_NEGATIVE = re.compile(
    r'(/menu\.do|/contents\.do|quickSchedule|frs/index|memberJoin|goLogin|memberInfo|memberBye|memberReservation|'
    r'memberFind|/search\.do|allSchedule\.do|/renewal/frm|/support/main|/etc/system|board/reserve/list|'
    r'board/system/list|board/form/list|/mypage|/login|/join|/logout|/sitemap|/faq|/rent)', re.I)
DETAIL_LINK_HINT = re.compile(
    r'(view|idx=|aseq=|seq=|wr_id=|bo_idx=|no=\d|article|read|detail|nttSn=|nttId=|postView|bseq=\d|ix=\d|'
    r'board_id=|document_srl=|boardSeq=|ntt|postId=|/notice/\d|/post/\d|mode=view|act=view|dataSid=)', re.I)
NAV_CONTAINER_JS = ("n => !!n.closest('nav,header,footer,aside,.gnb,.lnb,.snb,.quick,.util,.sitemap,.allmenu,"
                    ".all_menu,.all-menu,.top_menu,.topmenu,.sub_menu,.submenu,#gnb,#lnb,#header,#footer,"
                    ".quick_menu,.mypage,.breadcrumb,.location,.tab,.tabs,.paging,.pagination,.family_site')")
FALLBACK_KEYWORDS = ["공모사업", "지원사업 공모", "전시 공모", "공연 공모", "지원금 공모", "작품 공모", "전시공간 지원", "모집 공고"]
PLACEHOLDER_HREF = re.compile(r'^\s*(#.*|javascript:.*)?\s*$', re.I)


def normalize(t): return re.sub(r'\s+', ' ', re.sub(r'[\[\]<>【】《》「」『』\(\)\{\}·•★◆▶►≪≫～~]', ' ', t)).strip()
def clean(t): return re.sub(r'\s+', ' ', (t or '').replace('\n', ' ')).strip()
def looks_like_navigation_href(h): return bool(h) and bool(NAV_HREF_NEGATIVE.search(h))
def is_too_old(t):
    ys = [int(y) for y in re.findall(r'(?<!\d)(20\d{2})(?!\d)', t)]
    return bool(ys) and max(ys) < CURRENT_YEAR - OLD_YEAR_TOLERANCE


def rule_filter_debug(title, href="", category_required=False):
    t = title.strip()
    if t in MENU_EXACT_BLACKLIST: return False, "메뉴 문구 정확일치"
    if looks_like_navigation_href(href): return False, "네비게이션 URL"
    if len(t) < 8: return False, f"너무 짧음({len(t)}자)"
    if is_too_old(t): return False, "과거 연도"
    n = normalize(title)
    for p in BLACKLIST_PATTERNS:
        if re.search(p, n): return False, f"블랙리스트[{p}]"
    cat = next((p for p in CATEGORY_WORDS if re.search(p, n)), None)
    if category_required and not cat: return False, "카테고리어 없음(종합사이트)"
    pur = next((p for p in PURPOSE_WORDS if re.search(p, n)), None)
    if not pur: return False, "목적어 없음"
    return True, f"{cat or '사이트'}·{pur}"


# ═════════════════════ 날짜 파싱 ═════════════════════
DATE_PAT = re.compile(r'(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})')
def _to_date(y, m, d):
    try: return date(int(y), int(m), int(d))
    except Exception: return None

def extract_deadline(text):
    text = text or ""
    m = re.search(r'[~～]\s*' + DATE_PAT.pattern, text)
    if m:
        d = _to_date(*m.groups())
        if d: return d.isoformat()
    m = re.search(r'마감\s*:?\s*' + DATE_PAT.pattern, text)
    if m:
        d = _to_date(*m.groups())
        if d: return d.isoformat()
    ds = [_to_date(*g) for g in DATE_PAT.findall(text)]
    ds = [d for d in ds if d]
    return ds[-1].isoformat() if len(ds) >= 2 else (ds[0].isoformat() if ds else "")

def extract_period_deadline(body):
    """상세 본문에서 접수/신청/모집 기간의 종료일. 없으면 ''."""
    for m in re.finditer(r'(접수|신청|모집|공모)\s*기\s*간[^\n]{0,80}', body or ""):
        seg = m.group(0)
        ds = [_to_date(*g) for g in DATE_PAT.findall(seg)]
        ds = [d for d in ds if d]
        if ds: return max(ds).isoformat()
        # "2026.3.1 ~ 3.29" 형태
        y = re.search(r'20\d{2}', seg)
        tail = re.search(r'[~～]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})', seg)
        if y and tail:
            d = _to_date(y.group(0), tail.group(1), tail.group(2))
            if d: return d.isoformat()
    return ""

def days_left(iso):
    try: return (date.fromisoformat(iso) - TODAY).days
    except Exception: return None


# ═════════════════════ Gemini ═════════════════════
GEMINI_PREFER = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest", "gemini-2.5-flash-lite", "gemini-2.0-flash-lite"]
GEMINI_FAILURE_LOG = []
_GEMINI = {"model": None, "ok": False}
HARD_EXCLUDE = {"RESULT_ANNOUNCEMENT", "SETTLEMENT_ADMIN", "COMMITTEE_OR_JUDGE", "GENERAL_PUBLIC_RECRUIT",
                "EVENT_PROMO_ONLY", "JOB_POSTING", "SITE_MENU", "EXPIRED"}
SYSTEM_INSTRUCTION = ("당신은 문화예술 분야 보조금·공모사업 심사 전문가입니다. 전시·공연·체험(축제 포함) 분야의 "
    "개인사업자·단체·작가가 직접 신청서를 내어 예산·공간·활동기회를 받을 수 있는 '신규 신청 가능 공고'만 골라냅니다. "
    "애매하면 포함(기회 누락이 더 큰 손해). 단 홈페이지 메뉴명, 이미 끝난 결과·수상작·투표 안내, 채용·용역, "
    "일반 시민 참가자 모집은 확실히 제외합니다. 반드시 JSON 배열만 출력합니다.")
PROMPT = """아래 게시글 목록을 판정하세요. 각 항목은 id, title, 있으면 snippet(본문 일부)로 구성됩니다.
{bad_examples}
[제외 사유 코드] RESULT_ANNOUNCEMENT(결과/수상작/투표), SETTLEMENT_ADMIN(정산·교부·행정), COMMITTEE_OR_JUDGE(위원 모집),
GENERAL_PUBLIC_RECRUIT(시민·청소년·수강생 개인 참가자), EVENT_PROMO_ONLY(행사 홍보), JOB_POSTING(채용·용역),
SITE_MENU(메뉴명), EXPIRED(본문상 접수 마감됨), OTHER, NONE(포함)

출력 형식(JSON 배열만): [{{"id":1,"is_relevant":true,"confidence":"high","exclude_reason":"NONE"}}, ...]
confidence는 high/medium/low 중 하나.

목록:
{items}
"""

def gemini_init():
    if not GEMINI_KEY:
        print("! Gemini 키 없음 → fallback 모드"); return
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_KEY)
        avail = []
        try:
            for m in genai.list_models():
                if "generateContent" in getattr(m, "supported_generation_methods", []):
                    avail.append(m.name.replace("models/", ""))
        except Exception as e:
            print(f"! list_models 실패: {e}")
        chosen = next((p for p in GEMINI_PREFER if p in avail), None) or next((a for a in avail if "flash" in a), None) \
                 or (avail[0] if avail else GEMINI_PREFER[0])
        _GEMINI["model"] = chosen; _GEMINI["ok"] = True
        print(f"✓ Gemini 모델: {chosen} (사용가능 {len(avail)}개)")
    except Exception as e:
        print(f"! Gemini 초기화 실패: {e}"); GEMINI_FAILURE_LOG.append(f"init: {e}")

def _parse_json(text):
    text = (text or "").strip()
    text = re.sub(r'^```(?:json)?\s*', '', text); text = re.sub(r'\s*```$', '', text)
    try: return json.loads(text)
    except Exception:
        m = re.search(r'\[.*\]', text, re.S)
        return json.loads(m.group(0)) if m else []

def should_include(r):
    if r.get("is_relevant") is True: return True
    return not (r.get("confidence") == "high" and r.get("exclude_reason") in HARD_EXCLUDE)

def gemini_judge(items, bad_examples):
    """items에 gemini_conf / gemini_reason 채움. 실패 시 fail-open(전부 포함, conf='unverified')."""
    if not items: return items
    if not _GEMINI["ok"]:
        for it in items: it["gemini_conf"] = "unverified"
        return items
    import google.generativeai as genai
    be = ("[사용자가 '관련없음'으로 표시한 과거 예시 — 이런 유형은 제외 쪽으로]\n" + "\n".join(f"- {t}" for t in bad_examples[-10:])) if bad_examples else ""
    out = []
    for i in range(0, len(items), 20):
        batch = items[i:i+20]
        lines = []
        for k, it in enumerate(batch, 1):
            s = f' | snippet: {it["snippet"][:500]}' if it.get("snippet") else ""
            lines.append(f'{k}. title: {it["title"]}{s}')
        prompt = PROMPT.format(bad_examples=be, items="\n".join(lines))
        parsed, err = None, None
        for attempt in range(3):
            try:
                model = genai.GenerativeModel(_GEMINI["model"], system_instruction=SYSTEM_INSTRUCTION)
                cfg = genai.GenerationConfig(response_mime_type="application/json", temperature=0.1)
                parsed = _parse_json(model.generate_content(prompt, generation_config=cfg).text)
                if isinstance(parsed, list): break
                parsed = None
            except Exception as e:
                err = e; time.sleep(3 + 3*attempt)
        if parsed is None:
            GEMINI_FAILURE_LOG.append(str(err)[:300]); print(f"     [Gemini 실패] {err}")
            for it in batch: it["gemini_conf"] = "unverified"; out.append(it)
            continue
        by_id = {r.get("id"): r for r in parsed if isinstance(r, dict)}
        for k, it in enumerate(batch, 1):
            r = by_id.get(k)
            if r is None: it["gemini_conf"] = "unverified"; out.append(it); continue
            it["gemini_conf"] = r.get("confidence", "medium"); it["gemini_reason"] = r.get("exclude_reason", "NONE")
            if should_include(r): out.append(it)
            else: print(f"       ✗(AI) {it['title'][:50]} ← {it['gemini_reason']}")
        if i + 20 < len(items): time.sleep(2)
    return out


# ═════════════════════ 사이트 ═════════════════════
SITES = [
    {"name": "광주문화재단", "url": "https://www.gctf.or.kr/web/board/1/postList", "type": "board"},
    {"name": "MLDC", "url": "https://mldc.kr/notice", "type": "board"},
    {"name": "미마프", "url": "http://www.mimaf.net/xe/index.php?mid=notice", "type": "board", "page_param": "page", "pages": 2},
    {"name": "한국문화예술교육진흥원", "url": "https://www.kh.or.kr/brd/board/644/L/SITES/100/menu/371?brdCodeField=SITES&brdCodeValue=100", "type": "board"},
    {"name": "리콜렉션", "url": "https://recollection.kr/bbs/board.php?bo_table=notice&page=1", "type": "board", "page_param": "page", "pages": 2},
    {"name": "전주문화재단", "url": "https://www.jge.go.kr/jgemain/na/ntt/selectNttList.do?mi=2116&bbsId=1123", "type": "board"},
    {"name": "나주문화재단", "url": "https://www.njcf.or.kr/www/community/notices", "type": "board"},
    {"name": "담양문화재단", "url": "https://www.damyangcf.or.kr/user/board/lists/board_cd/4010", "type": "board"},
    {"name": "전남문화재단_타기관", "url": "https://www.jncf.or.kr/jact/open/otherevents.do", "type": "board"},
    {"name": "전남문화재단_협업", "url": "https://www.jncf.or.kr/jact/open/collusion.do", "type": "board"},
    {"name": "전남문화재단_공지", "url": "https://www.jncf.or.kr/jact/open/notice.do", "type": "board"},
    {"name": "광주문화재단_공지", "url": "https://www.gjcf.or.kr/cf/news/notice.do", "type": "board"},
    {"name": "고양문화재단", "url": "https://www.gtcc.or.kr/bbs/board.php?bo_table=info&page=1", "type": "board", "page_param": "page", "pages": 2},
    {"name": "문화예술", "url": "http://xn--9p4b13eb4bd6i.com/notice", "type": "board"},
    {"name": "국립아시아문화전당_공모", "url": "https://www.ncas.or.kr/board/contest/list?menuNo=&currentPageNo=1&searchCondition=", "type": "board", "page_param": "currentPageNo", "pages": 2},
    {"name": "위비티", "url": "https://www.wevity.com/?c=find&s=1&gub=1&cidx=&sp=&sw=&gbn=list&mode=new", "type": "wevity"},
    {"name": "국립아시아문화전당_공지", "url": "https://www.acc.go.kr/main/board/board.do?PID=0701&boardID=NOTICE", "type": "board"},
    {"name": "광주비엔날레", "url": "https://www.gwangjubiennale.org/gb/notice.do", "type": "board"},
    {"name": "전북문화관광재단", "url": "https://www.jbct.or.kr/notice.php", "type": "board"},
    {"name": "전북문화관광재단_공모", "url": "https://www.jbct.or.kr/c_notice.php", "type": "board"},
    {"name": "순천문화재단_공모캘린더", "url": "https://www.cfsc.or.kr/contents/news/news0106.asp", "type": "calendar"},
    {"name": "순천문화재단_타기관공모", "url": "https://www.cfsc.or.kr/contents/open/open0501.asp", "type": "board"},
    {"name": "순천문화재단_공모게시판", "url": "https://www.cfsc.or.kr/contents/open/open0102.asp?bseq=1&cat=39&yy=", "type": "board"},
    {"name": "목포문화재단_문화도시", "url": "https://mpcf.or.kr/bbs/board.php?bo_table=notice&sca=%EB%AC%B8%ED%99%94%EB%8F%84%EC%8B%9C", "type": "board", "page_param": "page", "pages": 2},
    {"name": "목포문화재단_전체", "url": "https://mpcf.or.kr/bbs/board.php?bo_table=notice", "type": "board", "page_param": "page", "pages": 2},
]


# ═════════════════════ 브라우저 ═════════════════════
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
STEALTH_JS = """
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
Object.defineProperty(navigator,'languages',{get:()=>['ko-KR','ko','en-US']});
Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3]});
window.chrome = window.chrome || {runtime:{}};
"""

def new_context(p):
    browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
    ctx = browser.new_context(user_agent=UA, locale="ko-KR", timezone_id="Asia/Seoul",
                              viewport={"width": 1366, "height": 900}, ignore_https_errors=True,
                              extra_http_headers={"Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8"})
    ctx.add_init_script(STEALTH_JS)
    ctx.route("**/*", lambda route: route.abort() if route.request.resource_type in ("image", "media", "font") else route.continue_())
    return browser, ctx

def safe_goto(page, url, label=""):
    """3단계 재시도. 성공 True/실패 False. Cloudflare/봇차단 페이지면 대기 후 재시도."""
    plans = [("domcontentloaded", 30000), ("domcontentloaded", 45000), ("commit", 60000)]
    for i, (wu, to) in enumerate(plans, 1):
        try:
            page.goto(url, wait_until=wu, timeout=to)
            try: page.wait_for_selector("table tbody tr, ul li a, article, .board, .bbs", timeout=8000)
            except Exception: pass
            page.wait_for_timeout(1500)
            html = (page.content() or "")[:4000].lower()
            if "just a moment" in html or "cf-challenge" in html or "checking your browser" in html:
                print(f"     [{label}] 봇차단 페이지 감지 → 10초 대기"); page.wait_for_timeout(10000)
                if "just a moment" in (page.content() or "")[:4000].lower(): continue
            return True
        except Exception as e:
            print(f"     [{label}] 접속 {i}차 실패({wu}/{to}ms): {str(e)[:80]}")
            time.sleep(2 * i)
    return False

def set_page_param(url, key, val):
    u = urlparse(url); q = parse_qs(u.query, keep_blank_values=True); q[key] = [str(val)]
    return urlunparse(u._replace(query=urlencode(q, doseq=True)))

def resolve_href(page, raw):
    if not raw or PLACEHOLDER_HREF.match(raw.strip()): return ""
    try: return urljoin(page.url, raw.strip())
    except Exception: return ""

def in_nav(el):
    try: return bool(el.evaluate(NAV_CONTAINER_JS))
    except Exception: return False

def visible(el):
    try: return el.is_visible()
    except Exception: return True

def dedup_same_batch(items):
    seen, out = set(), []
    for it in items:
        k = (it.get("detail_url") or "").strip() or f"__NOHREF__::{normalize(it['title'])}"
        if k in seen: continue
        seen.add(k); out.append(it)
    return out


# ═════════════════════ 스크래퍼 ═════════════════════
def _mk(name, title, url, detail, deadline): return {"name": name, "title": title[:120], "site_url": url, "detail_url": detail, "deadline": deadline}

def scrape_board_page(page, url, name):
    raw = []
    if not safe_goto(page, url, name): return raw
    # 1) 표 게시판
    for row in page.query_selector_all("table tbody tr"):
        try:
            cells = row.query_selector_all("td")
            if len(cells) < 2: continue
            first = cells[0].inner_text().strip()
            if not re.match(r'^\d+$', first) and first not in ("공지", "고정", "NOTICE", "Notice", "N", "new"): 
                if not re.search(r'^(공지|필독|\[공지\])', first): continue
            a = next((c.query_selector("a") for c in cells if c.query_selector("a")), None)
            if not a: continue
            title = clean(a.inner_text())[:100]; href = a.get_attribute("href") or ""
            if len(title) < 8 or title in MENU_EXACT_BLACKLIST or looks_like_navigation_href(href): continue
            raw.append(_mk(name, title, url, resolve_href(page, href), extract_deadline(row.inner_text())))
        except Exception: pass
    # 2) 리스트 게시판 — nav 컨테이너 밖 + (보이거나 게시글형 URL)
    if not raw:
        seen = set()
        for li in page.query_selector_all("ul li, ol li, .list li, .board_list li, .bbs_list li"):
            try:
                a = li.query_selector("a")
                if not a: continue
                title = clean(a.inner_text())[:100]; href = a.get_attribute("href") or ""
                if len(title) < 8 or title in seen or title in MENU_EXACT_BLACKLIST or looks_like_navigation_href(href): continue
                if in_nav(a): continue
                if not visible(a) and not DETAIL_LINK_HINT.search(href): continue
                seen.add(title)
                raw.append(_mk(name, title, url, resolve_href(page, href), extract_deadline(li.inner_text())))
            except Exception: pass
    # 3) 모든 링크 중 게시글형 URL만
    if not raw:
        seen = set()
        for a in page.query_selector_all("a"):
            try:
                href = a.get_attribute("href") or ""
                if not DETAIL_LINK_HINT.search(href) or looks_like_navigation_href(href): continue
                title = clean(a.inner_text())[:100]
                if len(title) < 8 or title in seen or title in MENU_EXACT_BLACKLIST or in_nav(a): continue
                seen.add(title); raw.append(_mk(name, title, url, resolve_href(page, href), ""))
            except Exception: pass
    return raw

def scrape_board(page, site, known_keys):
    """known-stop 페이지네이션: 다음 페이지에 미등록 글이 없으면 중단."""
    url, name = site["url"], site["name"]
    raw = scrape_board_page(page, url, name)
    pages, pp = site.get("pages", 1), site.get("page_param")
    for pn in range(2, pages + 1):
        if not pp or not raw: break
        more = scrape_board_page(page, set_page_param(url, pp, pn), name)
        if not more: break
        unknown = [m for m in more if make_key(m) not in known_keys and make_key_legacy(m) not in known_keys]
        raw.extend(more)
        if not unknown: break
    return raw

WEVITY_MAX_PAGES = 3
def scrape_wevity(page, site, known_keys):
    url, name = site["url"], site["name"]; raw, seen = [], set()
    for gp in range(1, WEVITY_MAX_PAGES + 1):
        target = set_page_param(url, "gp", gp)
        if not safe_goto(page, target, name): break
        anchors = page.query_selector_all("a[href*='gbn=view']")
        if not anchors: break
        new = False
        for a in anchors:
            try:
                href = a.get_attribute("href") or ""; m = re.search(r'[?&]ix=(\d+)', href)
                if not m or m.group(1) in seen: continue
                seen.add(m.group(1)); new = True
                title = re.sub(r'(?:\s*(?:SPECIAL|IDEA|NEW|HOT))+$', '', clean(a.inner_text()), flags=re.I).strip()
                if len(title) < 8: continue
                deadline = ""
                try:
                    li = a.evaluate("n => { const l = n.closest('li'); return l ? l.innerText : ''; }") or ""
                    dm = re.search(r'D-(\d+)', li)
                    deadline = (TODAY + timedelta(days=int(dm.group(1)))).isoformat() if dm else extract_deadline(li)
                except Exception: pass
                raw.append(_mk(name, title, url, urljoin(page.url, href), deadline))
            except Exception: continue
        if not new: break
    return raw

CALENDAR_ALLOWED = {"공모"}
def scrape_calendar(page, site, known_keys):
    url, name = site["url"], site["name"]; raw, seen = [], set()
    ny, nm = (TODAY.year, TODAY.month + 1) if TODAY.month < 12 else (TODAY.year + 1, 1)
    for yy, mm in [(TODAY.year, TODAY.month), (ny, nm)]:
        cal = f"{url}?y={yy}&m={mm:02d}"
        if not safe_goto(page, cal, name): continue
        page.wait_for_timeout(2500)
        for li in page.query_selector_all("ul li"):
            try:
                t = clean(li.inner_text())
                m = re.match(r'^\s*(공모|전시|공연|교육|축제\s*[·・]?\s*행사)\b', t)
                if not m or m.group(1).replace(" ", "") not in CALENDAR_ALLOWED: continue
                title = t[len(m.group(0)):].strip()
                per = re.search(r'(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})', title)
                deadline = per.group(2) if per else ""
                title = re.sub(r'_?\d{4}-\d{2}-\d{2}\s*~\s*\d{4}-\d{2}-\d{2}_?', '', title).strip(" _-·")
                if len(title) < 8 or normalize(title) in seen: continue
                seen.add(normalize(title))
                a = li.query_selector("a"); href = resolve_href(page, a.get_attribute("href") or "") if a else ""
                raw.append(_mk(name, title, cal, href, deadline))
            except Exception: continue
    return raw

SCRAPERS = {"board": scrape_board, "wevity": scrape_wevity, "calendar": scrape_calendar}

def fetch_detail(page, url):
    """(본문 앞 1500자, 접수기간 종료일)"""
    try:
        if not safe_goto(page, url, "detail"): return "", ""
        body = ""
        for sel in ("article", ".view_con", ".board_view", ".bbs_view", ".view", "#content", "#contents", "main", "body"):
            el = page.query_selector(sel)
            if el:
                body = clean(el.inner_text())
                if len(body) > 200: break
        return body[:1500], extract_period_deadline(body)
    except Exception: return "", ""


# ═════════════════════ DB ═════════════════════
def make_key(it):
    h = (it.get("detail_url") or "").strip()
    return f"{it['name']}|{h}" if h else f"{it['name']}|{normalize(it['title'])}"
def make_key_legacy(it): return f"{it['name']}|{it['title']}"
def short_id(key): return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]

def load_db():
    db = {"version": 2, "posts": {}, "blocked": [], "bad_examples": [], "rejected_recent": [], "fb_index": {}}
    if not os.path.exists(KNOWN_FILE): return db
    try:
        data = json.load(open(KNOWN_FILE, encoding="utf-8"))
        if isinstance(data, list): 
            for k in data: db["posts"][k] = {"legacy": True}
        elif isinstance(data, dict) and data.get("version") == 2:
            db.update(data)
        elif isinstance(data, dict):
            for k in data.keys(): db["posts"][k] = {"legacy": True}
    except Exception as e: print(f"! DB 로드 실패: {e}")
    return db

def save_db(db):
    db["rejected_recent"] = db["rejected_recent"][-300:]; db["bad_examples"] = db["bad_examples"][-50:]
    json.dump(db, open(KNOWN_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


# ═════════════════════ ntfy ═════════════════════
def _hdr(v):
    try: v.encode("ascii"); return v
    except UnicodeEncodeError: return "=?UTF-8?B?" + base64.b64encode(v.encode()).decode() + "?="

def _post(body, headers, topic=None):
    topic = topic or NTFY_TOPIC
    if not topic: print("  ! NTFY_TOPIC 없음\n" + body); return False
    for attempt in range(3):
        try:
            r = requests.post(f"{NTFY_SERVER}/{topic}", data=body.encode("utf-8"), headers=headers, timeout=20)
            if r.status_code < 300: return True
            print(f"  ! ntfy {r.status_code}: {r.text[:150]}")
        except Exception as e: print(f"  ! ntfy 실패: {e}")
        time.sleep(2)
    return False

def _actions(item, key):
    acts = []
    detail, site_url = item.get("detail_url") or "", item.get("site_url") or ""
    if detail: acts.append(f"view, 열기, {detail}, clear=true")
    if NTFY_FEEDBACK_TOPIC:
        acts.append(f'http, 관련없음, {NTFY_SERVER}/{NTFY_FEEDBACK_TOPIC}, method=POST, body=nope:{short_id(key)}, clear=true')
    acts.append(f"view, 목록, {site_url}")
    return "; ".join(acts[:3])

def priority_for(item):
    conf, dl = item.get("gemini_conf", ""), days_left(item.get("deadline") or "")
    if conf == "high" and dl is not None and 0 <= dl <= 7: return "high"
    if conf == "high": return "default"
    return "low"

def conf_text(item):
    return {"high": "AI 신뢰도 높음", "medium": "AI 신뢰도 보통", "low": "AI 확인 필요", "unverified": "AI 미검증"}.get(item.get("gemini_conf", ""), "")

def send_post(item, key):
    dl = item.get("deadline") or ""; n = days_left(dl)
    line2 = f"마감 {dl}" + (f" (D-{n})" if n is not None and n >= 0 else "") if dl else "마감 미확인"
    basis = f"근거: {item.get('rule_reason','')}"
    if item.get("verified_by_body"): basis += " · 본문검증"
    body = f"{item['title']}\n{line2}\n{basis} · {conf_text(item)}\n출처: {item['name']}"
    if not item.get("detail_url"): body += "\n(상세링크 없음 → 목록으로 이동)"
    headers = {"Title": _hdr(f"[{item['name']}] 새 공모"), "Priority": priority_for(item), "Tags": "loudspeaker",
               "Click": item.get("detail_url") or item.get("site_url"), "Actions": _hdr(_actions(item, key))}
    return _post(body, headers)

def _chunks(lines, limit):
    out, cur, size = [], [], 0
    for ln in lines:
        b = len(ln.encode()) + 1
        if cur and size + b > limit: out.append(cur); cur, size = [], 0
        cur.append(ln); size += b
    if cur: out.append(cur)
    return out

def send_grouped(site, items):
    site_url = items[0].get("site_url", "")
    lines = [f"• {it['title']}" + (f" (~{it['deadline']})" if it.get("deadline") else "") for it in items]
    ch = _chunks(lines, NTFY_BODY_LIMIT)
    for idx, c in enumerate(ch, 1):
        start = sum(len(x) for x in ch[:idx-1]); part = items[start:start+len(c)]
        acts = "; ".join(f"view, {it['title'][:12]}…, {it.get('detail_url') or site_url}" for it in part[:3])
        headers = {"Title": _hdr(f"[{site}] 새 공모 {len(items)}개" + (f" ({idx}/{len(ch)})" if len(ch) > 1 else "")),
                   "Priority": "default", "Tags": "loudspeaker", "Click": site_url, "Actions": _hdr(acts)}
        _post("\n".join(c), headers); time.sleep(0.5)

def send_system(title, body, prio="low"):
    _post(body, {"Title": _hdr(title), "Priority": prio, "Tags": "warning"})


# ═════════════════════ 피드백 수거 ═════════════════════
def collect_feedback(db):
    if not NTFY_FEEDBACK_TOPIC: return 0
    try:
        r = requests.get(f"{NTFY_SERVER}/{NTFY_FEEDBACK_TOPIC}/json?poll=1&since=72h", timeout=20)
        if r.status_code >= 300: return 0
        seen_ids = set(db.get("fb_seen", []))
        n = 0
        for line in r.text.splitlines():
            try: msg = json.loads(line)
            except Exception: continue
            if msg.get("event") != "message" or msg.get("id") in seen_ids: continue
            seen_ids.add(msg["id"])
            m = re.match(r'nope:([0-9a-f]{12})', (msg.get("message") or "").strip())
            if not m: continue
            key = db["fb_index"].get(m.group(1))
            if not key: continue
            post = db["posts"].get(key, {})
            url = key.split("|", 1)[1]
            if url and url not in db["blocked"]: db["blocked"].append(url)
            t = post.get("title")
            if t and t not in db["bad_examples"]: db["bad_examples"].append(t)
            post["feedback"] = "nope"; n += 1
        db["fb_seen"] = list(seen_ids)[-500:]
        print(f"피드백 수거: {n}건 (차단 URL {len(db['blocked'])}, 예시 {len(db['bad_examples'])})")
        return n
    except Exception as e:
        print(f"! 피드백 수거 실패: {e}"); return 0


# ═════════════════════ 리마인더 / 리포트 / 리플레이 ═════════════════════
def send_reminders(db):
    if NOW.hour >= 12: return
    for key, p in db["posts"].items():
        if not p.get("notified") or p.get("reminded") or p.get("feedback") == "nope": continue
        n = days_left(p.get("deadline") or "")
        if n is None or not (0 <= n <= REMIND_DAYS): continue
        item = {"name": p.get("site", ""), "title": p.get("title", ""), "detail_url": p.get("url", ""),
                "site_url": p.get("site_url", ""), "deadline": p["deadline"], "gemini_conf": "high", "rule_reason": "마감 임박 리마인더"}
        headers = {"Title": _hdr(f"[D-{n}] {p.get('site','')} 마감 임박"), "Priority": "high", "Tags": "alarm_clock",
                   "Click": item["detail_url"] or item["site_url"], "Actions": _hdr(_actions(item, key))}
        _post(f"{p.get('title','')}\n마감 {p['deadline']} (D-{n})", headers); p["reminded"] = True; time.sleep(0.4)

def weekly_report(db):
    if NOW.weekday() != 6 or NOW.hour < 17: return
    os.makedirs(REPORT_DIR, exist_ok=True)
    fn = f"{REPORT_DIR}/{NOW:%G-W%V}.md"
    if os.path.exists(fn): return
    week = [r for r in db["rejected_recent"] if r.get("date", "") >= (TODAY - timedelta(days=7)).isoformat()]
    lines = [f"# 주간 제외 목록 {NOW:%Y-%m-%d}", "", f"총 {len(week)}건", ""]
    for r in week: lines.append(f"- [{r['site']}] {r['title']} ← {r['reason']}" + (f" ({r['url']})" if r.get("url") else ""))
    open(fn, "w", encoding="utf-8").write("\n".join(lines))
    send_system("[주간] 제외 목록 리포트", f"이번 주 규칙/AI가 제외한 {len(week)}건이 {fn}에 저장됨. 놓친 게 있는지 5분만 검수해 주세요.")

def replay():
    import glob
    files = sorted(glob.glob(f"{FIXTURE_DIR}/*.json"))[-60:]
    db = load_db(); changed = 0
    for f in files:
        for it in json.load(open(f, encoding="utf-8")):
            ok, reason = rule_filter_debug(it["title"], it.get("detail_url", ""), it["name"] == "위비티")
            prev = db["posts"].get(make_key(it), {}).get("decision")
            now = "pass" if ok else "reject"
            if prev and prev != now:
                changed += 1; print(f"[변경] {prev}→{now} [{it['name']}] {it['title']}  ({reason})")
    print(f"리플레이 완료: {len(files)}개 파일, 판정 변경 {changed}건")


# ═════════════════════ main ═════════════════════
def run_full():
    from playwright.sync_api import sync_playwright
    db = load_db(); known_keys = set(db["posts"].keys()); first_run = not known_keys
    print(f"=== v7.0 시작 {NOW:%Y-%m-%d %H:%M} / 등록 {len(known_keys)}건 {'(첫 실행: 등록만)' if first_run else ''} ===")
    collect_feedback(db); gemini_init()
    blocked = set(db["blocked"])
    all_raw, raw_counts, new_items = [], {}, []

    with sync_playwright() as p:
        browser, ctx = new_context(p); page = ctx.new_page()
        for site in SITES:
            name = site["name"]; print(f"\n▶ [{name}]")
            try: items = SCRAPERS.get(site.get("type", "board"), scrape_board)(page, site, known_keys)
            except Exception as e: print(f"     예외: {e}"); items = []
            items = dedup_same_batch(items); raw_counts[name] = len(items); all_raw.extend(items)
            passed = 0
            for it in items:
                k = make_key(it)
                if k in known_keys or make_key_legacy(it) in known_keys: continue
                if (it.get("detail_url") or "") in blocked: continue
                ok, reason = rule_filter_debug(it["title"], it.get("detail_url", ""), site.get("type") == "wevity")
                if ok: it["rule_reason"] = reason; new_items.append(it); passed += 1
                else:
                    print(f"       ✗ {it['title'][:50]} ← {reason}")
                    db["posts"][k] = {"title": it["title"], "url": it.get("detail_url", ""), "site": name, "site_url": it["site_url"],
                                      "first_seen": TODAY.isoformat(), "decision": "reject", "reason": reason}
                    db["rejected_recent"].append({"date": TODAY.isoformat(), "site": name, "title": it["title"], "reason": reason, "url": it.get("detail_url", "")})
            print(f"     수집 {len(items)} / 신규 통과 {passed}")

        # 상세페이지 검증 (신규 후보만, 상한)
        for it in new_items[:DETAIL_FETCH_LIMIT]:
            if not it.get("detail_url"): continue
            body, dl = fetch_detail(page, it["detail_url"])
            if body: it["snippet"] = body; it["verified_by_body"] = True
            if dl: it["deadline"] = dl
        browser.close()

    # 마감 지난 글 제외
    fresh = []
    for it in new_items:
        n = days_left(it.get("deadline") or "")
        if n is not None and n < 0 and it.get("verified_by_body"):
            print(f"       ✗ {it['title'][:50]} ← 접수 마감({it['deadline']})")
            db["posts"][make_key(it)] = {"title": it["title"], "url": it.get("detail_url", ""), "site": it["name"], "site_url": it["site_url"],
                                         "first_seen": TODAY.isoformat(), "decision": "reject", "reason": "마감"}
        else: fresh.append(it)

    # Gemini (애매건에는 snippet 포함되어 있음)
    final = gemini_judge(fresh, db["bad_examples"]) if fresh else []
    final_keys = {make_key(x) for x in final}
    print(f"\n신규 후보 {len(new_items)} → 마감제외 후 {len(fresh)} → AI 통과 {len(final)}")

    # 발송
    if not first_run and final:
        by_site = {}
        for it in final: by_site.setdefault(it["name"], []).append(it)
        for site, items in by_site.items():
            print(f"📣 [{site}] {len(items)}건")
            if len(items) <= PER_POST_MAX_PER_SITE:
                for it in items: send_post(it, make_key(it)); time.sleep(0.4)
            else: send_grouped(site, items)

    # DB 기록
    for it in fresh:
        k = make_key(it); db["fb_index"][short_id(k)] = k
        db["posts"][k] = {"title": it["title"], "url": it.get("detail_url", ""), "site": it["name"], "site_url": it["site_url"],
                          "first_seen": TODAY.isoformat(), "deadline": it.get("deadline", ""),
                          "decision": "pass" if k in final_keys else "reject_ai", "reason": it.get("rule_reason", ""),
                          "conf": it.get("gemini_conf", ""), "notified": (not first_run and k in final_keys)}
        if k not in final_keys:
            db["rejected_recent"].append({"date": TODAY.isoformat(), "site": it["name"], "title": it["title"], "reason": f"AI:{it.get('gemini_reason','')}", "url": it.get("detail_url", "")})
    if not first_run: send_reminders(db)
    weekly_report(db)
    os.makedirs(FIXTURE_DIR, exist_ok=True)
    json.dump(all_raw, open(f"{FIXTURE_DIR}/raw_{NOW:%Y-%m-%d_%H}.json", "w", encoding="utf-8"), ensure_ascii=False)
    save_db(db)

    zero = [n for n, c in raw_counts.items() if c == 0]
    db_zero = db.setdefault("zero_streak", {})
    for n in raw_counts: db_zero[n] = db_zero.get(n, 0) + 1 if n in zero else 0
    save_db(db)
    if zero:
        hot = [n for n in zero if db_zero.get(n, 0) >= 3]
        send_system("[점검] 0건 수집 사이트" + (" — 3회 연속(개편 의심)" if hot else ""),
                    "\n".join(f"• {n}" + (" ⚠️" if n in hot else "") for n in zero), "default" if hot else "low")
    if GEMINI_FAILURE_LOG:
        send_system("[점검] Gemini 일부 실패", f"{len(GEMINI_FAILURE_LOG)}회 → 해당 건은 'AI 미검증'으로 발송\n{GEMINI_FAILURE_LOG[-1]}")
    print("=== 종료 ===")

def run_feedback_only():
    db = load_db(); collect_feedback(db); save_db(db)

if __name__ == "__main__":
    if "--replay" in sys.argv: replay()
    elif RUN_MODE == "feedback": run_feedback_only()
    else: run_full()

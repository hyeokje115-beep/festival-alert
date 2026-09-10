#!/usr/bin/env python3
import json, os, re, time
from datetime import datetime
from playwright.sync_api import sync_playwright
import requests
import google.generativeai as genai

NTFY_TOPIC  = os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = "https://ntfy.sh"
KNOWN_FILE  = "known_posts.json"
GEMINI_KEY  = os.environ.get("GEMINI_API_KEY", "")

KEYWORDS = ["전시", "공연", "체험", "박람회"]

# ── 사전 규칙 필터 (명백한 쓰레기 차단) ──────────────────────────────────
HARD_EXCLUDE_CONTAINS = [
    "하위메뉴", "더보기", "프로그램더보기", "하위메뉴 있음",
    "공연예매", "공연예매권", "티켓", "예매",
]
HARD_EXCLUDE_ENDS = ["더보기", "있음", "하위메뉴"]
HARD_EXCLUDE_PATTERNS = [
    r'^[가-힣\s]{2,8}$',            # 2~8자 순한글 (메뉴명)
    r'.+/.+/.+',                     # 슬래시 2개 이상 (콘서트/전시회/공연)
    r'^\d+회차',                     # N회차로 시작
]

SITES = [
    {"name": "광주문화재단",          "url": "https://www.gctf.or.kr/web/board/1/postList"},
    {"name": "MLDC",                 "url": "https://mldc.kr/notice"},
    {"name": "미마프",                "url": "http://www.mimaf.net/xe/index.php?mid=notice"},
    {"name": "한국문화예술교육진흥원",  "url": "https://www.kh.or.kr/brd/board/644/L/SITES/100/menu/371?brdCodeField=SITES&brdCodeValue=100"},
    {"name": "리콜렉션",              "url": "https://recollection.kr/bbs/board.php?bo_table=notice&page=1"},
    {"name": "전주문화재단",           "url": "https://www.jge.go.kr/jgemain/na/ntt/selectNttList.do?mi=2116&bbsId=1123"},
    {"name": "나주문화재단",           "url": "https://www.njcf.or.kr/www/community/notices"},
    {"name": "담양문화재단",           "url": "https://www.damyangcf.or.kr/user/board/lists/board_cd/4010"},
    {"name": "전남문화재단_타기관",     "url": "https://www.jncf.or.kr/jact/open/otherevents.do"},
    {"name": "전남문화재단_협업",       "url": "https://www.jncf.or.kr/jact/open/collusion.do"},
    {"name": "전남문화재단_공지",       "url": "https://www.jncf.or.kr/jact/open/notice.do"},
    {"name": "광주문화재단_공지",       "url": "https://www.gjcf.or.kr/cf/news/notice.do"},
    {"name": "고양문화재단",           "url": "https://www.gtcc.or.kr/bbs/board.php?bo_table=info&page=1"},
    {"name": "문화예술",               "url": "http://xn--9p4b13eb4bd6i.com/notice"},
    {"name": "국립아시아문화전당_공모",  "url": "https://www.ncas.or.kr/board/contest/list?menuNo=&currentPageNo=1&searchCondition="},
    {"name": "위비티",                 "url": "https://www.wevity.com/?c=find&s=1&gub=1"},
    {"name": "국립아시아문화전당_공지",  "url": "https://www.acc.go.kr/main/board/board.do?PID=0701&boardID=NOTICE"},
    {"name": "광주비엔날레",           "url": "https://www.gwangjubiennale.org/gb/notice.do"},
    {"name": "전북문화관광재단",        "url": "https://www.jbct.or.kr/notice.php"},
    {"name": "전북문화관광재단_공모",   "url": "https://www.jbct.or.kr/c_notice.php"},
    {"name": "순천문화재단",           "url": "https://www.cfsc.or.kr/contents/open/open0501.asp"},
    {"name": "목포문화재단_문화도시",   "url": "https://mpcf.or.kr/bbs/board.php?bo_table=notice&sca=%EB%AC%B8%ED%99%94%EB%8F%84%EC%8B%9C"},
    {"name": "목포문화재단_전체",       "url": "https://mpcf.or.kr/bbs/board.php?bo_table=notice"},
]

if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)
    gemini = genai.GenerativeModel("gemini-1.5-flash")
    print("✓ Gemini 연결됨")
else:
    gemini = None
    print("! Gemini 키 없음")


def hard_filter(title):
    """규칙 기반 사전 필터 - 명백한 쓰레기 즉시 차단"""
    t = title.strip()

    # 길이
    if len(t) < 12:
        return False

    # 키워드 없으면 제외
    if not any(k in t for k in KEYWORDS):
        return False

    # 슬래시 포함 → 카테고리 (콘서트/전시회/공연)
    if "/" in t:
        return False

    # 중간점(·) + 짧으면 → 프로그램명 (창제작 어린이·청소년 공연)
    if "·" in t and len(t) < 25:
        return False

    # 특정 단어 포함
    for word in HARD_EXCLUDE_CONTAINS:
        if word in t:
            return False

    # 특정 단어로 끝남
    for word in HARD_EXCLUDE_ENDS:
        if t.endswith(word):
            return False

    # 패턴
    for pat in HARD_EXCLUDE_PATTERNS:
        if re.search(pat, t):
            return False

    # "전시실", "체험관", "공연장" 등 공간명으로 끝나는 것
    if re.search(r'(전시실|체험관|공연장|전시관|박물관)$', t):
        return False

    # "N회차 공연 안내" 형태
    if re.search(r'\d+회차.*(안내|공지)', t):
        return False

    return True


def gemini_filter(candidates):
    """
    Gemini 강화 필터 - Few-shot 예시 포함
    실제로 신청/지원 가능한 공모만 통과
    """
    if not gemini or not candidates:
        return candidates

    titles = [c["title"] for c in candidates]
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles))

    prompt = f"""
너는 문화재단 공모사업 공고 필터링 AI야.
전시·공연·체험·박람회 종사자가 실제로 "신청/지원" 할 수 있는 공모 공고만 골라야 해.

━━━ 절대 제외 (이런 건 무조건 NO) ━━━
❌ 공간명/시설명: "작품전시실", "체험관", "전시관"
❌ 카테고리/분류: "콘서트/전시회/공연", "교육·체험"  
❌ 상설 프로그램: "토요상설공연", "풍류소리 N회차"
❌ UI 텍스트: "하위메뉴 있음", "더보기", "프로그램더보기"
❌ 단순 일정 안내: "N회차 공연 안내", "공연일정", "공연일정 안내"
❌ 선정 결과 첨부만: "선정 결과 첨부파일", "모집 첨부파일"
❌ 프로그램명: "ACC 아시아 예술체험", "창제작 어린이 공연"
❌ 공연 티켓/예매 관련

━━━ 반드시 포함 (이런 건 YES) ━━━
✅ 지원사업 공모 공고 (모집, 선정, 지원금)
✅ 참가자/사업자/단체 모집 공고
✅ 공모전, 작품 공모
✅ 예시: "2026년 광주버스킹월드컵 공연 참가자 모집 공고"
✅ 예시: "전시공간 지원사업 공모"
✅ 예시: "[박물관박람회] K-Heritage 팝업스토어 참여 모집"

━━━ 판단 기준 ━━━
"이 공고를 보고 신청서를 낼 수 있는가?" → YES면 포함, NO면 제외

[제목 목록]
{numbered}

응답 규칙:
- 포함할 번호만 쉼표로 출력
- 없으면 정확히 "없음" 출력
- 다른 말 절대 금지
예시: 2,5,8
"""
    try:
        resp = gemini.generate_content(prompt)
        text = resp.text.strip()
        print(f"     Gemini 응답: {text}")

        if "없음" in text:
            return []

        valid = set()
        for n in re.findall(r'\d+', text):
            valid.add(int(n))

        return [candidates[i-1] for i in valid if 1 <= i <= len(candidates)]

    except Exception as e:
        print(f"     [!] Gemini 오류: {e}")
        return candidates  # 오류 시 원본 반환


def gemini_raw_analyze(page_text, site_name, site_url):
    """후보 0개일 때 raw 텍스트 직접 분석"""
    if not gemini:
        return []

    prompt = f"""
너는 문화재단 공모사업 공고 추출 AI야.
사이트: {site_name}

아래 텍스트에서 "전시, 공연, 체험, 박람회" 관련
실제로 신청/지원 가능한 공모 공고만 추출해줘.

제외: 메뉴명, 카테고리, 상설프로그램, 단순 안내, UI 텍스트
포함: 모집 공고, 지원사업, 공모전, 참가자 모집

[텍스트]
{page_text[:4000]}

JSON 배열로만 응답. 없으면 []:
[
  {{"title": "공고 제목", "deadline": "YYYY-MM-DD 또는 빈 문자열"}}
]
"""
    try:
        resp = gemini.generate_content(prompt)
        text = resp.text.strip()
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if not match:
            return []

        items = json.loads(match.group())
        result = []
        for item in items:
            title = item.get("title", "").strip()
            if len(title) < 12:
                continue
            result.append({
                "name": site_name,
                "title": title[:80],
                "deadline": item.get("deadline", ""),
                "site_url": site_url,
                "first_seen": datetime.now().strftime("%Y-%m-%d"),
            })
        return result

    except Exception as e:
        print(f"     [!] Gemini raw 오류: {e}")
        return []


def extract_deadline(text):
    patterns = [
        r'\d{4}[.\-]\d{1,2}[.\-]\d{1,2}',
        r'\d{4}년\s*\d{1,2}월\s*\d{1,2}일',
        r'\d{2}[.\-]\d{1,2}[.\-]\d{1,2}',
    ]
    found = []
    for pat in patterns:
        found += re.findall(pat, text)
    return found[-1] if found else ""


def clean(text):
    return re.sub(r'\s+', ' ', text.strip().replace("\n", " ")).strip()


def scrape_candidates(page, site_url, name):
    candidates = []
    raw_text = ""

    try:
        page.goto(site_url, wait_until="networkidle", timeout=40000)
        page.wait_for_timeout(2000)
        raw_text = page.inner_text("body")

        # 전략1: 번호 있는 테이블 행
        rows = page.query_selector_all("table tbody tr")
        for row in rows:
            try:
                cells = row.query_selector_all("td")
                if len(cells) < 2:
                    continue
                first = cells[0].inner_text().strip()
                if not re.match(r'^\d+$', first):
                    continue
                title_el = None
                for cell in cells[1:]:
                    title_el = cell.query_selector("a")
                    if title_el:
                        break
                if not title_el:
                    continue
                title = clean(title_el.inner_text())
                if not hard_filter(title):
                    continue
                candidates.append({
                    "name": name,
                    "title": title[:80],
                    "deadline": extract_deadline(row.inner_text()),
                    "site_url": site_url,
                    "first_seen": datetime.now().strftime("%Y-%m-%d"),
                })
            except Exception:
                pass

        # 전략2: li 기반
        if not candidates:
            for item in page.query_selector_all("ul li, ol li"):
                try:
                    title_el = item.query_selector("a")
                    if not title_el:
                        continue
                    title = clean(title_el.inner_text())
                    if not hard_filter(title):
                        continue
                    candidates.append({
                        "name": name,
                        "title": title[:80],
                        "deadline": extract_deadline(item.inner_text()),
                        "site_url": site_url,
                        "first_seen": datetime.now().strftime("%Y-%m-%d"),
                    })
                except Exception:
                    pass

    except Exception as e:
        print(f"     [!] {name} 스크래핑 오류: {e}")

    return candidates, raw_text


def scrape_all():
    all_posts = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
        ))

        for site in SITES:
            name = site["name"]
            url  = site["url"]
            print(f"\n  ↳ [{name}]")

            candidates, raw_text = scrape_candidates(page, url, name)
            print(f"     1차 후보: {len(candidates)}개")

            if candidates:
                # Gemini 강화 필터
                filtered = gemini_filter(candidates)
                print(f"     Gemini 후: {len(filtered)}개")
                time.sleep(2)  # API 레이트 리밋 방지

                for item in filtered:
                    post_id = f"{name}|{item['title'][:50]}"
                    all_posts[post_id] = item

            else:
                # raw 텍스트 직접 분석
                print(f"     후보 없음 → Gemini raw 분석")
                raw_results = gemini_raw_analyze(raw_text, name, url)
                print(f"     raw 결과: {len(raw_results)}개")
                time.sleep(2)

                for item in raw_results:
                    post_id = f"{name}|{item['title'][:50]}"
                    all_posts[post_id] = item

        browser.close()

    return all_posts


def load_known():
    if os.path.exists(KNOWN_FILE):
        with open(KNOWN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_known(data):
    with open(KNOWN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def send_ntfy(title, body, click_url=""):
    if not NTFY_TOPIC:
        return
    payload = {
        "topic": NTFY_TOPIC,
        "title": title,
        "message": body,
        "priority": 4,
        "tags": ["loudspeaker"],
    }
    if click_url:
        payload["click"] = click_url
    r = requests.post(NTFY_SERVER, json=payload, timeout=10)
    print(f"  [{'✓' if r.status_code==200 else '✗'}] {title}")


def main():
    print(f"\n공모사업 알림 봇: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    current   = scrape_all()
    known     = load_known()
    new_items = {k: v for k, v in current.items() if k not in known}

    print(f"\n전체: {len(current)}개 | 신규: {len(new_items)}개")

    if new_items:
        by_site = {}
        for v in new_items.values():
            by_site.setdefault(v["name"], []).append(v)

        for site_name, items in by_site.items():
            list_url = items[0]["site_url"]

            if len(items) == 1:
                item = items[0]
                deadline_str = f"\n📅 마감: {item['deadline']}" if item.get("deadline") else ""
                send_ntfy(
                    title=f"📢 [{site_name}] 새 공모",
                    body=f"{item['title']}{deadline_str}",
                    click_url=list_url,
                )
            else:
                lines = []
                for i in items:
                    line = f"• {i['title']}"
                    if i.get("deadline"):
                        line += f" (~{i['deadline']})"
                    lines.append(line)
                send_ntfy(
                    title=f"📢 [{site_name}] 새 공모 {len(items)}개",
                    body="\n".join(lines),
                    click_url=list_url,
                )
    else:
        send_ntfy(
            title="✅ 신규 공모 없음",
            body=f"확인 시각: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        )

    known.update(current)
    save_known(known)
    print("완료")


if __name__ == "__main__":
    main()

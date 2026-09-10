#!/usr/bin/env python3
"""
공모사업 알림 봇 v6.2
핵심 개선:
1. 캘린더 사이트 전용 스크래퍼 (strong 태그 추출, 4초 대기, 월별 URL 동적 생성)
2. 전 단계 디버그 로그 (수집→규칙필터→Gemini→결과 전부 출력)
3. 규칙 필터 정교화 (r"모집\s*공고" 추가, 블랙리스트 오버블로킹 제거)
"""

import json, os, re, time, traceback
from datetime import datetime
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
    "공연", "전시", "체험", "박람회",
    "기획전", "전시회", "아트페어",
    "문화예술", "예술단체",
]

PURPOSE_WORDS = [
    "공모전", "공모사업", "공모", "지원사업", "지원금",
    "모집공고",
    r"모집\s*공고",        # ← 공백 있는 버전 추가 (핵심 수정!)
    "신청접수", "작품모집",
    r"참여작가",
    r"예술가?\s*모집",
    r"단체\s*모집",
    r"예술인\s*모집",
    r"작가\s*모집",
    r"참여\s*단체\s*모집",
    r"팀\s*모집",
]

BLACKLIST_PATTERNS = [
    # 결과/선정
    r"결과\s*발표", r"결과\s*공개", r"결과\s*안내", r"선정\s*결과",
    r"최종\s*선정", r"심사\s*결과", r"수상\s*결과", r"합격자?\s*발표",
    r"\d+차\s*결과", r"행정심사\s*결과",
    # 첨부파일/정산
    r"첨부파일", r"정산\s*안내서?",
    # 행정조직
    r"사업\s*추진단", r"^\s*TF\s*$", r"^\s*위원회\s*$",
    # 상설/정기 공연(공모 아님)
    r"상설공연", r"정기공연",
    r"\d{1,2}월\s*공연\s*안내",   # 9월 공연 안내
    r"\d{1,2}일\s*공연\s*안내",   # 13일 공연 안내
    r"공연\s*소개", r"공연\s*일정",
    # UI 잔여물
    r"^\s*(더보기|바로가기|하위메뉴)\s*$",
    r"홍보\s*추진",
]

FALLBACK_KEYWORDS = [
    "공모사업", "지원사업 공모", "전시 공모", "공연 공모",
    "지원금 공모", "작품 공모", "전시공간 지원", "모집 공고",
]


def normalize(text: str) -> str:
    text = re.sub(r'[\[\]<>【】《》「」『』\(\)\{\}·•★◆▶►≪≫～~]', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def rule_filter_debug(title: str) -> tuple:
    """
    규칙 기반 1차 필터
    Returns: (통과여부, 사유문자열)
    """
    raw_len = len(title.strip())
    if raw_len < 8:
        return False, f"너무 짧음({raw_len}자)"

    n = normalize(title)

    # 블랙리스트
    for p in BLACKLIST_PATTERNS:
        if re.search(p, n):
            return False, f"블랙리스트[{p}]"

    # 카테고리 (공연/전시/체험 등)
    cat_match = None
    for p in CATEGORY_WORDS:
        if re.search(p, n):
            cat_match = p
            break
    if not cat_match:
        return False, "카테고리어 없음(공연·전시·체험 없음)"

    # 목적어 (공모/지원사업/모집 등)
    pur_match = None
    for p in PURPOSE_WORDS:
        if re.search(p, n):
            pur_match = p
            break
    if not pur_match:
        return False, "목적어 없음(공모·지원사업·모집공고 없음)"

    return True, f"통과 [cat={cat_match} / pur={pur_match}]"


# ═══════════════════════════════════════════════════════════
# Gemini
# ═══════════════════════════════════════════════════════════

if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)
    gemini_model = genai.GenerativeModel("gemini-1.5-flash")
    print("✓ Gemini 연결됨")
else:
    gemini_model = None
    print("! Gemini 키 없음 → fallback 모드")

GEMINI_PROMPT = """다음 제목들은 1차 규칙 필터(공연/전시/체험 + 공모/지원사업/모집)를 통과했습니다.

[최종 판단 기준]
"문화예술 사업자·단체가 이 공고를 보고 신청서를 제출하여 지원금·공간·사업기회를 받을 수 있는가?"

✅ 포함해야 할 것:
- 전시/공연/체험 공간 또는 사업 지원금 공모
- 예술단체·작가·예술인 대상 창작지원 공모
- 작품 공모전 (단체·예술인이 출품 신청하는 것)
- 참여단체·예술인 모집 (사업자·단체 자격으로 신청)

❌ 제외해야 할 것:
- 일반 시민·관객 대상 체험/참가 모집
- 청소년·어린이 교육프로그램 수강생 모집
- 공연·전시 일정 안내, 소개글
- 결과발표, 선정공지, 첨부파일
- 행정조직명(TF, 추진단, 위원회)
- 영업/광고/홍보 관련

[제목 목록]
{titles}

포함할 번호만 쉼표로. 없으면 "없음". 이유 설명 금지.
예: 1,3,5"""


def gemini_filter(candidates: list) -> list:
    if not candidates:
        return []
    if not gemini_model:
        print("     [Gemini 없음] fallback 키워드 필터 적용")
        return [c for c in candidates if any(w in c["title"] for w in FALLBACK_KEYWORDS)]

    result = []
    for i in range(0, len(candidates), 25):
        batch = candidates[i:i+25]
        numbered = "\n".join(f"{j+1}. {c['title']}" for j, c in enumerate(batch))
        try:
            resp = gemini_model.generate_content(GEMINI_PROMPT.format(titles=numbered))
            text = resp.text.strip()
            print(f"     Gemini 응답: {text[:120]}")
            if "없음" in text and not re.search(r'\d', text):
                pass  # 없음
            else:
                for n in re.findall(r'\d+', text):
                    idx = int(n) - 1
                    if 0 <= idx < len(batch):
                        result.append(batch[idx])
            time.sleep(2)
        except Exception as e:
            print(f"     Gemini 오류: {e}")
            for c in batch:
                if any(w in c["title"] for w in FALLBACK_KEYWORDS):
                    result.append(c)
    return result


# ═══════════════════════════════════════════════════════════
# 사이트 목록 (type: "board" | "calendar")
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
    {"name": "위비티",                "url": "https://www.wevity.com/?c=find&s=1&gub=1",            "type": "board"},
    {"name": "국립아시아문화전당_공지", "url": "https://www.acc.go.kr/main/board/board.do?PID=0701&boardID=NOTICE", "type": "board"},
    {"name": "광주비엔날레",          "url": "https://www.gwangjubiennale.org/gb/notice.do",         "type": "board"},
    {"name": "전북문화관광재단",       "url": "https://www.jbct.or.kr/notice.php",                   "type": "board"},
    {"name": "전북문화관광재단_공모",  "url": "https://www.jbct.or.kr/c_notice.php",                 "type": "board"},
    # 순천 캘린더 (전용 스크래퍼 사용)
    {"name": "순천문화재단_공모캘린더","url": "https://www.cfsc.or.kr/contents/news/news0106.asp",   "type": "calendar"},
    {"name": "순천문화재단_타기관공모","url": "https://www.cfsc.or.kr/contents/open/open0501.asp",   "type": "board"},
    {"name": "목포문화재단_문화도시",  "url": "https://mpcf.or.kr/bbs/board.php?bo_table=notice&sca=%EB%AC%B8%ED%99%94%EB%8F%84%EC%8B%9C", "type": "board"},
    {"name": "목포문화재단_전체",      "url": "https://mpcf.or.kr/bbs/board.php?bo_table=notice",    "type": "board"},
]


# ═══════════════════════════════════════════════════════════
# 공통 유틸
# ═══════════════════════════════════════════════════════════

def clean(text: str) -> str:
    return re.sub(r'\s+', ' ', text.replace('\n', ' ')).strip()


def extract_deadline(text: str) -> str:
    """마감일 추출 (~ 또는 마감: 패턴)"""
    m = re.search(r'[~～]\s*(\d{2,4}[.\-/]\d{1,2}[.\-/]\d{1,2})', text)
    if m: return m.group(1)
    m = re.search(r'마감\s*:?\s*(\d{2,4}[.\-]\d{1,2}[.\-]\d{1,2})', text)
    if m: return m.group(1)
    dates = re.findall(r'\d{4}[.\-]\d{1,2}[.\-]\d{1,2}', text)
    return dates[-1] if len(dates) >= 2 else (dates[0] if dates else "")


# ═══════════════════════════════════════════════════════════
# 스크래퍼: 캘린더 전용 (cfsc 등 #! 링크 + strong 구조)
# ═══════════════════════════════════════════════════════════

def scrape_calendar(page, url: str, name: str) -> list:
    """
    캘린더형 사이트 전용.
    - 현재 연월 파라미터 자동 추가
    - <strong> 태그로 제목만 정확히 추출
    - 4초 대기로 JS 렌더링 보장
    """
    raw = []
    now = datetime.now()
    base_url = url.split("?")[0]
    cal_url  = f"{base_url}?y={now.year}&m={now.month}"

    print(f"     캘린더 URL: {cal_url}")
    try:
        page.goto(cal_url, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(4000)   # JS 렌더링 추가 대기

        # strong/b 태그가 나타날 때까지 대기 (최대 8초)
        try:
            page.wait_for_selector("ul li a strong, ul li a b", timeout=8000)
        except:
            print(f"     [경고] strong 요소 감지 실패, 계속 진행")

        seen  = set()
        count = 0

        # ── 방법 A: strong/b 태그 직접 순회 (가장 정확) ──
        strongs = page.query_selector_all("ul li a strong, ul li a b")
        print(f"     strong/b 요소 발견: {len(strongs)}개")

        for el in strongs:
            try:
                title = clean(el.inner_text())
                if len(title) < 8 or title in seen:
                    continue

                # 부모 li에서 날짜 추출
                li_text = el.evaluate(
                    "node => { const li = node.closest('li'); return li ? li.innerText : ''; }"
                )
                deadline = extract_deadline(li_text)

                seen.add(title)
                count += 1
                raw.append({
                    "name":     name,
                    "title":    title,
                    "site_url": url,   # 기본 URL (클릭 시 공모소식 목록으로 이동)
                    "deadline": deadline,
                })
            except Exception:
                pass

        # ── 방법 B: strong 못 잡으면 ul li a 전체 텍스트 → 정제 ──
        if not raw:
            print(f"     [fallback] ul li a 전체 텍스트 방식으로 전환")
            for li in page.query_selector_all("ul li"):
                try:
                    a = li.query_selector("a")
                    if not a: continue

                    full = clean(a.inner_text())
                    # 앞 카테고리 라벨 제거
                    title = re.sub(r'^(공모|전시|공연|교육|축제·?행사|체험)\s+', '', full)
                    # 날짜 이후 잘라냄
                    title = re.sub(r'\s*\d{4}-\d{2}-\d{2}.*$', '', title).strip()
                    title = title[:100]

                    if len(title) < 8 or title in seen:
                        continue
                    seen.add(title)
                    raw.append({
                        "name":     name,
                        "title":    title,
                        "site_url": url,
                        "deadline": extract_deadline(li.inner_text()),
                    })
                except Exception:
                    pass

        print(f"     캘린더 수집 완료: {count}개 (strong방식) / 총 {len(raw)}개")

    except Exception as e:
        print(f"     [{name}] 캘린더 오류: {e}")
        traceback.print_exc()

    return raw


# ═══════════════════════════════════════════════════════════
# 스크래퍼: 일반 게시판
# ═══════════════════════════════════════════════════════════

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
                a = next((c.query_selector("a") for c in cells[1:]
                          if c.query_selector("a")), None)
                if not a: continue
                title = clean(a.inner_text())[:100]
                if len(title) < 8: continue
                raw.append({
                    "name":     name,
                    "title":    title,
                    "site_url": url,
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
                    if len(title) < 8 or title in seen: continue
                    seen.add(title)
                    raw.append({
                        "name":     name,
                        "title":    title,
                        "site_url": url,
                        "deadline": extract_deadline(li.inner_text()),
                    })
                except Exception:
                    pass

        # ─ 전략 3: 전체 a 태그 (최후) ─
        if not raw:
            seen = set()
            for a in page.query_selector_all("a"):
                try:
                    title = clean(a.inner_text())[:100]
                    if len(title) < 8 or title in seen: continue
                    seen.add(title)
                    raw.append({
                        "name":     name,
                        "title":    title,
                        "site_url": url,
                        "deadline": "",
                    })
                except Exception:
                    pass

    except Exception as e:
        print(f"     [{name}] 오류: {e}")
    return raw


# ═══════════════════════════════════════════════════════════
# 전체 파이프라인
# ═══════════════════════════════════════════════════════════

def scrape_all() -> dict:
    all_posts = {}

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

            # 스크래핑
            if stype == "calendar":
                raw = scrape_calendar(page, url, name)
            else:
                raw = scrape_board(page, url, name)

            print(f"  수집: {len(raw)}개")

            # ── 1차 규칙 필터 ──
            rule_passed = []
            for item in raw:
                ok, reason = rule_filter_debug(item["title"])
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
                key = f"{name}|{item['title'][:50]}"
                all_posts[key] = item

        browser.close()
    return all_posts


# ═══════════════════════════════════════════════════════════
# known_posts 관리
# ═══════════════════════════════════════════════════════════

def load_known() -> dict:
    if os.path.exists(KNOWN_FILE):
        with open(KNOWN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_known(data: dict):
    with open(KNOWN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════
# ntfy 알림 전송
# ═══════════════════════════════════════════════════════════

def send_ntfy(title: str, body: str, click_url: str = ""):
    if not NTFY_TOPIC:
        return
    payload = {
        "topic":    NTFY_TOPIC,
        "title":    title,
        "message":  body,
        "priority": 4,
        "tags":     ["loudspeaker"],
    }
    if click_url:
        payload["click"] = click_url
    try:
        r = requests.post(
            f"{NTFY_SERVER}",
            json=payload,
            timeout=10,
            headers={"Content-Type": "application/json"},
        )
        icon = "✓" if r.status_code == 200 else f"✗({r.status_code})"
        print(f"  [{icon}] ntfy: {title}")
    except Exception as e:
        print(f"  [!] ntfy 오류: {e}")


# ═══════════════════════════════════════════════════════════
# 메인
# ═══════════════════════════════════════════════════════════

def main():
    print(f"\n공모사업 알림 봇 v6.2 | {datetime.now().strftime('%Y-%m-%d %H:%M KST')}")
    print("=" * 55)

    current = scrape_all()
    known   = load_known()

    new_items = {k: v for k, v in current.items() if k not in known}
    print(f"\n{'═'*55}")
    print(f"전체 공모 풀: {len(current)}건 | 신규: {len(new_items)}건")

    if new_items:
        # 사이트별 그룹핑
        by_site: dict[str, list] = {}
        for v in new_items.values():
            by_site.setdefault(v["name"], []).append(v)

        for site_name, items in by_site.items():
            list_url = items[0]["site_url"]

            if len(items) == 1:
                item = items[0]
                dl   = f"\n📅 마감: {item['deadline']}" if item.get("deadline") else ""
                send_ntfy(
                    f"📢 [{site_name}] 새 공모",
                    f"{item['title']}{dl}",
                    list_url,
                )
            else:
                lines = []
                for i in items:
                    line = f"• {i['title']}"
                    if i.get("deadline"):
                        line += f" (~{i['deadline']})"
                    lines.append(line)
                send_ntfy(
                    f"📢 [{site_name}] 새 공모 {len(items)}개",
                    "\n".join(lines),
                    list_url,
                )
    else:
        send_ntfy(
            "✅ 신규 공모 없음",
            f"확인 시각: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        )

    # known_posts 업데이트
    known.update(current)
    save_known(known)
    print("\n완료.")


if __name__ == "__main__":
    main()

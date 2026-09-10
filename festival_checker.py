#!/usr/bin/env python3
"""
공모사업 알림 봇 v6.0
1차: 규칙 기반 필터 (화이트리스트 AND + 블랙리스트)
2차: Gemini 최종 검증
클릭 시 → 목록 URL 고정
"""

import json, os, re, time
from datetime import datetime
from playwright.sync_api import sync_playwright
import requests
import google.generativeai as genai

# ══ 환경변수 ══
NTFY_TOPIC  = os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = "https://ntfy.sh"
KNOWN_FILE  = "known_posts.json"
GEMINI_KEY  = os.environ.get("GEMINI_API_KEY", "")

# ══ 필터링 규칙 ══════════════════════════════════════════════════════════

# 화이트리스트-1: 카테고리 (최소 1개 AND 필수)
CATEGORY_WORDS = [
    "공연", "전시", "체험", "박람회",
    "기획전", "전시회", "뮤지컬", "아트페어",
    "문화예술", "예술단체",
]

# 화이트리스트-2: 목적어 (최소 1개 AND 필수)
PURPOSE_WORDS = [
    "공모전", "공모사업", "공모", "지원사업", "지원금",
    "모집공고", "신청접수", "작품모집", "참여작가",
    r"예술가\s*모집", r"단체\s*모집", r"예술인\s*모집", r"작가\s*모집",
]

# 블랙리스트: 하나라도 있으면 즉시 제외 (Gemini도 건너뜀)
BLACKLIST = [
    # 단독 단어
    r"정산", r"안내서", r"추진단", r"첨부파일", r"합격", r"후기",
    r"행정심사", r"상설공연", r"정기공연", r"하위메뉴", r"더보기",
    r"TF\b", r"위원회", r"입장권", r"수강료",
    # 결과/선정 복합 패턴
    r"결과\s*발표", r"결과\s*공개", r"결과\s*안내", r"결과\s*공지",
    r"선정\s*결과", r"최종\s*선정", r"심사\s*결과", r"수상\s*결과",
    r"합격자?\s*발표", r"선정자\s*발표", r"\d+차\s*결과",
    # 일정/소개 복합 패턴
    r"공연\s*일정", r"공연\s*소개", r"전시\s*소개",
    r"콘서트\s*안내", r"행사\s*소개", r"프로그램\s*소개",
    r"일자별\s*[공연전시]",
    # 홍보성
    r"홍보\s*추진", r"사전\s*홍보",
]

# Gemini 없을 때 fallback (절대 놓치면 안 되는 키워드)
FALLBACK_KEYWORDS = [
    "공모사업", "지원사업 공모", "전시 공모", "공연 공모",
    "지원금 공모", "작품 공모", "전시공간 지원",
]


def normalize(text: str) -> str:
    """특수기호 제거 후 정규화 → [공모], <전시>, 「지원사업」 전부 처리"""
    text = re.sub(r'[\[\]<>【】《》「」『』\(\)\{\}·•★◆▶►≪≫]', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def has_any(text: str, patterns: list) -> bool:
    n = normalize(text)
    return any(re.search(p, n) for p in patterns)


def rule_filter(title: str) -> bool:
    """
    True  = 1차 통과 (Gemini 검증 대상)
    False = 즉시 제외
    """
    if len(title.strip()) < 10:
        return False
    if has_any(title, BLACKLIST):
        return False
    if not has_any(title, CATEGORY_WORDS):
        return False
    if not has_any(title, PURPOSE_WORDS):
        return False
    return True


# ══ Gemini 초기화 ══
if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)
    gemini_model = genai.GenerativeModel("gemini-1.5-flash")
    print("✓ Gemini 연결됨")
else:
    gemini_model = None
    print("! Gemini 키 없음 → fallback 모드")

GEMINI_PROMPT = """다음 제목들은 이미 1차 규칙 필터를 통과했습니다.
(공연/전시/체험 + 공모/지원사업 키워드 포함, 결과발표·첨부파일 등 제외 완료)

[최종 판단 기준]
"문화예술 사업자 또는 예술단체가 이 공고를 보고 신청서를 제출하여
지원금·공간·사업기회를 받을 수 있는가?"

✅ 포함 대상:
- 전시/공연/체험 공간 지원 공모
- 예술단체·예술가 대상 지원금·보조금 공모
- 창작·제작비 지원 공모
- 작품 공모전 (예술가/단체 신청)

❌ 제외 대상:
- 일반 시민·관객 참여 행사 (야행, 체험 관광, 시민 공연자 등)
- 청소년·어린이 교육 프로그램
- 단체명·위원회·기관 소개 (추진단, TF 등)
- 공연·전시 일정/홍보/안내
- 결과 통보, 선정 안내

[제목 목록]
{titles}

포함할 번호만 쉼표로. 없으면 "없음". 설명 절대 금지.
예: 1,3,5"""


def gemini_filter(candidates: list) -> list:
    if not candidates:
        return []
    if not gemini_model:
        return [c for c in candidates if any(w in c["title"] for w in FALLBACK_KEYWORDS)]

    result = []
    for i in range(0, len(candidates), 25):
        batch = candidates[i:i + 25]
        numbered = "\n".join(f"{j+1}. {c['title']}" for j, c in enumerate(batch))
        prompt = GEMINI_PROMPT.format(titles=numbered)
        try:
            resp = gemini_model.generate_content(prompt)
            text = resp.text.strip()
            print(f"     Gemini: {text[:60]}")
            if "없음" in text and not re.search(r'\d', text):
                pass
            else:
                for n in re.findall(r'\d+', text):
                    idx = int(n) - 1
                    if 0 <= idx < len(batch):
                        result.append(batch[idx])
            time.sleep(2)
        except Exception as e:
            print(f"     Gemini 오류: {e} → fallback")
            for c in batch:
                if any(w in c["title"] for w in FALLBACK_KEYWORDS):
                    result.append(c)
    return result


# ══ 사이트 목록 ══
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
    {"name": "순천문화재단_공모소식", "url": "https://www.cfsc.or.kr/contents/news/news0106.asp"},
]


# ══ 유틸리티 ══
def clean(text: str) -> str:
    return re.sub(r'\s+', ' ', text.replace("\n", " ")).strip()


def extract_deadline(row_text: str) -> str:
    """마감일 추출: "~날짜" 또는 "마감 날짜" 우선"""
    m = re.search(r'[~]\s*(\d{2,4}[.\-/]\d{1,2}[.\-/]\d{1,2})', row_text)
    if m:
        return m.group(1)
    m = re.search(r'마감\s*:?\s*(\d{2,4}[.\-]\d{1,2}[.\-]\d{1,2})', row_text)
    if m:
        return m.group(1)
    dates = re.findall(r'\d{4}[.\-]\d{1,2}[.\-]\d{1,2}', row_text)
    return dates[-1] if dates else ""


# ══ 스크래핑 ══
def scrape_site(page, url: str, name: str) -> list:
    raw = []
    try:
        page.goto(url, wait_until="networkidle", timeout=40000)
        page.wait_for_timeout(2000)

        # 전략1: 번호 있는 테이블 행 (번호셀 = 숫자인 tr만)
        rows = page.query_selector_all("table tbody tr")
        for row in rows:
            try:
                cells = row.query_selector_all("td")
                if len(cells) < 2:
                    continue
                if not re.match(r'^\d+$', cells[0].inner_text().strip()):
                    continue
                a = next((c.query_selector("a") for c in cells[1:] if c.query_selector("a")), None)
                if not a:
                    continue
                title = clean(a.inner_text())[:100]
                if len(title) < 5:
                    continue
                raw.append({"name": name, "title": title, "site_url": url,
                            "deadline": extract_deadline(row.inner_text())})
            except Exception:
                pass

        # 전략2: li 기반
        if not raw:
            seen = set()
            for li in page.query_selector_all("ul li, ol li"):
                try:
                    a = li.query_selector("a")
                    if not a:
                        continue
                    title = clean(a.inner_text())[:100]
                    if len(title) < 5 or title in seen:
                        continue
                    seen.add(title)
                    raw.append({"name": name, "title": title, "site_url": url,
                                "deadline": extract_deadline(li.inner_text())})
                except Exception:
                    pass

        # 전략3: 전체 a 태그 (최후 수단)
        if not raw:
            seen = set()
            for a in page.query_selector_all("a"):
                try:
                    title = clean(a.inner_text())[:100]
                    if len(title) < 5 or title in seen:
                        continue
                    seen.add(title)
                    raw.append({"name": name, "title": title, "site_url": url,
                                "deadline": ""})
                except Exception:
                    pass

    except Exception as e:
        print(f"     [{name}] 오류: {e}")
    return raw


def scrape_all() -> dict:
    all_posts = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
        ))
        for site in SITES:
            name, url = site["name"], site["url"]
            print(f"\n  [{name}]")
            raw = scrape_site(page, url, name)

            # 1차: 규칙 필터
            rule_passed = [item for item in raw if rule_filter(item["title"])]
            print(f"     수집:{len(raw)} → 규칙:{len(rule_passed)}", end="")

            # 2차: Gemini
            final = gemini_filter(rule_passed)
            print(f" → 최종:{len(final)}")

            for item in final:
                key = f"{name}|{item['title'][:50]}"
                all_posts[key] = item

        browser.close()
    return all_posts


# ══ known_posts 관리 ══
def load_known() -> dict:
    if os.path.exists(KNOWN_FILE):
        with open(KNOWN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_known(data: dict):
    with open(KNOWN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ══ 알림 ══
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
        payload["click"] = click_url   # 목록 URL 고정 → 클릭하면 공지 목록으로 이동
    try:
        r = requests.post(NTFY_SERVER, json=payload, timeout=10)
        print(f"  [{'✓' if r.status_code == 200 else '✗'}] {title}")
    except Exception as e:
        print(f"  [!] ntfy 오류: {e}")


# ══ 메인 ══
def main():
    print(f"\n공모사업 알림 봇 v6.0 | {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    current   = scrape_all()
    known     = load_known()
    new_items = {k: v for k, v in current.items() if k not in known}

    print(f"\n전체: {len(current)} | 신규: {len(new_items)}")

    if new_items:
        by_site: dict[str, list] = {}
        for v in new_items.values():
            by_site.setdefault(v["name"], []).append(v)

        for site_name, items in by_site.items():
            list_url = items[0]["site_url"]   # ← 항상 목록 URL

            if len(items) == 1:
                item = items[0]
                dl = f"\n📅 마감: {item['deadline']}" if item.get("deadline") else ""
                send_ntfy(
                    title=f"📢 [{site_name}] 새 공모",
                    body=f"{item['title']}{dl}",
                    click_url=list_url,
                )
            else:
                lines = [
                    f"• {i['title']}" + (f" (~{i['deadline']})" if i.get("deadline") else "")
                    for i in items
                ]
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

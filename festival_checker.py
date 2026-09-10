#!/usr/bin/env python3
"""
공모사업 알림 봇 - 키워드: 전시, 공연, 체험, 박람회
"""

import json, os, re
from datetime import datetime
from playwright.sync_api import sync_playwright
import requests

NTFY_TOPIC  = os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = "https://ntfy.sh"
KNOWN_FILE  = "known_posts.json"

KEYWORDS = ["전시", "공연", "체험", "박람회"]

POST_WORDS = ["모집", "공고", "선정", "결과", "신청", "공모전", "참가자",
              "참여자", "지원사업", "안내", "개최", "운영", "추진",
              "구매", "평가", "지원금", "제안서", "공모"]

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


def is_real_post(title):
    """실제 게시글인지 판별 - 시뮬레이션으로 도출한 최적 조건"""
    t = title.strip()

    # 1. 최소 길이 15자
    if len(t) < 15:
        return False

    # 2. 슬래시(/) 포함 → 카테고리 분류형 텍스트 제외
    if "/" in t:
        return False

    # 3. 중간점(·) 포함 → "교육·체험" 같은 카테고리 제외
    if "·" in t:
        return False

    # 4. 키워드 없으면 무조건 제외
    if not any(k in t for k in KEYWORDS):
        return False

    # 5. 연도(2024~2029) 포함 OR (공모성단어 + 20자 이상)
    has_year = bool(re.search(r'20(2[4-9])', t))
    has_post_word = any(w in t for w in POST_WORDS)

    if has_year:
        return True
    if has_post_word and len(t) >= 20:
        return True

    return False


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


def clean_title(text):
    t = text.strip().replace("\n", " ")
    return re.sub(r'\s+', ' ', t).strip()


def try_numbered_rows(page, site_url, name):
    """
    게시판 번호(숫자)가 있는 행만 추출 - 가장 정확한 방법
    일반 게시판은 첫 번째 td가 번호(숫자)
    """
    posts = {}
    rows = page.query_selector_all("table tbody tr")
    for row in rows:
        try:
            cells = row.query_selector_all("td")
            if len(cells) < 2:
                continue

            # 첫 번째 셀이 숫자(게시글 번호)인지 확인
            first_text = cells[0].inner_text().strip()
            if not re.match(r'^\d+$', first_text):
                continue  # 번호 없으면 메뉴/헤더행 → 제외

            # 제목 셀에서 링크 찾기
            title_el = None
            for cell in cells[1:]:
                title_el = cell.query_selector("a")
                if title_el:
                    break
            if not title_el:
                continue

            title = clean_title(title_el.inner_text())
            if not is_real_post(title):
                continue

            deadline = extract_deadline(row.inner_text())
            post_id  = f"{name}|{title[:50]}"
            posts[post_id] = {
                "name": name,
                "title": title[:80],
                "deadline": deadline,
                "site_url": site_url,
                "first_seen": datetime.now().strftime("%Y-%m-%d"),
            }
        except Exception:
            pass
    return posts


def try_list_items(page, site_url, name):
    """li 기반 게시판"""
    posts = {}
    selectors = [
        "ul.board_list li", "ul.list li", "ol li",
        ".board-list li", ".post-list li", "li.item"
    ]
    for sel in selectors:
        items = page.query_selector_all(sel)
        if not items:
            continue
        for item in items:
            try:
                title_el = item.query_selector("a")
                if not title_el:
                    continue
                title = clean_title(title_el.inner_text())
                if not is_real_post(title):
                    continue
                deadline = extract_deadline(item.inner_text())
                post_id  = f"{name}|{title[:50]}"
                posts[post_id] = {
                    "name": name,
                    "title": title[:80],
                    "deadline": deadline,
                    "site_url": site_url,
                    "first_seen": datetime.now().strftime("%Y-%m-%d"),
                }
            except Exception:
                pass
        if posts:
            break
    return posts


def try_all_links(page, site_url, name):
    """마지막 수단: 전체 링크에서 필터링"""
    posts = {}
    for el in page.query_selector_all("a"):
        try:
            title = clean_title(el.inner_text())
            if not is_real_post(title):
                continue
            post_id = f"{name}|{title[:50]}"
            posts[post_id] = {
                "name": name,
                "title": title[:80],
                "deadline": "",
                "site_url": site_url,
                "first_seen": datetime.now().strftime("%Y-%m-%d"),
            }
        except Exception:
            pass
    return posts


def scrape_site(page, site):
    url  = site["url"]
    name = site["name"]
    posts = {}

    try:
        page.goto(url, wait_until="networkidle", timeout=40000)
        page.wait_for_timeout(2000)

        # 전략 1: 번호 있는 행 (가장 정확)
        posts = try_numbered_rows(page, url, name)

        # 전략 2: li 기반
        if not posts:
            posts = try_list_items(page, url, name)

        # 전략 3: 전체 링크 (마지막 수단)
        if not posts:
            posts = try_all_links(page, url, name)

    except Exception as e:
        print(f"     [!] {name} 오류: {e}")

    return posts


def scrape_all():
    all_posts = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
        ))
        for site in SITES:
            print(f"  ↳ [{site['name']}] 스캔 중...")
            posts = scrape_site(page, site)
            print(f"     → {len(posts)}개 감지")
            all_posts.update(posts)
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
        print("[!] NTFY_TOPIC 미설정")
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

    r = requests.post(
        NTFY_SERVER,
        json=payload,
        timeout=10,
    )
    print(f"  [{'✓' if r.status_code==200 else '✗'}] {title}")


def main():
    print(f"\n공모사업 알림 봇 시작: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    current   = scrape_all()
    known     = load_known()
    new_items = {k: v for k, v in current.items() if k not in known}

    print(f"\n전체: {len(current)}개 | 신규: {len(new_items)}개")

    if new_items:
        by_site = {}
        for v in new_items.values():
            by_site.setdefault(v["name"], []).append(v)

        for site_name, items in by_site.items():
            list_url = items[0]["site_url"]  # 항상 목록 URL 고정

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

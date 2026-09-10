#!/usr/bin/env python3
"""
공모사업 알림 봇 - 키워드: 전시, 공연, 체험, 박람회
"""

import json, os, re
from datetime import datetime
from playwright.sync_api import sync_playwright
import requests

# ── 설정 ─────────────────────────────────────────────────────────────────
NTFY_TOPIC  = os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = "https://ntfy.sh"
KNOWN_FILE  = "known_posts.json"

KEYWORDS = ["전시", "공연", "체험", "박람회"]

SITES = [
    {"name": "광주문화재단",       "url": "https://www.gctf.or.kr/web/board/1/postList"},
    {"name": "MLDC",              "url": "https://mldc.kr/notice"},
    {"name": "미마프",             "url": "http://www.mimaf.net/xe/index.php?mid=notice"},
    {"name": "한국문화예술교육진흥원", "url": "https://www.kh.or.kr/brd/board/644/L/SITES/100/menu/371?brdCodeField=SITES&brdCodeValue=100"},
    {"name": "리콜렉션",           "url": "https://recollection.kr/bbs/board.php?bo_table=notice&page=1"},
    {"name": "전주문화재단",        "url": "https://www.jge.go.kr/jgemain/na/ntt/selectNttList.do?mi=2116&bbsId=1123"},
    {"name": "나주문화재단",        "url": "https://www.njcf.or.kr/www/community/notices"},
    {"name": "담양문화재단",        "url": "https://www.damyangcf.or.kr/user/board/lists/board_cd/4010"},
    {"name": "전남문화재단_타기관",  "url": "https://www.jncf.or.kr/jact/open/otherevents.do"},
    {"name": "전남문화재단_협업",    "url": "https://www.jncf.or.kr/jact/open/collusion.do"},
    {"name": "전남문화재단_공지",    "url": "https://www.jncf.or.kr/jact/open/notice.do"},
    {"name": "광주문화재단_공지",    "url": "https://www.gjcf.or.kr/cf/news/notice.do"},
    {"name": "고양문화재단",        "url": "https://www.gtcc.or.kr/bbs/board.php?bo_table=info&page=1"},
    {"name": "문화예술",            "url": "http://xn--9p4b13eb4bd6i.com/notice"},
    {"name": "국립아시아문화전당",   "url": "https://www.ncas.or.kr/board/contest/list?menuNo=&currentPageNo=1&searchCondition="},
    {"name": "위비티",              "url": "https://www.wevity.com/?c=find&s=1&gub=1"},
    {"name": "국립아시아문화전당_공지","url": "https://www.acc.go.kr/main/board/board.do?PID=0701&boardID=NOTICE"},
    {"name": "광주비엔날레",        "url": "https://www.gwangjubiennale.org/gb/notice.do"},
    {"name": "전북문화관광재단",     "url": "https://www.jbct.or.kr/notice.php"},
    {"name": "전북문화관광재단_공모","url": "https://www.jbct.or.kr/c_notice.php"},
    {"name": "순천문화재단",        "url": "https://www.cfsc.or.kr/contents/open/open0501.asp"},
    {"name": "목포문화재단",        "url": "https://mpcf.or.kr/bbs/board.php?bo_table=notice&sca=%EB%AC%B8%ED%99%94%EB%8F%84%EC%8B%9C"},
    {"name": "목포문화재단_전체",    "url": "https://mpcf.or.kr/bbs/board.php?bo_table=notice"},
]
# ─────────────────────────────────────────────────────────────────────────


def has_keyword(text):
    return any(k in text for k in KEYWORDS)


def make_abs(href, base_url):
    if not href or href.startswith("javascript") or href.strip() == "#":
        return base_url
    if href.startswith("http"):
        return href
    from urllib.parse import urljoin
    return urljoin(base_url, href)


def extract_deadline(text):
    patterns = [
        r'\d{4}[.\-]\d{1,2}[.\-]\d{1,2}',
        r'\d{4}년\s*\d{1,2}월\s*\d{1,2}일',
        r'\d{1,2}[.\-]\d{1,2}[.\-]\d{2,4}',
    ]
    found = []
    for pat in patterns:
        found += re.findall(pat, text)
    return found[-1] if found else ""


def scrape_site(page, site):
    posts = {}
    url = site["url"]
    name = site["name"]

    try:
        page.goto(url, wait_until="networkidle", timeout=40000)
        page.wait_for_timeout(2000)

        # 1. 테이블 행 방식 시도
        rows = page.query_selector_all("table tbody tr")
        if rows:
            for row in rows:
                try:
                    title_el = row.query_selector("td a, a")
                    if not title_el:
                        continue
                    title = title_el.inner_text().strip().replace("\n", " ")
                    if not title or len(title) < 4:
                        continue
                    if not has_keyword(title):
                        continue
                    href = title_el.get_attribute("href") or ""
                    row_text = row.inner_text()
                    deadline = extract_deadline(row_text)
                    post_id = f"{name}|{title[:50]}"
                    posts[post_id] = {
                        "name": name,
                        "title": title[:80],
                        "deadline": deadline,
                        "url": make_abs(href, url),
                        "first_seen": datetime.now().strftime("%Y-%m-%d"),
                    }
                except Exception:
                    pass

        # 2. 리스트(li) 방식 시도
        if not posts:
            items = page.query_selector_all("ul li, ol li, .list-item, .board-item")
            for item in items:
                try:
                    title_el = item.query_selector("a")
                    if not title_el:
                        continue
                    title = title_el.inner_text().strip().replace("\n", " ")
                    if not title or len(title) < 4:
                        continue
                    if not has_keyword(title):
                        continue
                    href = title_el.get_attribute("href") or ""
                    item_text = item.inner_text()
                    deadline = extract_deadline(item_text)
                    post_id = f"{name}|{title[:50]}"
                    posts[post_id] = {
                        "name": name,
                        "title": title[:80],
                        "deadline": deadline,
                        "url": make_abs(href, url),
                        "first_seen": datetime.now().strftime("%Y-%m-%d"),
                    }
                except Exception:
                    pass

        # 3. 전체 링크 방식 (마지막 수단)
        if not posts:
            for el in page.query_selector_all("a"):
                try:
                    title = el.inner_text().strip().replace("\n", " ")
                    if not title or len(title) < 6 or len(title) > 100:
                        continue
                    if not has_keyword(title):
                        continue
                    href = el.get_attribute("href") or ""
                    post_id = f"{name}|{title[:50]}"
                    posts[post_id] = {
                        "name": name,
                        "title": title[:80],
                        "deadline": "",
                        "url": make_abs(href, url),
                        "first_seen": datetime.now().strftime("%Y-%m-%d"),
                    }
                except Exception:
                    pass

    except Exception as e:
        print(f"     [!] 오류: {e}")

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
    headers = {
        "Title": title.encode("utf-8"),
        "Priority": "high",
        "Tags": "loudspeaker",
    }
    if click_url:
        headers["Click"] = click_url
    r = requests.post(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers=headers,
        timeout=10,
    )
    print(f"  [{'✓' if r.status_code==200 else '✗'}] {title}")


def main():
    print(f"\n공모사업 알림 봇 시작: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    current = scrape_all()
    known = load_known()
    new_items = {k: v for k, v in current.items() if k not in known}

    print(f"\n전체: {len(current)}개 | 신규: {len(new_items)}개")

    if new_items:
        by_site = {}
        for v in new_items.values():
            by_site.setdefault(v["name"], []).append(v)

        for site_name, items in by_site.items():
            if len(items) == 1:
                item = items[0]
                deadline_str = f"\n📅 마감: {item['deadline']}" if item.get("deadline") else ""
                send_ntfy(
                    title=f"📢 [{site_name}] 새 공모",
                    body=f"{item['title']}{deadline_str}",
                    click_url=item["url"],
                )
            else:
                lines = []
                for i in items[:10]:
                    line = f"• {i['title']}"
                    if i.get("deadline"):
                        line += f" (~{i['deadline']})"
                    lines.append(line)
                if len(items) > 10:
                    lines.append(f"… 외 {len(items)-10}개")
                send_ntfy(
                    title=f"📢 [{site_name}] 새 공모 {len(items)}개",
                    body="\n".join(lines),
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

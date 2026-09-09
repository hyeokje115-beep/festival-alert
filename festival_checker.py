#!/usr/bin/env python3
"""
다중 사이트 공모사업 알림 봇
새 게시물이 등록되면 ntfy.sh를 통해 핸드폰으로 알림을 보냅니다.
"""

import json, os, re
from datetime import datetime
from playwright.sync_api import sync_playwright
import requests

# ── 설정 ─────────────────────────────────────────────────────────────────
NTFY_TOPIC  = os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = "https://ntfy.sh"
KNOWN_FILE  = "known_posts.json"
KEYWORDS    = ["축제", "전시", "체험", "공연"]

SITES = [
    {
        "name": "아트누리",
        "url": "https://www.artnuri.or.kr/crawler/info/search.do?key=2301170002",
        "type": "generic",
    },
    {
        "name": "문화포털 지원사업",
        "url": "https://www.culture.go.kr/portal/cltBnf/cltSup/list.do?pageIndex=1&menuNo=200104&hidSubType=&sPeriod=&sWord=&searchPageUnit=10&chkBDatas=&chkSDatas=&chkGDatas=&chkRDatas=&searchFldCd=&sSdate=&sEdate=&searchSttscd=S&sSort=2&clctInstSeCd=&trgtInstCd=CT00000&clctFldCd=",
        "type": "generic",
    },
    {
        "name": "보조사업포털",
        "url": "https://www.bojo.go.kr/bojo.do",
        "type": "bojo",   # 키워드 검색 필요
    },
    {
        "name": "경기도문화재단",
        "url": "https://gdctf.or.kr/front/M0000184/article/list.do?pageIndex=1&cateId=&atcId=&searchType=title&searchKeyword=",
        "type": "generic",
    },
]
# ─────────────────────────────────────────────────────────────────────────


def match_keyword(text):
    return any(k in text for k in KEYWORDS)


def scrape_generic(page, site):
    """공통 게시판 스크래퍼 - 링크+제목 추출"""
    posts = {}
    page.goto(site["url"], wait_until="networkidle", timeout=40000)
    page.wait_for_timeout(2000)

    # 모든 링크 수집
    for el in page.query_selector_all("a"):
        try:
            title = el.inner_text().strip().replace("\n", " ")
            href  = el.get_attribute("href") or ""
            if not title or len(title) < 4 or not match_keyword(title):
                continue

            # 고유 ID 생성
            post_id = f"{site['name']}|{href or title[:30]}"
            posts[post_id] = {
                "name": site["name"],
                "title": title[:80],
                "url": make_abs(href, site["url"]),
                "first_seen": datetime.now().strftime("%Y-%m-%d"),
            }
        except Exception:
            pass
    return posts


def scrape_bojo(page, site):
    """보조사업포털 - 키워드별 검색"""
    posts = {}
    for kw in KEYWORDS:
        try:
            search_url = f"https://www.bojo.go.kr/bojo.do?menuNo=1000&searchWord={kw}"
            page.goto(search_url, wait_until="networkidle", timeout=40000)
            page.wait_for_timeout(2000)

            for el in page.query_selector_all("a"):
                title = el.inner_text().strip().replace("\n", " ")
                href  = el.get_attribute("href") or ""
                if not title or len(title) < 4:
                    continue
                post_id = f"보조사업포털|{href or title[:30]}"
                posts[post_id] = {
                    "name": "보조사업포털",
                    "title": title[:80],
                    "url": make_abs(href, site["url"]),
                    "first_seen": datetime.now().strftime("%Y-%m-%d"),
                }
        except Exception as e:
            print(f"  [!] bojo 키워드 '{kw}' 오류: {e}")
    return posts


def make_abs(href, base):
    if not href:
        return base
    if href.startswith("http"):
        return href
    from urllib.parse import urljoin
    return urljoin(base, href)


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
            try:
                if site["type"] == "bojo":
                    posts = scrape_bojo(page, site)
                else:
                    posts = scrape_generic(page, site)
                print(f"     → {len(posts)}개 감지")
                all_posts.update(posts)
            except Exception as e:
                print(f"     [!] 오류: {e}")
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
    print(f"  [{'✓' if r.status_code==200 else '✗'}] 알림 전송: {title}")


def main():
    print(f"\n공모사업 알림 봇 시작: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    current = scrape_all()
    known   = load_known()
    new_items = {k: v for k, v in current.items() if k not in known}

    print(f"\n전체: {len(current)}개 | 신규: {len(new_items)}개")

    if new_items:
        # 사이트별로 묶어서 알림
        by_site = {}
        for v in new_items.values():
            by_site.setdefault(v["name"], []).append(v)

        for site_name, items in by_site.items():
            if len(items) == 1:
                item = items[0]
                send_ntfy(
                    title=f"📢 [{site_name}] 새 공모",
                    body=item["title"],
                    click_url=item["url"],
                )
            else:
                body = "\n".join(f"• {i['title']}" for i in items[:10])
                if len(items) > 10:
                    body += f"\n… 외 {len(items)-10}개"
                send_ntfy(
                    title=f"📢 [{site_name}] 새 공모 {len(items)}개",
                    body=body,
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

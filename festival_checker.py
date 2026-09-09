#!/usr/bin/env python3
"""
다중 사이트 공모사업 알림 봇
새 게시물이 등록되면 ntfy.sh를 통해 핸드폰으로 알림을 보냅니다.
"""

import json, os
from datetime import datetime
from playwright.sync_api import sync_playwright
import requests

# ── 설정 ─────────────────────────────────────────────────────────────────
NTFY_TOPIC  = os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = "https://ntfy.sh"
KNOWN_FILE  = "known_posts.json"
KEYWORDS    = ["축제", "전시", "체험", "공연", "지원사업", "모집", "공모"]

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
        "url": "https://www.bojo.go.kr/bojo.do?menuNo=1000",
        "type": "bojo",
    },
    {
        "name": "경기도문화재단",
        "url": "https://gdctf.or.kr/front/M0000184/article/list.do?pageIndex=1&cateId=&atcId=&searchType=title&searchKeyword=",
        "type": "generic",
    },
]
# ─────────────────────────────────────────────────────────────────────────

def match_keyword(text):
    # 메뉴/네비 제외: 너무 짧거나 연도/공고 없으면 제외
    if len(text) < 8:
        return False
    
    # 실제 공모 제목 패턴
    post_keywords = ["공고", "모집", "신청", "선정", "공모", "지원사업", "축제", "전시", "체험", "공연"]
    has_post_keyword = any(k in text for k in post_keywords)
    
    # 연도 포함 여부
    has_year = any(str(y) in text for y in [2024, 2025, 2026])
    
    # 메뉴성 텍스트 제외
    menu_words = ["찾기", "바로가기", "다운로드", "안내", "통계", "캘린더", "목록", "로그인", "회원가입"]
    is_menu = any(m in text for m in menu_words)
    
    if is_menu:
        return False
    
    return has_post_keyword or has_year

def make_abs(href, base):
    if not href or href.startswith("javascript"):
        return base
    if href.startswith("http"):
        return href
    from urllib.parse import urljoin
    return urljoin(base, href)


def scrape_generic(page, site):
    """공통 게시판 스크래퍼"""
    posts = {}
    page.goto(site["url"], wait_until="networkidle", timeout=40000)
    page.wait_for_timeout(2000)

    for el in page.query_selector_all("a"):
        try:
            title = el.inner_text().strip().replace("\n", " ")
            href  = el.get_attribute("href") or ""
            if not title or len(title) < 4 or not match_keyword(title):
                continue

            # 마감일 추출 시도
            deadline = ""
            try:
                parent = el.evaluate_handle("el => el.closest('tr, li, div.item')")
                if parent:
                    spans = parent.query_selector_all("td, span, p")
                    for span in spans:
                        t = span.inner_text().strip()
                        if ("~" in t or "까지" in t) and ("." in t or "-" in t):
                            deadline = t[:30]
                            break
            except Exception:
                pass

            post_id = f"{site['name']}|{href or title[:40]}"
            posts[post_id] = {
                "name": site["name"],
                "title": title[:80],
                "deadline": deadline,
                "url": make_abs(href, site["url"]),
                "first_seen": datetime.now().strftime("%Y-%m-%d"),
            }
        except Exception:
            pass
    return posts


def scrape_bojo(page, site):
    """보조사업포털 - 키워드 자동 입력 후 검색"""
    posts = {}
    for kw in KEYWORDS:
        try:
            print(f"     bojo 키워드 '{kw}' 검색 중...")
            page.goto("https://www.bojo.go.kr/bojo.do?menuNo=1000",
                      wait_until="networkidle", timeout=40000)
            page.wait_for_timeout(2000)

            # 검색창 키워드 입력
            search_input = page.query_selector(
                "input[name='searchWord'], input[placeholder*='공모'], input[type='text']"
            )
            if search_input:
                search_input.fill(kw)
                page.wait_for_timeout(500)

            # 조회 버튼 클릭
            search_btn = page.query_selector(
                "button.btn_search, button:has-text('조회'), input[type='submit']"
            )
            if search_btn:
                search_btn.click()
                page.wait_for_timeout(3000)

            # 결과 목록 파싱
            rows = page.query_selector_all("table tbody tr, .list_area li, .board_list li")
            for row in rows:
                try:
                    title_el = row.query_selector("td.subject a, .tit a, a.title, td a")
                    if not title_el:
                        continue
                    title = title_el.inner_text().strip()
                    href  = title_el.get_attribute("href") or ""
                    if not title or len(title) < 4:
                        continue

                    # 마감일 추출
                    deadline = ""
                    for td in row.query_selector_all("td, span, p"):
                        t = td.inner_text().strip()
                        if ("~" in t or "까지" in t) and ("." in t or "-" in t):
                            deadline = t[:30]
                            break

                    post_id = f"보조사업포털|{title[:40]}"
                    posts[post_id] = {
                        "name": "보조사업포털",
                        "title": title[:80],
                        "deadline": deadline,
                        "url": make_abs(href, "https://www.bojo.go.kr"),
                        "first_seen": datetime.now().strftime("%Y-%m-%d"),
                    }
                except Exception:
                    pass

        except Exception as e:
            print(f"  [!] bojo 키워드 '{kw}' 오류: {e}")
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
                body = "\n".join(
                    f"• {i['title']}" + (f" (~{i['deadline'][-10:]})" if i.get("deadline") else "")
                    for i in items[:10]
                )
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

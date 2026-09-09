#!/usr/bin/env python3
"""
다중 사이트 공모사업 알림 봇
"""

import json, os, re
from datetime import datetime
from playwright.sync_api import sync_playwright
import requests

# ── 설정 ─────────────────────────────────────────────────────────────────
NTFY_TOPIC  = os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = "https://ntfy.sh"
KNOWN_FILE  = "known_posts.json"

# 공모 관련 키워드 (제목에 하나라도 있으면 감지)
POST_KEYWORDS = ["공고", "모집", "신청", "선정", "공모", "지원사업",
                 "축제", "전시", "체험", "공연", "지원금", "보조금"]

# 메뉴/네비 제외 키워드 (이것만 있으면 제외)
MENU_WORDS = ["찾기", "바로가기", "다운로드", "통계", "캘린더",
              "로그인", "회원가입", "마이페이지", "사이트맵"]

SITES = [
    {
        "name": "아트누리",
        "url": "https://www.artnuri.or.kr/crawler/info/search.do?key=2301170002",
        "type": "generic",
        "base": "https://www.artnuri.or.kr",
    },
    {
        "name": "문화포털 지원사업",
        "url": "https://www.culture.go.kr/portal/cltBnf/cltSup/list.do?pageIndex=1&menuNo=200104&hidSubType=&sPeriod=&sWord=&searchPageUnit=10&chkBDatas=&chkSDatas=&chkGDatas=&chkRDatas=&searchFldCd=&sSdate=&sEdate=&searchSttscd=S&sSort=2&clctInstSeCd=&trgtInstCd=CT00000&clctFldCd=",
        "type": "culture",
        "base": "https://www.culture.go.kr",
    },
    {
        "name": "보조사업포털",
        "url": "https://www.bojo.go.kr/bojo.do?menuNo=1000",
        "type": "bojo",
        "base": "https://www.bojo.go.kr",
    },
    {
        "name": "경기도문화재단",
        "url": "https://gdctf.or.kr/front/M0000184/article/list.do?pageIndex=1&cateId=&atcId=&searchType=title&searchKeyword=",
        "type": "generic",
        "base": "https://gdctf.or.kr",
    },
]
# ─────────────────────────────────────────────────────────────────────────


def is_valid_post(text):
    if len(text) < 6:
        return False
    # 메뉴성 텍스트만으로 이루어진 경우 제외
    if all(m in text for m in MENU_WORDS[:3]):
        return False
    # 공모 키워드 하나라도 포함
    return any(k in text for k in POST_KEYWORDS)


def make_abs(href, base):
    if not href or href.startswith("javascript") or href == "#":
        return base
    if href.startswith("http"):
        return href
    from urllib.parse import urljoin
    return urljoin(base, href)


def extract_deadline(text):
    """날짜 패턴 추출"""
    patterns = [
        r'\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}',  # 2025.08.31
        r'\d{4}년\s*\d{1,2}월\s*\d{1,2}일',    # 2025년 8월 31일
    ]
    for pat in patterns:
        matches = re.findall(pat, text)
        if matches:
            return matches[-1]  # 보통 마지막 날짜가 마감일
    return ""


def scrape_culture(page, site):
    """문화포털 전용 스크래퍼 - 목록 테이블 직접 파싱"""
    posts = {}
    try:
        page.goto(site["url"], wait_until="networkidle", timeout=40000)
        page.wait_for_timeout(3000)

        # 목록 행 탐색
        rows = page.query_selector_all("table tbody tr, ul.list li, .bd-list li, .support-list li")
        print(f"     문화포털 rows: {len(rows)}개")

        for row in rows:
            try:
                # 제목 링크 찾기
                title_el = row.query_selector("a")
                if not title_el:
                    continue
                title = title_el.inner_text().strip().replace("\n", " ")
                href  = title_el.get_attribute("href") or ""

                if not title or len(title) < 4:
                    continue

                # 행 전체 텍스트에서 마감일 추출
                row_text = row.inner_text()
                deadline = extract_deadline(row_text)

                post_id = f"{site['name']}|{title[:40]}"
                posts[post_id] = {
                    "name": site["name"],
                    "title": title[:80],
                    "deadline": deadline,
                    "url": make_abs(href, site["base"]),
                    "first_seen": datetime.now().strftime("%Y-%m-%d"),
                }
            except Exception:
                pass

        # rows가 0이면 일반 링크 방식으로 폴백
        if not posts:
            for el in page.query_selector_all("a"):
                try:
                    title = el.inner_text().strip().replace("\n", " ")
                    href  = el.get_attribute("href") or ""
                    if not title or len(title) < 6:
                        continue
                    if not is_valid_post(title):
                        continue
                    post_id = f"{site['name']}|{title[:40]}"
                    posts[post_id] = {
                        "name": site["name"],
                        "title": title[:80],
                        "deadline": "",
                        "url": make_abs(href, site["base"]),
                        "first_seen": datetime.now().strftime("%Y-%m-%d"),
                    }
                except Exception:
                    pass

    except Exception as e:
        print(f"  [!] 문화포털 오류: {e}")
    return posts


def scrape_generic(page, site):
    """공통 게시판 스크래퍼"""
    posts = {}
    page.goto(site["url"], wait_until="networkidle", timeout=40000)
    page.wait_for_timeout(2000)

    # 테이블 행 우선 시도
    rows = page.query_selector_all("table tbody tr, .board-list tr, ul.list li")
    if rows:
        for row in rows:
            try:
                title_el = row.query_selector("a")
                if not title_el:
                    continue
                title = title_el.inner_text().strip().replace("\n", " ")
                href  = title_el.get_attribute("href") or ""
                if not title or len(title) < 6:
                    continue
                if not is_valid_post(title):
                    continue
                row_text = row.inner_text()
                deadline = extract_deadline(row_text)
                post_id = f"{site['name']}|{title[:40]}"
                posts[post_id] = {
                    "name": site["name"],
                    "title": title[:80],
                    "deadline": deadline,
                    "url": make_abs(href, site["base"]),
                    "first_seen": datetime.now().strftime("%Y-%m-%d"),
                }
            except Exception:
                pass

    # 테이블 없으면 전체 링크 방식
    if not posts:
        for el in page.query_selector_all("a"):
            try:
                title = el.inner_text().strip().replace("\n", " ")
                href  = el.get_attribute("href") or ""
                if not title or len(title) < 6:
                    continue
                if not is_valid_post(title):
                    continue
                post_id = f"{site['name']}|{title[:40]}"
                posts[post_id] = {
                    "name": site["name"],
                    "title": title[:80],
                    "deadline": "",
                    "url": make_abs(href, site["base"]),
                    "first_seen": datetime.now().strftime("%Y-%m-%d"),
                }
            except Exception:
                pass

    return posts


def scrape_bojo(page, site):
    """보조사업포털 - 키워드 자동 입력 후 검색"""
    posts = {}
    for kw in ["축제", "공연", "전시", "체험"]:
        try:
            print(f"     bojo 키워드 '{kw}' 검색 중...")
            page.goto("https://www.bojo.go.kr/bojo.do?menuNo=1000",
                      wait_until="networkidle", timeout=40000)
            page.wait_for_timeout(2000)

            # 검색창 입력
            search_input = page.query_selector("input[name='searchWord'], input[type='text']")
            if search_input:
                search_input.click()
                search_input.fill("")
                search_input.type(kw)
                page.wait_for_timeout(500)

            # 조회 버튼 클릭
            search_btn = page.query_selector("button:has-text('조회'), .btn_search, input[type='submit']")
            if search_btn:
                search_btn.click()
                page.wait_for_timeout(3000)

            # 결과 파싱
            rows = page.query_selector_all("table tbody tr")
            for row in rows:
                try:
                    title_el = row.query_selector("td a, a")
                    if not title_el:
                        continue
                    title = title_el.inner_text().strip()
                    href  = title_el.get_attribute("href") or ""
                    if not title or len(title) < 4:
                        continue

                    row_text = row.inner_text()
                    deadline = extract_deadline(row_text)

                    post_id = f"보조사업포털|{kw}|{title[:40]}"
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
            print(f"  [!] bojo '{kw}' 오류: {e}")
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
                elif site["type"] == "culture":
                    posts = scrape_culture(page, site)
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
                body_lines = []
                for i in items[:10]:
                    line = f"• {i['title']}"
                    if i.get("deadline"):
                        line += f" (~{i['deadline']})"
                    body_lines.append(line)
                if len(items) > 10:
                    body_lines.append(f"… 외 {len(items)-10}개")
                send_ntfy(
                    title=f"📢 [{site_name}] 새 공모 {len(items)}개",
                    body="\n".join(body_lines),
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

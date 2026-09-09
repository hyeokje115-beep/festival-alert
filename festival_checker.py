#!/usr/bin/env python3
"""
visitkorea 축제 캘린더 신규 등록 알림 봇
새 축제가 등록되면 ntfy.sh를 통해 핸드폰으로 알림을 보냅니다.
"""

import json, os, sys
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright
import requests

# ── 설정 ─────────────────────────────────────────────────────────────────
NTFY_TOPIC  = os.environ.get("NTFY_TOPIC", "")   # GitHub Secret에서 주입
NTFY_SERVER = "https://ntfy.sh"
KNOWN_FILE  = "known_festivals.json"
BASE_URL    = "https://korean.visitkorea.or.kr/kfes/list/festivalCalendar.do"
LOOK_AHEAD_MONTHS = 3   # 현재 달 포함 몇 달 앞까지 체크
# ─────────────────────────────────────────────────────────────────────────


def get_months_to_check():
    now = datetime.now()
    return [(
        (now + timedelta(days=30 * i)).year,
        (now + timedelta(days=30 * i)).month
    ) for i in range(LOOK_AHEAD_MONTHS + 1)]


def scrape_all_festivals(months):
    festivals = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
        ))
        for year, month in months:
            url = f"{BASE_URL}?selYearMonth={year}{month:02d}"
            print(f"  ↳ {year}년 {month}월 스캔 중...")
            page.goto(url, wait_until="networkidle", timeout=40_000)

            # 각 날짜 클릭 → 해당 날 축제 로드 → 파싱
            day_cells = page.query_selector_all("td")
            clicked = set()
            for cell in day_cells:
                txt = cell.inner_text().strip()[:2]
                if not txt.isdigit() or txt in clicked:
                    continue
                clicked.add(txt)
                try:
                    cell.click()
                    page.wait_for_timeout(700)
                    festivals.update(_parse_cards(page))
                except Exception:
                    pass

            festivals.update(_parse_cards(page))
        browser.close()
    return festivals


def _parse_cards(page):
    result = {}
    for link in page.query_selector_all("a[href*='fstvlCntntsId']"):
        href = link.get_attribute("href") or ""
        fid = ""
        for seg in href.replace("?", "&").split("&"):
            if seg.startswith("fstvlCntntsId="):
                fid = seg.split("=", 1)[1]
                break
        if not fid or fid in result:
            continue

        name = period = region = ""
        try:
            el = link.query_selector("strong, .tit, h3, h4")
            name = (el or link).inner_text().strip()[:60]
        except Exception:
            pass
        try:
            for span in link.query_selector_all("span, p, li, em"):
                t = span.inner_text().strip()
                if "~" in t and "." in t and not period:
                    period = t
                elif any(k in t for k in ["도 ", "시 ", "군 ", "구 "]) and not region:
                    region = t
        except Exception:
            pass

        result[fid] = {
            "name": name or f"축제_{fid[:8]}",
            "period": period,
            "region": region,
            "url": f"https://korean.visitkorea.or.kr/kfes/detail/fstvlDetail.do?fstvlCntntsId={fid}",
            "first_seen": datetime.now().strftime("%Y-%m-%d"),
        }
    return result


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
        "Priority": "default",
        "Tags": "tada",
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
    print(f"\n축제 알림 봇 시작: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    months  = get_months_to_check()
    current = scrape_all_festivals(months)
    known   = load_known()
    new_items = {k: v for k, v in current.items() if k not in known}

    print(f"전체: {len(current)}개 | 신규: {len(new_items)}개")

    if new_items:
        vals = list(new_items.values())
        if len(vals) == 1:
            f = vals[0]
            send_ntfy(
                title=f"🎉 새 축제: {f['name']}",
                body=f"{f['period']}\n{f['region']}",
                click_url=f["url"],
            )
        else:
            summary = "\n".join(
                f"• {v['name']} ({v['region'] or v['period'][:12]})"
                for v in vals[:10]
            )
            if len(vals) > 10:
                summary += f"\n… 외 {len(vals)-10}개"
            send_ntfy(
                title=f"🎉 새 축제 {len(vals)}개 등록!",
                body=summary,
                click_url=BASE_URL,
            )

    known.update(current)
    save_known(known)
    print("완료")


if __name__ == "__main__":
    main()

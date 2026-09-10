#!/usr/bin/env python3
"""
공모사업 알림 봇 v5.0
- 키워드 사전 필터 제거, Gemini가 단독 판단
- 공모/지원사업 신청 가능한 공고만 선별
"""

import json, os, re, time
from datetime import datetime
from playwright.sync_api import sync_playwright
import requests
import google.generativeai as genai

NTFY_TOPIC  = os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = "https://ntfy.sh"
KNOWN_FILE  = "known_posts.json"
GEMINI_KEY  = os.environ.get("GEMINI_API_KEY", "")

# Gemini 오류 시 최소 안전망 (절대 놓치면 안 되는 단어)
FALLBACK_WORDS = ["공모사업", "지원사업", "공모전", "공모 신청", "신청 접수",
                  "지원금", "지원 공모", "공연 공모", "전시 공모", "작품 공모"]

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

# ── Gemini 초기화 ─────────────────────────────────────────────────────────
if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)
    gemini = genai.GenerativeModel("gemini-1.5-flash")
    print("✓ Gemini 연결됨")
else:
    gemini = None
    print("! Gemini 키 없음")


def build_prompt(numbered_titles):
    return f"""당신은 문화예술 공모사업 전문 큐레이터입니다.

[대상자]
전시·공연·체험 사업을 운영하는 문화예술 종사자(사업자/예술단체)

[선별 목적]
이 사람이 "사업계획서 또는 신청서를 제출하여 지원금·공간·사업 기회를 받을 수 있는" 공모·지원사업 공고만 추출

━━━ 반드시 포함 ✅ ━━━
- 전시·공연·체험·박람회 분야 지원금/보조금/공간 공모사업
- 예술단체·공연단체 대상 사업 공모
- 창작지원·제작비·유통지원 공모
- 예술가·단체가 사업 수행자로 선발되는 공모
- 공연·전시 참여 예술가/단체 모집 (사업자 자격)

━━━ 절대 제외 ❌ ━━━
- 일반 시민·관객 참여 체험/행사 (야행, AR체험, 국가유산 체험, 시민 참여)
- 공연·전시 일정/소개/안내 (콘서트 안내, 프로그램 소개)
- 결과 발표 단독 공지 (선정 결과, 합격자 발표, 수상 결과)
- 첨부파일 공지 (결과 첨부파일, 모집 첨부파일)
- 청소년·어린이 교육/체험 프로그램
- 상설 프로그램명 (토요상설공연, 정기공연, 정월대보름 행사)
- 메뉴명·카테고리·UI텍스트 (더보기, 하위메뉴)
- 홍보·사전홍보·추진 안내
- TF·추진단·위원회 등 조직명
- 행사 홍보성 소개 (축제 소개, 박람회 홍보)

━━━ 실제 판단 예시 ━━━
❌ "담양 국가유산 야행 체험 '아침의 숨' 참여자 모집" → 시민 체험행사
❌ "명량해전 AR을 체험해보세요" → 관광 홍보
❌ "2026 목요콘서트 9월 공연 안내" → 일정 안내
❌ "토요상설공연 작품 공모 첨부파일" → 결과 첨부파일
❌ "토요상설공연 '토요 음향사' 선정 결과 첨부파일" → 결과 첨부
❌ "ACC 아시아 예술체험" → 교육 프로그램명
❌ "ACC 청소년 전시연계교육" → 청소년 교육
❌ "창제작 어린이·청소년 공연" → 프로그램명
❌ "2025 명량대첩 일자별 공연소개" → 프로그램 소개
❌ "공연예술대관료 지원사업추진단" → 조직명
❌ "민간 공연장 활성화 지원TF" → TF 조직명
❌ "서울국제관광박람회 사전 홍보 추진" → 홍보
❌ "광주지역 대표 공연 콘텐츠 유통 활성화 지원 최종 선정 결과" → 결과 발표
❌ "희경루 풍류소리 8회차 공연 안내" → 공연 일정
❌ "토요상설공연 정월대보름 한마당" → 행사

✅ "2026 나빌레라 문화센터 전시공간 지원사업 공모" → 공간 지원 공모
✅ "공연 창작 지원금 공모 신청 접수" → 창작 지원 공모
✅ "전시작가 공모전" → 작품 공모
✅ "문화예술단체 공모사업 모집" → 공모사업
✅ "2026 공연예술 제작 지원사업" → 지원사업
✅ "전시·공연 분야 사업자 공모" → 공모

━━━ 핵심 판단 기준 ━━━
"문화예술 사업자·단체가 이 공고를 보고 신청서를 제출하여 지원을 받을 수 있는가?"
→ YES → 포함 / NO → 제외

[제목 목록]
{numbered_titles}

응답: 포함 번호만 쉼표로. 없으면 "없음". 설명 절대 금지.
예: 2,5,8"""


def gemini_filter(candidates):
    """Gemini로 공모사업 공고만 선별 (30개씩 배치)"""
    if not candidates:
        return []
    if not gemini:
        # Gemini 없으면 fallback 단어 기반
        return [c for c in candidates if any(w in c["title"] for w in FALLBACK_WORDS)]

    result = []
    batch_size = 25  # 25개씩 처리 (토큰 안전)

    for i in range(0, len(candidates), batch_size):
        batch = candidates[i:i + batch_size]
        titles = [c["title"] for c in batch]
        numbered = "\n".join(f"{j+1}. {t}" for j, t in enumerate(titles))

        try:
            prompt = build_prompt(numbered)
            resp = gemini.generate_content(prompt)
            text = resp.text.strip()
            print(f"     Gemini({i//batch_size+1}): {text[:80]}")

            if "없음" in text and not re.search(r'\d', text):
                pass  # 진짜 없음
            else:
                valid = set()
                for n in re.findall(r'\d+', text):
                    valid.add(int(n))
                for idx in valid:
                    if 1 <= idx <= len(batch):
                        result.append(batch[idx - 1])

            time.sleep(2)  # API 레이트 리밋

        except Exception as e:
            print(f"     Gemini 오류: {e} → fallback 필터 적용")
            # 오류 시 강한 공모 단어 있는 것만 (놓치지 않으려는 안전망)
            for c in batch:
                if any(w in c["title"] for w in FALLBACK_WORDS):
                    result.append(c)

    return result


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


def minimal_filter(title):
    """명백한 UI 쓰레기만 제거 (최소한만)"""
    t = title.strip()
    if len(t) < 8:
        return False
    # 슬래시 2개 이상: 콘서트/전시회/공연 형태
    if t.count("/") >= 2:
        return False
    # UI 텍스트
    for w in ["하위메뉴", "더보기", "TOP", "PREV", "NEXT", "로그인", "회원가입"]:
        if w in t:
            return False
    return True


def scrape_site(page, site_url, name):
    """게시판 전체 수집 (키워드 필터 없음)"""
    candidates = []

    try:
        page.goto(site_url, wait_until="networkidle", timeout=40000)
        page.wait_for_timeout(2000)

        # 전략1: 번호 있는 테이블 행 (가장 정확)
        rows = page.query_selector_all("table tbody tr")
        for row in rows:
            try:
                cells = row.query_selector_all("td")
                if len(cells) < 2:
                    continue
                first = cells[0].inner_text().strip()
                # 첫 셀이 숫자여야 게시글 행
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
                if not minimal_filter(title):
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
            seen = set()
            for item in page.query_selector_all("ul li, ol li"):
                try:
                    title_el = item.query_selector("a")
                    if not title_el:
                        continue
                    title = clean(title_el.inner_text())
                    if not minimal_filter(title) or title in seen:
                        continue
                    seen.add(title)
                    candidates.append({
                        "name": name,
                        "title": title[:80],
                        "deadline": extract_deadline(item.inner_text()),
                        "site_url": site_url,
                        "first_seen": datetime.now().strftime("%Y-%m-%d"),
                    })
                except Exception:
                    pass

        # 전략3: 전체 링크 (마지막 수단)
        if not candidates:
            seen = set()
            for el in page.query_selector_all("a"):
                try:
                    title = clean(el.inner_text())
                    if not minimal_filter(title) or title in seen:
                        continue
                    seen.add(title)
                    candidates.append({
                        "name": name,
                        "title": title[:80],
                        "deadline": "",
                        "site_url": site_url,
                        "first_seen": datetime.now().strftime("%Y-%m-%d"),
                    })
                except Exception:
                    pass

    except Exception as e:
        print(f"     [!] {name} 스크래핑 오류: {e}")

    return candidates


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

            candidates = scrape_site(page, url, name)
            print(f"     수집: {len(candidates)}개")

            if not candidates:
                continue

            # Gemini 필터 (핵심)
            filtered = gemini_filter(candidates)
            print(f"     최종: {len(filtered)}개")

            for item in filtered:
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
        "topic":    NTFY_TOPIC,
        "title":    title,
        "message":  body,
        "priority": 4,
        "tags":     ["loudspeaker"],
    }
    if click_url:
        payload["click"] = click_url
    r = requests.post(NTFY_SERVER, json=payload, timeout=10)
    print(f"  [{'✓' if r.status_code == 200 else '✗'}] {title}")


def main():
    print(f"\n공모사업 알림 봇 v5.0: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

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

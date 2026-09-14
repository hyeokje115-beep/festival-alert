# -*- coding: utf-8 -*-
"""
prefilter.py — 1차 키워드 필터 · 게시글/링크 판별 · 날짜(게시일·접수기간·마감) 추출   [2단계 / 4]


Gemini 를 부르기 전에 "볼 필요 없는 것" 을 걸러내고, 알림에 표시할 날짜를 뽑는다.
표준 라이브러리만 사용. config 의 키워드 목록을 그대로 읽는다.


  is_non_post_link(text, href)     첨부파일 · 메뉴/커뮤니티 버튼 · 페이지 이동 링크 → 게시글 아님
  classify_title(title)            제목만으로 1차 판정  (passed / body_required)
  classify_with_body(title, body)  '본문 필요' 판정을 본문으로 마무리
  body_says_closed(body)           본문에 "접수가 마감되었습니다" 류 문구
  extract_dates(text, title)       접수 시작 · 마감 · 원문 문구 · 상시모집 여부
  parse_list_date(cell)            목록 날짜 셀 ("2025.05.01", "25-05-01", "05-01", "14:30")
  is_expired / days_left / format_deadline
  title_signature(title)           사이트가 달라도 같은 공고를 묶는 서명 (교차 중복 제거용)


규칙
  1) 링크 텍스트/href 가 첨부·버튼·페이지이동·배지면 게시글 아님
  2) 제목에 "[마감]" "[종료]" "마감되었" 표기 → 탈락
  3) 제목에 제외어(EXCLUDE) → 탈락.  단 '약한 제외어'(개최 안내·행사 안내·초대 …)만 걸리고
     강한 공모 신호어(공모·모집공고·접수기간·지원사업 …)가 함께 있으면 살려서 Gemini 에 넘김
     예) "청년작가 공모전 개최 안내" 통과 / "공모 선정결과 안내" 탈락
  4) 제목에 분야어(전시·공연·체험 …) → 통과
  5) 제목에 공모 신호어만 있음 → 본문에 분야어가 있어야 통과 (classify_with_body)
  6) 비교는 공백 제거 + 소문자 ("공연 일정" == "공연일정"). 제외어는 제목에만 적용(본문엔 '결과'·'안내'가 흔함)


로컬 점검
  python prefilter.py                                 내장 예시로 규칙 확인
  python prefilter.py "제목1" "제목2" ...             제목 판정
  python prefilter.py --date "접수기간: 2025. 5. 1. ~ 5. 20."   날짜 추출 확인
"""
from __future__ import annotations


import re
import sys
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Optional
from urllib.parse import urlsplit


import config




# ─────────────────────────────────────────────────────────────────────
# 1. 정규화 · 키워드 준비
# ─────────────────────────────────────────────────────────────────────
def norm(text) -> str:
    """비교용: NFKC 정규화 + 소문자 + 공백 제거"""
    t = unicodedata.normalize("NFKC", str(text or "")).lower()
    return re.sub(r"\s+", "", t)




def _prep(keywords) -> list[tuple[str, str]]:
    seen, out = set(), []
    for kw in keywords:
        n = norm(kw)
        if n and n not in seen:
            seen.add(n)
            out.append((kw, n))
    out.sort(key=lambda p: -len(p[1]))          # 긴 키워드 먼저 보고
    return out




_DOMAIN = _prep(config.DOMAIN_KEYWORDS)
_CALL = _prep(config.CALL_KEYWORDS)
_EXCLUDE = _prep(config.EXCLUDE_KEYWORDS)
_NON_POST_TEXT = {norm(x) for x in config.NON_POST_LINK_TEXT}


# 강한 공모 신호어와 함께 있으면 탈락시키지 않는 '약한' 제외어 (최종 판단은 Gemini)
SOFT_EXCLUDE = {norm(k) for k in ("개최 안내", "진행 안내", "행사 안내", "프로그램 안내", "초대")}
STRONG_CALL = {norm(k) for k in (
    "공모", "모집공고", "접수기간", "신청서 접수", "제안서 접수", "지원사업", "지원신청", "참가신청서",
    "사업자 모집", "운영업체 모집", "운영단체 모집", "위탁업체 모집", "참여기업 모집", "참여단체 모집",
)}




def find_keywords(text, prepared: list[tuple[str, str]]) -> list[str]:
    n = norm(text)
    return [kw for kw, nk in prepared if nk in n]




def has_strong_call(text) -> bool:
    n = norm(text)
    return any(k in n for k in STRONG_CALL)




# ─────────────────────────────────────────────────────────────────────
# 2. 제목 정리 · 마감 표기
# ─────────────────────────────────────────────────────────────────────
_BADGE_TOKENS = {"new", "n", "hot", "h", "새글", "신규", "공지", "notice", "필독", "top", "update", "updated"}
_TRAIL_COUNT_RE = re.compile(r"\s*[\[\(]\s*\d{1,4}\s*[\]\)]\s*$")          # 댓글/조회 수 "[3]" "(12)"
_CLOSED_TAG_RE = re.compile(r"[\[\(【<]\s*(?:접수|모집|신청|공모)?\s*(?:마감|종료|완료)\s*(?:됨)?\s*[\]\)】>]")
_CLOSED_TITLE_PHRASES = tuple(norm(p) for p in (
    "접수 마감되었", "접수가 마감", "모집이 마감", "모집 종료", "접수 종료",
    "마감되었", "종료되었", "마감됨", "종료됨", "마감된 공고",
))
_CLOSED_BODY_PHRASES = tuple(norm(p) for p in (
    "접수가 마감되었", "접수 마감되었", "모집이 마감되었", "모집 마감되었", "접수가 종료되었",
    "모집이 종료되었", "마감된 공고입니다", "종료된 공고입니다", "접수기간이 종료", "접수기간이 지났",
))




def clean_title(title) -> str:
    t = unicodedata.normalize("NFKC", str(title or ""))
    t = re.sub(r"\s+", " ", t).strip()
    t = _TRAIL_COUNT_RE.sub("", t)
    toks = t.split(" ")
    while toks and toks[0].lower() in _BADGE_TOKENS:
        toks.pop(0)
    while toks and toks[-1].lower() in _BADGE_TOKENS:
        toks.pop()
    return " ".join(toks).strip()




def is_closed_title(title) -> bool:
    t = str(title or "")
    n = norm(t)
    return bool(_CLOSED_TAG_RE.search(t)) or any(p in n for p in _CLOSED_TITLE_PHRASES)




def body_says_closed(body) -> bool:
    n = norm(str(body or "")[:3000])
    return any(p in n for p in _CLOSED_BODY_PHRASES)




# ─────────────────────────────────────────────────────────────────────
# 3. 게시글이 아닌 링크
# ─────────────────────────────────────────────────────────────────────
_BADGE_RE = re.compile(r"^(?:new|n|hot|h|top|notice|공지|필독|새글|\d+)$", re.I)




def is_non_post_link(text, href: str = "") -> tuple[bool, str]:
    """(게시글 아님 여부, 이유). 첨부파일 · 메뉴/커뮤니티 버튼 · 페이지 번호 · 빈 제목 등."""
    t = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(text or ""))).strip()
    hl = str(href or "").strip().lower()
    if hl.startswith(config.NON_POST_HREF_PREFIX):
        return True, "mailto/tel 링크"
    if "://" in hl:
        p = urlsplit(hl)
        pq = p.path + (f"?{p.query}" if p.query else "")
    else:
        pq = hl
    path_only = pq.split("?", 1)[0].split("#", 1)[0]
    if path_only.endswith(config.NON_POST_HREF_EXT):
        return True, "첨부파일 링크"
    for hint in config.NON_POST_HREF_HINT:
        if hint in pq:
            return True, f"href 에 '{hint}'"
    n = norm(t)
    if not n:
        return True, "링크 텍스트 없음"
    if n in _NON_POST_TEXT:
        return True, "메뉴/버튼 텍스트"
    if _BADGE_RE.match(t):
        return True, "배지/페이지 번호"
    if len(n) < config.MIN_TITLE_LEN:
        return True, f"제목이 너무 짧음({len(n)}자)"
    return False, ""




# ─────────────────────────────────────────────────────────────────────
# 4. 1차 판정
# ─────────────────────────────────────────────────────────────────────
@dataclass
class PrefilterResult:
    passed: bool                      # 1차 통과 (Gemini 로 넘길 대상)
    stage: str                        # closed / exclude / domain / call_only / call_body / none
    body_required: bool = False       # True = 본문을 읽어 classify_with_body 로 마무리해야 함
    matched_domain: list = field(default_factory=list)
    matched_call: list = field(default_factory=list)
    matched_exclude: list = field(default_factory=list)
    reason: str = ""
    score: int = 0                    # 통과 글 우선순위 (Gemini 호출 한도 초과 시 높은 것부터)


    def to_dict(self) -> dict:
        return asdict(self)




def classify_title(title) -> PrefilterResult:
    t = clean_title(title)
    if len(norm(t)) < config.MIN_TITLE_LEN:
        return PrefilterResult(False, "none", reason="제목 없음/너무 짧음")
    if is_closed_title(t):
        return PrefilterResult(False, "closed", reason="제목에 마감/종료 표기")


    domain = find_keywords(t, _DOMAIN)
    call = find_keywords(t, _CALL)
    excl = find_keywords(t, _EXCLUDE)
    strong = has_strong_call(t)


    if excl:
        hard = [k for k in excl if norm(k) not in SOFT_EXCLUDE]
        if hard or not strong:
            return PrefilterResult(False, "exclude", False, domain, call, excl,
                                   reason="제외어: " + ", ".join((hard or excl)[:3]))


    score = (2 if domain else 0) + (3 if call else 0) + (1 if strong else 0)
    if domain:
        reason = "분야어: " + ", ".join(domain[:3])
        if call:
            reason += " / 신호어: " + ", ".join(call[:3])
        if excl:
            reason += " / 약한 제외어 무시: " + ", ".join(excl[:2])
        return PrefilterResult(True, "domain", False, domain, call, excl, reason, score)
    if call:
        return PrefilterResult(False, "call_only", True, domain, call, excl,
                               "신호어만 있음(" + ", ".join(call[:3]) + ") → 본문에서 분야어 확인 필요", score)
    return PrefilterResult(False, "none", reason="분야어·신호어 없음")




def classify_with_body(title, body) -> PrefilterResult:
    """제목 판정이 '본문 필요' 일 때 본문 앞부분에서 분야어를 찾아 마무리."""
    base = classify_title(title)
    if base.passed or not base.body_required:
        return base
    text = str(body or "")
    if not text.strip():
        base.body_required = False
        base.reason += " (본문 없음 → 탈락)"
        return base
    domain = find_keywords(text[:8000], _DOMAIN)
    if domain:
        return PrefilterResult(True, "call_body", False, domain, base.matched_call, base.matched_exclude,
                               f"신호어 {', '.join(base.matched_call[:2])} + 본문 분야어 {', '.join(domain[:3])}",
                               base.score + 2)
    return PrefilterResult(False, "call_only", False, [], base.matched_call, base.matched_exclude,
                           "본문에 분야어(전시·공연·체험) 없음", base.score)




# ─────────────────────────────────────────────────────────────────────
# 5. 날짜 · 마감
# ─────────────────────────────────────────────────────────────────────
# 연도 없는 날짜 "5.20" "5. 20." "5월 20일"  (config.DATE_REGEX_NOYEAR 보다 한국식 끝 마침표를 허용)
_NOYEAR_RE = re.compile(r"(?<![\d.])(\d{1,2})\s*(?:[./]|월)\s*(\d{1,2})(?!\d)\s*일?")
_YY_DATE_RE = re.compile(r"(?<!\d)(\d{2})\s*[.\-/]\s*(\d{1,2})\s*[.\-/]\s*(\d{1,2})(?!\d)")   # 25.05.01
_MD_DASH_RE = re.compile(r"^(\d{1,2})[-./](\d{1,2})\.?$")                                    # 05-01
_TIME_ONLY_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")
_UNIT_AFTER = ("억", "만", "원", "%", "천", "배", "점", "㎡", "평", "kg", "cm", "km", "회", "개")
_ALWAYS_OPEN_RE = re.compile(
    r"상시\s*(?:모집|접수|공모)|수시\s*(?:모집|접수|공모)|예산\s*소진\s*시|소진\s*시\s*까지|연중\s*(?:상시|접수|모집)")
_TAIL_RE = re.compile(
    r"\s*(?:[(（][^)）\n]{0,3}[)）])?\s*(?:\d{1,2}\s*:\s*\d{2}|\d{1,2}\s*시(?:\s*\d{1,2}\s*분)?)?\s*(?:까지|마감)?")
_HINT_RE_CACHE: dict[str, re.Pattern] = {}




def _iso(y, m, d) -> Optional[str]:
    try:
        y, m, d = int(y), int(m), int(d)
        if y < 100:
            y += 2000
        return date(y, m, d).isoformat()
    except (TypeError, ValueError):
        return None




def _plus_year(iso: str) -> str:
    d = date.fromisoformat(iso)
    try:
        return d.replace(year=d.year + 1).isoformat()
    except ValueError:                                   # 2월 29일
        return d.replace(year=d.year + 1, day=28).isoformat()




def _hint_re(h: str) -> re.Pattern:
    r = _HINT_RE_CACHE.get(h)
    if r is None:
        r = re.compile(re.escape(h).replace("\\ ", "\\s*"))   # "접수 기간" → 공백 유무 모두 허용
        _HINT_RE_CACHE[h] = r
    return r




def _hint_kind(h: str) -> str:
    n = norm(h)
    if "까지" in n:
        return "until"
    if "마감" in n or "기한" in n:
        return "deadline"
    return "period"




def find_dates(text, *, allow_noyear: bool = False, default_year: Optional[int] = None) -> list[tuple[int, int, str]]:
    """(시작, 끝, 'YYYY-MM-DD') 목록, 위치순.
    allow_noyear=True 면 '5.20' '5월 20일' 도 인식 — 연도는 앞에 나온 날짜의 연도, 없으면 default_year/올해."""
    text = unicodedata.normalize("NFKC", str(text or ""))
    found: list[tuple[int, int, str]] = []
    for m in config.DATE_REGEX.finditer(text):
        iso = _iso(*m.groups())
        if iso:
            found.append((m.start(), m.end(), iso))
    if not allow_noyear:
        return found
    today = config.now_kst().date()
    anchored = bool(found)
    base_year = default_year or (int(found[0][2][:4]) if found else today.year)
    for m in _NOYEAR_RE.finditer(text):
        if any(s <= m.start() < e for s, e, _ in found):
            continue
        tail = text[m.end():].lstrip()[:2]
        if tail.startswith(_UNIT_AFTER):                 # "3.5억" 같은 숫자 제외
            continue
        prev = [iso for s, e, iso in found if e <= m.start()]
        year = int(prev[-1][:4]) if prev else base_year
        iso = _iso(year, m.group(1), m.group(2))
        if not iso:
            continue
        if not anchored and default_year is None and date.fromisoformat(iso) < today - timedelta(days=60):
            iso = _plus_year(iso)                        # 12월에 "~1.15" → 내년
        found.append((m.start(), m.end(), iso))
    found.sort()
    return found




def parse_date(text, *, allow_noyear: bool = False) -> str:
    """텍스트에서 첫 날짜 하나 → 'YYYY-MM-DD' (없으면 '')"""
    text = unicodedata.normalize("NFKC", str(text or ""))
    found = find_dates(text)
    if found:
        return found[0][2]
    m = _YY_DATE_RE.search(text)
    if m:
        iso = _iso(*m.groups())
        if iso:
            return iso
    if allow_noyear:
        found = find_dates(text, allow_noyear=True)
        if found:
            return found[0][2]
    return ""




def parse_list_date(cell) -> str:
    """게시판 목록의 날짜 셀. '14:30'(오늘 글) · '05-01'(올해) 형식도 처리."""
    t = unicodedata.normalize("NFKC", str(cell or "")).strip()
    if not t:
        return ""
    iso = parse_date(t, allow_noyear=True)
    today = config.now_kst().date()
    if not iso:
        if _TIME_ONLY_RE.match(t):
            return today.isoformat()
        m = _MD_DASH_RE.match(t)
        if m:
            iso = _iso(today.year, m.group(1), m.group(2)) or ""
    if iso and date.fromisoformat(iso) > today + timedelta(days=7) and not config.DATE_REGEX.search(t):
        d = date.fromisoformat(iso)                      # 연도 없는 미래 날짜 → 작년 글
        iso = d.replace(year=d.year - 1).isoformat() if not (d.month == 2 and d.day == 29) else ""
    return iso




def _snippet(text: str, a: int, b: int) -> str:
    m = _TAIL_RE.match(text, b)                          # 뒤따르는 "(화) 18:00까지" 포함
    end = m.end() if m else b
    return re.sub(r"\s+", " ", text[a:end]).strip(" :：~-–")[:120]




def _sanity(out: dict) -> dict:
    today = config.now_kst().date()
    for k in ("start", "deadline"):
        if out[k]:
            try:
                if date.fromisoformat(out[k]) > today + timedelta(days=730):
                    out[k] = ""
            except ValueError:
                out[k] = ""
    if out["start"] and out["deadline"] and out["deadline"] < out["start"]:
        out["start"] = ""
    return out




def extract_dates(text, title: str = "") -> dict:
    """접수기간/마감 추출.
    반환 {"start", "deadline": 'YYYY-MM-DD'|'', "period_text": 원문 문구, "always_open": bool, "source": str}
    우선순위: ① 힌트어(접수기간·모집기간·접수마감…) 뒤 창  ② "날짜 + 까지/마감"  ③ 제목의 "~날짜" """
    body = unicodedata.normalize("NFKC", str(text or ""))
    ttl = unicodedata.normalize("NFKC", str(title or "")).strip()
    full = f"{ttl}\n{body}" if ttl else body
    out = {"start": "", "deadline": "", "period_text": "",
           "always_open": bool(_ALWAYS_OPEN_RE.search(full)), "source": ""}
    if not full.strip():
        return out


    # ① 힌트어 — 기간형(접수기간…) 을 마감형(접수마감·마감…) 보다 먼저 본다
    hits = []
    for h in config.DEADLINE_HINT_WORDS:
        kind = _hint_kind(h)
        if kind == "until":
            continue
        for m in _hint_re(h).finditer(full):
            hits.append((0 if kind == "period" else 1, m.start(), m.end(), h, kind))
    hits.sort()
    used: set[int] = set()
    for _, hs, he, h, kind in hits:
        if hs in used:
            continue
        used.add(hs)
        generic = norm(h) == "마감"                      # "마감" 단독은 짧은 창 + 바로 뒤 날짜만
        window = full[he:he + (25 if generic else 45 if kind == "deadline" else 90)]
        dates = find_dates(window, allow_noyear=True)
        if not dates or (generic and dates[0][0] > 12):
            continue
        start = deadline = ""
        if kind == "deadline":
            deadline, last_end = dates[0][2], dates[0][1]
        elif len(dates) >= 2:
            start, deadline, last_end = dates[0][2], dates[1][2], dates[1][1]
            if deadline < start:
                deadline = _plus_year(deadline)          # 12.20 ~ 1.10
        else:
            s, e, iso = dates[0]
            if "부터" in window[e:e + 6]:
                start, last_end = iso, e
            else:
                deadline, last_end = iso, e
        out.update(start=start, deadline=deadline, period_text=_snippet(full, hs, he + last_end),
                   source=f"hint:{h}")
        return _sanity(out)


    # ② "2025.5.20(화) 18:00까지" / "2025.5.20 마감"
    for s, e, iso in find_dates(full):
        m = _TAIL_RE.match(full, e)
        if m and re.search(r"까지|마감", full[e:m.end()]):
            out.update(deadline=iso, period_text=_snippet(full, s, e), source="until")
            return _sanity(out)


    # ③ 제목 "(~5.20)" "5.1 ~ 5.20"
    if ttl and "~" in ttl:
        tpos = ttl.index("~")
        dates = find_dates(ttl, allow_noyear=True)
        after = [d for d in dates if d[0] > tpos]
        before = [d for d in dates if d[1] <= tpos]
        if after:
            a = before[-1][0] if before else tpos
            out.update(deadline=after[0][2], start=before[-1][2] if before else "",
                       period_text=_snippet(ttl, a, after[0][1]), source="title")
    return _sanity(out)




def days_left(deadline) -> Optional[int]:
    if not deadline:
        return None
    try:
        return (date.fromisoformat(str(deadline)) - config.now_kst().date()).days
    except ValueError:
        return None




def is_expired(deadline, grace_days: Optional[int] = None) -> bool:
    """마감이 지났는지. grace 0 = 마감 당일까지 허용 (config.EXPIRED_GRACE_DAYS)"""
    n = days_left(deadline)
    if n is None:
        return False
    g = config.EXPIRED_GRACE_DAYS if grace_days is None else grace_days
    return n < -g




def format_deadline(deadline) -> str:
    n = days_left(deadline)
    if n is None:
        return "미확인"
    if n < 0:
        return f"{deadline} (마감 지남)"
    if n == 0:
        return f"{deadline} (오늘 마감)"
    return f"{deadline} (D-{n})"




# ─────────────────────────────────────────────────────────────────────
# 6. 교차 사이트 중복용 서명
# ─────────────────────────────────────────────────────────────────────
def title_signature(title) -> str:
    """'[공고] 2025 ○○ 공모 (~5.20)' 과 '2025 ○○ 공모' 를 같은 공고로 보기 위한 서명"""
    t = unicodedata.normalize("NFKC", str(title or "")).lower()
    t = re.sub(r"[\[\(【][^\]\)】]{0,20}[\]\)】]", " ", t)     # 괄호 태그 제거
    t = re.sub(r"[^0-9a-z가-힣]+", "", t)
    return t[:80]




# ─────────────────────────────────────────────────────────────────────
# 7. 로컬 점검 CLI
# ─────────────────────────────────────────────────────────────────────
_DEMO_TITLES = [
    "2025 하반기 기획전시 참여작가 공모",
    "2025 문화예술 지원사업 공모 결과 발표",
    "어린이 체험자 모집 안내",
    "5월 공연 일정 안내",
    "청년작가 공모전 개최 안내",
    "2025 지역문화 콘텐츠 공모",
    "[마감] 공연단체 모집 공고",
    "체험프로그램 운영업체 모집 공고",
    "○○문화재단 정규직 채용 공고",
    "공연장 대관 안내",
]
_DEMO_LINKS = [
    ("첨부파일", "/download.do?id=1"),
    ("공모요강.hwp", "/files/공모요강.hwp"),
    ("다음", "?page=2"),
    ("2025 전시 공모", "/board/view.do?id=10"),
]
_DEMO_DATES = [
    "접수기간: 2025. 5. 1.(목) ~ 5. 20.(화) 18:00까지",
    "제출기한 2025년 6월 30일 17시 마감",
    "○○ 전시 공모 (~12.31)",
    "상시 모집",
]




def _print_title(t: str) -> None:
    r = classify_title(t)
    tag = "PASS" if r.passed else ("BODY" if r.body_required else "FAIL")
    print(f"  [{tag} {r.stage:9}] {t}  — {r.reason}")




def _print_dates(s: str) -> None:
    d = extract_dates(s)
    print(f"  {s!r}\n    → 시작 {d['start'] or '-'} / 마감 {d['deadline'] or '-'} / "
          f"상시 {d['always_open']} / 출처 {d['source'] or '-'} / 문구 {d['period_text'] or '-'}")




def _demo() -> None:
    print("■ 제목 판정  (PASS=Gemini 로 / BODY=본문 확인 후 결정 / FAIL=탈락)")
    for t in _DEMO_TITLES:
        _print_title(t)
    print("\n■ 링크 판정")
    for text, href in _DEMO_LINKS:
        bad, why = is_non_post_link(text, href)
        print(f"  [{'게시글 아님' if bad else '게시글    '}] {text!r:18} {href:26} {why}")
    print("\n■ 본문 보완 판정")
    r = classify_with_body("2025 지역문화 콘텐츠 공모", "본 공모는 지역의 전시 콘텐츠를 발굴하기 위한 사업입니다.")
    print(f"  [{'PASS' if r.passed else 'FAIL'} {r.stage:9}] 2025 지역문화 콘텐츠 공모 + 본문  — {r.reason}")
    print("\n■ 날짜 추출")
    for s in _DEMO_DATES:
        _print_dates(s)




def main(argv: list[str]) -> None:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    args = argv[1:]
    if not args:
        _demo()
    elif args[0] == "--date":
        for s in args[1:] or [""]:
            _print_dates(s)
    else:
        for t in args:
            _print_title(t)




if __name__ == "__main__":
    main(sys.argv)
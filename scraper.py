# -*- coding: utf-8 -*-
"""
scraper.py — Playwright(Chromium) 게시판 목록 · 상세 수집                        [3단계 / 4]


  Scraper  (with 문으로 사용, 브라우저 1개를 열어 모든 사이트에 재사용)
    fetch_list(site)               목록 → ListResult(rows=[ListRow])  셀렉터가 안 맞으면 대체 셀렉터 자동 탐색
    fetch_detail(url)              상세 → DetailResult(body · posted_date · attachments)
    open_js_row(site, row)         href 가 javascript:/# 인 글을 클릭해서 열기 (같은 창 · 새 창 모두 처리)
    probe(url)                     /add 전 사전 점검: 사이트 이름 · 동작하는 셀렉터 · 표본 제목
  guess_site_name(page_title, url) "공지사항 | 광주문화재단" → "광주문화재단 공지사항"


ListRow → main 이 post dict 로 옮긴다
  key          storage.post_key. URL 글은 URL 기반('u:'), javascript 글은 사이트+제목 기반('t:') — 실행마다 동일
  title        prefilter.clean_title 적용 (NEW 배지 · 댓글수 · '새창' 문구 제거)
  url          절대 URL. '' 이면 javascript 글 → open_js_row 로 열어야 본문을 볼 수 있음
  posted_date  'YYYY-MM-DD' | ''  (date_selector 셀 → 없으면 행의 짧은 셀에서 날짜 추정)


규칙
  · 첨부파일 · 메뉴/커뮤니티 버튼 · 페이지 번호 링크는 prefilter.is_non_post_link 로 제외 (게시글이 아님)
  · 등록된 셀렉터로 3행 이상 나오면 그대로 사용. 아니면 FALLBACK_SELECTORS 를 모두 시도해
    "행 수 + 날짜가 있는 행 ×2" 점수가 가장 높은 조합을 쓴다 → ListResult.selector_used / selector_changed
    (main 이 이 값으로 festival_sites.json 의 셀렉터를 자동 보정)
  · 이미지 · 미디어 · 폰트 요청은 차단(속도). CSS 는 유지(숨김 요소 텍스트를 innerText 에서 빼기 위해)
  · 환경변수 SCRAPER_DEBUG_DIR 이 있으면 실패한 사이트 스크린샷을 그 폴더에 저장 (Actions 아티팩트로 확인)


로컬 점검
  python scraper.py probe <URL>                   /add 시뮬레이션 (이름 · 셀렉터 · 표본 5개)
  python scraper.py list <site_id | 이름 | URL>    목록 수집 + 1차 필터 태그
  python scraper.py detail <게시글 URL>            본문 · 첨부 · 게시일 · 접수기간 추출 확인
  python scraper.py all                           활성 사이트 전체 목록 수집 요약 (저장 · 알림 없음)
"""
from __future__ import annotations


import os
import re
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urljoin, urlsplit


import config
import prefilter
from storage import Sites, normalize_site, normalize_url, post_key


try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright
except ImportError:                                    # pip install -r requirements.txt 전
    sync_playwright = None


    class PlaywrightError(Exception):                  # type: ignore[no-redef]
        pass


    class PlaywrightTimeout(PlaywrightError):          # type: ignore[no-redef]
        pass




# ─────────────────────────────────────────────────────────────────────
# 1. 설정
# ─────────────────────────────────────────────────────────────────────
MIN_ROWS_TRUST = 3                                     # 등록 셀렉터로 이 이상 나오면 대체 탐색 생략
_BLOCK_TYPES = frozenset({"image", "media", "font"})
_debug_env = os.getenv("SCRAPER_DEBUG_DIR", "").strip()
DEBUG_DIR: Optional[Path] = None
if _debug_env:
    DEBUG_DIR = Path(_debug_env) if Path(_debug_env).is_absolute() else config.BASE_DIR / _debug_env


# (목록 컨테이너, 게시글 행, 제목 링크) — 위에서부터 시도
FALLBACK_SELECTORS: list[tuple[str, str, str]] = [
    ("table:has(tbody)", "tbody > tr", "a[href]"),
    ("table", "tr", "a[href]"),
    ("ul.board-list, ul.board_list, ul.bbs-list, ul.bbs_list, ul.list, ul.lst, ul.notice-list, ul.notice_list, "
     "ul.board, ul.bbs, .board-list ul, .board_list ul, .bbs-list ul, .bbs_list ul, .list-wrap ul, .list_wrap ul, "
     ".board-body ul, .board_body ul",
     "li", "a[href]"),
    ("div.board-list, div.board_list, div.bbs-list, div.bbs_list, div.list-wrap, div.list_wrap, div.notice-list, "
     "div.notice_list, div.card-list, div.card_list, div.thumb-list, div.thumb_list, div.gallery-list, "
     "div.gallery_list, section.list, .board-body, .board_body",
     "li, dl, article, .item, .card, .row, .list-item, .list_item", "a[href]"),
    ("ul, ol", "li", "a[href]"),
    ("body", "li, tr, dl, article, .item, .card", "a[href]"),
]
_SELECTOR_KEYS = ("list_selector", "row_selector", "link_selector")


_TITLE_NOISE_RE = re.compile(
    r"\s*[-–(\[]?\s*(?:새\s*창(?:으로)?\s*(?:열기|열림|이동)?|자세히\s*보기|상세\s*보기|내용\s*보기)\s*[)\]]?\s*$")
_POSTED_RE = re.compile(
    r"(?:등록일|작성일|게시일|공고일|등록\s*일자|작성\s*일자|공고\s*일자|게시\s*일자|등록\s*날짜|작성\s*날짜)"
    r"\s*[:：]?\s*([^\n]{0,30})")
_FILE_HINTS = ("download", "filedown", "file_down", "atchfile", "getfile", "fileid=", "attach")


# 목록 추출: 컨테이너마다 행을 모으고 "행 수 + 날짜 있는 행×2" 점수가 가장 높은 컨테이너를 고른다
_LIST_JS = r"""
(args) => {
  const { listSel, rowSel, linkSel, dateSel, maxRows } = args;
  const DATE_RE = /(?:20\d{2}|(?:^|[^\d])\d{2})\s*[.\-\/년]\s*\d{1,2}\s*[.\-\/월]\s*\d{1,2}|(?:^|\s)\d{1,2}:\d{2}(?:\s|$)/;
  const txt = (el) => ((el && (el.innerText !== undefined ? el.innerText : el.textContent)) || '')
    .replace(/\s+/g, ' ').trim();
  let containers;
  try {
    containers = listSel ? Array.from(document.querySelectorAll(listSel)) : [document.body];
  } catch (e) {
    return { error: 'list selector: ' + e.message, rows: [], containerCount: 0, containerIndex: -1 };
  }
  let best = null;
  containers.forEach((c, idx) => {
    let rows;
    try { rows = Array.from(c.querySelectorAll(rowSel)); } catch (e) { return; }
    let score = 0;
    const collected = [];
    for (const r of rows) {
      let links;
      try { links = Array.from(r.querySelectorAll(linkSel)); } catch (e) { return; }
      if (!links.length) continue;
      const items = links.map((a) => ({
        text: txt(a),
        href: a.getAttribute('href') || '',
        abs: a.href || '',
        onclick: a.getAttribute('onclick') || '',
        title_attr: a.getAttribute('title') || '',
      })).filter((l) => l.text || l.title_attr);
      if (!items.length) continue;
      const rowText = txt(r).slice(0, 400);
      const cells = Array.from(r.querySelectorAll('td, th, dd, dt, span, em, p, div, time, li, strong'))
        .map(txt).filter((t) => t && t.length <= 40).slice(0, 30);
      let dateCell = '';
      if (dateSel) { try { dateCell = txt(r.querySelector(dateSel)); } catch (e) { dateCell = ''; } }
      score += 1 + ((dateCell || DATE_RE.test(rowText)) ? 2 : 0);
      collected.push({ links: items, rowText, cells, dateCell });
      if (collected.length >= maxRows) break;
    }
    if (collected.length && (!best || score > best.score)) best = { score, idx, rows: collected };
  });
  if (!best) return { rows: [], containerCount: containers.length, containerIndex: -1 };
  return { rows: best.rows, containerCount: containers.length, containerIndex: best.idx, score: best.score };
}
"""


# 상세 추출: 머리말/꼬리말/메뉴 제거 → 전용 본문 영역 우선, 없으면 넓은 영역 → 없으면 body
_DETAIL_JS = r"""
() => {
  const txt = (el) => ((el && el.innerText) || '').trim();
  document.querySelectorAll(
    'script, style, noscript, nav, header, footer, aside, #header, #footer, #gnb, #lnb, #snb, ' +
    '.header, .footer, .gnb, .lnb, .snb, .skip, .skipnav, .breadcrumb, .location, .quick, .quickmenu, ' +
    '.sitemap, .sns, .share'
  ).forEach((e) => e.remove());
  const specific = [
    '.board-view', '.board_view', '.bbs-view', '.bbs_view', '.bbsView', '.boardView', '#board-view', '#boardView',
    '#bbs_view', '.view-content', '.view_content', '.view-con', '.view_con', '.viewCon', '.view-body', '.view_body',
    '.board-content', '.board_content', '.board-detail', '.board_detail', '.view-wrap', '.view_wrap', '.viewBox',
    '.view_box', '.view-box', '.detail', '.detail-content', '.post-content', '.entry-content', '.article-content',
    '.content-view', '.contents-view', 'article'
  ];
  const broad = ['[role="main"]', 'main', '#content', '#contents', '#container', '.content', '.contents', '.container'];
  const pick = (sels) => {
    let bestEl = null, bestLen = 0;
    for (const s of sels) {
      let els;
      try { els = document.querySelectorAll(s); } catch (e) { continue; }
      for (const el of els) {
        const t = txt(el);
        if (t.length > bestLen) { bestLen = t.length; bestEl = el; }
      }
    }
    return [bestEl, bestLen];
  };
  let [el, len] = pick(specific);
  if (len < 200) { const [el2, len2] = pick(broad); if (len2 > len) { el = el2; len = len2; } }
  const body = txt(document.body);
  const text = (el && len >= 80) ? txt(el) : body;
  const links = Array.from(document.querySelectorAll('a[href]')).slice(0, 300).map((a) => ({
    text: txt(a).slice(0, 120), href: a.href || a.getAttribute('href') || ''
  }));
  return { title: document.title || '', text: text.slice(0, 30000), links, bodyLen: body.length };
}
"""




# ─────────────────────────────────────────────────────────────────────
# 2. 결과 자료형
# ─────────────────────────────────────────────────────────────────────
@dataclass
class ListRow:
    key: str
    title: str
    url: str
    href: str = ""
    posted_date: str = ""
    raw_date: str = ""
    is_js: bool = False
    index: int = 0


    def to_dict(self) -> dict:
        return asdict(self)




@dataclass
class ListResult:
    site_id: str = ""
    url: str = ""
    ok: bool = False
    rows: list = field(default_factory=list)
    error: str = ""
    page_title: str = ""
    http_status: Optional[int] = None
    selector_used: dict = field(default_factory=dict)
    selector_changed: bool = False
    raw_row_count: int = 0
    elapsed: float = 0.0
    screenshot: str = ""




@dataclass
class DetailResult:
    ok: bool = False
    url: str = ""
    body: str = ""
    posted_date: str = ""
    attachments: list = field(default_factory=list)
    page_title: str = ""
    http_status: Optional[int] = None
    error: str = ""
    elapsed: float = 0.0




# ─────────────────────────────────────────────────────────────────────
# 3. 헬퍼
# ─────────────────────────────────────────────────────────────────────
def _clean_ws(s: Any) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(s or ""))).strip()




def _short_err(e: Exception) -> str:
    msg = str(e).split("Call log")[0]
    return re.sub(r"\s+", " ", msg).strip()[:160]




def _close_quietly(page) -> None:
    try:
        if page is not None:
            page.close()
    except Exception:
        pass




def _route_filter(route) -> None:
    try:
        if route.request.resource_type in _BLOCK_TYPES:
            route.abort()
        else:
            route.continue_()
    except Exception:
        pass




def _pick_link(links: list[dict]) -> Optional[tuple[str, str, str, str]]:
    """행 안의 링크 중 게시글 제목 링크 하나 → (제목, href, 절대URL, onclick). 없으면 None"""
    best: Optional[tuple[str, str, str, str]] = None
    for ln in links:
        text = _clean_ws(ln.get("text"))
        title_attr = _clean_ws(ln.get("title_attr"))
        if title_attr and len(title_attr) > len(text):
            head = text.rstrip(".… ")[:6]
            if not text or (head and head in title_attr):       # 말줄임된 제목은 title 속성이 온전한 경우가 많다
                text = title_attr
        text = _TITLE_NOISE_RE.sub("", text).strip()
        href = str(ln.get("href") or "")
        bad, _ = prefilter.is_non_post_link(text, href)
        if bad:
            continue
        if best is None or len(text) > len(best[0]):
            best = (text, href, str(ln.get("abs") or ""), str(ln.get("onclick") or ""))
    return best




def _row_date(r: dict, title: str) -> tuple[str, str]:
    """(원문 셀, 'YYYY-MM-DD'). date_selector 셀 → 짧은 셀 순회 → 행 텍스트(연도 포함 날짜만)"""
    cell = _clean_ws(r.get("dateCell"))
    if cell:
        return cell, prefilter.parse_list_date(cell)
    tnorm = prefilter.norm(title)
    for c in r.get("cells") or []:
        c = _clean_ws(c)
        if not c or len(c) > 25:
            continue
        cn = prefilter.norm(c)
        if cn and cn in tnorm:                                   # 제목(또는 그 일부)인 셀은 건너뜀
            continue
        d = prefilter.parse_list_date(c)
        if d:
            return c, d
    row_text = _clean_ws(r.get("rowText")).replace(title, " ")
    d = prefilter.parse_date(row_text)
    return (row_text[:40] if d else ""), d




def _dedupe_rows(rows: list[ListRow]) -> list[ListRow]:
    seen: dict[str, ListRow] = {}
    for r in rows:
        prev = seen.get(r.key)
        if prev is None or len(r.title) > len(prev.title):
            seen[r.key] = r
    return sorted(seen.values(), key=lambda x: x.index)




def _rows_score(rows: list[ListRow]) -> int:
    return len(rows) + 2 * sum(1 for r in rows if r.posted_date)




def _clean_body(text: str) -> str:
    lines = [re.sub(r"[ \t\u00a0]+", " ", ln).strip() for ln in unicodedata.normalize("NFKC", text or "").splitlines()]
    out: list[str] = []
    blank = 0
    for ln in lines:
        if not ln:
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(ln)
    return "\n".join(out).strip()[:20000]




def _attachments(links: list[dict]) -> list[str]:
    names: list[str] = []
    for ln in links:
        href = str(ln.get("href") or "")
        hl = href.lower()
        path = hl.split("?", 1)[0].split("#", 1)[0]
        if not (path.endswith(config.NON_POST_HREF_EXT) or any(h in hl for h in _FILE_HINTS)):
            continue
        name = _clean_ws(ln.get("text")) or unquote(path.rsplit("/", 1)[-1])
        name = re.sub(r"^(첨부파일|다운로드|미리보기|바로보기)\s*[:：]?\s*", "", name).strip()
        if name and name.lower() not in ("다운로드", "미리보기", "바로보기", "첨부파일", "download") and name not in names:
            names.append(name[:80])
        if len(names) >= 10:
            break
    return names




def _detail_posted_date(body: str) -> str:
    for m in _POSTED_RE.finditer(body[:6000]):
        d = prefilter.parse_date(m.group(1))
        if d:
            return d
    return ""




_TITLE_SPLIT_RE = re.compile(r"\s*(?:\||::|»|>>|>|—|–)\s*|\s+-\s+|\s+:\s+")
_BOARD_WORDS = ("공지", "게시판", "알림", "소식", "공고", "목록", "모집", "공모", "사업", "안내", "뉴스", "보도", "자료",
                "notice", "board", "news", "list")




def guess_site_name(page_title: str, url: str) -> str:
    """페이지 <title> 에서 기관명(+게시판명) 추출. 실패 시 도메인."""
    segs = [s.strip() for s in _TITLE_SPLIT_RE.split(_clean_ws(page_title)) if s and s.strip()]
    org = [s for s in segs if not any(w in s.lower() for w in _BOARD_WORDS)]
    board = [s for s in segs if s not in org]
    host = urlsplit(url).netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    name = org[0] if org else host
    if board and board[0] != name:
        name = f"{name} {board[0]}"
    return name[:40] or host




# ─────────────────────────────────────────────────────────────────────
# 4. Scraper
# ─────────────────────────────────────────────────────────────────────
class Scraper:
    """브라우저 1개를 열어 여러 사이트를 순서대로 읽는다. with 문으로 사용."""


    def __init__(self, headless: bool = True):
        if sync_playwright is None:
            raise RuntimeError("playwright 가 설치되지 않았습니다: pip install -r requirements.txt && "
                               "python -m playwright install chromium")
        self.headless = headless
        self._pw = None
        self._browser = None
        self._context = None


    def __enter__(self) -> "Scraper":
        self.start()
        return self


    def __exit__(self, *exc) -> None:
        self.close()


    def start(self) -> None:
        if self._browser is not None:
            return
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        self._context = self._browser.new_context(
            user_agent=config.USER_AGENT, locale="ko-KR", timezone_id="Asia/Seoul",
            viewport={"width": 1366, "height": 900}, ignore_https_errors=True,
        )
        self._context.set_default_timeout(config.PAGE_TIMEOUT_MS)
        self._context.route("**/*", _route_filter)


    def close(self) -> None:
        for obj in (self._context, self._browser):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._context = self._browser = self._pw = None


    # ---- 내부 -----------------------------------------------------------
    def _new_page(self):
        if self._browser is None:
            self.start()
        return self._context.new_page()


    def _goto(self, page, url: str, timeout_ms: int):
        resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=min(6_000, timeout_ms))
        except PlaywrightError:
            pass
        return resp


    def _screenshot(self, page, name: str) -> str:
        if DEBUG_DIR is None or page is None:
            return ""
        try:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            path = DEBUG_DIR / (re.sub(r"[^0-9A-Za-z_.-]+", "_", name)[:60] + ".png")
            page.screenshot(path=str(path), full_page=False)
            return str(path)
        except Exception:
            return ""


    def _extract_rows(self, page, sel: tuple[str, str, str], date_sel: str) -> dict:
        ls, rs, lk = sel
        try:
            out = page.evaluate(_LIST_JS, {"listSel": ls, "rowSel": rs, "linkSel": lk,
                                           "dateSel": date_sel, "maxRows": config.MAX_ROWS_PER_SITE * 3})
            return out if isinstance(out, dict) else {}
        except PlaywrightError:
            return {}


    def _rows_from_raw(self, site: dict, base_url: str, raw_rows: list) -> list[ListRow]:
        site_id = str(site.get("id") or "")
        rows: list[ListRow] = []
        for idx, r in enumerate(raw_rows):
            if not isinstance(r, dict):
                continue
            pick = _pick_link(r.get("links") or [])
            if pick is None:
                continue
            text, href, abs_url, _onclick = pick
            title = prefilter.clean_title(text)
            if len(prefilter.norm(title)) < config.MIN_TITLE_LEN:
                continue
            hl = href.strip().lower()
            is_js = hl == "" or hl.startswith(("javascript:", "#"))
            url = ""
            if not is_js:
                url = abs_url if abs_url.startswith(("http://", "https://")) else urljoin(base_url, href)
            raw_date, posted = _row_date(r, title)
            rows.append(ListRow(key=post_key(url, site_id, title), title=title, url=url, href=href,
                                posted_date=posted, raw_date=raw_date, is_js=is_js, index=idx))
        return rows


    def _read_detail(self, page, res: DetailResult) -> None:
        try:
            data = page.evaluate(_DETAIL_JS)
        except PlaywrightError:
            data = {}
        data = data if isinstance(data, dict) else {}
        text = str(data.get("text") or "")
        if len(text) < 300:                                      # 본문이 iframe 안에 있는 게시판
            for fr in page.frames:
                if fr == page.main_frame:
                    continue
                try:
                    d2 = fr.evaluate(_DETAIL_JS)
                except PlaywrightError:
                    continue
                if isinstance(d2, dict) and len(str(d2.get("text") or "")) > len(text):
                    data, text = d2, str(d2.get("text") or "")
        res.url = page.url
        res.page_title = _clean_ws(data.get("title"))
        res.body = _clean_body(text)
        res.attachments = _attachments(data.get("links") or [])
        res.posted_date = _detail_posted_date(res.body)
        res.ok = len(res.body) >= 20
        if not res.ok and not res.error:
            res.error = "본문 텍스트를 찾지 못함"


    # ---- 공개 API -------------------------------------------------------
    def fetch_list(self, site: dict) -> ListResult:
        t0 = time.monotonic()
        site_id = str(site.get("id") or "")
        url = str(site.get("url") or "")
        res = ListResult(site_id=site_id, url=url)
        configured = tuple(str(site.get(k) or config.DEFAULT_SITE[k]) for k in _SELECTOR_KEYS)
        date_sel = str(site.get("date_selector") or "")
        page = None
        try:
            page = self._new_page()
            resp = self._goto(page, url, config.PAGE_TIMEOUT_MS)
            res.http_status = resp.status if resp else None
            res.page_title = _clean_ws(page.title())
            if res.http_status and res.http_status >= 400:
                res.error = f"HTTP {res.http_status}" + (" (봇 차단 가능)" if res.http_status in (403, 429) else "")
                res.screenshot = self._screenshot(page, site_id or "list")
                return res
            try:
                page.wait_for_selector(configured[0], timeout=6_000)   # JS 렌더링 게시판 대기
            except PlaywrightError:
                pass


            candidates = [configured] + [fb for fb in FALLBACK_SELECTORS if fb != configured]
            best_rows: list[ListRow] = []
            best_sel = configured
            best_raw = 0
            for i, sel in enumerate(candidates):
                raw = self._extract_rows(page, sel, date_sel)
                raw_rows = raw.get("rows") or []
                rows = self._rows_from_raw(site, url, raw_rows)
                if _rows_score(rows) > _rows_score(best_rows):
                    best_rows, best_sel, best_raw = rows, sel, len(raw_rows)
                if i == 0 and len(rows) >= MIN_ROWS_TRUST:
                    break
            rows = _dedupe_rows(best_rows)[: config.MAX_ROWS_PER_SITE]
            res.rows = rows
            res.raw_row_count = best_raw
            res.selector_used = dict(zip(_SELECTOR_KEYS, best_sel))
            res.selector_changed = bool(rows) and best_sel != configured
            res.ok = bool(rows)
            if not rows:
                res.error = "게시글 행을 찾지 못함 (셀렉터 불일치 · 로그인 필요 · 봇 차단 페이지 가능)"
                res.screenshot = self._screenshot(page, site_id or "list")
        except PlaywrightTimeout:
            res.error = f"페이지 로딩 시간 초과 ({config.PAGE_TIMEOUT_MS // 1000}s)"
            res.screenshot = self._screenshot(page, site_id or "list")
        except PlaywrightError as e:
            res.error = "브라우저 오류: " + _short_err(e)
        except Exception as e:                                   # noqa: BLE001
            res.error = f"예상치 못한 오류: {type(e).__name__}: {str(e)[:120]}"
        finally:
            _close_quietly(page)
            res.elapsed = round(time.monotonic() - t0, 1)
        return res


    def fetch_detail(self, url: str, referer: str = "") -> DetailResult:
        t0 = time.monotonic()
        res = DetailResult(url=url)
        page = None
        try:
            page = self._new_page()
            if referer:
                page.set_extra_http_headers({"Referer": referer})
            resp = self._goto(page, url, config.DETAIL_TIMEOUT_MS)
            res.http_status = resp.status if resp else None
            if res.http_status and res.http_status >= 400:
                res.error = f"HTTP {res.http_status}"
                return res
            self._read_detail(page, res)
        except PlaywrightTimeout:
            res.error = f"상세 페이지 로딩 시간 초과 ({config.DETAIL_TIMEOUT_MS // 1000}s)"
        except PlaywrightError as e:
            res.error = "브라우저 오류: " + _short_err(e)
        except Exception as e:                                   # noqa: BLE001
            res.error = f"예상치 못한 오류: {type(e).__name__}: {str(e)[:120]}"
        finally:
            _close_quietly(page)
            res.elapsed = round(time.monotonic() - t0, 1)
        return res


    def open_js_row(self, site: dict, row: ListRow, selectors: Optional[dict] = None) -> DetailResult:
        """javascript 링크 글: 목록을 다시 열어 제목을 클릭 → 같은 창 이동 또는 새 창 모두 처리"""
        t0 = time.monotonic()
        res = DetailResult(url="")
        sel = selectors or {}
        ls, rs, lk = (str(sel.get(k) or site.get(k) or config.DEFAULT_SITE[k]) for k in _SELECTOR_KEYS)
        list_url = str(site.get("url") or "")
        page = popup = None
        try:
            page = self._new_page()
            self._goto(page, list_url, config.PAGE_TIMEOUT_MS)
            needle = row.title[:20]
            loc = page.locator(ls).locator(rs).locator(lk).filter(has_text=needle)
            if loc.count() == 0:
                loc = page.get_by_role("link", name=needle)
            if loc.count() == 0:
                res.error = "목록에서 해당 제목 링크를 다시 찾지 못함"
                return res
            target = loc.first
            before = normalize_url(page.url)
            try:
                with page.context.expect_page(timeout=3_000) as pi:
                    target.click(timeout=8_000)
                popup = pi.value
            except PlaywrightTimeout:
                popup = None                                     # 새 창 없음 → 같은 창 이동 확인
            if popup is not None:
                try:
                    popup.wait_for_load_state("domcontentloaded", timeout=config.DETAIL_TIMEOUT_MS)
                    if popup.url in ("", "about:blank"):
                        popup.wait_for_url(lambda u: u not in ("", "about:blank"), timeout=8_000)
                except PlaywrightError:
                    pass
                reader = popup
            else:
                try:
                    page.wait_for_url(lambda u: normalize_url(u) != before, timeout=6_000)
                except PlaywrightError:
                    pass
                if normalize_url(page.url) == before:
                    res.error = "클릭 후 페이지 이동 없음 (AJAX 상세 · 팝업 차단 등)"
                    return res
                reader = page
            try:
                reader.wait_for_load_state("networkidle", timeout=6_000)
            except PlaywrightError:
                pass
            self._read_detail(reader, res)
        except PlaywrightTimeout:
            res.error = "클릭 열기 시간 초과"
        except PlaywrightError as e:
            res.error = "브라우저 오류: " + _short_err(e)
        except Exception as e:                                   # noqa: BLE001
            res.error = f"예상치 못한 오류: {type(e).__name__}: {str(e)[:120]}"
        finally:
            _close_quietly(popup)
            _close_quietly(page)
            res.elapsed = round(time.monotonic() - t0, 1)
        return res


    def probe(self, url: str) -> dict:
        """/add 전 사전 점검. commands(4단계) 가 이 결과로 이름·셀렉터를 채워 자동 등록한다."""
        site = normalize_site({"url": url})
        res = self.fetch_list(site)
        return {
            "ok": res.ok, "url": site["url"], "page_title": res.page_title,
            "name": guess_site_name(res.page_title, site["url"]),
            "rows": len(res.rows),
            "dated_rows": sum(1 for r in res.rows if r.posted_date),
            "js_rows": sum(1 for r in res.rows if r.is_js),
            "sample": [r.title for r in res.rows[:5]],
            "selectors": res.selector_used, "selector_changed": res.selector_changed,
            "http_status": res.http_status, "error": res.error, "elapsed": res.elapsed,
        }




# ─────────────────────────────────────────────────────────────────────
# 5. 로컬 점검 CLI
# ─────────────────────────────────────────────────────────────────────
def _tag(title: str) -> str:
    r = prefilter.classify_title(title)
    return ("PASS" if r.passed else "BODY" if r.body_required else "FAIL"), r.reason




def _resolve_site(query: str) -> Optional[dict]:
    hits = Sites().find(query)
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        print("대상이 여러 개입니다: " + ", ".join(h["id"] for h in hits[:10]))
        return None
    if "." in query and " " not in query:
        return normalize_site({"url": query, "name": "(임시)"})
    print(f"사이트를 찾지 못했습니다: {query}")
    return None




def _cmd_probe(sc: Scraper, url: str) -> None:
    p = sc.probe(url)
    print(f"URL        : {p['url']}")
    print(f"HTTP       : {p['http_status']}   소요 {p['elapsed']}s")
    print(f"페이지 제목: {p['page_title'] or '(없음)'}")
    print(f"추정 이름  : {p['name']}")
    print(f"셀렉터     : {p['selectors'].get('list_selector')} / {p['selectors'].get('row_selector')} / "
          f"{p['selectors'].get('link_selector')}" + ("  (대체 셀렉터로 탐색됨)" if p["selector_changed"] else ""))
    print(f"게시글     : {p['rows']}행 (날짜 인식 {p['dated_rows']}, javascript 링크 {p['js_rows']})")
    for t in p["sample"]:
        print(f"   · {t}")
    print("결과       : " + ("OK — /add 로 등록하면 이 설정으로 감시합니다" if p["ok"] else f"실패 — {p['error']}"))




def _cmd_list(sc: Scraper, query: str) -> None:
    site = _resolve_site(query)
    if site is None:
        return
    res = sc.fetch_list(site)
    print(f"사이트     : {site['name']} ({site['id']})   HTTP {res.http_status}   소요 {res.elapsed}s")
    print(f"페이지 제목: {res.page_title or '(없음)'}")
    su = res.selector_used
    print(f"셀렉터     : {su.get('list_selector')} / {su.get('row_selector')} / {su.get('link_selector')}"
          + ("  (대체 셀렉터 — 사이트 설정 갱신 권장)" if res.selector_changed else "  (등록값)"))
    if not res.ok:
        print(f"실패       : {res.error}" + (f"   스크린샷 {res.screenshot}" if res.screenshot else ""))
        return
    dated = sum(1 for r in res.rows if r.posted_date)
    js = sum(1 for r in res.rows if r.is_js)
    print(f"게시글     : {len(res.rows)}행 (원시 {res.raw_row_count}, 날짜 인식 {dated}, javascript 링크 {js})\n")
    for r in res.rows:
        tag, reason = _tag(r.title)
        print(f"  [{tag}] {r.posted_date or '    -     '}  {r.title}")
        print(f"         {r.url or '(javascript: ' + r.href[:50] + ')'}   — {reason}")




def _cmd_detail(sc: Scraper, url: str) -> None:
    d = sc.fetch_detail(url)
    print(f"URL      : {d.url}   HTTP {d.http_status}   소요 {d.elapsed}s")
    print(f"제목     : {d.page_title or '(없음)'}")
    if not d.ok:
        print(f"실패     : {d.error}")
        return
    ex = prefilter.extract_dates(d.body, d.page_title)
    print(f"본문     : {len(d.body):,}자   게시일 {d.posted_date or '미확인'}   마감표기 {prefilter.body_says_closed(d.body)}")
    print(f"접수기간 : {ex['start'] or '-'} ~ {ex['deadline'] or '-'}   상시 {ex['always_open']}   "
          f"출처 {ex['source'] or '-'}   문구 {ex['period_text'] or '-'}")
    print(f"첨부     : {', '.join(d.attachments) if d.attachments else '(없음)'}")
    print("\n--- 본문 앞부분 ---")
    print(d.body[:800])




def _cmd_all(sc: Scraper) -> None:
    sites = Sites().enabled()
    print(f"활성 사이트 {len(sites)}개 목록 수집 (저장 · 알림 없음)\n")
    print(f"{'ID':30} {'행':>3} {'날짜':>4} {'JS':>3} {'셀렉터':4} {'초':>5}  비고")
    print("-" * 100)
    fails = 0
    for s in sites:
        r = sc.fetch_list(s)
        dated = sum(1 for x in r.rows if x.posted_date)
        js = sum(1 for x in r.rows if x.is_js)
        sel = "대체" if r.selector_changed else ("등록" if r.ok else "-")
        note = r.error or (f"예: {r.rows[0].title[:40]}" if r.rows else "")
        if not r.ok:
            fails += 1
        print(f"{s['id'][:30]:30} {len(r.rows):>3} {dated:>4} {js:>3} {sel:4} {r.elapsed:>5.1f}  {note}")
    print(f"\n성공 {len(sites) - fails} / 실패 {fails}")




def main(argv: list[str]) -> None:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    cmd = argv[1].lower() if len(argv) > 1 else "help"
    if cmd not in ("probe", "list", "detail", "all") or (cmd != "all" and len(argv) < 3):
        print(__doc__)
        return
    with Scraper() as sc:
        if cmd == "probe":
            _cmd_probe(sc, argv[2])
        elif cmd == "list":
            _cmd_list(sc, " ".join(argv[2:]))
        elif cmd == "detail":
            _cmd_detail(sc, argv[2])
        else:
            _cmd_all(sc)




if __name__ == "__main__":
    main(sys.argv)
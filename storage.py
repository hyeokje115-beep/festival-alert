# -*- coding: utf-8 -*-
"""
storage.py — JSON 저장소                                        [1단계 / 9]

리포지토리에 커밋되는 JSON 4개를 읽고 쓴다 (GitHub Actions 가 실행 뒤 커밋).

  festival_sites.json   Sites          감시 사이트 목록  (/add 로 자동 등록)
  known_posts.json      KnownPosts     이미 본 게시글    (중복 알림 방지, 구버전 형식 자동 이관)
  feedback.json         FeedbackStore  보낸 알림 + 좋아요/싫어요 피드백 (Gemini 교정 예시로 재사용)
  bot_state.json        BotState       텔레그램 update offset, 진행 중 명령

공용 헬퍼
  normalize_url(url, base="")           추적/세션/목록페이지 파라미터 제거 → 중복 판정용 URL
  post_key(url, site_id="", title="")   게시글 고유키 (URL 기반, URL 없으면 사이트+제목)
  make_site_id(url)                     사이트 id 자동 생성  예) gjcf_or_kr_1a2b3c

규칙
  · 클래스는 생성 시 파일을 읽고, 변경 후 .save() 를 호출해야 디스크에 반영된다.
  · 저장은 임시파일 → os.replace 로 원자적 처리 (실행이 중단돼도 파일이 깨지지 않음).
  · 손상된 JSON 은 <파일명>.broken 으로 백업하고 빈 상태로 시작한다.

로컬 점검 명령
  python storage.py check                 파일 상태 · 사이트/게시글/피드백 개수 · 환경변수 점검
  python storage.py sites                 사이트 목록
  python storage.py add <URL> [이름]      사이트 수동 등록
  python storage.py enable|disable|remove <id 또는 이름>
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import config

VOTE_UP = "up"
VOTE_DOWN = "down"

STATUS_ALERTED   = "alerted"     # 알림 발송
STATUS_SEEDED    = "seeded"      # 새 사이트 첫 스캔 — 기존 글, 알림 없이 기록
STATUS_PREFILTER = "prefilter"   # 1차 키워드 필터 탈락
STATUS_AI_NO     = "ai_no"       # Gemini: 공모 아님
STATUS_LOW_CONF  = "low_conf"    # Gemini: 확신 부족
STATUS_EXPIRED   = "expired"     # 마감 지난 글
STATUS_MIGRATED  = "migrated"    # 구버전 known_posts 에서 이관


# ─────────────────────────────────────────────────────────────────────
# 1. 저수준 JSON I/O
# ─────────────────────────────────────────────────────────────────────
def load_json(path: Path | str, default: Any) -> Any:
    path = Path(path)
    if not path.exists():
        return copy.deepcopy(default)
    try:
        text = path.read_text(encoding="utf-8-sig")
        if not text.strip():
            return copy.deepcopy(default)
        return json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        broken = path.with_name(path.name + ".broken")
        try:
            path.replace(broken)
            print(f"[storage] {path.name} 손상 -> {broken.name} 으로 백업, 빈 상태로 시작 ({e})")
        except OSError:
            print(f"[storage] {path.name} 손상 (백업 실패): {e}")
        return copy.deepcopy(default)


def save_json(path: Path | str, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ─────────────────────────────────────────────────────────────────────
# 2. URL · 키 헬퍼
# ─────────────────────────────────────────────────────────────────────
_DROP_PARAMS = frozenset({
    # 추적
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid", "igshid",
    # 세션
    "jsessionid", "phpsessid", "sessionid", "session_id", "ci_session", "asp.net_sessionid",
    # 목록 페이지 / 검색 상태 (상세글 URL 에 섞여 같은 글이 다른 URL 로 보이는 원인)
    "page", "pageindex", "pageno", "pagenum", "cpage", "currentpage", "pageunit", "pagesize",
    "searchcnd", "searchwrd", "searchkeyword", "searchcondition", "searchtype", "searchword",
    "keyword", "sort", "order", "orderby", "listurl", "returnurl",
})


def normalize_url(url: str, base: str = "") -> str:
    """중복 판정용 URL (표시용 아님). 추적·세션·목록 파라미터 제거, 쿼리 정렬, www 제거."""
    url = (url or "").strip()
    if not url:
        return ""
    low = url.lower()
    if base and "://" not in url and not low.startswith(("javascript:", "mailto:", "tel:", "#")):
        url = urljoin(base, url)
    p = urlsplit(url)
    if not p.netloc:
        return url                       # javascript:, #, base 없는 상대경로 등은 그대로
    netloc = p.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = re.sub(r";jsessionid=[^/?#]*", "", p.path, flags=re.I) or "/"
    query = sorted(
        (k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in _DROP_PARAMS
    )
    fragment = p.fragment if p.fragment.startswith(("/", "!")) else ""   # SPA 라우트만 유지
    return urlunsplit((p.scheme.lower() or "https", netloc, path, urlencode(query), fragment))


def _norm_title(title: str) -> str:
    t = (title or "").lower()
    t = re.sub(r"\((new|n|hot)\)|\[(new|n|hot)\]|\b(new|hot)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def post_key(url: str, site_id: str = "", title: str = "") -> str:
    """게시글 고유키. 실제 URL 이면 'u:' + URL 해시, 아니면 't:' + (사이트+제목) 해시."""
    nu = normalize_url(url)
    if nu and urlsplit(nu).netloc:
        return "u:" + hashlib.sha1(nu.encode("utf-8")).hexdigest()[:20]
    return "t:" + hashlib.sha1(f"{site_id}|{_norm_title(title)}".encode("utf-8")).hexdigest()[:20]


def make_site_id(url: str) -> str:
    u = url if "://" in url else "https://" + url
    host = urlsplit(u).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    slug = re.sub(r"[^a-z0-9]+", "_", host).strip("_")[:28] or "site"
    return f"{slug}_{hashlib.sha1(normalize_url(u).encode('utf-8')).hexdigest()[:6]}"


def _as_bool(v: Any, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "y", "yes", "on")
    return bool(v)


# ─────────────────────────────────────────────────────────────────────
# 3. Sites — festival_sites.json
# ─────────────────────────────────────────────────────────────────────
_SITE_KEY_ALIASES = {
    "site_id": "id", "site_name": "name",
    "list_url": "url", "board_url": "url",
    "selector_list": "list_selector", "selector_row": "row_selector", "selector_link": "link_selector",
    "base": "base_url",
}


def normalize_site(site: dict) -> dict:
    """구버전/부분 입력을 DEFAULT_SITE 스키마로 맞춘다. 모르는 키는 보존."""
    src = {_SITE_KEY_ALIASES.get(k, k): v for k, v in site.items()}
    s = copy.deepcopy(config.DEFAULT_SITE)
    s.update({k: v for k, v in src.items() if v is not None})

    s["url"] = str(s.get("url") or "").strip()
    if s["url"] and "://" not in s["url"]:
        s["url"] = "https://" + s["url"]
    s["id"] = str(s.get("id") or "").strip() or (make_site_id(s["url"]) if s["url"] else "")
    s["name"] = str(s.get("name") or "").strip() or (urlsplit(s["url"]).netloc or s["id"])
    if not s.get("base_url") and s["url"]:
        p = urlsplit(s["url"])
        s["base_url"] = f"{p.scheme}://{p.netloc}"
    for k in ("list_selector", "row_selector", "link_selector"):
        if not s.get(k):
            s[k] = config.DEFAULT_SITE[k]
    s["enabled"] = _as_bool(s.get("enabled"), True)
    s["verified"] = _as_bool(s.get("verified"), False)
    try:
        s["fail_count"] = int(s.get("fail_count") or 0)
    except (TypeError, ValueError):
        s["fail_count"] = 0
    return s


class Sites:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or config.SITES_FILE)
        raw = load_json(self.path, [])
        if isinstance(raw, dict):                 # {"sites": [...]} 형태도 허용
            raw = raw.get("sites", [])
        self.items: list[dict] = []
        self.skipped = 0
        seen_ids: set[str] = set()
        for s in raw if isinstance(raw, list) else []:
            if not isinstance(s, dict):
                self.skipped += 1
                continue
            n = normalize_site(s)
            if not n["url"] or n["id"] in seen_ids:
                self.skipped += 1
                continue
            seen_ids.add(n["id"])
            self.items.append(n)

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self):
        return iter(self.items)

    def save(self) -> None:
        save_json(self.path, self.items)

    def all(self) -> list[dict]:
        return list(self.items)

    def enabled(self) -> list[dict]:
        return [s for s in self.items if s.get("enabled")]

    def get(self, site_id: str) -> Optional[dict]:
        for s in self.items:
            if s["id"] == site_id:
                return s
        return None

    def find(self, query: str) -> list[dict]:
        """id 완전일치 → URL 일치 → 이름/URL/id 부분일치 순으로 찾는다."""
        q = (query or "").strip()
        if not q:
            return []
        ql = q.lower()
        hit = [s for s in self.items if s["id"].lower() == ql]
        if hit:
            return hit
        if "://" in q or "." in q:
            nq = normalize_url(q if "://" in q else "https://" + q)
            hit = [s for s in self.items if normalize_url(s["url"]) == nq]
            if hit:
                return hit
        return [s for s in self.items
                if ql in s["name"].lower() or ql in s["url"].lower() or ql in s["id"].lower()]

    def add(self, url: str, name: str = "", added_by: str = "", **overrides) -> tuple[bool, str, Optional[dict]]:
        """(성공여부, 메시지, 사이트) 반환. 저장은 호출자가 .save()."""
        url = (url or "").strip()
        if not url:
            return False, "URL 이 비어 있습니다.", None
        if "://" not in url:
            url = "https://" + url
        p = urlsplit(url)
        if p.scheme not in ("http", "https") or "." not in p.netloc:
            return False, f"올바른 URL 이 아닙니다: {url}", None
        nu = normalize_url(url)
        for s in self.items:
            if normalize_url(s["url"]) == nu:
                return False, f"이미 등록된 사이트입니다: {s['name']} ({s['id']})", s
        site = normalize_site({
            "url": url, "name": (name or "").strip(), "added_by": added_by,
            "added_at": config.now_kst_iso(), "enabled": True, "verified": False, **overrides,
        })
        if self.get(site["id"]):
            site["id"] += "_" + hashlib.sha1(url.encode("utf-8")).hexdigest()[6:10]
        self.items.append(site)
        return True, f"등록 완료: {site['name']} ({site['id']})", site

    def update(self, site_id: str, **fields) -> Optional[dict]:
        s = self.get(site_id)
        if s is None:
            return None
        merged = normalize_site({**s, **{k: v for k, v in fields.items() if v is not None}})
        merged["id"] = s["id"]
        s.clear()
        s.update(merged)
        return s

    def remove(self, site_id: str) -> Optional[dict]:
        s = self.get(site_id)
        if s is not None:
            self.items.remove(s)
        return s

    def set_enabled(self, site_id: str, enabled: bool) -> Optional[dict]:
        s = self.get(site_id)
        if s is not None:
            s["enabled"] = bool(enabled)
            if enabled:
                s["fail_count"] = 0
        return s

    def touch(self, site_id: str, ok: bool, status: str = "") -> Optional[dict]:
        """스캔 결과 기록. ok=True → verified, fail_count=0 / ok=False → fail_count+1"""
        s = self.get(site_id)
        if s is None:
            return None
        s["last_checked"] = config.now_kst_iso()
        s["last_status"] = status or ("ok" if ok else "fail")
        if ok:
            s["fail_count"] = 0
            s["verified"] = True
        else:
            s["fail_count"] = int(s.get("fail_count") or 0) + 1
        return s


# ─────────────────────────────────────────────────────────────────────
# 4. KnownPosts — known_posts.json  (v2: {"version":2, "posts": {key: {...}}})
# ─────────────────────────────────────────────────────────────────────
class KnownPosts:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or config.KNOWN_POSTS_FILE)
        raw = load_json(self.path, {"version": 2, "posts": {}})
        self.migrated = not (isinstance(raw, dict) and raw.get("version") == 2 and isinstance(raw.get("posts"), dict))
        self.data = self._migrate(raw)
        self.posts: dict[str, dict] = self.data["posts"]

    @staticmethod
    def _migrate(raw: Any) -> dict:
        if isinstance(raw, dict) and raw.get("version") == 2 and isinstance(raw.get("posts"), dict):
            return raw
        now = config.now_kst_iso()
        posts: dict[str, dict] = {}

        def put(url: str = "", title: str = "", site_id: str = "", info: Any = None) -> None:
            url, title, site_id = str(url or ""), str(title or ""), str(site_id or "")
            if not url and not title:
                return
            rec = {"site_id": site_id, "title": title, "url": url, "first_seen": now, "last_seen": now,
                   "status": STATUS_MIGRATED, "reason": ""}
            if isinstance(info, dict):
                for k in ("first_seen", "seen", "seen_at", "date", "added"):
                    if isinstance(info.get(k), str) and info[k]:
                        rec["first_seen"] = info[k]
                        break
                if isinstance(info.get("status"), str):
                    rec["status"] = info["status"]
            posts.setdefault(post_key(url, site_id, title), rec)

        if isinstance(raw, list):                                    # [url, ...] / [{...}, ...]
            for item in raw:
                if isinstance(item, str):
                    put(url=item)
                elif isinstance(item, dict):
                    put(item.get("url") or item.get("link", ""), item.get("title", ""),
                        item.get("site_id") or item.get("site", ""), item)
        elif isinstance(raw, dict):
            for k, v in raw.items():
                if k in ("version", "updated_at"):
                    continue
                if isinstance(v, list):                              # {site_id: [url, ...]}
                    for u in v:
                        if isinstance(u, str):
                            put(url=u, site_id=k)
                        elif isinstance(u, dict):
                            put(u.get("url") or u.get("link", ""), u.get("title", ""), u.get("site_id") or k, u)
                elif isinstance(v, dict):                            # {url 또는 key: {...}}
                    put(v.get("url") or v.get("link") or (k if "://" in k else ""), v.get("title", ""),
                        v.get("site_id") or v.get("site", ""), v)
                elif "://" in k:                                     # {url: "제목"} / {url: true}
                    put(url=k, title=v if isinstance(v, str) else "")
        return {"version": 2, "updated_at": now, "posts": posts}

    def __len__(self) -> int:
        return len(self.posts)

    def __contains__(self, key: str) -> bool:
        return key in self.posts

    def is_known(self, key: str) -> bool:
        return key in self.posts

    def get(self, key: str) -> Optional[dict]:
        return self.posts.get(key)

    def mark(self, key: str, *, site_id: str = "", title: str = "", url: str = "",
             status: str = STATUS_ALERTED, reason: str = "") -> dict:
        now = config.now_kst_iso()
        rec = self.posts.get(key)
        if rec is None:
            rec = {"site_id": site_id, "title": title, "url": url, "first_seen": now, "last_seen": now,
                   "status": status, "reason": reason}
            self.posts[key] = rec
        else:
            rec["last_seen"] = now
            if status:
                rec["status"] = status
            if reason:
                rec["reason"] = reason
            for k, v in (("site_id", site_id), ("title", title), ("url", url)):
                if v and not rec.get(k):
                    rec[k] = v
        return rec

    def seen(self, key: str) -> bool:
        """목록에 여전히 보이는 기존 글의 last_seen 갱신 (prune 에서 오래 안 보인 글부터 정리)."""
        rec = self.posts.get(key)
        if rec is None:
            return False
        rec["last_seen"] = config.now_kst_iso()
        return True

    def count(self, site_id: Optional[str] = None) -> int:
        if site_id is None:
            return len(self.posts)
        return sum(1 for r in self.posts.values() if r.get("site_id") == site_id)

    def prune(self, keep_per_site: Optional[int] = None) -> int:
        keep = keep_per_site or config.KNOWN_KEEP_PER_SITE
        by_site: dict[str, list[tuple[str, str]]] = {}
        for k, r in self.posts.items():
            by_site.setdefault(r.get("site_id", ""), []).append((r.get("last_seen") or r.get("first_seen", ""), k))
        removed = 0
        for lst in by_site.values():
            if len(lst) <= keep:
                continue
            lst.sort(reverse=True)
            for _, k in lst[keep:]:
                del self.posts[k]
                removed += 1
        return removed

    def save(self) -> None:
        self.data["version"] = 2
        self.data["updated_at"] = config.now_kst_iso()
        self.data["posts"] = self.posts
        save_json(self.path, self.data)


# ─────────────────────────────────────────────────────────────────────
# 5. FeedbackStore — feedback.json
#    alerts   : {"<chat_id>:<message_id>": {key, site_id, site_name, title, url, deadline,
#                                            ai_reason, ai_confidence, sent_at, votes{user_id: up/down}}}
#    feedback : [{at, chat_id, user_id, user_name, vote, key, site_id, site_name, title, url,
#                 ai_reason, ai_confidence}]   (게시글×사용자 당 1건, 재투표 시 교체)
# ─────────────────────────────────────────────────────────────────────
class FeedbackStore:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or config.FEEDBACK_FILE)
        d = load_json(self.path, {})
        if not isinstance(d, dict):
            d = {}
        self.alerts: dict[str, dict] = d.get("alerts") if isinstance(d.get("alerts"), dict) else {}
        self.feedback: list[dict] = d.get("feedback") if isinstance(d.get("feedback"), list) else []

    @staticmethod
    def alert_id(chat_id: int | str, message_id: int | str) -> str:
        return f"{chat_id}:{message_id}"

    def register_alert(self, chat_id: int | str, message_id: int | str, post: dict) -> dict:
        rec = {
            "key": post.get("key", ""),
            "site_id": post.get("site_id", ""),
            "site_name": post.get("site_name", ""),
            "title": post.get("title", ""),
            "url": post.get("url", ""),
            "deadline": post.get("deadline", ""),
            "ai_reason": post.get("ai_reason", ""),
            "ai_confidence": post.get("ai_confidence"),
            "sent_at": config.now_kst_iso(),
            "votes": {},
        }
        self.alerts[self.alert_id(chat_id, message_id)] = rec
        return rec

    def get_alert(self, chat_id: int | str, message_id: int | str) -> Optional[dict]:
        return self.alerts.get(self.alert_id(chat_id, message_id))

    def vote(self, chat_id: int | str, message_id: int | str, user_id: int | str,
             user_name: str, vote: str) -> tuple[str, Optional[dict]]:
        """반환: ("recorded" | "changed" | "same" | "unknown" | "invalid", alert)"""
        if vote not in (VOTE_UP, VOTE_DOWN):
            return "invalid", None
        a = self.get_alert(chat_id, message_id)
        if a is None:
            return "unknown", None
        uid = str(user_id)
        votes = a.setdefault("votes", {})
        prev = votes.get(uid)
        if prev == vote:
            return "same", a
        votes[uid] = vote
        self.feedback = [f for f in self.feedback
                         if not (f.get("key") == a.get("key") and str(f.get("user_id")) == uid)]
        self.feedback.append({
            "at": config.now_kst_iso(), "chat_id": chat_id, "user_id": user_id, "user_name": user_name,
            "vote": vote, "key": a.get("key", ""), "site_id": a.get("site_id", ""),
            "site_name": a.get("site_name", ""), "title": a.get("title", ""), "url": a.get("url", ""),
            "ai_reason": a.get("ai_reason", ""), "ai_confidence": a.get("ai_confidence"),
        })
        return ("changed" if prev else "recorded"), a

    def consensus(self) -> dict[str, dict]:
        """게시글(key)별 최종 판정. 가족 간 의견이 갈리면 보수적으로 down (오탐 최소화 우선)."""
        out: dict[str, dict] = {}
        for f in self.feedback:
            key = f.get("key") or f"t:{f.get('title', '')}"
            c = out.setdefault(key, {
                "key": key, "title": f.get("title", ""), "site_id": f.get("site_id", ""),
                "site_name": f.get("site_name", ""), "ai_reason": f.get("ai_reason", ""),
                "n_up": 0, "n_down": 0, "at": "",
            })
            c["n_up" if f.get("vote") == VOTE_UP else "n_down"] += 1
            c["at"] = max(c["at"], f.get("at", ""))
        for c in out.values():
            c["vote"] = VOTE_DOWN if c["n_down"] >= c["n_up"] else VOTE_UP
        return out

    def examples(self, limit: Optional[int] = None) -> list[dict]:
        """Gemini 프롬프트용 최근 예시. 싫어요(교정)를 최대 2/3, 나머지는 좋아요."""
        limit = limit or config.FEEDBACK_EXAMPLES_IN_PROMPT
        items = sorted(self.consensus().values(), key=lambda c: c["at"], reverse=True)
        downs = [c for c in items if c["vote"] == VOTE_DOWN]
        ups = [c for c in items if c["vote"] == VOTE_UP]
        take_down = min(len(downs), max(1, limit * 2 // 3))
        take_up = min(len(ups), limit - take_down)
        take_down = min(len(downs), limit - take_up)
        return downs[:take_down] + ups[:take_up]

    def stats(self) -> dict:
        cons = self.consensus()
        up = sum(1 for c in cons.values() if c["vote"] == VOTE_UP)
        by_site: dict[str, dict] = {}
        for c in cons.values():
            b = by_site.setdefault(c["site_id"] or "?", {"name": c["site_name"], "up": 0, "down": 0})
            b[c["vote"]] += 1
        return {"alerts": len(self.alerts), "votes": len(self.feedback), "posts_voted": len(cons),
                "up": up, "down": len(cons) - up, "by_site": by_site}

    def prune(self) -> None:
        cutoff = (config.now_kst() - timedelta(days=config.ALERT_KEEP_DAYS)).strftime(config.TIME_FMT)
        self.alerts = {k: a for k, a in self.alerts.items() if a.get("sent_at", "") >= cutoff}
        if len(self.feedback) > config.FEEDBACK_KEEP_MAX:
            self.feedback = self.feedback[-config.FEEDBACK_KEEP_MAX:]

    def save(self) -> None:
        save_json(self.path, {"version": 1, "updated_at": config.now_kst_iso(),
                              "alerts": self.alerts, "feedback": self.feedback})


# ─────────────────────────────────────────────────────────────────────
# 6. BotState — bot_state.json
# ─────────────────────────────────────────────────────────────────────
class BotState:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or config.BOT_STATE_FILE)
        d = load_json(self.path, {})
        self.data: dict = d if isinstance(d, dict) else {}
        self.data.setdefault("update_offset", 0)
        self.data.setdefault("pending", {})
        self.data.setdefault("last_scan_at", "")
        self.data.setdefault("last_inbox_at", "")

    @property
    def offset(self) -> int:
        try:
            return int(self.data.get("update_offset") or 0)
        except (TypeError, ValueError):
            return 0

    @offset.setter
    def offset(self, value: int) -> None:
        self.data["update_offset"] = int(value)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, **kv: Any) -> None:
        self.data.update(kv)

    def pending(self, chat_id: int | str) -> Optional[dict]:
        """진행 중 대화형 명령 (예: /add 후 이름 입력 대기)"""
        return self.data["pending"].get(str(chat_id))

    def set_pending(self, chat_id: int | str, data: Optional[dict]) -> None:
        if data is None:
            self.data["pending"].pop(str(chat_id), None)
        else:
            self.data["pending"][str(chat_id)] = data

    def save(self) -> None:
        self.data["updated_at"] = config.now_kst_iso()
        save_json(self.path, self.data)


# ─────────────────────────────────────────────────────────────────────
# 7. 로컬 점검 CLI
# ─────────────────────────────────────────────────────────────────────
def _cmd_check() -> None:
    print(f"데이터 폴더  : {config.BASE_DIR}")
    for p in (config.SITES_FILE, config.KNOWN_POSTS_FILE, config.FEEDBACK_FILE, config.BOT_STATE_FILE):
        state = f"{p.stat().st_size:,} bytes" if p.exists() else "없음 (첫 실행 때 자동 생성)"
        print(f"  {p.name:20} {state}")

    sites = Sites()
    verified = sum(1 for s in sites if s["verified"])
    extra = f", 형식 불량으로 건너뜀 {sites.skipped}개" if sites.skipped else ""
    print(f"\n사이트       : {len(sites)}개 (활성 {len(sites.enabled())}, 검증 {verified}{extra})")

    known = KnownPosts()
    note = " (구버전 형식 -> v2 로 이관됨, 다음 저장 때 파일에 반영)" if known.migrated else ""
    print(f"알려진 게시글: {len(known)}개{note}")

    st = FeedbackStore().stats()
    print(f"알림/피드백  : 보낸 알림 {st['alerts']}건, 투표 {st['votes']}건 "
          f"(게시글 {st['posts_voted']}건: 좋아요 {st['up']} / 싫어요 {st['down']})")
    print(f"텔레그램     : update offset {BotState().offset}")

    problems = config.validate(need_telegram=True, need_gemini=True)
    print("\n환경변수     : " + ("OK" if not problems else " | ".join(problems)))


def _cmd_sites() -> None:
    sites = Sites()
    if not len(sites):
        print("등록된 사이트가 없습니다.")
        return
    print(f"{'ID':30} {'상태':4} {'검증':4} {'실패':4} 이름")
    print("-" * 78)
    for s in sites:
        print(f"{s['id'][:30]:30} {'ON' if s['enabled'] else 'OFF':4} "
              f"{'Y' if s['verified'] else '-':4} {s['fail_count']:<4} {s['name']}")
        print(f"{'':46}{s['url']}")


def main(argv: list[str]) -> None:
    try:
        sys.stdout.reconfigure(errors="replace")   # Windows 콘솔에서 출력 불가 문자로 중단되지 않게
    except Exception:
        pass
    cmd = argv[1].lower() if len(argv) > 1 else "check"
    if cmd == "check":
        _cmd_check()
    elif cmd == "sites":
        _cmd_sites()
    elif cmd == "add" and len(argv) >= 3:
        sites = Sites()
        ok, msg, _ = sites.add(argv[2], name=" ".join(argv[3:]), added_by="cli")
        if ok:
            sites.save()
        print(msg)
    elif cmd in ("enable", "disable", "remove") and len(argv) >= 3:
        sites = Sites()
        hits = sites.find(" ".join(argv[2:]))
        if len(hits) != 1:
            print(f"대상이 {len(hits)}개 입니다. id 를 정확히 입력하세요: " + ", ".join(h["id"] for h in hits[:10]))
            return
        s = hits[0]
        if cmd == "remove":
            sites.remove(s["id"])
            print(f"삭제: {s['name']} ({s['id']})")
        else:
            sites.set_enabled(s["id"], cmd == "enable")
            print(f"{'활성' if cmd == 'enable' else '비활성'}: {s['name']} ({s['id']})")
        sites.save()
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)

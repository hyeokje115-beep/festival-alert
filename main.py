# -*- coding: utf-8 -*-
"""
main.py — 실행 진입점                                                             [4단계 / 4]


  python main.py scan     명령·👍👎 처리 → 사이트 스캔 → 1차 필터 → 본문 → Gemini → 알림        (KST 09:00 / 13:30 / 18:00)
  python main.py inbox    텔레그램 명령(/add …) · 👍👎 처리만
  python main.py check    설정 · 파일 · 봇 토큰 점검 후 확인 메시지 발송


scan 흐름
  ① inbox 먼저 실행 — /add 로 추가된 사이트와 새 👍👎 를 이번 스캔에 반영
  ② 사이트별 목록 수집 → 실패는 fail_count+1 (연속 SITE_FAIL_DISABLE_AFTER 회면 자동 비활성)
     · 대체 셀렉터로 잡힌 사이트는 festival_sites.json 셀렉터를 자동 보정
     · 기록이 하나도 없는 사이트(첫 스캔)는 기존 글을 알림 없이 기록만 (FIRST_SCAN_SILENT)
  ③ 새 글: 1차 키워드 필터 → 교차 사이트 중복 → 본문 열람 → 마감 문구/마감일 확인 → 후보
  ④ 후보를 1차 점수 순으로 Gemini 판정 (호출 상한 초과분은 기록하지 않고 다음 실행에서 재판정)
  ⑤ 알림 발송(마감 임박 순, 최대 ALERT_MAX_PER_RUN) → 발송 즉시 known_posts · feedback 저장 (중복 알림 방지)
  ⑥ 요약을 bot_state 에 기록 (/status) · 문제가 있으면 관리자에게 무음 요약


옵션 환경변수
  DRY_RUN=1          알림 발송 · 파일 저장 없이 흐름만 확인 (Gemini 는 호출)
  SCAN_SUMMARY       always | issues(기본) | never   — 스캔 요약을 관리자 채팅에 무음으로 보낼지
  SCAN_SITES         쉼표로 구분한 site id/이름 — 이 사이트만 스캔 (로컬 테스트)
  SCAN_RUNNER        kr = 한국 PC 러너: kr_only 사이트만 스캔 / 미설정 = GitHub 러너: kr_only 제외
  SKIP_INBOX=1       scan 앞의 명령·👍👎 처리 생략 (보조 러너가 텔레그램 오프셋을 건드리지 않도록)
"""
from __future__ import annotations


import os
import sys
import time
import traceback
from dataclasses import dataclass
from typing import Any, Optional


import config
import prefilter
from commands import CommandHandler
from judge import Judge, final_decision
from storage import (STATUS_ALERTED, STATUS_EXPIRED, STATUS_LOW_CONF, STATUS_PREFILTER, STATUS_SEEDED, BotState,
                     FeedbackStore, KnownPosts, Sites)
from telegram_client import (TelegramClient, TelegramError, broadcast, esc, format_alert, handle_vote, send_alert,
                             strip_tags)


MODES = ("scan", "inbox", "check")
STATUS_DUPLICATE = "duplicate"
DRY_RUN = os.getenv("DRY_RUN", "").strip().lower() in ("1", "true", "yes", "y")
SCAN_SUMMARY = os.getenv("SCAN_SUMMARY", "issues").strip().lower()
SCAN_SITES = [s.strip() for s in os.getenv("SCAN_SITES", "").split(",") if s.strip()]
SCAN_RUNNER = os.getenv("SCAN_RUNNER", "").strip().lower()                        # "kr" = 한국 PC 러너
SKIP_INBOX = os.getenv("SKIP_INBOX", "").strip().lower() in ("1", "true", "yes", "y")
# 해외 IP 차단으로 보이는 실패 사유 — 이 사유로 연속 SITE_FAIL_DISABLE_AFTER 회면 비활성 대신 한국 러너로 이관
_CONN_ERROR_MARKS = ("ERR_CONNECTION_TIMED_OUT", "ERR_CONNECTION_REFUSED", "ERR_CONNECTION_RESET",
                     "ERR_TIMED_OUT", "ERR_ADDRESS_UNREACHABLE", "페이지 로딩 시간 초과")




def _is_connection_error(err: str) -> bool:
    e = err or ""
    return any(m in e for m in _CONN_ERROR_MARKS)




def log(msg: str) -> None:
    print(f"[{config.now_kst().strftime('%H:%M:%S')}] {msg}", flush=True)




# ─────────────────────────────────────────────────────────────────────
# 1. 런타임 (저장소 + 클라이언트 + 지연 생성 브라우저)
# ─────────────────────────────────────────────────────────────────────
class Runtime:
    def __init__(self, need_client: bool = True):
        self.sites = Sites()
        self.known = KnownPosts()
        self.feedback = FeedbackStore()
        self.state = BotState()
        self.client: Optional[TelegramClient] = TelegramClient() if need_client else None
        self._scraper = None


    def scraper(self):
        if self._scraper is None:
            import scraper as _scraper                            # playwright 는 필요할 때만 로드
            self._scraper = _scraper.Scraper()
            self._scraper.start()
        return self._scraper


    def save(self) -> None:
        if DRY_RUN:
            return
        for obj in (self.sites, self.known, self.feedback, self.state):
            try:
                obj.save()
            except Exception as e:                                # noqa: BLE001
                log(f"저장 실패 {type(obj).__name__}: {e}")


    def close(self) -> None:
        if self._scraper is not None:
            try:
                self._scraper.close()
            except Exception:                                     # noqa: BLE001
                pass
            self._scraper = None




# ─────────────────────────────────────────────────────────────────────
# 2. inbox — 명령 · 👍👎
# ─────────────────────────────────────────────────────────────────────
def run_inbox(rt: Runtime) -> dict:
    stats = {"updates": 0, "votes": 0, "commands": 0, "errors": 0}
    client = rt.client
    assert client is not None
    handler = CommandHandler(client, rt.sites, rt.known, rt.feedback, rt.state, rt.scraper)
    try:
        client.delete_webhook()
        if rt.state.get("commands_version") != 2:
            client.set_my_commands()
            rt.state.set(commands_set=True, commands_version=2)
    except TelegramError as e:
        log(f"봇 초기화 경고: {e}")


    offset = rt.state.offset
    for _ in range(10):                                           # 최대 1000건/실행
        try:
            updates = client.get_updates(offset=offset or None)
        except TelegramError as e:
            log(f"getUpdates 실패: {e}")
            stats["errors"] += 1
            break
        if not updates:
            break
        for u in updates:
            offset = max(offset, int(u.get("update_id") or 0) + 1)
            stats["updates"] += 1
            try:
                if u.get("callback_query"):
                    st = handle_vote(client, rt.feedback, u["callback_query"])
                    if st in ("recorded", "changed"):
                        stats["votes"] += 1
                    cq = u["callback_query"]
                    log(f"👍👎 {st}: {cq.get('data')} by {((cq.get('from') or {}).get('first_name'))}")
                elif u.get("message"):
                    st = handler.handle_message(u["message"])
                    if st not in ("ignored", "denied"):
                        stats["commands"] += 1
                    txt = (u["message"].get("text") or "")[:60]
                    log(f"명령 {st}: chat={((u['message'].get('chat') or {}).get('id'))} {txt!r}")
            except Exception:                                     # noqa: BLE001
                stats["errors"] += 1
                traceback.print_exc()
        if len(updates) < config.TELEGRAM_POLL_LIMIT:
            break


    rt.state.offset = offset
    rt.state.set(last_inbox_at=config.now_kst_iso(), last_inbox=stats)
    rt.save()
    log(f"inbox: 업데이트 {stats['updates']} · 투표 {stats['votes']} · 명령 {stats['commands']} · 오류 {stats['errors']}")
    return stats




# ─────────────────────────────────────────────────────────────────────
# 3. scan
# ─────────────────────────────────────────────────────────────────────
@dataclass
class Candidate:
    site: dict
    row: Any                         # scraper.ListRow
    pre: Any                         # prefilter.PrefilterResult
    body: str
    attachments: list
    detail_ok: bool
    extracted: dict
    posted_date: str


    def post(self) -> dict:
        ex = self.extracted
        return {
            "key": self.row.key, "site_id": self.site["id"], "site_name": self.site["name"],
            "title": self.row.title, "url": self.row.url or self.site["url"],
            "posted_date": self.posted_date, "start": ex.get("start", ""), "deadline": ex.get("deadline", ""),
            "period_text": ex.get("period_text", ""), "always_open": bool(ex.get("always_open")),
            "body_checked": self.detail_ok, "prefilter": self.pre.reason,
            "ai_reason": "", "ai_confidence": None,
        }




def _mark(rt: Runtime, site: dict, row: Any, status: str, reason: str) -> None:
    rt.known.mark(row.key, site_id=site["id"], title=row.title, url=row.url, status=status, reason=reason[:200])




def _recent_signatures(known: KnownPosts) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for rec in known.posts.values():
        if rec.get("status") != STATUS_ALERTED:
            continue
        sig = prefilter.title_signature(rec.get("title", ""))
        if len(sig) >= 8:
            out[sig] = rec
    return out




def _select_sites(rt: Runtime) -> list[dict]:
    sites = rt.sites.enabled()
    if SCAN_RUNNER == "kr":
        sites = [s for s in sites if s.get("kr_only")]                # 한국 러너: 이관된 사이트만
    else:
        sites = [s for s in sites if not s.get("kr_only")]            # GitHub 러너: 이관된 사이트 제외
    if not SCAN_SITES:
        return sites
    picked: list[dict] = []
    for q in SCAN_SITES:
        for s in rt.sites.find(q):
            if s not in picked:
                picked.append(s)
    return picked




def _collect_site(rt: Runtime, sc, site: dict, recent_sigs: dict, run_sigs: dict, stat: dict,
                  candidates: list, fails: list, disabled: list, selector_fixed: list) -> None:
    sid, sname = site["id"], site["name"]
    res = sc.fetch_list(site)
    if not res.ok:
        rt.sites.touch(sid, False, f"fail:{res.error[:80]}")
        fails.append(f"{sname} — {res.error[:60]}")
        log(f"✗ {sname}: {res.error}  (연속 {site['fail_count']}회)")
        if SCAN_RUNNER != "kr" and _is_connection_error(res.error):
            n_conn = int(site.get("conn_fail_count") or 0) + 1
            rt.sites.update(sid, conn_fail_count=n_conn)
            if n_conn >= config.SITE_FAIL_DISABLE_AFTER:
                # 해외 IP 차단 추정 — 비활성 대신 한국 러너 담당으로 이관 (활성 유지, 실패 카운트 초기화)
                rt.sites.update(sid, kr_only=True, conn_fail_count=0, fail_count=0)
                stat.setdefault("moved_kr", []).append(sname)
                log(f"  🇰🇷 연속 {n_conn}회 연결 실패 → 한국 러너로 이관 (kr_only)")
                return
        elif site.get("conn_fail_count"):
            rt.sites.update(sid, conn_fail_count=0)                  # 다른 종류의 실패 — 연결 실패 연속 끊김
        if int(site.get("fail_count") or 0) >= config.SITE_FAIL_DISABLE_AFTER:
            rt.sites.set_enabled(sid, False)
            disabled.append(sname)
            log(f"  ⏸ 연속 {site['fail_count']}회 실패 → 자동 비활성")
        return
    rt.sites.touch(sid, True, "ok")
    if site.get("conn_fail_count"):
        rt.sites.update(sid, conn_fail_count=0)
    stat["sites_ok"] += 1
    if res.selector_changed:
        rt.sites.update(sid, **res.selector_used)
        selector_fixed.append(sname)


    first_scan = rt.known.count(sid) == 0
    new_rows = []
    for row in res.rows:
        if rt.known.is_known(row.key):
            rt.known.seen(row.key)
        else:
            new_rows.append(row)
    stat["new_rows"] += len(new_rows)
    if first_scan and config.FIRST_SCAN_SILENT and new_rows:
        for row in new_rows:
            _mark(rt, site, row, STATUS_SEEDED, "첫 스캔 — 기존 글 기록")
        stat["seeded"] += len(new_rows)
        log(f"· {sname}: 첫 스캔, {len(new_rows)}건 기록만 (알림 없음)")
        return


    detail_left = config.DETAIL_FETCH_MAX_PER_SITE
    n_cand = 0
    for row in new_rows:
        pre = prefilter.classify_title(row.title)
        if not pre.passed and not pre.body_required:
            _mark(rt, site, row, STATUS_PREFILTER, pre.reason)
            continue
        sig = prefilter.title_signature(row.title)
        if len(sig) >= 8:
            if sig in recent_sigs:
                _mark(rt, site, row, STATUS_DUPLICATE, f"이미 알린 공고와 같은 제목 ({recent_sigs[sig].get('site_id', '')})")
                continue
            if sig in run_sigs:
                _mark(rt, site, row, STATUS_DUPLICATE, f"같은 실행의 다른 사이트 후보와 중복 ({run_sigs[sig]})")
                continue


        body, attachments, detail_ok, posted = "", [], False, row.posted_date
        if config.FETCH_DETAIL_BODY and detail_left > 0:
            detail_left -= 1
            if row.is_js or not row.url:
                d = sc.open_js_row(site, row, res.selector_used)
            else:
                d = sc.fetch_detail(row.url, referer=site["url"])
            if d.ok:
                body, attachments, detail_ok = d.body, d.attachments, True
                posted = posted or d.posted_date
            else:
                log(f"  본문 실패: {row.title[:40]} — {d.error}")


        if pre.body_required:
            pre = prefilter.classify_with_body(row.title, body)
            if not pre.passed:
                _mark(rt, site, row, STATUS_PREFILTER, pre.reason)
                continue
        if body and prefilter.body_says_closed(body):
            _mark(rt, site, row, STATUS_EXPIRED, "본문에 접수 마감 문구")
            continue
        ex = prefilter.extract_dates(body, row.title)
        if ex["deadline"] and prefilter.is_expired(ex["deadline"]):
            _mark(rt, site, row, STATUS_EXPIRED, f"마감 지남 ({ex['deadline']})")
            continue


        if len(sig) >= 8:
            run_sigs[sig] = sname
        candidates.append(Candidate(site, row, pre, body, attachments, detail_ok, ex, posted))
        n_cand += 1
    log(f"✓ {sname}: {len(res.rows)}행 · 새 글 {len(new_rows)} · 후보 {n_cand}"
        + (" · 셀렉터 자동 보정" if res.selector_changed else ""))




def _summary_text(s: dict) -> str:
    lines = [
        f"🧾 <b>스캔 요약</b> {esc(str(s['at'])[:16])} · {s['elapsed']}초",
        f"사이트 {s['sites_ok']}/{s['sites_total']} · 새 글 {s['new_rows']} (첫 스캔 기록 {s['seeded']}) · 1차 통과 {s['candidates']}",
        f"Gemini {s['gemini_calls']}회 (오류 {s['gemini_errors']} · 👎차단 {s['feedback_blocked']}) · "
        f"알림 {s['alerts']}건 · 확인요청 {s.get('reviewed', 0)}건 · 보류 {s['deferred']}",
    ]
    if s.get("gemini_last_error"):
        lines.append("🤖 Gemini 오류: " + esc(str(s["gemini_last_error"])[:160]))
    if s["sites_fail"]:
        lines.append("⚠️ 실패: " + ", ".join(esc(x) for x in s["sites_fail"][:8]))
    if s["disabled"]:
        lines.append(f"⏸ 자동 비활성(연속 {config.SITE_FAIL_DISABLE_AFTER}회 실패): "
                     + ", ".join(esc(x) for x in s["disabled"]) + " → /enable 로 재시도")
    if s.get("moved_kr"):
        lines.append("🇰🇷 해외 접속 차단 추정 → 한국 러너로 이관: " + ", ".join(esc(x) for x in s["moved_kr"])
                     + " (PC 가 켜지면 스캔 · 되돌리기 /kr off &lt;id&gt;)")
    if s["selector_fixed"]:
        lines.append("🔧 셀렉터 자동 보정: " + ", ".join(esc(x) for x in s["selector_fixed"][:8]))
    if s["send_errors"]:
        lines.append("✉️ 발송 실패: " + " / ".join(esc(x) for x in s["send_errors"][:3]))
    return "\n".join(lines)




def run_scan(rt: Runtime) -> int:
    t0 = time.monotonic()
    assert rt.client is not None
    problems = config.validate(need_telegram=True, need_gemini=True)
    if problems:
        msg = "설정 오류로 스캔 중단: " + " | ".join(problems)
        log(msg)
        if not DRY_RUN:
            broadcast(rt.client, "⚠️ " + esc(msg), config.TELEGRAM_ADMIN_CHAT_IDS, silent=False)
        return 1


    inbox = ({"updates": 0, "votes": 0, "commands": 0, "errors": 0, "skipped": True} if SKIP_INBOX
             else run_inbox(rt))
    if not rt.state.get("bot_enabled", True):
        log("봇이 중지 상태입니다. 이번 예약 스캔과 알림을 건너뜁니다.")
        rt.state.set(last_scan_at=config.now_kst_iso(), last_scan={
            "at": config.now_kst_iso(), "skipped": True, "reason": "bot_disabled", "inbox": inbox,
        })
        rt.save()
        return 0
    judge = Judge(rt.feedback)
    sites = _select_sites(rt)
    if not sites:
        log("활성 사이트가 없습니다. 봇에게 게시판 URL 을 보내 등록하세요.")
        return 0
    log(f"scan 시작: 사이트 {len(sites)}개 · 모델 {judge.model} · Gemini 상한 {judge.max_calls}회"
        + (" · DRY_RUN" if DRY_RUN else ""))


    recent_sigs = _recent_signatures(rt.known)
    run_sigs: dict[str, str] = {}
    stat = {"sites_ok": 0, "new_rows": 0, "seeded": 0}
    candidates: list[Candidate] = []
    fails: list[str] = []
    disabled: list[str] = []
    selector_fixed: list[str] = []


    # ── ② ③ 수집
    sc = rt.scraper()
    for site in sites:
        try:
            _collect_site(rt, sc, site, recent_sigs, run_sigs, stat, candidates, fails, disabled, selector_fixed)
        except Exception as e:                                    # noqa: BLE001
            traceback.print_exc()
            rt.sites.touch(site["id"], False, f"fail:{type(e).__name__}")
            fails.append(f"{site['name']} — {type(e).__name__}")
    rt.save()                                                     # 수집 결과 중간 저장


    # ── ④ Gemini 판정 (1차 점수 높은 것 · 최근 글 먼저)
    candidates.sort(key=lambda c: (c.pre.score, c.posted_date), reverse=True)
    to_alert: list[dict] = []
    to_review: list[dict] = []                                    # 확신 부족 — 사람이 👍/👎 로 직접 판단
    defer_max = int(getattr(config, "DEFER_MAX_RETRY", 3))         # 이 횟수 넘게 계속 실패하면 확인요청으로 전환
    fail_counts: dict = rt.state.get("defer_fail_counts", {}) or {}
    deferred = 0
    for c in candidates:
        post = c.post()
        v = judge.judge(post, c.body, extracted=c.extracted, attachments=c.attachments, prefilter_reason=c.pre.reason)
        dec = final_decision(post, v, c.extracted)
        if v.status in ("error", "budget", "skipped"):
            n = int(fail_counts.get(c.row.key, 0)) + 1
            if v.status == "error" and getattr(judge, "exhausted", False):
                n = max(n, defer_max)                             # 모델 자체가 없음 — 재시도 무의미, 이번 실행에서 확인요청
            if n >= defer_max:
                fail_counts.pop(c.row.key, None)
                post.update(ai_reason=f"AI 판정 실패 {n}회({v.status}: {v.reason[:60]}) — 1차 필터 근거: {c.pre.reason or '없음'}",
                            ai_confidence=0.0, category="", applicant="")
                to_review.append(post)
                _mark(rt, c.site, c.row, STATUS_LOW_CONF, f"판정 {n}회 연속 실패 → 확인요청 전환")
                log(f"  🤔 확인 요청(판정 {n}회 실패, {v.status}): {c.row.title[:40]}")
            else:
                fail_counts[c.row.key] = n
                deferred += 1
                log(f"  ⏳ 보류({v.status}, {n}/{defer_max}회): {c.row.title[:40]} — {v.reason[:80]}")
            continue
        fail_counts.pop(c.row.key, None)                           # 판정 성공 — 실패 기록 제거
        post.update(deadline=dec.deadline, start=dec.start, always_open=dec.always_open,
                    period_text=dec.period_text or post["period_text"], ai_reason=v.reason,
                    ai_confidence=v.confidence, category=v.category, applicant=v.applicant)
        if dec.alert:
            to_alert.append(post)
            log(f"  📢 알림 예정: {c.row.title[:40]} ({v.confidence:.0%}) — {v.reason[:60]}")
        else:
            _mark(rt, c.site, c.row, dec.status, dec.note)
            if dec.status == STATUS_LOW_CONF:
                to_review.append(post)
                log(f"  🤔 확인 요청(확신 {v.confidence:.0%}): {c.row.title[:40]} — {v.reason[:60]}")
            else:
                log(f"  – {dec.status}: {c.row.title[:40]} — {dec.note[:60]}")


    rt.state.set(defer_fail_counts=fail_counts)                   # 다음 실행에서 이어서 카운트


    # ── ⑤ 발송 (마감 임박 순, 마감 미확인은 뒤로)
    to_alert.sort(key=lambda p: (not p["deadline"], p["deadline"]))
    sent = 0
    send_errors: list[str] = []
    for post in to_alert:
        if sent >= config.ALERT_MAX_PER_RUN:
            deferred += 1
            continue
        if DRY_RUN:
            print("\n" + strip_tags(format_alert(post)) + "\n")
            sent += 1
            continue
        r = send_alert(rt.client, post, rt.feedback)
        send_errors += r["errors"]
        if r["sent"]:
            rt.known.mark(post["key"], site_id=post["site_id"], title=post["title"], url=post["url"],
                          status=STATUS_ALERTED, reason=post["ai_reason"][:200])
            sig = prefilter.title_signature(post["title"])
            if len(sig) >= 8:
                recent_sigs[sig] = {"site_id": post["site_id"]}
            sent += 1
            try:                                                  # 중복 알림 방지: 발송 즉시 기록
                rt.known.save()
                rt.feedback.save()
            except Exception as e:                                # noqa: BLE001
                log(f"저장 실패: {e}")


    # ── ⑤-보류 확신 부족 공고: '애매합니다, 확인해주세요' 로 발송 (👍/👎 는 기존 handle_vote 가 그대로 처리)
    review_max = int(getattr(config, "REVIEW_MAX_PER_RUN", 5))
    reviewed = 0
    for post in to_review[:review_max]:
        if DRY_RUN:
            print("\n" + strip_tags(format_alert(post, review=True)) + "\n")
            reviewed += 1
            continue
        r = send_alert(rt.client, post, rt.feedback, review=True)
        send_errors += r["errors"]
        if r["sent"]:
            reviewed += 1
            try:
                rt.feedback.save()
            except Exception as e:                                # noqa: BLE001
                log(f"저장 실패: {e}")


    # ── 공모 없음 알림 (실제 알림도, 확인요청도 하나도 없을 때만)
    if not DRY_RUN and sent == 0 and reviewed == 0 and not deferred and SCAN_RUNNER != "kr":
        broadcast(
            rt.client,
            f"📭 <b>새 공모 없음</b>  {esc(config.now_kst_iso()[:16])}\n"
            f"사이트 {stat['sites_ok']}/{len(sites)} 스캔 완료 · 새 글 {stat['new_rows']}건 검토\n"
            "<i>알림 기준에 맞는 공모가 없었습니다.</i>",
            config.TELEGRAM_ALERT_CHAT_IDS,
            silent=True,   # 무음 (소리 알림 원하면 False)
        )


    # ── ⑥ 마무리
    rt.known.prune()
    rt.feedback.prune()
    summary = {
        "at": config.now_kst_iso(), "elapsed": round(time.monotonic() - t0, 1),
        "sites_total": len(sites), "sites_ok": stat["sites_ok"], "sites_fail": fails, "disabled": disabled,
        "selector_fixed": selector_fixed, "new_rows": stat["new_rows"], "seeded": stat["seeded"],
        "candidates": len(candidates), "gemini_calls": judge.calls, "gemini_errors": judge.errors,
        "feedback_blocked": judge.blocked, "alerts": sent, "reviewed": reviewed, "deferred": deferred,
        "send_errors": send_errors, "inbox": inbox, "dry_run": DRY_RUN,
        "moved_kr": stat.get("moved_kr", []), "runner": SCAN_RUNNER or "github",
        "gemini_last_error": getattr(judge, "last_error", ""),
    }
    rt.state.set(last_scan_at=summary["at"], last_scan=summary)
    rt.save()


    log(f"scan 완료 {summary['elapsed']}s: 사이트 {stat['sites_ok']}/{len(sites)} · 새 글 {stat['new_rows']} · "
        f"후보 {len(candidates)} · Gemini {judge.calls}회(오류 {judge.errors}) · 알림 {sent} · 확인요청 {reviewed} · 보류 {deferred}"
        + (f" · 실패 {len(fails)}" if fails else "") + (f" · 비활성 {len(disabled)}" if disabled else ""))


    issues = bool(fails or disabled or judge.errors or send_errors)
    if not DRY_RUN and (SCAN_SUMMARY == "always" or (SCAN_SUMMARY == "issues" and issues)):
        errs = broadcast(rt.client, _summary_text(summary), config.TELEGRAM_ADMIN_CHAT_IDS, silent=True)
        for e in errs:
            log(f"요약 발송 실패: {e}")
    return 0




# ─────────────────────────────────────────────────────────────────────
# 4. check
# ─────────────────────────────────────────────────────────────────────
def run_check(rt: Runtime) -> int:
    print(config.summary())
    st = rt.feedback.stats()
    print(f"\n사이트 {len(rt.sites)}개 (활성 {len(rt.sites.enabled())}) · 기록 {len(rt.known)}건 · "
          f"알림 {st['alerts']}건 · 투표 {st['votes']}건 · offset {rt.state.offset}")
    failing = [s for s in rt.sites.enabled() if int(s.get("fail_count") or 0) > 0]
    for s in failing:
        print(f"  ⚠️ {s['name']} 실패 {s['fail_count']}회 — {s.get('last_status', '')}")
    ok = not config.validate(need_telegram=True, need_gemini=True)
    if rt.client is None:
        print("텔레그램: 토큰 없음")
        return 1
    try:
        me = rt.client.get_me()
        print(f"텔레그램 봇: {me.get('first_name')} (@{me.get('username')})  https://t.me/{me.get('username')}")
    except TelegramError as e:
        print(f"텔레그램 오류: {e}")
        ok = False
    if ok and not DRY_RUN:
        text = ("✅ <b>festival-alert 점검 완료</b>\n"
                f"사이트 {len(rt.sites)}개 (활성 {len(rt.sites.enabled())}) · 기록 {len(rt.known)}건\n"
                "스캔: 매일 09:00 · 13:30 · 18:00 KST / 명령 · 👍👎 처리: 15분 간격\n"
                "게시판 URL 을 보내면 바로 등록됩니다. 명령어: /help")
        errs = broadcast(rt.client, text, silent=True)
        print("확인 메시지 발송: " + ("성공" if not errs else " / ".join(errs)))
    print("\n결과: " + ("OK" if ok else "설정 문제 있음 — 위 항목 확인"))
    return 0 if ok else 1




# ─────────────────────────────────────────────────────────────────────
# 5. 진입점
# ─────────────────────────────────────────────────────────────────────
def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:                                             # noqa: BLE001
        pass
    mode = argv[1].lower() if len(argv) > 1 else "scan"
    if mode not in MODES:
        print(__doc__)
        return 2
    if DRY_RUN:
        log("DRY_RUN — 알림 발송 · 파일 저장 없음")
    rt: Optional[Runtime] = None
    try:
        rt = Runtime(need_client=bool(config.TELEGRAM_BOT_TOKEN))
        if rt.client is None and mode != "check":
            log("TELEGRAM_BOT_TOKEN 이 없어 실행할 수 없습니다.")
            return 1
        if mode == "check":
            return run_check(rt)
        if mode == "inbox":
            run_inbox(rt)
            return 0
        return run_scan(rt)
    except Exception:                                             # noqa: BLE001
        traceback.print_exc()
        if rt is not None:
            rt.save()                                             # 진행분 보존
        return 1
    finally:
        if rt is not None:
            rt.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))

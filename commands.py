# -*- coding: utf-8 -*-
"""
commands.py — 텔레그램 명령 처리                                                  [4단계 / 4]


  CommandHandler(client, sites, known, feedback, state, get_scraper).handle_message(message) -> str


명령 (텔레그램 입력창에서)
  /start /help            소개 · 명령어
  /id                     내 chat_id (화이트리스트에 없어도 응답 — 가족 등록용)
  /add <URL> [이름]       감시 사이트 자동 등록: 목록을 읽어 이름 · 셀렉터를 채우고, 기존 글은 알림 없이 기록
                          URL 만 보내도(명령 없이) 등록됨. /add 만 보내면 URL 을 물어봄 (/cancel 로 취소)
  /list                   사이트 목록 (상태 · 실패 횟수)
  /remove /disable /enable <id 또는 이름>   (관리자 chat_id 만)
  /status                 최근 스캔 결과 · 실패 사이트
  /stats                  알림 · 👍👎 통계 · 기록 분포
  /recent [N] [사이트|상태]  최근 판정 기록 N건 (제목 · 판정 · 근거 · 링크. 첫스캔/이관 기록 제외)


규칙
  · 화이트리스트(config.TELEGRAM_ALLOWED_CHAT_IDS) 밖 채팅은 /start /help /id 만 응답(chat_id 안내), 나머지 무시
  · 단톡방에서는 명령 · URL 이 아닌 일반 대화에 반응하지 않음
  · 파일 저장(.save()) 은 호출자(main.run_inbox) 가 한 번에 한다. 단 /add 는 사이트·기록을 즉시 저장.
"""
from __future__ import annotations


import re
import traceback
from collections import Counter
from typing import Callable, Optional
from urllib.parse import urlsplit


import config
from scraper import guess_site_name
from storage import (STATUS_AI_NO, STATUS_ALERTED, STATUS_EXPIRED, STATUS_LOW_CONF, STATUS_PREFILTER,
                     STATUS_SEEDED, BotState, FeedbackStore, KnownPosts, Sites, normalize_site, normalize_url)
from telegram_client import TelegramClient, TelegramError, esc, is_admin, is_allowed, user_display_name


_CMD_RE = re.compile(r"^/([A-Za-z_]\w*)(?:@\w+)?(?:\s+([\s\S]*))?$")
_URL_RE = re.compile(r"(?:https?://)?(?:[\w-]+\.)+[A-Za-z]{2,}(?:[:/?#]\S*)?")


STATUS_LABEL = {
    STATUS_ALERTED: "알림", STATUS_SEEDED: "첫스캔 기록", STATUS_PREFILTER: "1차 탈락", STATUS_AI_NO: "AI: 공모 아님",
    STATUS_LOW_CONF: "확신 부족", STATUS_EXPIRED: "마감", "stale": "오래된 글", "duplicate": "중복",
    "feedback_block": "👎 차단", "migrated": "이관",
}


HELP_TEXT = (
    "🤖 <b>공모 알림 봇</b>\n"
    "등록한 사이트에서 전시 · 공연 · 체험 분야의 <b>지금 신청 가능한 공모 · 지원사업</b>만 골라\n"
    "매일 <b>09:00 · 18:00</b> 에 알려드립니다.\n\n"
    "<b>명령어</b>\n"
    "/add &lt;게시판 URL&gt; [이름] — 사이트 등록 (URL 만 보내도 됨)\n"
    "/list — 감시 사이트 목록\n"
    "/remove · /disable · /enable &lt;id 또는 이름&gt;\n"
    "/status — 최근 스캔 결과\n"
    "/stats — 알림 · 👍👎 통계\n"
    "/recent [N] [사이트|상태] — 최근 판정 기록 (AI 근거 포함)\n"
    "/id — 내 chat_id\n\n"
    "알림의 👍/👎 버튼은 다음 판정에 반영됩니다.\n"
    "<i>명령과 버튼은 3시간 간격으로 처리되어 즉시 응답하지 않습니다.</i>"
)




def parse_command(text: str) -> tuple[str, str]:
    m = _CMD_RE.match((text or "").strip())
    if not m:
        return "", (text or "").strip()
    return m.group(1).lower(), (m.group(2) or "").strip()




def split_url_name(args: str) -> tuple[str, str]:
    url, rest = "", []
    for tok in (args or "").split():
        if not url and _URL_RE.fullmatch(tok):
            url = tok.strip("<>()[]\"'")
        else:
            rest.append(tok)
    return url, " ".join(rest).strip()




def _site_emoji(s: dict) -> str:
    if not s.get("enabled"):
        return "⏸"
    return "⚠️" if int(s.get("fail_count") or 0) > 0 else "✅"




class CommandHandler:
    def __init__(self, client: TelegramClient, sites: Sites, known: KnownPosts, feedback: FeedbackStore,
                 state: BotState, get_scraper: Optional[Callable] = None):
        self.client, self.sites, self.known, self.feedback, self.state = client, sites, known, feedback, state
        self.get_scraper = get_scraper


    # ---- 공용 -----------------------------------------------------------
    def _reply(self, chat_id, text: str, silent: bool = False) -> None:
        self.client.send_text(chat_id, text, silent=silent)


    def _denied_text(self, chat: dict, user: dict) -> str:
        return ("🔒 이 채팅은 아직 사용 권한이 없습니다.\n"
                f"chat_id: <code>{chat.get('id')}</code>  ({esc(chat.get('type', '?'))})\n"
                "관리자가 GitHub Secrets 의 <code>TELEGRAM_ALLOWED_CHAT_IDS</code> 에 이 값을 추가하면 사용할 수 있어요.")


    # ---- 진입 -----------------------------------------------------------
    def handle_message(self, message: dict) -> str:
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = (message.get("text") or "").strip()
        user = message.get("from") or {}
        if chat_id is None or not text:
            return "ignored"
        cmd, args = parse_command(text)
        allowed = is_allowed(chat_id)
        if not allowed:
            if cmd in ("start", "help", "id"):
                try:
                    self._reply(chat_id, self._denied_text(chat, user))
                except TelegramError as e:
                    print(f"[commands] 응답 실패 chat={chat_id}: {e}")
                return "denied"
            return "denied"


        if not cmd:
            pending = self.state.pending(chat_id)
            first = text.split()[0]
            if pending and pending.get("cmd") == "add":
                self.state.set_pending(chat_id, None)
                cmd, args = "add", text
            elif _URL_RE.fullmatch(first):
                cmd, args = "add", text
            elif chat.get("type") == "private":
                self._reply(chat_id, "게시판 URL 을 보내면 바로 등록합니다. 명령어는 /help 로 확인할 수 있어요.")
                return "hint"
            else:
                return "ignored"


        handler = getattr(self, f"_cmd_{cmd}", None)
        if handler is None:
            if chat.get("type") == "private":
                self._reply(chat_id, f"모르는 명령입니다: /{esc(cmd)}\n/help 로 명령어를 확인하세요.")
            return "unknown"
        try:
            return handler(chat_id, user, args, chat) or cmd
        except TelegramError as e:
            print(f"[commands] /{cmd} 텔레그램 오류 chat={chat_id}: {e}")
            return "error"
        except Exception as e:                                   # noqa: BLE001
            traceback.print_exc()
            try:
                self._reply(chat_id, f"⚠️ /{esc(cmd)} 처리 중 오류: {esc(type(e).__name__)} {esc(str(e)[:120])}")
            except TelegramError:
                pass
            return "error"


    # ---- 기본 -----------------------------------------------------------
    def _cmd_start(self, chat_id, user, args, chat):
        self._reply(chat_id, HELP_TEXT)


    def _cmd_help(self, chat_id, user, args, chat):
        self._reply(chat_id, HELP_TEXT)


    def _cmd_cancel(self, chat_id, user, args, chat):
        had = self.state.pending(chat_id) is not None
        self.state.set_pending(chat_id, None)
        self._reply(chat_id, "취소했습니다." if had else "진행 중인 작업이 없습니다.")


    def _cmd_id(self, chat_id, user, args, chat):
        role = "관리자" if is_admin(chat_id) else "사용 가능"
        lines = [f"chat_id: <code>{chat_id}</code>  ({esc(chat.get('type', '?'))}) — {role}",
                 f"사용자: {esc(user_display_name(user))}  (user_id <code>{user.get('id', '?')}</code>)"]
        if chat_id in config.TELEGRAM_ALERT_CHAT_IDS:
            lines.append("이 채팅으로 공모 알림이 발송됩니다.")
        self._reply(chat_id, "\n".join(lines))


    # ---- 사이트 등록 ----------------------------------------------------
    def _cmd_add(self, chat_id, user, args, chat):
        url, name = split_url_name(args)
        if not url:
            self.state.set_pending(chat_id, {"cmd": "add", "at": config.now_kst_iso()})
            self._reply(chat_id, "등록할 게시판의 <b>목록 페이지 URL</b> 을 보내주세요.\n"
                                 "예) https://www.gjcf.or.kr/board/notice\n(취소: /cancel)")
            return "add_pending"
        if "://" not in url:
            url = "https://" + url
        nu = normalize_url(url)
        for s in self.sites:
            if normalize_url(s["url"]) == nu:
                self._reply(chat_id, f"이미 등록된 사이트입니다: <b>{esc(s['name'])}</b> <code>{esc(s['id'])}</code>"
                                     + ("" if s["enabled"] else f"\n비활성 상태입니다 → /enable {esc(s['id'])}"))
                return "add_dup"


        who = user_display_name(user)
        res, err = None, ""
        try:
            sc = self.get_scraper() if self.get_scraper else None
            if sc is not None:
                res = sc.fetch_list(normalize_site({"url": url}))
        except Exception as e:                                   # noqa: BLE001
            err = f"{type(e).__name__}: {str(e)[:80]}"
        host = urlsplit(url).netloc


        if res is not None and res.ok:
            name = name or guess_site_name(res.page_title, url)
            ok, msg, site = self.sites.add(url, name, added_by=who, verified=True,
                                           last_checked=config.now_kst_iso(), last_status="ok", **res.selector_used)
            if not ok or site is None:
                self._reply(chat_id, "⚠️ " + esc(msg))
                return "add_fail"
            seeded = 0
            if config.FIRST_SCAN_SILENT:
                for row in res.rows:
                    if not self.known.is_known(row.key):
                        self.known.mark(row.key, site_id=site["id"], title=row.title, url=row.url,
                                        status=STATUS_SEEDED, reason=f"/add 등록 시 기존 글 ({who})")
                        seeded += 1
            self.sites.save()
            self.known.save()
            dated = sum(1 for r in res.rows if r.posted_date)
            sample = "\n".join(f"　· {esc(r.title[:60])}" for r in res.rows[:3])
            self._reply(chat_id,
                        "✅ <b>사이트 등록 완료</b>\n"
                        f"<b>{esc(site['name'])}</b>  <code>{esc(site['id'])}</code>\n{esc(site['url'])}\n\n"
                        f"게시글 {len(res.rows)}행 인식 (게시일 인식 {dated}행) · 셀렉터 "
                        f"{'자동 탐색' if res.selector_changed else '기본'}\n"
                        f"기존 글 {seeded}건은 알림 없이 기록했고, <b>다음 스캔부터 새 글만</b> 알려드립니다.\n"
                        f"표본:\n{sample}\n\n"
                        f"이름을 바꾸려면 /remove {esc(site['id'])} 후 /add URL 새이름")
            return "add_ok"


        reason = (res.error if res is not None else (err or "브라우저 사용 불가")).strip()
        ok, msg, site = self.sites.add(url, name or host, added_by=who, note=f"등록 시 목록 읽기 실패: {reason[:100]}")
        if not ok or site is None:
            self._reply(chat_id, "⚠️ " + esc(msg))
            return "add_fail"
        self.sites.save()
        self._reply(chat_id,
                    "⚠️ <b>등록은 했지만 목록을 읽지 못했습니다</b>\n"
                    f"<b>{esc(site['name'])}</b>  <code>{esc(site['id'])}</code>\n{esc(site['url'])}\n"
                    f"이유: {esc(reason[:120])}\n\n"
                    f"다음 스캔에서 다시 시도하며, {config.SITE_FAIL_DISABLE_AFTER}회 연속 실패하면 자동 비활성됩니다.\n"
                    "URL 이 게시판 <b>목록</b> 페이지인지 확인해 주세요. 잘못 등록했으면 "
                    f"/remove {esc(site['id'])}")
        return "add_warn"


    # ---- 사이트 관리 ----------------------------------------------------
    def _cmd_list(self, chat_id, user, args, chat):
        items = self.sites.all()
        if not items:
            self._reply(chat_id, "등록된 사이트가 없습니다. 게시판 URL 을 보내거나 /add 로 등록하세요.")
            return
        on = [s for s in items if s["enabled"]]
        failing = [s for s in on if int(s.get("fail_count") or 0) > 0]
        lines = [f"📋 <b>감시 사이트 {len(items)}개</b> (활성 {len(on)} · 실패 중 {len(failing)} · 비활성 {len(items) - len(on)})", ""]
        for i, s in enumerate(items, 1):
            checked = (s.get("last_checked") or "")[5:16] or "미확인"
            extra = f" · 실패 {s['fail_count']}회" if int(s.get("fail_count") or 0) else ""
            lines.append(f"{i}. {_site_emoji(s)} <b>{esc(s['name'])}</b>\n"
                         f"　 <code>{esc(s['id'])}</code> · 확인 {esc(checked)} · 기록 {self.known.count(s['id'])}건{extra}")
        lines.append("")
        lines.append("✅ 정상 · ⚠️ 최근 실패 · ⏸ 비활성    관리: /remove /disable /enable &lt;id&gt;")
        self._reply(chat_id, "\n".join(lines), silent=True)


    def _find_one(self, chat_id, query: str, verb: str) -> Optional[dict]:
        if not query:
            self._reply(chat_id, f"대상을 입력하세요: /{verb} &lt;id 또는 이름&gt;   (/list 에서 id 확인)")
            return None
        hits = self.sites.find(query)
        if len(hits) == 1:
            return hits[0]
        if not hits:
            self._reply(chat_id, f"'{esc(query)}' 에 해당하는 사이트가 없습니다. /list 로 확인하세요.")
            return None
        opts = "\n".join(f"　· {esc(h['name'])} — <code>{esc(h['id'])}</code>" for h in hits[:10])
        self._reply(chat_id, f"'{esc(query)}' 에 해당하는 사이트가 {len(hits)}개입니다. id 로 다시 지정해 주세요:\n{opts}")
        return None


    def _admin_only(self, chat_id) -> bool:
        if is_admin(chat_id):
            return True
        self._reply(chat_id, "이 명령은 관리자 채팅에서만 사용할 수 있습니다.")
        return False


    def _cmd_remove(self, chat_id, user, args, chat):
        if not self._admin_only(chat_id):
            return "denied"
        s = self._find_one(chat_id, args, "remove")
        if s is None:
            return
        self.sites.remove(s["id"])
        self.sites.save()
        self._reply(chat_id, f"🗑 삭제: <b>{esc(s['name'])}</b> <code>{esc(s['id'])}</code>\n"
                             f"다시 등록: /add {esc(s['url'])}")


    def _cmd_disable(self, chat_id, user, args, chat):
        if not self._admin_only(chat_id):
            return "denied"
        s = self._find_one(chat_id, args, "disable")
        if s is None:
            return
        self.sites.set_enabled(s["id"], False)
        self.sites.save()
        self._reply(chat_id, f"⏸ 비활성: <b>{esc(s['name'])}</b> — 스캔에서 제외됩니다. 재개: /enable {esc(s['id'])}")


    def _cmd_enable(self, chat_id, user, args, chat):
        if not self._admin_only(chat_id):
            return "denied"
        s = self._find_one(chat_id, args, "enable")
        if s is None:
            return
        self.sites.set_enabled(s["id"], True)
        self.sites.save()
        self._reply(chat_id, f"▶️ 활성: <b>{esc(s['name'])}</b> — 다음 스캔부터 다시 확인합니다.")


    # ---- 상태 · 통계 ----------------------------------------------------
    def _cmd_status(self, chat_id, user, args, chat):
        last = self.state.get("last_scan") or {}
        lines = ["🩺 <b>상태</b>"]
        if not last:
            lines.append("아직 스캔 기록이 없습니다. (첫 스캔은 기존 글을 기록만 하고 알림을 보내지 않습니다)")
        else:
            lines += [
                f"마지막 스캔: {esc(str(last.get('at', ''))[:16])} · {last.get('elapsed', '?')}초",
                f"사이트 {last.get('sites_ok', '?')}/{last.get('sites_total', '?')} 성공 · 새 글 {last.get('new_rows', 0)}"
                f" · 1차 통과 {last.get('candidates', 0)}",
                f"Gemini {last.get('gemini_calls', 0)}회 · 알림 {last.get('alerts', 0)}건 · 보류 {last.get('deferred', 0)}",
            ]
            if last.get("sites_fail"):
                lines.append("⚠️ 실패: " + ", ".join(esc(x) for x in last["sites_fail"][:8]))
            if last.get("disabled"):
                lines.append("⏸ 자동 비활성: " + ", ".join(esc(x) for x in last["disabled"]))
        failing = [s for s in self.sites.enabled() if int(s.get("fail_count") or 0) > 0]
        if failing:
            lines.append("")
            lines.append("연속 실패 중:")
            for s in failing:
                lines.append(f"　· {esc(s['name'])} {s['fail_count']}회 — {esc(str(s.get('last_status', ''))[:60])}")
        off = [s for s in self.sites.all() if not s["enabled"]]
        if off:
            lines.append(f"비활성 {len(off)}개: " + ", ".join(esc(s['name']) for s in off[:8]))
        lines.append("")
        lines.append(f"명령 처리: {esc(str(self.state.get('last_inbox_at') or '기록 없음')[:16])}")
        lines.append("스캔 예약: 매일 09:00 · 18:00 (KST, 수 분~수십 분 지연 가능)")
        self._reply(chat_id, "\n".join(lines), silent=True)


    def _cmd_stats(self, chat_id, user, args, chat):
        st = self.feedback.stats()
        voted = st["posts_voted"]
        acc = f"{st['up'] / voted:.0%}" if voted else "-"
        lines = [
            "📊 <b>알림 · 피드백 통계</b>",
            f"보낸 알림 {st['alerts']}건 (최근 {config.ALERT_KEEP_DAYS}일 보관) · 투표 {st['votes']}건",
            f"평가된 게시글 {voted}건 — 👍 {st['up']} / 👎 {st['down']}  (👍 비율 {acc})",
        ]
        by_site = sorted(st["by_site"].items(), key=lambda kv: (-kv[1]["down"], -kv[1]["up"]))
        if by_site:
            lines.append("")
            lines.append("사이트별 (👎 많은 순):")
            for sid, b in by_site[:8]:
                lines.append(f"　· {esc(b.get('name') or sid)} — 👍{b['up']} 👎{b['down']}")
        dist = Counter(r.get("status", "?") for r in self.known.posts.values())
        if dist:
            lines.append("")
            lines.append(f"기록된 게시글 {len(self.known)}건: " + " · ".join(
                f"{STATUS_LABEL.get(k, k)} {v}" for k, v in dist.most_common()))
        lines.append("")
        lines.append("👎 를 받은 공고와 같은 제목은 자동 차단되고, 최근 평가는 AI 판정 예시로 반영됩니다.")
        self._reply(chat_id, "\n".join(lines), silent=True)


    def _cmd_recent(self, chat_id, user, args, chat):
        """최근 판정 기록 — /recent [N] [사이트 id·이름 | 상태]  (기본 10건 · 최대 30건. 첫스캔·이관 기록은 제외)"""
        emoji = {STATUS_ALERTED: "📢", STATUS_LOW_CONF: "🤔", STATUS_AI_NO: "❌", STATUS_PREFILTER: "⛔",
                 STATUS_EXPIRED: "⏰", "duplicate": "🔁", "feedback_block": "👎", "stale": "🕰"}
        status_words = {k.lower(): k for k in STATUS_LABEL} | {v.lower(): k for k, v in STATUS_LABEL.items()}
        n, site_ids, status_q = 10, None, ""
        for tok in (args or "").split():
            tl = tok.lower()
            if tok.isdigit():
                n = max(1, min(int(tok), 30))
            elif tl in status_words:
                status_q = status_words[tl]
            elif site_ids is None and self.sites.find(tok):
                site_ids = {s["id"] for s in self.sites.find(tok)}
        rows = []
        for r in self.known.posts.values():
            st = r.get("status", "")
            if st in (STATUS_SEEDED, "migrated"):
                continue
            if site_ids is not None and r.get("site_id") not in site_ids:
                continue
            if status_q and st != status_q:
                continue
            rows.append(r)
        rows.sort(key=lambda r: str(r.get("last_seen") or r.get("first_seen") or ""), reverse=True)
        rows = rows[:n]
        if not rows:
            self._reply(chat_id, "조건에 맞는 판정 기록이 없습니다. (첫스캔 · 이관 기록은 표시하지 않습니다)\n"
                                 "예) /recent 20 · /recent ai_no · /recent 광주문화재단", silent=True)
            return
        title_q = f" — {esc(STATUS_LABEL.get(status_q, status_q))}" if status_q else ""
        lines = [f"🗂 <b>최근 판정 {len(rows)}건</b>{title_q}", ""]
        for i, r in enumerate(rows, 1):
            st = r.get("status", "?")
            site = self.sites.get(r.get("site_id") or "")
            sname = site["name"] if site else (r.get("site_id") or "?")
            title = esc((r.get("title") or "(제목 없음)")[:70])
            url = str(r.get("url") or "")
            head = f'<a href="{esc(url).replace(chr(34), "&quot;")}">{title}</a>' if url.startswith("http") else title
            when = str(r.get("last_seen") or r.get("first_seen") or "")[5:16]
            lines.append(f"{i}. {emoji.get(st, '▫️')} {head}\n"
                         f"　 {esc(sname)} · {esc(STATUS_LABEL.get(st, st))} · {esc(when)}")
            if r.get("reason"):
                lines.append(f"　 <i>{esc(str(r['reason'])[:120])}</i>")
        lines.append("")
        lines.append("필터: /recent 20 · /recent &lt;사이트 이름&gt; · /recent ai_no | low_conf | alerted | prefilter")
        self._reply(chat_id, "\n".join(lines), silent=True)

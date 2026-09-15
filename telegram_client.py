# -*- coding: utf-8 -*-
"""
telegram_client.py — 텔레그램 Bot API · 공모 알림 메시지 · 👍/👎 피드백 처리      [2단계 / 4]


  TelegramClient           Bot API 래퍼 (requests). 429/5xx/네트워크 오류 자동 재시도
  is_allowed / is_admin    chat_id 화이트리스트 검사 (config.TELEGRAM_ALLOWED_CHAT_IDS / ADMIN)
  format_alert(post)       알림 본문(HTML): 게시일 · 마감(D-day) · 접수기간 · 판정 근거 · 신뢰도 · 검증 근거
  vote_keyboard()          [👍 맞아요] [👎 아니에요] 인라인 버튼 (투표 수 표시)
  send_alert(...)          알림 대상 채팅 전체 발송 + FeedbackStore 등록 (버튼 ↔ 게시글 연결)
  handle_vote(...)         버튼 콜백 처리: 투표 기록 → 버튼 숫자 갱신 → 토스트
  broadcast(...)           시스템 공지 (사이트 자동 비활성, 실행 요약 등)


post(dict) 계약 — 3단계(scraper/judge) 가 채우고 4단계(main) 이 넘긴다
  key, site_id, site_name, title, url
  posted_date   "YYYY-MM-DD" | ""     게시일
  deadline      "YYYY-MM-DD" | ""     마감일
  period_text   원문 접수기간 문구 | ""
  always_open   True = 상시/수시 모집
  ai_reason     Gemini 한 줄 판정 근거
  ai_confidence 0.0 ~ 1.0
  body_checked  True = 본문까지 읽고 판정 / False = 제목만


로컬 점검
  python telegram_client.py me             봇 토큰 확인 (이름 · @username)
  python telegram_client.py updates        최근 수신 메시지의 chat_id 확인 (화이트리스트 설정용, offset 소비 없음)
  python telegram_client.py send "문구"    알림 대상 전체에 텍스트 발송
  python telegram_client.py test-alert     샘플 공모 알림(버튼 포함) 발송 — 버튼 응답은 4단계 inbox 가 처리
  python telegram_client.py commands       봇 명령 메뉴(/add, /list …) 등록 (1회)
"""
from __future__ import annotations


import html
import re
import sys
import time
from datetime import timedelta
from typing import Any, Iterable, Optional


import requests


import config
import prefilter
from storage import VOTE_DOWN, VOTE_UP, FeedbackStore


VOTE_PREFIX = "v:"              # callback_data = "v:up" / "v:down"
BTN_UP = "👍 맞아요"
BTN_DOWN = "👎 아니에요"


# 봇 명령 메뉴 — commands.py(4단계) 가 이 이름 그대로 구현한다
BOT_COMMANDS: list[tuple[str, str]] = [
    ("start",   "봇 소개 · 사용법"),
    ("help",    "명령어 목록"),
    ("id",      "내 chat_id 확인"),
    ("add",     "감시 사이트 등록  /add <게시판 URL> [이름]"),
    ("list",    "감시 사이트 목록"),
    ("remove",  "사이트 삭제  /remove <id 또는 이름>"),
    ("enable",  "사이트 활성화  /enable <id 또는 이름>"),
    ("disable", "사이트 비활성화  /disable <id 또는 이름>"),
    ("stats",   "알림 · 피드백 통계"),
    ("status",  "최근 스캔 상태"),
]




# ─────────────────────────────────────────────────────────────────────
# 1. API 클라이언트
# ─────────────────────────────────────────────────────────────────────
class TelegramError(Exception):
    def __init__(self, method: str, code: int, description: str):
        super().__init__(f"{method}: [{code}] {description}")
        self.method, self.code, self.description = method, code, description




class TelegramClient:
    def __init__(self, token: Optional[str] = None, timeout: Optional[float] = None):
        self.token = (token or config.TELEGRAM_BOT_TOKEN).strip()
        if not self.token:
            raise TelegramError("init", 0, "TELEGRAM_BOT_TOKEN 이 없습니다 (.env 또는 Secrets 확인)")
        self.timeout = float(timeout or config.TELEGRAM_TIMEOUT_SEC)
        self.session = requests.Session()
        self._last_send = 0.0


    # ---- 저수준 ---------------------------------------------------------
    def call(self, method: str, params: Optional[dict] = None, *,
             timeout: Optional[float] = None, retries: int = 3) -> Any:
        url = f"{config.TELEGRAM_API_BASE}/bot{self.token}/{method}"
        payload = {k: v for k, v in (params or {}).items() if v is not None}
        err: Optional[TelegramError] = None
        for attempt in range(retries + 1):
            try:
                r = self.session.post(url, json=payload, timeout=timeout or self.timeout)
            except requests.RequestException as e:
                msg = str(e).replace(self.token, "<token>")          # 로그에 토큰 노출 방지
                err = TelegramError(method, 0, f"네트워크 오류: {type(e).__name__} {msg[:120]}")
                if attempt < retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                break
            try:
                body = r.json()
            except ValueError:
                body = {}
            if body.get("ok"):
                return body.get("result")
            code = int(body.get("error_code") or r.status_code or 0)
            desc = str(body.get("description") or r.text[:200])
            err = TelegramError(method, code, desc)
            if code == 429:                                            # 발송 한도 → 지정 시간 대기 후 재시도
                wait = int((body.get("parameters") or {}).get("retry_after") or 3)
                time.sleep(min(wait + 1, 60))
                continue
            if code >= 500 and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            break
        assert err is not None
        raise err


    # ---- 조회 -----------------------------------------------------------
    def get_me(self) -> dict:
        return self.call("getMe") or {}


    def delete_webhook(self) -> None:
        """웹훅이 설정돼 있으면 getUpdates 가 409 로 실패하므로 폴링 전에 한 번 해제한다."""
        try:
            self.call("deleteWebhook", {"drop_pending_updates": False}, retries=1)
        except TelegramError as e:
            print(f"[telegram] deleteWebhook 실패(무시): {e}")


    def get_updates(self, offset: Optional[int] = None, limit: Optional[int] = None,
                    timeout_sec: int = 0) -> list[dict]:
        res = self.call("getUpdates", {
            "offset": offset,
            "limit": limit or config.TELEGRAM_POLL_LIMIT,
            "timeout": timeout_sec,
            "allowed_updates": ["message", "callback_query"],
        }, timeout=self.timeout + timeout_sec, retries=1)
        return list(res or [])


    # ---- 발송 -----------------------------------------------------------
    def _throttle(self) -> None:
        wait = config.TELEGRAM_SEND_INTERVAL - (time.monotonic() - self._last_send)
        if wait > 0:
            time.sleep(wait)
        self._last_send = time.monotonic()


    def send_message(self, chat_id: int | str, text: str, *, html_mode: bool = True,
                     reply_markup: Optional[dict] = None, reply_to: Optional[int] = None,
                     silent: bool = False) -> dict:
        self._throttle()
        params: dict[str, Any] = {
            "chat_id": chat_id, "text": text,
            "link_preview_options": {"is_disabled": True},
            "disable_notification": silent,
        }
        if html_mode:
            params["parse_mode"] = "HTML"
        if reply_markup:
            params["reply_markup"] = reply_markup
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        try:
            return self.call("sendMessage", params) or {}
        except TelegramError as e:
            if html_mode and e.code == 400 and "parse" in e.description.lower():
                params.pop("parse_mode", None)                         # HTML 깨짐 → 태그 제거 후 평문 재발송
                params["text"] = strip_tags(text)
                return self.call("sendMessage", params) or {}
            raise


    def send_text(self, chat_id: int | str, text: str, *, html_mode: bool = True,
                  silent: bool = False, reply_markup: Optional[dict] = None) -> list[dict]:
        """긴 텍스트를 한도에 맞게 나눠 보낸다. 버튼은 마지막 조각에만."""
        chunks = split_text(text, config.TELEGRAM_MAX_TEXT)
        out = []
        for i, chunk in enumerate(chunks):
            out.append(self.send_message(chat_id, chunk, html_mode=html_mode, silent=silent,
                                         reply_markup=reply_markup if i == len(chunks) - 1 else None))
        return out


    def edit_reply_markup(self, chat_id: int | str, message_id: int, reply_markup: Optional[dict]) -> Any:
        return self.call("editMessageReplyMarkup", {
            "chat_id": chat_id, "message_id": message_id,
            "reply_markup": reply_markup or {"inline_keyboard": []},
        }, retries=1)


    def answer_callback(self, callback_query_id: str, text: str = "", show_alert: bool = False) -> None:
        self.call("answerCallbackQuery", {
            "callback_query_id": callback_query_id,
            "text": text[:200] if text else None,
            "show_alert": show_alert,
        }, retries=0)


    def set_my_commands(self, commands: Optional[list[tuple[str, str]]] = None) -> None:
        self.call("setMyCommands", {
            "commands": [{"command": c, "description": d[:256]} for c, d in (commands or BOT_COMMANDS)],
        })




# ─────────────────────────────────────────────────────────────────────
# 2. 화이트리스트 · 텍스트 헬퍼
# ─────────────────────────────────────────────────────────────────────
def _to_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None




def is_allowed(chat_id: Any) -> bool:
    cid = _to_int(chat_id)
    return cid is not None and cid in config.TELEGRAM_ALLOWED_CHAT_IDS




def is_admin(chat_id: Any) -> bool:
    cid = _to_int(chat_id)
    return cid is not None and cid in config.TELEGRAM_ADMIN_CHAT_IDS




def user_display_name(user: Optional[dict]) -> str:
    user = user or {}
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x).strip()
    if name:
        return name
    if user.get("username"):
        return f"@{user['username']}"
    return str(user.get("id", "?"))




def esc(text: Any) -> str:
    """HTML 본문용 이스케이프 (< > &)"""
    return html.escape(str(text if text is not None else ""), quote=False)




def esc_attr(text: Any) -> str:
    """HTML 속성(href)용 이스케이프"""
    return html.escape(str(text if text is not None else ""), quote=True)




def strip_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text or ""))




def split_text(text: str, limit: int) -> list[str]:
    text = text or ""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks




# ─────────────────────────────────────────────────────────────────────
# 3. 알림 메시지 · 버튼
# ─────────────────────────────────────────────────────────────────────
def vote_keyboard(n_up: int = 0, n_down: int = 0) -> dict:
    return {"inline_keyboard": [[
        {"text": BTN_UP + (f" {n_up}" if n_up else ""), "callback_data": VOTE_PREFIX + VOTE_UP},
        {"text": BTN_DOWN + (f" {n_down}" if n_down else ""), "callback_data": VOTE_PREFIX + VOTE_DOWN},
    ]]}




def parse_vote_callback(data: Any) -> Optional[str]:
    if isinstance(data, str) and data.startswith(VOTE_PREFIX):
        v = data[len(VOTE_PREFIX):]
        if v in (VOTE_UP, VOTE_DOWN):
            return v
    return None




def tally(alert: Optional[dict]) -> tuple[int, int]:
    votes = (alert or {}).get("votes") or {}
    n_up = sum(1 for v in votes.values() if v == VOTE_UP)
    return n_up, len(votes) - n_up




def format_alert(post: dict, *, review: bool = False) -> str:
    title = esc((post.get("title") or "(제목 없음)")[:300])
    site = esc((post.get("site_name") or post.get("site_id") or "알 수 없는 사이트")[:60])
    url = str(post.get("url") or "")
    posted = str(post.get("posted_date") or "")
    deadline = str(post.get("deadline") or "")
    period = str(post.get("period_text") or "")[:120]
    reason = (str(post.get("ai_reason") or "").strip() or "판정 근거 없음")[:400]
    try:
        conf_txt = f"{float(post.get('ai_confidence')):.0%}"
    except (TypeError, ValueError):
        conf_txt = "미기재"
    basis = "제목+본문 검증" if post.get("body_checked") else "제목만 검증 (본문 열람 실패)"


    header = f"🤔 <b>애매한 공모 · 확인해주세요</b> · {site}" if review else f"📢 <b>새 공모</b> · {site}"
    lines = [header, f"<b>{title}</b>", ""]
    lines.append(f"🗓 게시일: {esc(posted) if posted else '미확인'}")
    if deadline:
        lines.append(f"⏰ 마감: <b>{esc(prefilter.format_deadline(deadline))}</b>")
        if period:
            lines.append(f"　 {esc(period)}")
    elif post.get("always_open"):
        lines.append("⏰ 마감: 상시/수시 모집" + (f" — {esc(period)}" if period else ""))
    elif period:
        lines.append(f"⏰ {esc(period)} (마감일 자동 추출 실패 — 본문 확인)")
    else:
        lines.append("⏰ 마감: 본문에서 확인 필요 (자동 추출 실패)")
    lines.append("")
    if review:
        lines.append(f"🤖 AI가 확신하지 못했습니다 (신뢰도 {conf_txt}) — {esc(reason)}")
        lines.append(basis)
    else:
        lines.append(f"🤖 {esc(reason)}")
        lines.append(f"신뢰도 {conf_txt} · {basis}")
    if url:
        lines.append(f'🔗 <a href="{esc_attr(url)}">공고 바로가기</a>')
    lines.append("")
    if review:
        lines.append("<i>공모가 맞으면 👍, 아니면 👎 — 다음 판정 정확도에 반영됩니다.</i>")
    else:
        lines.append("<i>맞는 알림이면 👍, 잘못 온 알림이면 👎 — 다음 판정에 반영됩니다.</i>")
    return "\n".join(lines)




def send_alert(client: TelegramClient, post: dict, feedback: FeedbackStore,
               chat_ids: Optional[Iterable[int]] = None, *, review: bool = False) -> dict:
    """알림 대상 전체에 발송하고 FeedbackStore 에 등록. 저장(feedback.save())은 호출자가 한다.
    review=True 면 확신 부족(low_conf) 판정을 '애매합니다, 확인해주세요' 문구로 발송한다.
    반환 {"sent": [(chat_id, message_id), ...], "errors": [str, ...]}"""
    targets = list(chat_ids) if chat_ids is not None else list(config.TELEGRAM_ALERT_CHAT_IDS)
    text = format_alert(post, review=review)
    result: dict[str, list] = {"sent": [], "errors": []}
    for cid in targets:
        try:
            msg = client.send_message(cid, text, reply_markup=vote_keyboard())
            mid = msg.get("message_id")
            if mid is not None:
                feedback.register_alert(cid, mid, post)
                result["sent"].append((cid, mid))
        except TelegramError as e:
            result["errors"].append(f"chat {cid}: {e}")
    return result




def broadcast(client: TelegramClient, text: str, chat_ids: Optional[Iterable[int]] = None, *,
              html_mode: bool = True, silent: bool = True) -> list[str]:
    """시스템 공지. 기본은 무음(휴대폰 알람 X). 실패 목록 반환."""
    errors: list[str] = []
    targets = list(chat_ids) if chat_ids is not None else list(config.TELEGRAM_ALERT_CHAT_IDS)
    for cid in targets:
        try:
            client.send_text(cid, text, html_mode=html_mode, silent=silent)
        except TelegramError as e:
            errors.append(f"chat {cid}: {e}")
    return errors




# ─────────────────────────────────────────────────────────────────────
# 4. 👍/👎 콜백 처리
# ─────────────────────────────────────────────────────────────────────
_TOAST = {
    ("recorded", VOTE_UP):   "👍 반영했습니다. 이런 공고는 계속 알려드릴게요.",
    ("recorded", VOTE_DOWN): "👎 반영했습니다. 이런 게시글은 다음 판정에서 걸러냅니다.",
    ("changed", VOTE_UP):    "의견을 👍 로 바꿨습니다.",
    ("changed", VOTE_DOWN):  "의견을 👎 로 바꿨습니다.",
    ("same", VOTE_UP):       "이미 👍 를 남기셨습니다.",
    ("same", VOTE_DOWN):     "이미 👎 를 남기셨습니다.",
}




def _answer_quietly(client: TelegramClient, cq_id: Optional[str], text: str = "", show_alert: bool = False) -> None:
    """3시간 간격 폴링에서는 콜백이 이미 만료('query is too old')된 경우가 대부분 → 조용히 무시."""
    if not cq_id:
        return
    try:
        client.answer_callback(cq_id, text, show_alert)
    except TelegramError as e:
        low = e.description.lower()
        if "too old" not in low and "invalid" not in low:
            print(f"[telegram] answerCallbackQuery 실패: {e}")




def handle_vote(client: TelegramClient, feedback: FeedbackStore, callback_query: dict) -> str:
    """반환: recorded | changed | same | unknown | denied | ignored   (feedback.save() 는 호출자가)"""
    cq = callback_query or {}
    cq_id = cq.get("id")
    vote = parse_vote_callback(cq.get("data"))
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    user = cq.get("from") or {}


    if vote is None or chat_id is None or message_id is None:
        _answer_quietly(client, cq_id)
        return "ignored"
    if not is_allowed(chat_id):
        _answer_quietly(client, cq_id, "이 채팅은 사용 권한이 없습니다.", show_alert=True)
        return "denied"


    status, alert = feedback.vote(chat_id, message_id, user.get("id", 0), user_display_name(user), vote)
    if status == "unknown" or alert is None:
        _answer_quietly(client, cq_id, f"보관 기간({config.ALERT_KEEP_DAYS}일)이 지난 알림이라 기록할 수 없습니다.",
                        show_alert=True)
        return "unknown"


    if status in ("recorded", "changed"):
        n_up, n_down = tally(alert)
        try:
            client.edit_reply_markup(chat_id, message_id, vote_keyboard(n_up, n_down))
        except TelegramError as e:
            if "not modified" not in e.description.lower():
                print(f"[telegram] 버튼 갱신 실패 chat={chat_id} msg={message_id}: {e}")
    _answer_quietly(client, cq_id, _TOAST.get((status, vote), "반영했습니다."))
    return status




# ─────────────────────────────────────────────────────────────────────
# 5. 로컬 점검 CLI
# ─────────────────────────────────────────────────────────────────────
def _sample_post() -> dict:
    today = config.now_kst().date()
    end = today + timedelta(days=14)
    return {
        "key": "t:sample-alert", "site_id": "sample_site", "site_name": "테스트 문화재단",
        "title": "[테스트] 2025 하반기 기획전시 참여작가 공모",
        "url": "https://example.org/board/view?id=1",
        "posted_date": today.isoformat(), "deadline": end.isoformat(),
        "period_text": f"접수기간: {today.strftime('%Y. %m. %d.')} ~ {end.strftime('%Y. %m. %d.')} 18:00까지",
        "always_open": False,
        "ai_reason": "기획전시에 참여할 작가를 모집하는 공모이며 접수기간과 신청 방법이 명시됨",
        "ai_confidence": 0.93, "body_checked": True,
    }




def _print_updates(client: TelegramClient) -> None:
    client.delete_webhook()
    ups = client.get_updates(offset=None, limit=100, timeout_sec=0)
    if not ups:
        print("수신된 메시지가 없습니다. 봇에게 아무 메시지나 보낸 뒤(단톡방이면 봇 초대 후 메시지) 다시 실행하세요.")
        return
    print(f"{'update_id':>10}  {'chat_id':>15}  {'종류':8} 채팅/보낸 사람  ·  내용")
    ids: set[int] = set()
    for u in ups:
        src = u.get("message") or u.get("callback_query") or {}
        msg = u.get("message") or (u.get("callback_query") or {}).get("message") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if isinstance(cid, int):
            ids.add(cid)
        who = chat.get("title") or user_display_name(src.get("from"))
        content = (u.get("message") or {}).get("text") or (u.get("callback_query") or {}).get("data") or ""
        print(f"{u.get('update_id', '?'):>10}  {str(cid):>15}  {chat.get('type', '?'):8} {who}  ·  {content[:40]}")
    if ids:
        print("\nTELEGRAM_ALLOWED_CHAT_IDS=" + ",".join(str(i) for i in sorted(ids)))
        print("(개인 채팅은 양수, 단톡방은 음수 id. 필요한 것만 골라 넣으세요)")




def main(argv: list[str]) -> None:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    cmd = argv[1].lower() if len(argv) > 1 else "help"
    if cmd not in ("me", "updates", "send", "test-alert", "commands"):
        print(__doc__)
        return
    if not config.TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN 이 없습니다. .env 를 확인하세요.")
        return
    client = TelegramClient()


    if cmd == "me":
        me = client.get_me()
        print(f"봇 이름   : {me.get('first_name')}  (@{me.get('username')})  id={me.get('id')}")
        print(f"대화 링크 : https://t.me/{me.get('username')}")
        return
    if cmd == "updates":
        _print_updates(client)
        return
    if cmd == "commands":
        client.set_my_commands()
        print("명령 메뉴 등록 완료: " + ", ".join("/" + c for c, _ in BOT_COMMANDS))
        return


    problems = config.validate(need_telegram=True)
    if problems:
        print("설정 오류: " + " | ".join(problems))
        return
    if cmd == "send":
        text = " ".join(argv[2:]).strip() or "테스트 메시지입니다."
        errors = broadcast(client, esc(text), silent=False)
        print(f"발송 대상 {len(config.TELEGRAM_ALERT_CHAT_IDS)}곳" + (f", 실패 {len(errors)}: {errors}" if errors else ", 모두 성공"))
        return
    if cmd == "test-alert":
        fb = FeedbackStore()
        res = send_alert(client, _sample_post(), fb)
        fb.save()
        print(f"샘플 알림 발송: 성공 {len(res['sent'])}건, 실패 {len(res['errors'])}건")
        for e in res["errors"]:
            print("  -", e)
        if res["sent"]:
            print("휴대폰에서 알림과 👍/👎 버튼을 확인하세요. 버튼 반영은 4단계 inbox 실행 시 처리됩니다.")




if __name__ == "__main__":
    main(sys.argv)

# -*- coding: utf-8 -*-
"""
judge.py — Gemini 2차 판정 · 가족 피드백 반영 · 최종 결정                          [3단계 / 4]


Gemini API 를 REST(requests) 로 직접 호출한다 — SDK(google-generativeai) 불필요, 응답은 JSON 스키마로 강제.


  Judge(feedback)                        실행 1회당 1개. 호출 상한 · 호출 간격 · 모델 대체(429/404) 관리
    .judge(post, body, extracted=, attachments=, prefilter_reason=)  → Verdict
    .budget_left() / .stats()
  final_decision(post, verdict, extracted) → Decision  (알림 여부 · known_posts 상태 · 표시할 날짜)
  feedback_block_reason(post, feedback)    👎 받은 공고와 같은 제목/키면 Gemini 호출 없이 차단
  build_system_prompt(feedback) · build_user_prompt(...)


판정 흐름 (4단계 main)
  prefilter 통과 → 👎 동일 공고 차단 → Gemini → final_decision → 알림 / known_posts 기록
  Verdict.status : ok | feedback_block | skipped(API 키 없음) | budget(호출 상한) | error(API 오류)
                   error · budget 인 글은 main 이 known 에 기록하지 않고 다음 실행에서 다시 판정한다.
                   (모든 모델이 404 면 Judge.exhausted=True → main 이 즉시 확인요청으로 전환)


피드백 루프
  · FeedbackStore.examples() — 게시글별 가족 합의(의견이 갈리면 👎), 👎 우선 — 를 시스템 프롬프트 예시로 넣는다
  · 👎 받은 글과 prefilter.title_signature 가 같은 글은 즉시 차단 (집계 사이트 재게재 · 재공지 방지)


로컬 점검
  python judge.py prompt                     현재 시스템 프롬프트(피드백 예시 포함) + 사용자 프롬프트 예시 출력
  python judge.py models                     API 키로 쓸 수 있는 Gemini 모델 목록
  python judge.py test                       내장 샘플 4건 판정 (API 4회 호출)
  python judge.py title "제목" ["본문"]      텍스트만으로 판정
  python judge.py url <게시글 URL> ["제목"]   scraper 로 본문을 읽어 실제 판정 (알림 X)
"""
from __future__ import annotations


import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any, Optional
from urllib.parse import urlsplit


import requests


import config
import prefilter
from storage import (STATUS_AI_NO, STATUS_ALERTED, STATUS_EXPIRED, STATUS_LOW_CONF, VOTE_DOWN,
                     FeedbackStore)


GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-2.5-flash"
FALLBACK_MODELS: list[str] = list(getattr(config, "GEMINI_FALLBACK_MODELS", ["gemini-2.5-flash-lite", "gemini-3.1-flash-lite"]))
MAX_POST_AGE_DAYS = int(getattr(config, "MAX_POST_AGE_DAYS", 60))      # 게시 후 n일 지났고 마감 정보도 없으면 보류
ALERT_REANNOUNCE = bool(getattr(config, "ALERT_REANNOUNCE", False))    # 재공고·기간연장 글도 알릴지


STATUS_STALE = "stale"
STATUS_FEEDBACK_BLOCK = "feedback_block"


CATEGORIES = ["전시", "공연", "체험", "복합", "기타"]
FLAG_WORDS = ["결과발표", "참가자모집", "일정안내", "공지행정", "채용입찰", "마감지남", "본문없음", "분야무관", "상시모집", "재공지"]


RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "is_call":     {"type": "BOOLEAN", "description": "지금 신청 가능한 전시·공연·체험 분야 공모/모집/지원사업 공고인가"},
        "confidence":  {"type": "NUMBER", "description": "0~1"},
        "category":    {"type": "STRING", "enum": CATEGORIES},
        "applicant":   {"type": "STRING", "description": "신청 주체 (예: 시각예술 작가, 공연단체, 체험프로그램 운영업체, 일반 시민)"},
        "start":       {"type": "STRING", "description": "접수 시작 YYYY-MM-DD 또는 빈 문자열"},
        "deadline":    {"type": "STRING", "description": "접수 마감 YYYY-MM-DD 또는 빈 문자열"},
        "always_open": {"type": "BOOLEAN"},
        "reason":      {"type": "STRING", "description": "60자 이내 한국어 근거 한 문장"},
        "flags":       {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["is_call", "confidence", "category", "applicant", "start", "deadline", "always_open", "reason", "flags"],
}




# ─────────────────────────────────────────────────────────────────────
# 1. 자료형
# ─────────────────────────────────────────────────────────────────────
@dataclass
class Verdict:
    is_call: bool = False
    confidence: float = 0.0
    reason: str = ""
    category: str = "기타"
    applicant: str = ""
    start: str = ""
    deadline: str = ""
    always_open: bool = False
    flags: list = field(default_factory=list)
    status: str = "ok"                 # ok | feedback_block | skipped | budget | error
    model: str = ""
    raw: str = ""


    def to_dict(self) -> dict:
        return asdict(self)




@dataclass
class Decision:
    alert: bool
    status: str                        # storage.STATUS_* / stale / feedback_block / error / budget / skipped
    note: str
    deadline: str = ""
    start: str = ""
    always_open: bool = False
    period_text: str = ""


    def to_dict(self) -> dict:
        return asdict(self)




# ─────────────────────────────────────────────────────────────────────
# 2. 프롬프트
# ─────────────────────────────────────────────────────────────────────
_SYSTEM_TEMPLATE = """당신은 한국 문화예술 기관 게시판 글을 심사하는 분류기입니다.
사용자: 전시·공연·체험 프로그램을 기획·운영·창작하는 예술가/사업자 가족. "지금 신청할 수 있는 공모·모집·지원사업 공고"만 휴대폰 알림으로 받고 싶어 하며, 잘못 온 알림을 매우 싫어합니다.


■ is_call = true 조건 (모두 충족해야 함)
1. 글의 목적이 신청을 받는 것: 공모 · 모집공고 · 지원사업 · 위탁운영자(업체·단체) 선정 · 제안서 접수 등
2. 신청 주체가 전시·공연·체험을 '제공하는 쪽': 작가 · 예술가 · 예술단체 · 공연팀 · 기획자 · 운영업체 · 사업자 · 강사 등
3. 전시 / 공연 / 체험(체험프로그램 · 체험부스 운영) 분야와 직접 관련
4. 오늘 기준 접수 진행 중 또는 예정 (마감이 지났으면 false)


■ is_call = false (대표 유형)
- 선정결과 · 심사결과 · 당선작 발표, 공모 결과 안내, 최종 선정 명단
- 관람객 · 참가자 · 체험자 · 수강생 · 교육생 · 서포터즈 · 자원봉사자 모집 = 일반 시민이 소비자로 참여하는 모집
- 전시 · 공연 일정 안내, 개막 · 행사 · 프로그램 운영 안내, 예매 · 티켓 안내
- 휴관 · 시설 · 변경 · 취소 공지, 보도자료, 뉴스레터, 결과보고 · 백서 · 설문
- 직원 · 인턴 채용, 물품구매 · 시설공사 · 일반 용역 입찰
  (단, 전시·공연·체험 프로그램의 기획·운영·제작 업체·단체를 선정하는 제안서 공모 · 위탁운영 모집은 true)
- 첨부파일 · 이미지 · 메뉴 등 게시글이 아닌 것
- {reannounce_rule}


■ 판정 규칙
- 제목보다 본문을 우선한다. 본문에서 '누가 신청하는가'와 '접수 기간'을 찾는다.
- 본문이 없으면 제목 · 첨부파일명으로 판단하되 confidence 는 0.75 이하.
- confidence: 신청 주체 · 접수기간 · 분야가 모두 명확할 때만 0.9 이상. 하나라도 불명확하면 0.7 이하.
- 애매하면 false. (놓치는 것보다 잘못 알리는 것이 더 나쁨)
- start / deadline: 본문에 있는 날짜만 YYYY-MM-DD 로. 연도가 없으면 게시일의 연도로 추정. 없으면 "". 상시 · 수시 모집이면 always_open = true.
- reason: 60자 이내 한국어 한 문장, 판단 근거 문구 포함. 예) "참여작가 모집, 접수 5.1~5.20 명시" / "선정결과 발표 글"
- category: 전시 / 공연 / 체험 / 복합 / 기타.  applicant: 신청 주체.
- flags: 해당하는 것 모두 — {flag_words}


■ 가족 피드백 (과거 알림에 대한 평가) — 유사한 글은 같은 방향으로 판정한다
👎 = 잘못된 알림(공모 아님 · 분야 무관 · 중복), 👍 = 올바른 알림
{feedback_block}


JSON 객체 하나만 출력합니다."""


_REANNOUNCE_EXCLUDE = "재공고 · 재공지 · 접수기간 연장 공지: 원 공고를 이미 알렸을 가능성이 크므로 false, flags 에 '재공지'"
_REANNOUNCE_INCLUDE = "재공고 · 접수기간 연장 공지: 신청이 가능하면 true 로 두되 flags 에 '재공지' 를 넣는다"




def _feedback_block(feedback: Optional[FeedbackStore]) -> str:
    if feedback is None:
        return "(아직 피드백 없음)"
    try:
        examples = feedback.examples()
    except Exception:                                            # noqa: BLE001
        examples = []
    if not examples:
        return "(아직 피드백 없음)"
    lines = []
    for c in examples:
        mark = "👎 잘못된 알림" if c.get("vote") == VOTE_DOWN else "👍 올바른 알림"
        site = f" ({c['site_name']})" if c.get("site_name") else ""
        why = f" — 당시 AI 근거: {str(c['ai_reason'])[:60]}" if c.get("ai_reason") else ""
        lines.append(f'- {mark}: "{str(c.get("title", ""))[:80]}"{site}{why}')
    return "\n".join(lines)




def build_system_prompt(feedback: Optional[FeedbackStore] = None) -> str:
    return (_SYSTEM_TEMPLATE
            .replace("{reannounce_rule}", _REANNOUNCE_INCLUDE if ALERT_REANNOUNCE else _REANNOUNCE_EXCLUDE)
            .replace("{flag_words}", ", ".join(FLAG_WORDS))
            .replace("{feedback_block}", _feedback_block(feedback)))




_HINT_RE = re.compile("|".join(map(re.escape, [
    "접수", "신청", "모집", "마감", "공모", "지원 대상", "지원대상", "참가 자격", "참가자격", "신청 자격", "신청자격", "제출"])))




def _excerpt(body: str, limit: Optional[int] = None) -> str:
    """본문이 길면 앞 60% + '접수·신청·모집·마감' 주변 발췌로 한도 안에 맞춘다."""
    limit = limit or config.GEMINI_BODY_MAX_CHARS
    body = (body or "").strip()
    if len(body) <= limit:
        return body
    head_len = int(limit * 0.6)
    pieces: list[str] = []
    used, last_end = 0, head_len
    for m in _HINT_RE.finditer(body, head_len):
        s, e = max(m.start() - 60, last_end), min(m.start() + 200, len(body))
        if s >= e:
            continue
        piece = body[s:e]
        if used + len(piece) > limit - head_len:
            break
        pieces.append(piece)
        used += len(piece)
        last_end = e
    return body[:head_len] + ("\n…\n" + "\n…\n".join(pieces) if pieces else "\n…(이하 생략)")




def build_user_prompt(post: dict, body: str = "", extracted: Optional[dict] = None,
                      attachments: Optional[list] = None, prefilter_reason: str = "") -> str:
    ex = extracted or {}
    period = "없음"
    if ex.get("always_open"):
        period = "상시/수시 모집 문구 있음"
    if ex.get("deadline") or ex.get("start"):
        period = f"{ex.get('start') or '?'} ~ {ex.get('deadline') or '?'}"
        if ex.get("period_text"):
            period += f'  (원문: "{str(ex["period_text"])[:100]}")'
    body_txt = _excerpt(body)
    lines = [
        f"[오늘] {config.today_kst()} (KST)",
        f"[사이트] {post.get('site_name') or post.get('site_id') or '미상'}",
        f"[제목] {post.get('title', '')}",
        f"[게시일] {post.get('posted_date') or '미확인'}",
        f"[URL] {post.get('url') or '(javascript 링크)'}",
        f"[1차 키워드 필터] {prefilter_reason or '-'}",
        f"[자동 추출 접수기간] {period}",
        f"[첨부파일] {', '.join(str(a) for a in attachments[:8]) if attachments else '없음'}",
        "[본문]",
        body_txt if body_txt else "(본문을 읽지 못함 — 제목 · 첨부파일명만으로 판단하고 confidence 는 0.75 이하로)",
    ]
    return "\n".join(lines)




# ─────────────────────────────────────────────────────────────────────
# 3. 피드백 차단
# ─────────────────────────────────────────────────────────────────────
def _down_signatures(feedback: Optional[FeedbackStore]) -> dict[str, str]:
    out: dict[str, str] = {}
    if feedback is None:
        return out
    try:
        cons = feedback.consensus()
    except Exception:                                            # noqa: BLE001
        return out
    for c in cons.values():
        if c.get("vote") != VOTE_DOWN:
            continue
        title = str(c.get("title") or "")
        sig = prefilter.title_signature(title)
        if len(sig) >= 8:
            out[sig] = title
        if c.get("key"):
            out["key:" + str(c["key"])] = title
    return out




def feedback_block_reason(post: dict, feedback: Optional[FeedbackStore], down_sigs: Optional[dict] = None) -> str:
    """👎 받은 게시글과 같은 key 또는 같은 제목 서명이면 차단 이유를, 아니면 '' 반환"""
    sigs = down_sigs if down_sigs is not None else _down_signatures(feedback)
    if not sigs:
        return ""
    key = str(post.get("key") or "")
    if key and ("key:" + key) in sigs:
        return "가족이 👎 평가한 게시글과 동일 (재알림 차단)"
    sig = prefilter.title_signature(post.get("title", ""))
    if len(sig) >= 8 and sig in sigs:
        return f'가족이 👎 평가한 공고와 같은 제목: "{sigs[sig][:40]}"'
    return ""




# ─────────────────────────────────────────────────────────────────────
# 4. 응답 해석
# ─────────────────────────────────────────────────────────────────────
def _load_json(text: str) -> Optional[dict]:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.I)
    t = re.sub(r"\s*```$", "", t)
    try:
        obj = json.loads(t)
    except ValueError:
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            return None
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        obj = obj[0]
    return obj if isinstance(obj, dict) else None




def _to_date(v: Any) -> str:
    s = str(v or "").strip()
    return prefilter.parse_date(s) if s else ""




def parse_verdict(text: str, model: str = "") -> Verdict:
    obj = _load_json(text)
    if obj is None:
        return Verdict(status="error", reason="Gemini 응답 JSON 파싱 실패", model=model, raw=(text or "")[:500])
    try:
        conf = float(obj.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    if conf > 1.0:
        conf = conf / 100.0 if conf <= 100 else 1.0
    conf = max(0.0, min(1.0, conf))
    is_call = obj.get("is_call")
    if isinstance(is_call, str):
        is_call = is_call.strip().lower() in ("true", "yes", "y", "1", "예")
    flags = obj.get("flags") or []
    if isinstance(flags, str):
        flags = [f.strip() for f in re.split(r"[,/、]", flags) if f.strip()]
    flags = [str(f).strip() for f in flags if str(f).strip()][:8]
    category = str(obj.get("category") or "기타").strip()
    if category not in CATEGORIES:
        category = "기타"
    reason = re.sub(r"\s+", " ", str(obj.get("reason") or "")).strip()[:200] or "근거 미기재"
    return Verdict(
        is_call=bool(is_call), confidence=conf, reason=reason, category=category,
        applicant=str(obj.get("applicant") or "").strip()[:60],
        start=_to_date(obj.get("start")), deadline=_to_date(obj.get("deadline")),
        always_open=bool(obj.get("always_open")), flags=flags, status="ok", model=model, raw=(text or "")[:1000],
    )




def _extract_text(data: dict) -> tuple[str, str]:
    cands = data.get("candidates") or []
    if not cands:
        pf = data.get("promptFeedback") or {}
        return "", f"응답 후보 없음 (blockReason={pf.get('blockReason', '?')})"
    c0 = cands[0] or {}
    parts = (c0.get("content") or {}).get("parts") or []
    text = "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict)).strip()
    if not text:
        return "", f"빈 응답 (finishReason={c0.get('finishReason', '?')})"
    return text, ""




def _retry_delay(resp: requests.Response, msg: str) -> float:
    try:
        for d in (resp.json().get("error") or {}).get("details") or []:
            rd = d.get("retryDelay") if isinstance(d, dict) else None
            if rd:
                return float(str(rd).rstrip("s")) + 1
    except (ValueError, AttributeError, TypeError):
        pass
    m = re.search(r"retry in ([\d.]+)\s*s", msg, re.I)
    return float(m.group(1)) + 1 if m else 20.0




# ─────────────────────────────────────────────────────────────────────
# 5. Judge
# ─────────────────────────────────────────────────────────────────────
class Judge:
    def __init__(self, feedback: Optional[FeedbackStore] = None, *, api_key: Optional[str] = None,
                 model: Optional[str] = None, max_calls: Optional[int] = None, dry_run: bool = False):
        self.api_key = (api_key or config.GEMINI_API_KEY or "").strip()
        primary = (model or config.GEMINI_MODEL or DEFAULT_MODEL).strip()
        self.models = [primary] + [m for m in FALLBACK_MODELS if m and m != primary]
        self.model_idx = 0
        self.max_calls = config.GEMINI_MAX_CALLS_PER_RUN if max_calls is None else int(max_calls)
        self.calls = 0
        self.errors = 0
        self.blocked = 0
        self.last_error = ""            # 마지막 Gemini 오류 원문 (스캔 요약 표시용)
        self.exhausted = False          # True = 모든 모델 404 → 이번 실행의 남은 호출 생략
        self._last_call = 0.0
        self.feedback = feedback
        self.dry_run = bool(dry_run) or not self.api_key
        self.system_prompt = build_system_prompt(feedback)
        self._down_sigs = _down_signatures(feedback)
        self.session = requests.Session()


    @property
    def model(self) -> str:
        return self.models[min(self.model_idx, len(self.models) - 1)]


    def budget_left(self) -> int:
        return max(0, self.max_calls - self.calls)


    def stats(self) -> dict:
        return {"calls": self.calls, "errors": self.errors, "blocked": self.blocked,
                "model": self.model, "budget_left": self.budget_left(), "dry_run": self.dry_run,
                "last_error": self.last_error, "exhausted": self.exhausted}

    # ---- 판정 -----------------------------------------------------------
    def judge(self, post: dict, body: str = "", *, extracted: Optional[dict] = None,
              attachments: Optional[list] = None, prefilter_reason: str = "") -> Verdict:
        blocked = feedback_block_reason(post, self.feedback, self._down_sigs)
        if blocked:
            self.blocked += 1
            return Verdict(is_call=False, confidence=1.0, reason=blocked, status=STATUS_FEEDBACK_BLOCK, flags=["재공지"])
        if self.dry_run:
            return Verdict(status="skipped", reason="GEMINI_API_KEY 없음 — 판정 생략")
        if self.budget_left() <= 0:
            return Verdict(status="budget", reason=f"Gemini 호출 상한({self.max_calls}회) 도달 — 다음 실행에서 판정")
        if self.exhausted:                                       # 모델 전멸 — 호출하지 않고 바로 오류 반환
            self.errors += 1
            return Verdict(status="error", reason=self.last_error or "사용 가능한 Gemini 모델 없음", model=self.model)
        prompt = build_user_prompt(post, body, extracted, attachments, prefilter_reason)
        text, model, err = self._generate(prompt)
        if err:
            self.errors += 1
            self.last_error = err
            print(f"[judge] 오류: {err}")
            return Verdict(status="error", reason=err, model=model)
        v = parse_verdict(text, model)
        if v.status == "error":
            self.errors += 1
            self.last_error = v.reason
        return v


    # ---- API ------------------------------------------------------------
    def _throttle(self) -> None:
        wait = config.GEMINI_MIN_INTERVAL_SEC - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()


    def _next_model(self) -> bool:
        if self.model_idx < len(self.models) - 1:
            self.model_idx += 1
            print(f"[judge] 모델 대체 → {self.model}")
            return True
        return False


    def _payload(self, model: str, prompt: str) -> dict:
        gen: dict[str, Any] = {
            "temperature": 0.1, "topP": 0.9, "maxOutputTokens": 1024,
            "responseMimeType": "application/json", "responseSchema": RESPONSE_SCHEMA,
        }
        if "2.5" in model and "flash" in model:
            gen["thinkingConfig"] = {"thinkingBudget": 0}          # 분류 작업엔 불필요 — 속도 · 토큰 절약
        return {
            "system_instruction": {"parts": [{"text": self.system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": gen,
        }


    def _generate(self, prompt: str) -> tuple[str, str, str]:
        """반환 (응답 텍스트, 사용 모델, 오류메시지). 오류면 텍스트는 ''"""
        last_err = ""
        attempts = config.GEMINI_RETRY + 1 + len(self.models)
        for _ in range(attempts):
            if self.calls >= self.max_calls:
                return "", self.model, last_err or "Gemini 호출 상한 도달"
            model = self.model
            self._throttle()
            self.calls += 1
            try:
                r = self.session.post(
                    f"{GEMINI_API_BASE}/models/{model}:generateContent",
                    headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
                    json=self._payload(model, prompt), timeout=config.GEMINI_TIMEOUT_SEC,
                )
            except requests.RequestException as e:
                last_err = f"네트워크 오류: {type(e).__name__}"
                time.sleep(3)
                continue
            if r.status_code == 200:
                try:
                    data = r.json()
                except ValueError:
                    last_err = "Gemini 응답이 JSON 이 아님"
                    continue
                text, why = _extract_text(data)
                if text:
                    return text, model, ""
                last_err = why
                continue
            try:
                msg = str(((r.json().get("error") or {}).get("message")) or "")[:200]
            except ValueError:
                msg = r.text[:200]
            low = msg.lower()
            code = r.status_code
            if code == 404 or (code == 400 and ("not found" in low or "not supported" in low)):
                last_err = f"모델 {model} 사용 불가: {msg}"
                if not self._next_model():
                    self.exhausted = True
                    print(f"[judge] 모든 모델 사용 불가 ({', '.join(self.models)}) — 이번 실행 Gemini 호출 중단")
                    return "", model, last_err
                continue
            if code == 429:
                last_err = f"Gemini 한도 초과(429): {msg[:80]}"
                wait = _retry_delay(r, msg)
                if wait > 45 and self._next_model():                # 오래 기다려야 하면 대체 모델로
                    continue
                time.sleep(min(wait, 60))
                continue
            if code >= 500:
                last_err = f"Gemini 서버 오류({code})"
                time.sleep(5)
                continue
            return "", model, f"Gemini {code}: {msg}"                # 400(요청 문제) · 401/403(키 문제) → 재시도 무의미
        return "", self.model, last_err or "Gemini 응답 실패"




# ─────────────────────────────────────────────────────────────────────
# 6. 최종 결정
# ─────────────────────────────────────────────────────────────────────
def final_decision(post: dict, verdict: Verdict, extracted: Optional[dict] = None, *,
                   min_confidence: Optional[float] = None) -> Decision:
    """정규식 추출(extracted) 우선, 비어 있으면 Gemini 값. 마감 지남 · 공모 아님 · 확신 부족 · 오래된 글 순으로 거른다."""
    ex = extracted or {}
    min_conf = config.GEMINI_MIN_CONFIDENCE if min_confidence is None else float(min_confidence)
    deadline = str(ex.get("deadline") or verdict.deadline or "")
    start = str(ex.get("start") or verdict.start or "")
    always_open = bool(ex.get("always_open") or verdict.always_open)
    common = dict(deadline=deadline, start=start, always_open=always_open, period_text=str(ex.get("period_text") or ""))


    if verdict.status != "ok":
        return Decision(False, verdict.status, verdict.reason, **common)
    if deadline and prefilter.is_expired(deadline):
        return Decision(False, STATUS_EXPIRED, f"마감 지남 ({deadline})", **common)
    if not deadline and not always_open and "마감지남" in verdict.flags:
        return Decision(False, STATUS_EXPIRED, "AI 판정: 접수가 끝난 글", **common)
    if not verdict.is_call:
        return Decision(False, STATUS_AI_NO, verdict.reason, **common)
    if verdict.confidence < min_conf:
        return Decision(False, STATUS_LOW_CONF,
                        f"확신 부족 {verdict.confidence:.0%} < {min_conf:.0%} — {verdict.reason}", **common)
    posted = str(post.get("posted_date") or "")
    if posted and not deadline and not always_open:
        try:
            age = (config.now_kst().date() - date.fromisoformat(posted)).days
        except ValueError:
            age = 0
        if age > MAX_POST_AGE_DAYS:
            return Decision(False, STATUS_STALE, f"게시 {age}일 경과 · 마감 정보 없음 → 보류", **common)
    return Decision(True, STATUS_ALERTED, verdict.reason, **common)




# ─────────────────────────────────────────────────────────────────────
# 7. 로컬 점검 CLI
# ─────────────────────────────────────────────────────────────────────
def _samples() -> list[tuple[str, str]]:
    today = config.now_kst().date()
    s, e = today + timedelta(days=3), today + timedelta(days=20)
    f = lambda d: d.strftime("%Y. %m. %d.")          # noqa: E731
    return [
        ("2025 하반기 기획전시 참여작가 공모",
         f"○○문화재단은 하반기 기획전시에 참여할 시각예술 작가를 공모합니다.\n접수기간: {f(s)} ~ {f(e)} 18:00\n"
         "지원자격: 만 19세 이상 시각예술 작가\n제출서류: 참가신청서, 포트폴리오\n문의: 전시팀"),
        ("2025 청년예술가 지원사업 선정결과 안내",
         "2025 청년예술가 지원사업 심사 결과 아래와 같이 선정되었음을 알려드립니다.\n선정자: 총 12명 (명단 첨부)"),
        ("여름방학 어린이 체험프로그램 참가자 모집",
         f"초등학생을 대상으로 여름방학 체험프로그램 참가자를 모집합니다.\n신청기간: {f(s)} ~ {f(e)}\n"
         "신청방법: 홈페이지 예약\n참가비: 무료"),
        ("○○ 거리예술축제 참여 공연팀 모집 공고",
         f"거리예술축제에서 공연할 버스킹 · 거리공연 단체를 모집합니다.\n모집기간: {f(s)} ~ {f(e)}\n"
         "지원자격: 공연 활동 경력 1년 이상 단체\n제출: 신청서, 공연영상"),
    ]




def _print_verdict(v: Verdict, d: Optional[Decision] = None) -> None:
    mark = "✅ 공모" if v.is_call else "❌ 아님"
    print(f"  {mark}  확신 {v.confidence:.2f}  [{v.category} / {v.applicant or '-'}]  {v.reason}")
    print(f"     접수 {v.start or '-'} ~ {v.deadline or '-'}  상시 {v.always_open}  flags {v.flags}  "
          f"status={v.status}  model={v.model or '-'}")
    if d is not None:
        print(f"     → 최종: {'📢 알림 발송' if d.alert else '알림 안 함'} ({d.status}) {d.note}")




def _run_one(judge: Judge, title: str, body: str, *, url: str = "", posted: str = "",
             site_name: str = "테스트", attachments: Optional[list] = None) -> None:
    pre = prefilter.classify_title(title)
    ex = prefilter.extract_dates(body, title)
    post = {"key": "", "title": title, "url": url, "posted_date": posted or config.today_kst(), "site_name": site_name}
    print(f"\n■ {title}")
    tag = "PASS" if pre.passed else ("BODY" if pre.body_required else "FAIL")
    print(f"  1차 필터 [{tag}] {pre.reason}" + ("" if pre.passed else "   (실제 흐름에서는 여기서 탈락/보류)"))
    v = judge.judge(post, body, extracted=ex, attachments=attachments, prefilter_reason=pre.reason)
    _print_verdict(v, final_decision(post, v, ex))




def _cmd_models() -> None:
    if not config.GEMINI_API_KEY:
        print("GEMINI_API_KEY 가 없습니다.")
        return
    r = requests.get(f"{GEMINI_API_BASE}/models", params={"key": config.GEMINI_API_KEY, "pageSize": 100}, timeout=30)
    if r.status_code != 200:
        print(f"조회 실패 HTTP {r.status_code}: {r.text[:200]}")
        return
    names = [m.get("name", "").replace("models/", "") for m in r.json().get("models", [])
             if "generateContent" in (m.get("supportedGenerationMethods") or [])]
    print("generateContent 지원 모델:")
    for n in sorted(names):
        print(f"  {'* ' if n == (config.GEMINI_MODEL or DEFAULT_MODEL) else '  '}{n}")
    print(f"\n현재 설정: {config.GEMINI_MODEL or DEFAULT_MODEL}  대체: {', '.join(FALLBACK_MODELS)}")




def main(argv: list[str]) -> None:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    cmd = argv[1].lower() if len(argv) > 1 else "help"
    if cmd == "prompt":
        fb = FeedbackStore()
        print(build_system_prompt(fb))
        print("\n" + "=" * 70 + "\n[사용자 프롬프트 예시]\n")
        t, b = _samples()[0]
        print(build_user_prompt({"title": t, "site_name": "테스트", "posted_date": config.today_kst()}, b,
                                prefilter.extract_dates(b, t), ["공모요강.hwp", "참가신청서.hwp"],
                                prefilter.classify_title(t).reason))
        return
    if cmd == "models":
        _cmd_models()
        return
    if cmd not in ("test", "title", "url"):
        print(__doc__)
        return
    problems = config.validate(need_telegram=False, need_gemini=True)
    if problems:
        print("설정 오류: " + " | ".join(problems))
        return
    judge = Judge(FeedbackStore())
    print(f"모델 {judge.model} · 호출 상한 {judge.max_calls} · 최소 확신 {config.GEMINI_MIN_CONFIDENCE:.0%} · "
          f"👎 차단 서명 {len(judge._down_sigs)}개")


    if cmd == "test":
        for t, b in _samples():
            _run_one(judge, t, b)
    elif cmd == "title":
        if len(argv) < 3:
            print(__doc__)
            return
        _run_one(judge, argv[2], argv[3] if len(argv) > 3 else "")
    else:
        if len(argv) < 3:
            print(__doc__)
            return
        import scraper                                            # playwright 필요 → 지연 임포트
        url = argv[2]
        with scraper.Scraper() as sc:
            d = sc.fetch_detail(url)
        if not d.ok:
            print(f"본문 읽기 실패: {d.error}")
            return
        title = argv[3] if len(argv) > 3 else d.page_title
        print(f"본문 {len(d.body):,}자 · 게시일 {d.posted_date or '미확인'} · 첨부 {len(d.attachments)}개")
        _run_one(judge, title, d.body, url=d.url, posted=d.posted_date, site_name=urlsplit(url).netloc,
                 attachments=d.attachments)
    print(f"\nGemini 호출 {judge.calls}회 · 오류 {judge.errors} · 피드백 차단 {judge.blocked}")




if __name__ == "__main__":
    main(sys.argv)

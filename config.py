# -*- coding: utf-8 -*-
"""
config.py — 공모 알림 텔레그램 봇 · 공통 설정                     [1단계 / 9]

역할
  · 환경변수(GitHub Secrets, 로컬은 .env) 읽기
  · 파일 경로 · 시간대 · 한도값 상수
  · 1차 키워드 필터(분야 / 공모신호 / 제외) 와 "게시글이 아닌 링크" 판별 규칙
  · validate() : 실행 전 필수 설정 점검

다른 모듈은 `import config` 후 `config.XXX` 로만 접근한다.
로컬 점검 : python config.py
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ════════════════════════════════════════════════════════════════════
# 0. 경로 · .env
# ════════════════════════════════════════════════════════════════════
BASE_DIR = Path(os.getenv("FESTIVAL_DATA_DIR") or Path(__file__).resolve().parent)


def _load_dotenv(path: Path) -> None:
    """KEY=VALUE 줄만 읽어, 아직 없는 환경변수만 채운다 (로컬 테스트용, 외부 패키지 불필요)."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_load_dotenv(BASE_DIR / ".env")

SITES_FILE       = BASE_DIR / "festival_sites.json"   # 감시 사이트 목록 (/add 로 자동 등록)
KNOWN_POSTS_FILE = BASE_DIR / "known_posts.json"      # 이미 본 게시글 (중복 알림 방지)
FEEDBACK_FILE    = BASE_DIR / "feedback.json"         # 보낸 알림 + 좋아요/싫어요 피드백
BOT_STATE_FILE   = BASE_DIR / "bot_state.json"        # 텔레그램 offset 등 봇 상태

# ════════════════════════════════════════════════════════════════════
# 1. 시간 (모든 기록은 KST 문자열)
# ════════════════════════════════════════════════════════════════════
KST = timezone(timedelta(hours=9), name="KST")
TIME_FMT = "%Y-%m-%d %H:%M:%S"
DATE_FMT = "%Y-%m-%d"


def now_kst() -> datetime:
    return datetime.now(KST)


def now_kst_iso() -> str:
    return now_kst().strftime(TIME_FMT)


def today_kst() -> str:
    return now_kst().strftime(DATE_FMT)


# ════════════════════════════════════════════════════════════════════
# 2. 텔레그램  (Secrets: TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_CHAT_IDS)
# ════════════════════════════════════════════════════════════════════
def _id_list(raw: str) -> list[int]:
    return [int(t) for t in re.split(r"[,\s;]+", raw or "") if t.lstrip("-").isdigit()]


TELEGRAM_BOT_TOKEN        = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
# 화이트리스트: 이 chat_id 에서 온 명령만 처리 (가족 개인 채팅 여러 개 또는 가족 단톡방 1개)
TELEGRAM_ALLOWED_CHAT_IDS = _id_list(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", ""))
# 알림을 보낼 곳 (미설정 시 화이트리스트 전체에 발송)
TELEGRAM_ALERT_CHAT_IDS   = _id_list(os.getenv("TELEGRAM_ALERT_CHAT_IDS", "")) or TELEGRAM_ALLOWED_CHAT_IDS
# /remove, /disable 등 관리 명령 허용 (미설정 시 화이트리스트 전체)
TELEGRAM_ADMIN_CHAT_IDS   = _id_list(os.getenv("TELEGRAM_ADMIN_CHAT_IDS", "")) or TELEGRAM_ALLOWED_CHAT_IDS

TELEGRAM_API_BASE      = "https://api.telegram.org"
TELEGRAM_TIMEOUT_SEC   = 20
TELEGRAM_POLL_LIMIT    = 100    # getUpdates 1회 최대 개수
TELEGRAM_MAX_TEXT      = 3800   # 메시지 길이 안전 한도 (실제 4096)
TELEGRAM_SEND_INTERVAL = 1.2    # 연속 발송 간 대기(초) — 초당 발송 한도 회피

# ════════════════════════════════════════════════════════════════════
# 3. Gemini  (Secrets: GEMINI_API_KEY / 선택: GEMINI_MODEL)
# ════════════════════════════════════════════════════════════════════
GEMINI_API_KEY           = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL             = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
# 주 모델이 404(사용 불가)면 순서대로 대체. 빈 환경변수(yml 빈 시크릿)도 기본값으로 처리. 종료된 gemini-2.0-flash 제외
GEMINI_FALLBACK_MODELS   = [m.strip() for m in (os.getenv("GEMINI_FALLBACK_MODELS", "").strip()
                            or "gemini-2.5-flash-lite,gemini-3.1-flash-lite,gemini-3.5-flash-lite").split(",") if m.strip()]
GEMINI_MAX_CALLS_PER_RUN = int(os.getenv("GEMINI_MAX_CALLS_PER_RUN", "40"))   # 1회 실행 호출 상한 (무료 한도 보호)
GEMINI_MIN_CONFIDENCE    = float(os.getenv("GEMINI_MIN_CONFIDENCE", "0.85"))  # 이 미만이면 확인요청
DEFER_MAX_RETRY          = int(os.getenv("DEFER_MAX_RETRY", "3"))             # 보류(오류/한도) 이 횟수 넘으면 확인요청으로 전환
GEMINI_MIN_INTERVAL_SEC  = 6.5    # 호출 간 최소 간격 (분당 한도 회피)
GEMINI_TIMEOUT_SEC       = 60
GEMINI_RETRY             = 2
GEMINI_BODY_MAX_CHARS    = 3500   # 본문은 이 길이까지만 전달

# ════════════════════════════════════════════════════════════════════
# 4. 크롤링 · 알림 한도
# ════════════════════════════════════════════════════════════════════
MAX_ROWS_PER_SITE         = 40      # 목록에서 읽는 최대 행 수
PAGE_TIMEOUT_MS           = 30_000
DETAIL_TIMEOUT_MS         = 20_000
FETCH_DETAIL_BODY         = True    # 1차 통과 글은 상세 페이지 본문까지 읽어 Gemini 에 전달
DETAIL_FETCH_MAX_PER_SITE = 8       # 사이트당 상세 페이지 최대 열람 수
FIRST_SCAN_SILENT         = True    # 새 사이트 첫 스캔: 기존 글은 기록만 하고 알림 X
SITE_FAIL_DISABLE_AFTER   = 5       # 연속 실패 n회 → 알림 후 자동 비활성
KNOWN_KEEP_PER_SITE       = 300     # known_posts 사이트당 보관 수
ALERT_MAX_PER_RUN         = 20      # 1회 실행 최대 알림 수 (초과분은 다음 실행으로)
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 사이트 1개의 표준 스키마 — festival_sites.json 항목은 storage.normalize_site 가 이 형태로 맞춘다.
DEFAULT_SITE = {
    "id": "",                              # 자동 생성 (storage.make_site_id)
    "name": "",                            # 표시 이름 (/add 시 <title> 자동 추출, 없으면 도메인)
    "url": "",                             # 게시판 목록 URL
    "base_url": "",                        # 상대 링크 보정용 (자동)
    "list_selector": "table:has(tbody)",   # 목록 컨테이너
    "row_selector": "tbody > tr",          # 게시글 행
    "link_selector": "a[href]",            # 행 안의 제목 링크
    "date_selector": "",                   # 행 안의 등록일 셀 (비우면 자동 추정)
    "enabled": True,
    "verified": False,                     # 첫 스캔에서 행이 1개 이상 잡히면 True
    "added_by": "",                        # 등록한 텔레그램 사용자
    "added_at": "",
    "last_checked": "",
    "last_status": "",                     # ok / fail:<이유>
    "fail_count": 0,                       # 연속 실패 수
    "note": "",
}

# ════════════════════════════════════════════════════════════════════
# 5. 피드백 루프
# ════════════════════════════════════════════════════════════════════
FEEDBACK_EXAMPLES_IN_PROMPT = 12   # Gemini 프롬프트에 넣는 최근 피드백 예시 수 (싫어요 우선)
FEEDBACK_KEEP_MAX           = 600  # feedback.json 보관 상한
ALERT_KEEP_DAYS             = 45   # 버튼이 유효한 기간 (지나면 alerts 에서 정리)

# ════════════════════════════════════════════════════════════════════
# 6. 1차 키워드 필터
#    prefilter.py 규칙 (요약)
#      - 제목에 EXCLUDE 가 있으면 즉시 탈락 (본문에는 적용 안 함: 본문엔 '결과', '안내' 가 흔함)
#      - 제목에 DOMAIN 이 있으면 통과
#      - 제목에 CALL 만 있으면 본문을 읽어 DOMAIN 이 있을 때 통과
#      - 비교는 공백 제거 + 소문자 기준 ("공연 일정" == "공연일정")
# ════════════════════════════════════════════════════════════════════
DOMAIN_KEYWORDS = [
    # --- 기본 카테고리 ---
    "전시", "공연", "체험",
    # --- 전시 관련 확장 ---
    "전시회", "기획전시", "전시공모", "전시지원",
    "입주작가", "레지던시 작가", "작가공모", "참여작가 모집",
    "아티스트 공모", "전시작가 모집", "전시업체 모집",
    # --- 공연 관련 확장 ---
    "공연예술", "공연지원", "공연공모", "공연단체 모집",
    "공연팀 모집", "무대공연 공모", "버스킹 공모", "공연기획 공모",
    # --- 체험 관련 확장 (운영 주체 모집에 한정) ---
    "체험프로그램 운영", "체험부스 운영", "체험행사 운영",
    "체험프로그램 업체 모집", "체험 콘텐츠 공모", "체험프로그램 공모",
    # --- 추가 ---
    "예술단체 모집", "예술인 모집", "작품 공모", "창작지원", "레지던시",
]

CALL_KEYWORDS = [
    # --- 공모 신청을 나타내는 공통 신호어 ---
    "공모", "공모전", "모집공고", "참여기업 모집",
    "운영업체 모집", "운영단체 모집", "위탁업체 모집",
    "신청서 접수", "접수기간", "지원신청", "참가신청서",
    "제안서 접수", "사업자 모집", "단체 모집",
    # --- 추가 ---
    "지원사업", "참여단체 모집", "프로그램 공모", "콘텐츠 공모", "기획 공모", "공모 안내",
]

INCLUDE_KEYWORDS = list(dict.fromkeys(DOMAIN_KEYWORDS + CALL_KEYWORDS))   # 호환용 합본

EXCLUDE_KEYWORDS = [
    # A. 결과/발표 (이미 끝난 공모의 결과 - 신청 대상 아님)
    "결과", "선정결과", "심사결과", "합격자 발표", "선정자 발표",
    "최종결과", "심사발표", "결과발표", "당선작", "당선자",
    "선정작 발표", "합격발표", "발표 안내", "선정팀 발표",
    # B. 참가자/체험자 모집 (일반인 대상 - 사업자·작가 신청이 아님)
    "체험자 모집", "참가자 모집", "관람객 모집", "수강생 모집",
    "신청자 모집", "방문객 모집", "참여자 모집", "관람 신청",
    "체험 신청", "수강 신청", "프로그램 참가 신청", "동아리 모집",
    "가족체험단 모집", "서포터즈 모집", "봉사자 모집",
    # C. 일정/안내성 게시글
    "일정 안내", "공연일정", "전시일정", "운영일정", "시간표",
    "관람안내", "이용안내", "개최 안내", "진행 안내", "행사 안내",
    "프로그램 안내", "관람시간", "운영시간 안내", "예매 안내",
    "예매 오픈", "티켓 안내",
    # D. 공지/행정성 게시글
    "공지사항", "안내사항", "휴관", "휴무", "임시휴관",
    "시설점검", "정기점검", "변경 안내", "연기 안내", "취소 안내",
    "오시는 길", "이용요금", "주차안내", "이용약관",
    # E. 사업 결과보고/백서/조사
    "사업결과", "백서", "성과보고", "결과보고서", "운영결과",
    "만족도 조사", "설문조사", "사업보고회",
    # F. 채용/입찰 (창작 공모와 무관한 행정 공고)
    "채용공고", "인턴모집", "입찰공고", "용역 공고", "계약직 채용",
    "정규직 채용", "공무직 채용",
    # G. 추가
    "채용", "입찰", "낙찰", "개찰", "수의계약", "견적",
    "교육생 모집", "어린이 모집", "청소년 모집", "초대",
    "보도자료", "뉴스레터", "소식지", "카드뉴스", "결과 안내", "선정 안내",
]

# ════════════════════════════════════════════════════════════════════
# 7. "게시글이 아닌 링크" 판별 (첨부파일 · 커뮤니티/메뉴 버튼 · 페이지 이동)
# ════════════════════════════════════════════════════════════════════
# 링크 텍스트가 아래 항목만으로 이뤄지면 게시글이 아님
NON_POST_LINK_TEXT = [
    "첨부파일", "첨부", "다운로드", "미리보기", "바로보기", "파일", "목록", "이전글", "다음글",
    "이전", "다음", "처음", "마지막", "글쓰기", "수정", "삭제", "답변", "신고", "인쇄", "공유",
    "스크랩", "좋아요", "댓글", "커뮤니티", "로그인", "회원가입", "홈", "메인", "사이트맵",
    "더보기", "전체보기", "검색", "닫기", "열기", "top", "new", "hot", "공지",
]
# 첨부파일 확장자
NON_POST_HREF_EXT = (
    ".pdf", ".hwp", ".hwpx", ".zip", ".7z", ".rar", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".mp4", ".mp3",
)
# href 에 포함되면 게시글이 아님 (소문자 비교)
NON_POST_HREF_HINT = (
    "download", "filedown", "file_down", "atchfile", "getfile", "fileid=",
    "login", "logout", "join", "member", "sitemap", "print", "share", "rss",
)
NON_POST_HREF_PREFIX = ("mailto:", "tel:", "sms:")
# 주의: "javascript:" 와 "#" 링크는 게시글일 수 있음(onclick 상세보기) → scraper 가 클릭으로 처리
MIN_TITLE_LEN = 4

# ════════════════════════════════════════════════════════════════════
# 8. 날짜 · 마감 추출 힌트 (scraper / judge 공용)
# ════════════════════════════════════════════════════════════════════
DEADLINE_HINT_WORDS = [
    "접수기간", "접수 기간", "모집기간", "모집 기간", "신청기간", "신청 기간",
    "공모기간", "공모 기간", "접수마감", "마감", "까지", "접수일시", "신청일시", "제출기한",
]
DATE_REGEX        = re.compile(r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*일?")
DATE_REGEX_NOYEAR = re.compile(r"(?<![\d.])(\d{1,2})\s*[./월]\s*(\d{1,2})\s*일?(?![\d.])")
EXPIRED_GRACE_DAYS = 0   # 마감일이 오늘보다 이전이면 알림 X (0 = 마감 당일까지 허용)


# ════════════════════════════════════════════════════════════════════
# 9. 점검
# ════════════════════════════════════════════════════════════════════
def validate(*, need_telegram: bool = True, need_gemini: bool = False) -> list[str]:
    """누락·이상 설정을 문자열 목록으로 반환. 비어 있으면 정상."""
    problems: list[str] = []
    if need_telegram:
        if not TELEGRAM_BOT_TOKEN:
            problems.append("TELEGRAM_BOT_TOKEN 없음")
        elif ":" not in TELEGRAM_BOT_TOKEN:
            problems.append("TELEGRAM_BOT_TOKEN 형식 이상 (123456:ABC... 형태)")
        if not TELEGRAM_ALLOWED_CHAT_IDS:
            problems.append("TELEGRAM_ALLOWED_CHAT_IDS 없음 (쉼표로 구분한 chat_id)")
    if need_gemini and not GEMINI_API_KEY:
        problems.append("GEMINI_API_KEY 없음")
    return problems


def _mask(secret: str, keep: int = 6) -> str:
    return f"{secret[:keep]}...({len(secret)}자)" if secret else "(없음)"


def summary() -> str:
    lines = [
        f"데이터 폴더        : {BASE_DIR}",
        f"TELEGRAM_BOT_TOKEN : {_mask(TELEGRAM_BOT_TOKEN)}",
        f"허용 chat_id       : {TELEGRAM_ALLOWED_CHAT_IDS or '(없음)'}",
        f"알림 대상 chat_id  : {TELEGRAM_ALERT_CHAT_IDS or '(없음)'}",
        f"관리자 chat_id     : {TELEGRAM_ADMIN_CHAT_IDS or '(없음)'}",
        f"GEMINI_API_KEY     : {_mask(GEMINI_API_KEY)}",
        f"GEMINI_MODEL       : {GEMINI_MODEL}",
        f"대체 모델          : {', '.join(GEMINI_FALLBACK_MODELS) or '(없음)'}",
        f"Gemini 호출 상한   : {GEMINI_MAX_CALLS_PER_RUN}회/실행, 최소 확신 {GEMINI_MIN_CONFIDENCE}",
        f"키워드             : 분야 {len(DOMAIN_KEYWORDS)} / 공모신호 {len(CALL_KEYWORDS)} / 제외 {len(EXCLUDE_KEYWORDS)}",
    ]
    problems = validate(need_telegram=True, need_gemini=True)
    lines.append("점검 결과          : " + ("OK" if not problems else " | ".join(problems)))
    return "\n".join(lines)


if __name__ == "__main__":
    print(summary())

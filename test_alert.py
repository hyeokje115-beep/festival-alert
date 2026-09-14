# -*- coding: utf-8 -*-
"""
test_alert.py — 전시/공연/체험 가짜 공모 알림을 실제 텔레그램으로 발송하는 테스트 스크립트

기존 파일(main.py, judge.py, telegram_client.py, storage.py 등)은 전혀 수정하지 않는다.
그 파일들이 이미 제공하는 함수(send_alert, FeedbackStore, TelegramClient)를 그대로 가져다 쓴다.

사용법 (프로젝트 폴더 안에서, 기존과 동일한 .env/환경변수가 설정된 상태로):
  python test_alert.py            → 전시 카테고리로 발송
  python test_alert.py 공연        → 공연 카테고리로 발송
  python test_alert.py 체험        → 체험 카테고리로 발송
  python test_alert.py 전시 공연 체험   → 셋 다 순서대로 발송

주의:
  - 실제 텔레그램 알림이 진짜로 갑니다 (config.TELEGRAM_ALERT_CHAT_IDS 로 발송).
  - feedback.json 에 테스트 알림이 기록됩니다 (👍/👎 버튼이 눌리면 학습 예시로 들어갈 수 있음).
    지우고 싶으면 발송 후 feedback.json 에서 "key": "t:test-..." 로 시작하는 항목을 삭제하면 됩니다.
  - known_posts.json, festival_sites.json 등은 건드리지 않습니다.
"""
import sys
import time

import config
from storage import FeedbackStore
from telegram_client import TelegramClient, send_alert

from datetime import timedelta

SAMPLES = {
    "전시": {
        "title": "[테스트] 2026 가을 기획전시 참여작가 공모",
        "site_name": "테스트 문화재단",
        "ai_reason": "시각예술 작가를 대상으로 전시 참여작가를 모집하며 접수기간이 명시됨",
    },
    "공연": {
        "title": "[테스트] 거리예술축제 참여 공연팀 모집 공고",
        "site_name": "테스트 문화재단",
        "ai_reason": "거리예술축제에서 공연할 버스킹·거리공연 단체를 모집하며 접수기간이 명시됨",
    },
    "체험": {
        "title": "[테스트] 체험프로그램 운영업체 모집 공고",
        "site_name": "테스트 문화재단",
        "ai_reason": "체험 부스를 운영할 업체를 모집하며 접수기간이 명시됨",
    },
}


def build_post(category: str) -> dict:
    s = SAMPLES[category]
    today = config.now_kst().date()
    end = today + timedelta(days=14)
    return {
        "key": f"t:test-{category}-{int(time.time())}",   # 매번 새 key → 중복 처리 없이 항상 발송됨
        "site_id": "test_site",
        "site_name": s["site_name"],
        "title": s["title"],
        "url": "https://example.org/board/view?id=1",
        "posted_date": today.isoformat(),
        "deadline": end.isoformat(),
        "period_text": f"접수기간: {today.strftime('%Y. %m. %d.')} ~ {end.strftime('%Y. %m. %d.')} 18:00까지",
        "always_open": False,
        "ai_reason": s["ai_reason"],
        "ai_confidence": 0.93,
        "body_checked": True,
    }


def main(argv: list[str]) -> None:
    cats = [c for c in argv[1:] if c in SAMPLES] or ["전시"]
    problems = config.validate(need_telegram=True)
    if problems:
        print("설정 오류: " + " | ".join(problems))
        return

    client = TelegramClient()
    fb = FeedbackStore()

    for cat in cats:
        post = build_post(cat)
        res = send_alert(client, post, fb)
        print(f"[{cat}] 발송: 성공 {len(res['sent'])}건, 실패 {len(res['errors'])}건")
        for e in res["errors"]:
            print("   -", e)
        time.sleep(1)

    fb.save()
    print("\n휴대폰에서 알림을 확인하세요. (fb.save() 로 feedback.json 에 기록됨)")


if __name__ == "__main__":
    main(sys.argv)

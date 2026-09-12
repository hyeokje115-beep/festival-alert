name: Festival Alert

on:
  workflow_dispatch:
    inputs:
      test_notification:
        description: "ntfy 연결 테스트 알림 1건 보내기"
        required: true
        type: boolean
        default: true

  schedule:
    # 한국 시간 09:00
    - cron: "0 0 * * *"
    # 한국 시간 18:00
    - cron: "0 9 * * *"

permissions:
  contents: write

concurrency:
  group: festival-alert-state
  cancel-in-progress: false

defaults:
  run:
    shell: bash

jobs:
  check:
    runs-on: ubuntu-latest
    timeout-minutes: 45

    env:
      PYTHONUNBUFFERED: "1"
      TZ: Asia/Seoul
      STATE_DB: festival_state_v7.sqlite3
      SITES_CONFIG: festival_sites.json
      DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}

    steps:
      - name: Checkout
        uses: actions/checkout@v6
        with:
          ref: ${{ github.event.repository.default_branch }}
          fetch-depth: 0
          persist-credentials: true

      - name: Set up Python
        uses: actions/setup-python@v6
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          python -m pip install requests beautifulsoup4 playwright filelock
          python -m pip check
          python -m playwright install --with-deps chromium

      - name: Check connections
        id: connections
        env:
          NTFY_TOPIC: ${{ secrets.NTFY_TOPIC }}
          NTFY_TOKEN: ${{ secrets.NTFY_TOKEN }}
          NTFY_SERVER: ${{ secrets.NTFY_SERVER || vars.NTFY_SERVER || 'https://ntfy.sh' }}
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
          GEMINI_MODEL: ${{ vars.GEMINI_MODEL || 'gemini-2.5-flash' }}
          SEND_TEST: ${{ github.event_name == 'workflow_dispatch' && inputs.test_notification && 'true' || 'false' }}
        run: |
          python - <<'PY'
          import os
          import re
          from urllib.parse import urlsplit

          import requests

          def fail(message):
              print(f"::error::{message}")
              raise SystemExit(1)

          def summary(message):
              with open(
                  os.environ["GITHUB_STEP_SUMMARY"],
                  "a",
                  encoding="utf-8",
              ) as handle:
                  handle.write(message + "\n")

          def output(name, value):
              with open(
                  os.environ["GITHUB_OUTPUT"],
                  "a",
                  encoding="utf-8",
              ) as handle:
                  handle.write(f"{name}={value}\n")

          def post_json(service, url, payload, headers):
              try:
                  response = requests.post(
                      url,
                      json=payload,
                      headers=headers,
                      timeout=(10, 40),
                      allow_redirects=False,
                  )
              except requests.RequestException as exc:
                  fail(
                      f"{service} 통신 실패: {type(exc).__name__}. "
                      "자동 재전송하지 않았습니다."
                  )

              if response.status_code != 200:
                  fail(
                      f"{service} HTTP {response.status_code}. "
                      "인증·권한·서버 주소·모델·사용량 제한을 확인해야 합니다."
                  )

              try:
                  data = response.json()
              except ValueError:
                  fail(f"{service} 응답이 JSON이 아닙니다.")

              if not isinstance(data, dict):
                  fail(f"{service} 응답 구조가 예상과 다릅니다.")

              return data

          topic = os.environ.get("NTFY_TOPIC", "")
          server = os.environ.get("NTFY_SERVER", "").rstrip("/")
          token = os.environ.get("NTFY_TOKEN", "")

          if not re.fullmatch(r"[-_A-Za-z0-9]{1,64}", topic):
              fail(
                  "NTFY_TOPIC이 없거나 형식이 잘못되었습니다. "
                  "Secrets에는 전체 URL이 아닌 토픽 이름을 저장해야 합니다."
              )

          try:
              parsed = urlsplit(server)
              valid_server = (
                  server == server.strip()
                  and parsed.scheme == "https"
                  and bool(parsed.hostname)
                  and not parsed.username
                  and not parsed.password
                  and not parsed.query
                  and not parsed.fragment
              )
          except ValueError:
              valid_server = False

          if not valid_server:
              fail("NTFY_SERVER는 유효한 HTTPS 서버 주소여야 합니다.")

          ntfy_headers = {"Content-Type": "application/json"}
          if token:
              ntfy_headers["Authorization"] = f"Bearer {token}"

          # 수동 실행에서 사용자가 선택한 경우에만 테스트 알림을 발송합니다.
          # 이 테스트는 공모 수집·필터 통과 여부와 별개입니다.
          if os.environ.get("SEND_TEST") == "true":
              data = post_json(
                  "ntfy",
                  server + "/",
                  {
                      "topic": topic,
                      "title": "[연결 테스트] 공모 알림",
                      "message": (
                          "GitHub Actions에서 보낸 수동 연결 테스트입니다.\n"
                          "신규 공모 알림은 아닙니다.\n"
                          "이 메시지가 보이면 휴대폰까지의 수신 경로를 "
                          "확인할 수 있습니다."
                      ),
                      "priority": 3,
                  },
                  ntfy_headers,
              )

              if not (
                  data.get("event") == "message"
                  and data.get("topic") == topic
                  and isinstance(data.get("id"), str)
                  and bool(data["id"])
              ):
                  fail(
                      "ntfy 응답에서 발행 확인 정보를 찾지 못했습니다. "
                      "결과가 불명확하므로 자동 재전송하지 않습니다."
                  )

              output("ntfy_test", "accepted")
              print("ntfy 서버가 테스트 알림을 접수했습니다.")
              summary(
                  "### ntfy 연결 테스트\n"
                  "- 서버 발행 확인: 성공\n"
                  "- 휴대폰 표시 여부: 사용자가 직접 확인해야 합니다.\n"
                  "- 이 결과는 공모 수집·필터 검증 완료를 의미하지 않습니다.\n"
              )
          else:
              output("ntfy_test", "not_requested")
              print("예약 실행 또는 테스트 미선택: 테스트 알림을 보내지 않습니다.")

          key = os.environ.get("GEMINI_API_KEY", "").strip()
          if not key:
              fail("Secrets에 GEMINI_API_KEY가 없습니다.")

          model = (
              os.environ.get("GEMINI_MODEL", "").strip()
              or "gemini-2.5-flash"
          ).removeprefix("models/")

          if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
              fail("GEMINI_MODEL 형식이 잘못되었습니다.")

          # 실제 생성 요청으로 키·모델 접근·현재 API 응답을 확인합니다.
          # 실패했을 때 분류를 생략하고 원본 공고를 보내지 않습니다.
          result = post_json(
              "Gemini",
              (
                  "https://generativelanguage.googleapis.com/v1beta/"
                  f"models/{model}:generateContent"
              ),
              {
                  "contents": [
                      {
                          "role": "user",
                          "parts": [
                              {"text": "Reply with the single word OK."}
                          ],
                      }
                  ],
                  "generationConfig": {
                      "temperature": 0,
                      "maxOutputTokens": 1024,
                  },
              },
              {
                  "Content-Type": "application/json",
                  "x-goog-api-key": key,
              },
          )

          candidates = result.get("candidates")
          if not (
              isinstance(candidates, list)
              and candidates
              and isinstance(candidates, dict)
          ):
              fail("Gemini 응답에 유효한 생성 결과가 없습니다.")

          candidate = candidates
          content = candidate.get("content")
          parts = content.get("parts") if isinstance(content, dict) else None

          if not (
              candidate.get("finishReason") == "STOP"
              and isinstance(parts, list)
              and any(
                  isinstance(part, dict)
                  and isinstance(part.get("text"), str)
                  and part["text"].strip()
                  for part in parts
              )
          ):
              fail("Gemini 연결 확인 요청이 정상적으로 완료되지 않았습니다.")

          with open(
              os.environ["GITHUB_ENV"],
              "a",
              encoding="utf-8",
          ) as handle:
              handle.write(f"GEMINI_MODEL={model}\n")

          print(f"Gemini API 응답 확인: {model}")
          summary(f"- Gemini API 연결 확인: 성공 (`{model}`)\n")
          PY

      - name: Check Python and initialize configuration
        id: prepare
        run: |
          python - <<'PY'
          import ast
          from pathlib import Path

          path = Path("festival_checker.py")
          if not path.is_file():
              raise SystemExit(
                  "저장소에서 실행할 Python 프로그램을 찾지 못했습니다."
              )

          source = path.read_text(encoding="utf-8-sig")
          tree = ast.parse(source, filename=str(path))

          # 구형 프로그램은 알 수 없는 옵션을 무시하고 실행할 수 있으므로,
          # 실제 실행 전에 필요한 CLI 인터페이스부터 확인합니다.
          options = set()
          for node in ast.walk(tree):
              if (
                  isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute)
                  and node.func.attr == "add_argument"
              ):
                  for argument in node.args:
                      if (
                          isinstance(argument, ast.Constant)
                          and isinstance(argument.value, str)
                      ):
                          options.add(argument.value)

          required = {"--send", "--self-test", "--init-config"}
          missing = sorted(required - options)
          if missing:
              raise SystemExit(
                  "현재 Python은 이 워크플로가 대상으로 하는 수정판과 "
                  "인터페이스가 다릅니다. 누락 옵션: "
                  + ", ".join(missing)
                  + ". 구형 수집기를 임의로 실행하지 않고 중단합니다."
              )

          print("Python 구문 및 필수 실행 옵션 확인 완료.")
          PY

          python -m py_compile festival_checker.py
          python festival_checker.py --self-test

          if [ ! -f "$SITES_CONFIG" ]; then
            python festival_checker.py --init-config
          fi

          if [ ! -f "$SITES_CONFIG" ]; then
            echo "::error::사이트 설정 초기화 후에도 설정 파일이 없습니다."
            exit 1
          fi

      - name: Run checker
        id: checker
        env:
          NTFY_TOPIC: ${{ secrets.NTFY_TOPIC }}
          NTFY_TOKEN: ${{ secrets.NTFY_TOKEN }}
          NTFY_SERVER: ${{ secrets.NTFY_SERVER || vars.NTFY_SERVER || 'https://ntfy.sh' }}
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
        run: |
          # 점검 모드가 아닌 실제 발송 모드입니다.
          # 최초 기준 저장, 미검증 사이트, 필터 탈락까지 강제로 통과시키지는 않습니다.
          python festival_checker.py --send

      - name: Checkpoint state database
        id: checkpoint
        if: ${{ always() && steps.prepare.outcome == 'success' }}
        run: |
          python - <<'PY'
          import os
          import sqlite3
          from pathlib import Path

          database_path = Path(os.environ["STATE_DB"])
          config_path = Path(os.environ["SITES_CONFIG"])

          if database_path.is_file():
              connection = sqlite3.connect(
                  str(database_path),
                  timeout=30,
              )
              try:
                  connection.execute("PRAGMA busy_timeout=30000")

                  result = connection.execute(
                      "PRAGMA wal_checkpoint(TRUNCATE)"
                  ).fetchone()

                  if not result or result != 0:
                      raise RuntimeError(
                          "SQLite 체크포인트가 완료되지 않았습니다. "
                          "불완전한 DB를 저장하지 않습니다."
                      )

                  integrity = connection.execute(
                      "PRAGMA quick_check"
                  ).fetchall()

                  if integrity != [("ok",)]:
                      raise RuntimeError(
                          "SQLite 무결성 검사 실패. 자동 저장을 중단합니다."
                      )
              finally:
                  connection.close()

              print("SQLite 체크포인트 및 무결성 검사 완료.")
          else:
              print("이번 실행에서 저장할 SQLite DB가 없습니다.")

          ready = database_path.is_file() or config_path.is_file()
          with open(
              os.environ["GITHUB_OUTPUT"],
              "a",
              encoding="utf-8",
          ) as handle:
              handle.write(f"ready={'true' if ready else 'false'}\n")
          PY

      - name: Save state
        id: save
        if: ${{ always() && steps.checkpoint.outcome == 'success' && steps.checkpoint.outputs.ready == 'true' }}
        run: |
          set -euo pipefail

          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

          for file in "$STATE_DB" "$SITES_CONFIG"; do
            if [ -f "$file" ]; then
              git add -f -- "$file"
            fi
          done

          if git diff --cached --quiet; then
            echo "상태 변경 없음."
            exit 0
          fi

          git commit -m "chore: persist festival alert state [skip ci]"

          # 원격 변경과 충돌하면 실패를 표시합니다.
          # 강제 푸시로 다른 변경이나 상태 이력을 덮어쓰지 않습니다.
          git pull --rebase origin "$DEFAULT_BRANCH"
          git push origin "HEAD:$DEFAULT_BRANCH"

      - name: Write run summary
        if: ${{ always() }}
        env:
          CONNECTION_RESULT: ${{ steps.connections.outcome }}
          PREPARE_RESULT: ${{ steps.prepare.outcome }}
          CHECKER_RESULT: ${{ steps.checker.outcome }}
          SAVE_RESULT: ${{ steps.save.outcome }}
        run: |
          python - <<'PY'
          import json
          import os
          from pathlib import Path

          lines = [
              "",
              "## 실행 결과",
              "",
              "| 단계 | 결과 |",
              "|---|---|",
              f"| 연결 검사 | {os.environ.get('CONNECTION_RESULT') or '미실행'} |",
              f"| Python 자체 검사·초기화 | {os.environ.get('PREPARE_RESULT') or '미실행'} |",
              f"| 공모 검사·발송 | {os.environ.get('CHECKER_RESULT') or '미실행'} |",
              f"| 상태 저장 | {os.environ.get('SAVE_RESULT') or '미실행'} |",
              "",
              "- 예약 확인 시각: 한국 시간 09:00 / 18:00",
              "- 워크플로 성공은 신규 공모 존재 또는 휴대폰 수신을 보장하지 않습니다.",
              "- 최초 기준 저장에서는 기존 글을 새 공모로 발송하지 않습니다.",
          ]

          path = Path(os.environ["SITES_CONFIG"])
          if path.is_file():
              try:
                  data = json.loads(path.read_text(encoding="utf-8-sig"))
                  if isinstance(data, list):
                      sites = data
                  elif isinstance(data, dict):
                      sites = data.get("sites")
                  else:
                      sites = None

                  if (
                      isinstance(sites, list)
                      and sites
                      and all(isinstance(site, dict) for site in sites)
                  ):
                      active = [
                          site for site in sites
                          if site.get("enabled", True) is not False
                      ]
                      verified = sum(
                          site.get("verified") is True
                          for site in active
                      )

                      lines.extend([
                          "",
                          "## 사이트 설정 상태",
                          f"- 활성 사이트: {len(active)}",
                          f"- verified=true 표시: {verified}",
                      ])

                      if active and verified == 0:
                          lines.extend([
                              "",
                              "**활성 사이트가 모두 미검증 상태입니다.**",
                              "현재 수정판의 정책상 실제 공모 발송이 차단됩니다.",
                              "이 워크플로는 미검증 사이트를 임의로 승인하지 않습니다.",
                          ])
                  else:
                      lines.append(
                          "- 사이트별 상태는 프로그램 실행 로그를 확인하세요."
                      )
              except (OSError, ValueError):
                  lines.append("- 사이트 설정 요약을 읽지 못했습니다.")

          with open(
              os.environ["GITHUB_STEP_SUMMARY"],
              "a",
              encoding="utf-8",
          ) as handle:
              handle.write("\n".join(lines) + "\n")
          PY

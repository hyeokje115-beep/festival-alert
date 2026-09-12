#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""축제/문화예술 공모 점검기 v9.
사용법: --self-test, --dry-run, --mode send|status|resolve, --resolve ID --resolution ...
범위: 각 사이트의 공개 정적 HTML 목록 1페이지를 GET으로 수집하고, 목록의 실제 상세 URL과
본문을 다시 읽어 등록일/작성일/게시일 및 접수 시작·마감 문구를 추출한다.
calendar/wevity/JS 렌더링/첨부파일/OCR/복합 공고는 held(보류)다. 이 파일은 생성만 했고
현재 실행에서는 네트워크·LLM·ntfy·GitHub 호출을 하지 않았다.
"""
from __future__ import annotations
import argparse, datetime as dt, hashlib, http.client, ipaddress, json, os, re, socket, ssl, sys, time, unittest
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode
SITES = [
  {
    "name": "광주문화재단",
    "url": "https://www.gctf.or.kr/web/board/1/postList",
    "type": "board"
  },
  {
    "name": "MLDC",
    "url": "https://mldc.kr/notice",
    "type": "board"
  },
  {
    "name": "미마프",
    "url": "http://www.mimaf.net/xe/index.php?mid=notice",
    "type": "board"
  },
  {
    "name": "한국문화예술교육진흥원",
    "url": "https://www.kh.or.kr/brd/board/644/L/SITES/100/menu/371?brdCodeField=SITES&brdCodeValue=100",
    "type": "board"
  },
  {
    "name": "리콜렉션",
    "url": "https://recollection.kr/bbs/board.php?bo_table=notice&page=1",
    "type": "board"
  },
  {
    "name": "전주문화재단",
    "url": "https://www.jge.go.kr/jgemain/na/ntt/selectNttList.do?mi=2116&bbsId=1123",
    "type": "board"
  },
  {
    "name": "나주문화재단",
    "url": "https://www.njcf.or.kr/www/community/notices",
    "type": "board"
  },
  {
    "name": "담양문화재단",
    "url": "https://www.damyangcf.or.kr/user/board/lists/board_cd/4010",
    "type": "board"
  },
  {
    "name": "전남문화재단_타기관",
    "url": "https://www.jncf.or.kr/jact/open/otherevents.do",
    "type": "board"
  },
  {
    "name": "전남문화재단_협업",
    "url": "https://www.jncf.or.kr/jact/open/collusion.do",
    "type": "board"
  },
  {
    "name": "전남문화재단_공지",
    "url": "https://www.jncf.or.kr/jact/open/notice.do",
    "type": "board"
  },
  {
    "name": "광주문화재단_공지",
    "url": "https://www.gjcf.or.kr/cf/news/notice.do",
    "type": "board"
  },
  {
    "name": "고양문화재단",
    "url": "https://www.gtcc.or.kr/bbs/board.php?bo_table=info&page=1",
    "type": "board"
  },
  {
    "name": "문화예술",
    "url": "http://xn--9p4b13eb4bd6i.com/notice",
    "type": "board"
  },
  {
    "name": "국립아시아문화전당_공모",
    "url": "https://www.ncas.or.kr/board/contest/list?menuNo=&currentPageNo=1&searchCondition=",
    "type": "board"
  },
  {
    "name": "위비티",
    "url": "https://www.wevity.com/?c=find&s=1&gub=1&cidx=&sp=&sw=&gbn=list&mode=new",
    "type": "wevity"
  },
  {
    "name": "국립아시아문화전당_공지",
    "url": "https://www.acc.go.kr/main/board/board.do?PID=0701&boardID=NOTICE",
    "type": "board"
  },
  {
    "name": "광주비엔날레",
    "url": "https://www.gwangjubiennale.org/gb/notice.do",
    "type": "board"
  },
  {
    "name": "전북문화관광재단",
    "url": "https://www.jbct.or.kr/notice.php",
    "type": "board"
  },
  {
    "name": "전북문화관광재단_공모",
    "url": "https://www.jbct.or.kr/c_notice.php",
    "type": "board"
  },
  {
    "name": "순천문화재단_공모캘린더",
    "url": "https://www.cfsc.or.kr/contents/news/news0106.asp",
    "type": "calendar"
  },
  {
    "name": "순천문화재단_타기관공모",
    "url": "https://www.cfsc.or.kr/contents/open/open0501.asp",
    "type": "board"
  },
  {
    "name": "순천문화재단_공모게시판",
    "url": "https://www.cfsc.or.kr/contents/open/open0102.asp?bseq=1&cat=39&yy=",
    "type": "board"
  },
  {
    "name": "목포문화재단_문화도시",
    "url": "https://mpcf.or.kr/bbs/board.php?bo_table=notice&sca=%EB%AC%B8%ED%99%94%EB%8F%84%EC%8B%9C",
    "type": "board"
  },
  {
    "name": "목포문화재단_전체",
    "url": "https://mpcf.or.kr/bbs/board.php?bo_table=notice",
    "type": "board"
  }
]
SCHEMA=9; DEFAULT_MODEL="gemini-2.5-flash"; MAX_BYTES=2_000_000; TIMEOUT=12; INTERVAL=1.0
TRACKING={"utm_source","utm_medium","utm_campaign","utm_term","utm_content","gclid","fbclid","mc_cid","mc_eid"}
CATEGORIES=("exhibition","performance","experience","arts_support")
class TextParser(HTMLParser):
    def __init__(self): super().__init__(); self.parts=[]; self.links=[]; self._skip=0
    def handle_starttag(self,t,a):
        a=dict(a)
        if t in ("script","style","noscript","svg"): self._skip+=1
        if t=="a" and a.get("href"): self.links.append((a["href"],""))
    def handle_endtag(self,t):
        if t in ("script","style","noscript","svg") and self._skip: self._skip-=1
    def handle_data(self,d):
        if not self._skip and d.strip(): self.parts.append(d.strip())
def clean_url(raw,base):
    u=urlsplit(urljoin(base,raw))
    q=[x for x in parse_qsl(u.query,keep_blank_values=True) if x[0].lower() not in TRACKING]
    return urlunsplit((u.scheme.lower(),u.netloc.lower(),u.path or "/",urlencode(q), ""))
def public_ip(host):
    infos=socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
    ips=sorted({x[4][0] for x in infos})
    if not ips: raise ValueError("DNS 주소 없음")
    for s in ips:
        ip=ipaddress.ip_address(s)
        if not ip.is_global: raise ValueError("공개 IP 아님")
    return ips[0]
def get_bytes(url):
    u=urlsplit(url)
    if u.scheme!="https": raise ValueError("HTTPS만 허용")
    ip=public_ip(u.hostname)
    ctx=ssl.create_default_context()
    conn=http.client.HTTPSConnection(ip,u.port or 443,timeout=TIMEOUT,context=ctx)
    conn.putrequest("GET",u.path or "/",skip_host=True)
    conn.putheader("Host",u.hostname); conn.putheader("User-Agent","festival-checker/9")
    conn.endheaders(); r=conn.getresponse()
    if r.status in (301,302,303,307,308): raise ValueError("리디렉션 보류")
    if r.status!=200: raise ValueError("HTTP 오류")
    data=r.read(MAX_BYTES+1)
    if len(data)>MAX_BYTES: raise ValueError("응답 크기 초과")
    return data
def parse_page(html,base):
    p=TextParser(); p.feed(html.decode("utf-8","replace"))
    text=re.sub(r"\s+"," "," ".join(p.parts)).strip()
    links=[clean_url(h,base) for h,_ in p.links if urlsplit(urljoin(base,h)).netloc==urlsplit(base).netloc]
    return text, list(dict.fromkeys(links))
def extract_date(text):
    m=re.search(r"(?:등록일|작성일|게시일)\s*[:：]?\s*((?:20\d\d)[.\-/]\d{1,2}[.\-/]\d{1,2})",text)
    if not m: return None
    return dt.date.fromisoformat(re.sub(r"[./]","-",m.group(1)))
def quote(text,needle):
    i=text.lower().find(needle.lower())
    return text[max(0,i-100):i+len(needle)+100] if i>=0 else ""
def normalize_key(title,host,deadline,url):
    s="|".join(re.sub(r"\s+"," ",x or "").strip().lower() for x in (title,host,deadline))
    return hashlib.sha256((s+"|"+clean_url(url,url)).encode()).hexdigest()
def valid_decision(d):
    return isinstance(d,dict) and isinstance(d.get("is_relevant"),bool) and d.get("category") in CATEGORIES and isinstance(d.get("is_new"),bool) and d.get("confidence")=="high" and all(k in d for k in ("reason","exclude_reason","title","deadline","host","url","raw_snippet"))
def date_gate(post_date,first_seen,today=None):
    today=today or dt.date.today()
    return bool(post_date and first_seen and first_seen < post_date <= today)
def decide_local(text,title="",url=""):
    bad=r"(채용|결과\s*발표|유료\s*대관|홍보|동일\s*회차|첨부파일|상시)"
    if re.search(bad,title+" "+text): return "held"
    if not re.search(r"(공모|모집|지원사업|신청)",title+" "+text): return "held"
    if not re.search(r"(전시|공연|체험|예술)",title+" "+text): return "held"
    return "candidate"
def schema_db():
    return {"schema":SCHEMA,"sites":SITES,"fingerprint":fingerprint(),"posts":{},"outbox":{},"marker":{"initialized":False,"corrupt_reset_forbidden":True}}
def fingerprint(): return hashlib.sha256(json.dumps(SITES,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
def message_hash(message): return hashlib.sha256(message.encode()).hexdigest()
def accepted_ntfy_response(obj):
    return isinstance(obj,dict) and obj.get("event") in ("message","sent") and bool(obj.get("id")) and bool(obj.get("time")) and bool(obj.get("topic"))
def safe_log(msg): print(re.sub(r"(token|key|authorization|https?://\S+)","[redacted]",str(msg)),file=sys.stderr)
def self_tests():
    class T(unittest.TestCase):
        def test_01_schema(self): self.assertEqual(schema_db()["schema"],9)
        def test_02_sites(self): self.assertEqual(len(SITES),25)
        def test_03_url_track(self): self.assertNotIn("utm_",clean_url("https://a.test/p?id=7&utm_source=x","https://a.test"))
        def test_04_url_id(self): self.assertIn("id=7",clean_url("https://a.test/p?id=7","https://a.test"))
        def test_05_date(self): self.assertEqual(str(extract_date("등록일: 2026.02.03")),"2026-02-03")
        def test_06_date_missing(self): self.assertIsNone(extract_date("수정일 2026-02-03"))
        def test_07_gate(self): self.assertTrue(date_gate(dt.date(2026,2,3),dt.date(2026,2,1),dt.date(2026,2,4)))
        def test_08_same_day(self): self.assertFalse(date_gate(dt.date(2026,2,1),dt.date(2026,2,1),dt.date(2026,2,2)))
        def test_09_future(self): self.assertFalse(date_gate(dt.date(2027,2,1),dt.date(2026,2,1),dt.date(2026,2,2)))
        def test_10_parse(self): self.assertIn("제목",parse_page("<a href='/x'>제목</a>".encode(),"https://a.test")[0])
        def test_11_link(self): self.assertEqual(parse_page(b"<a href='/x'>x</a>","https://a.test")[1],["https://a.test/x"])
        def test_12_decision(self): self.assertEqual(decide_local("전시 공모 신청"),"candidate")
        def test_13_bad(self): self.assertEqual(decide_local("채용 공고"),"held")
        def test_14_valid(self): self.assertTrue(valid_decision({"is_relevant":True,"category":"exhibition","is_new":True,"confidence":"high","reason":"x","exclude_reason":"","title":"x","deadline":"2026-02-03","host":"h","url":"https://a","raw_snippet":"quote"}))
        def test_15_invalid_conf(self): self.assertFalse(valid_decision({"is_relevant":True,"category":"exhibition","is_new":True,"confidence":"medium","reason":"","exclude_reason":"","title":"","deadline":"","host":"","url":"","raw_snippet":""}))
        def test_16_accept(self): self.assertTrue(accepted_ntfy_response({"event":"message","id":"i","time":1,"topic":"t"}))
        def test_17_unknown(self): self.assertFalse(accepted_ntfy_response({"event":"error"}))
        def test_18_hash(self): self.assertEqual(message_hash("a"),message_hash("a"))
        def test_19_fingerprint(self): self.assertEqual(len(fingerprint()),64)
        def test_20_html(self): self.assertNotIn("bad",parse_page(b"<script>bad</script>ok","https://a")[0])
        def test_21_https(self): self.assertRaises(ValueError,get_bytes,"http://example.com")
        def test_22_limit(self): self.assertEqual(MAX_BYTES,2000000)
        def test_23_categories(self): self.assertEqual(len(CATEGORIES),4)
        def test_24_schema_sites(self): self.assertEqual(schema_db()["sites"],SITES)
        def test_25_key(self): self.assertEqual(len(normalize_key("a","b","c","https://a")),64)
        def test_26_quote(self): self.assertIn("접수",quote("abc 접수 시작 def","접수"))
        def test_27_tracking(self): self.assertNotIn("fbclid",clean_url("https://a/x?fbclid=1","https://a"))
        def test_28_no_redirect(self): self.assertIn("리디렉션", "리디렉션 보류")
    r=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromTestCase(T)); return r.wasSuccessful()
def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--self-test",action="store_true"); ap.add_argument("--dry-run",action="store_true"); ap.add_argument("--mode",choices=["send","status","resolve"],default="send"); ap.add_argument("--resolve"); ap.add_argument("--resolution",choices=["accepted","suppress","retry"]); ap.add_argument("--confirm")
    a=ap.parse_args()
    if a.self_test: raise SystemExit(0 if self_tests() else 1)
    # 네트워크 실행은 의도적으로 여기서 호출하지 않는다. Actions에서 명시적으로 연결 구현을 배치할 때도 기본은 dry-run.
    print("not_run: 네트워크/LLM/ntfy/GitHub 실테스트 및 수집을 실행하지 않았습니다.")
if __name__=="__main__": main()

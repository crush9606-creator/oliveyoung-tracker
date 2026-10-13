# -*- coding: utf-8 -*-
import os, re, time, uuid
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
from playwright.sync_api import sync_playwright

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
TOKEN       = os.environ.get("TOKEN", "")

URL = "https://www.oliveyoung.co.kr/store/goods/getGoodsDetail.do?goodsNo={}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")

# 올리브영이 상세페이지를 JS 렌더링(Next.js)으로 바꾸면서 "N명이 보고 있어요" 숫자가
# HTML에 더 이상 안 박힘 → 실시간 조회자 API(rts)를 직접 호출해서 값을 가져온다.
#   (1) 상품 HTML에서 대표 옵션의 표준코드(바코드) 추출
#   (2) rts.ai API 호출 → item.count(현재 동시 조회자)
# 봇차단(클라우드플레어)에 안 걸리도록, 매 제품마다 페이지를 통째로 열지 않고
# 처음 1회만 goto로 통과시킨 뒤 나머지는 브라우저 내부 fetch(사이트가 쓰는 그 방식)로 처리.
CUID = str(uuid.uuid4())
VIEW_RE = re.compile(r"([\d,]+)\s*명이\s*보고\s*있어요")   # 폴백용

# 브라우저 안에서 실행: 상품 HTML fetch → 바코드 추출 → rts API fetch → count 반환
JS_FETCH = r"""
async ([gno, cuid]) => {
  const dig = /"standardCode":"(\d{8,14})"/g;
  try {
    const h = await fetch(
      `https://www.oliveyoung.co.kr/store/goods/getGoodsDetail.do?goodsNo=${gno}`,
      { credentials: 'include' }).then(r => r.text());
    const s = h.split('\\"').join('"');
    let bc = null;
    const i = s.indexOf('"representFlag":true');
    if (i >= 0) {
      const win = s.slice(Math.max(0, i - 500), i);
      const ms = [...win.matchAll(dig)];
      if (ms.length) bc = ms[ms.length - 1][1];
    }
    if (!bc) { const m = s.match(/"standardCode":"(\d{8,14})"/); if (m) bc = m[1]; }
    if (!bc) return null;
    const r = await fetch(
      `https://rts.ai.oliveyoung.co.kr/api/stat?itemid=${bc}&type=view&cuid=${cuid}`,
      { headers: { 'accept': 'application/json' } });
    if (!r.ok) return null;
    const j = await r.json();
    return (j && j.item && typeof j.item.count !== 'undefined') ? j.item.count : null;
  } catch (e) { return null; }
}
"""

def now_seoul():
    return datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S")

def get_products():
    """시트의 '제품설정' 탭에서 제품 목록을 읽어온다. (구글 응답 지연 대비 재시도)"""
    last = None
    for attempt in range(3):
        try:
            r = requests.get(WEBHOOK_URL, params={"token": TOKEN}, timeout=60)
            return r.json().get("products", [])
        except Exception as e:
            last = e
            time.sleep(5)
    print("제품목록 불러오기 실패(doGet 응답 지연):", last)
    return []

def rep_barcode(html):
    """상품 HTML에서 대표 옵션(representFlag:true)의 표준코드(바코드)를 뽑는다."""
    s = html.replace('\\"', '"')
    idx = s.find('"representFlag":true')
    if idx >= 0:
        win = s[max(0, idx - 500):idx]
        ms = re.findall(r'"standardCode":"(\d{8,14})"', win)
        if ms:
            return ms[-1]
    m = re.search(r'"standardCode":"(\d{8,14})"', s)
    return m.group(1) if m else None

def via_fetch(page, goods_no):
    """가벼운 방식: 브라우저 내부 fetch로 바코드+조회자 수를 한 번에."""
    try:
        v = page.evaluate(JS_FETCH, [goods_no, CUID])
    except Exception:
        v = None
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        return None

def via_page(page, goods_no):
    """폴백: 실제로 페이지를 열어 바코드 추출 후 rts 호출."""
    for attempt in range(2):
        try:
            page.goto(URL.format(goods_no), wait_until="domcontentloaded", timeout=40000)
        except Exception:
            page.wait_for_timeout(2000)
            continue
        barcode = None
        for _ in range(3):
            barcode = rep_barcode(page.content())
            if barcode:
                break
            page.wait_for_timeout(1500)
        if not barcode:
            continue
        try:
            cnt = page.evaluate(
                """async ([bc, cuid]) => {
                    try {
                        const r = await fetch(
                          `https://rts.ai.oliveyoung.co.kr/api/stat?itemid=${bc}&type=view&cuid=${cuid}`,
                          { headers: { 'accept': 'application/json' } });
                        if (!r.ok) return null;
                        const j = await r.json();
                        return (j && j.item && typeof j.item.count !== 'undefined') ? j.item.count : null;
                    } catch (e) { return null; }
                }""",
                [barcode, CUID]
            )
        except Exception:
            cnt = None
        if cnt is not None:
            try:
                return int(cnt)
            except Exception:
                return None
        # 폴백의 폴백: 화면 텍스트에서라도
        try:
            page.wait_for_timeout(2000)
            m = VIEW_RE.search(page.inner_text("body"))
            if m:
                return int(m.group(1).replace(",", ""))
        except Exception:
            pass
        return None
    return None

def read_count(page, goods_no):
    # 1) 가벼운 fetch 방식 (최대 2번)
    for _ in range(2):
        v = via_fetch(page, goods_no)
        if v is not None:
            return v
        page.wait_for_timeout(1000)
    # 2) 폴백: 실제 페이지 로드
    return via_page(page, goods_no)

def main():
    if not WEBHOOK_URL.startswith("http"):
        print("WEBHOOK_URL 미설정"); return

    products = get_products()
    print(f"제품설정에서 {len(products)}개 불러옴")
    if not products:
        print("제품 목록이 비어있음 (제품설정 탭 확인)"); return

    # 같은 상품번호는 한 번만 크롤링 → 여러 이름(블록)에 같은 값 기록 (겹치는 제품 처리)
    by_goods = {}
    for p in products:
        g = p.get("goodsNo")
        if not g:
            continue
        by_goods.setdefault(g, []).append(p["name"])

    rows = []
    ts = now_seoul()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(user_agent=UA, locale="ko-KR", viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        # 최초 1회: 실제 페이지를 열어 올영 봇차단(클라우드플레어) 통과(쿠키 획득)
        first = next(iter(by_goods), None)
        if first:
            try:
                page.goto(URL.format(first), wait_until="domcontentloaded", timeout=40000)
                page.wait_for_timeout(3000)
            except Exception:
                pass

        for goods_no, names in by_goods.items():
            cnt = None
            try:
                cnt = read_count(page, goods_no)
            except Exception as e:
                print(f"[{goods_no}] 오류: {e}")
            print(f"[{goods_no}] {cnt}  -> {', '.join(names)}")
            for name in names:
                rows.append({"time": ts, "name": name, "goodsNo": goods_no,
                             "count": cnt if cnt is not None else ""})
            time.sleep(1)
        browser.close()

    for attempt in range(3):
        try:
            r = requests.post(WEBHOOK_URL, json={"token": TOKEN, "rows": rows}, timeout=60)
            print("시트 전송:", r.status_code, r.text[:200])
            break
        except Exception as e:
            print(f"시트 전송 재시도({attempt+1}/3):", e)
            time.sleep(5)

if __name__ == "__main__":
    main()

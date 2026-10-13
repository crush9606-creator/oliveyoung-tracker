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
#  (1) 상품페이지 HTML에서 대표 옵션의 표준코드(바코드) 추출
#  (2) rts.ai API를 호출해 item.count(현재 동시 조회자) 수신
RTS = "https://rts.ai.oliveyoung.co.kr/api/stat?itemid={}&type=view&cuid={}"
CUID = str(uuid.uuid4())                      # 실행 1회당 클라이언트 식별자 1개
VIEW_RE = re.compile(r"([\d,]+)\s*명이\s*보고\s*있어요")   # 혹시 모를 폴백용

def now_seoul():
    return datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S")

def get_products():
    """시트의 '제품설정' 탭에서 제품 목록을 읽어온다."""
    r = requests.get(WEBHOOK_URL, params={"token": TOKEN}, timeout=30)
    return r.json().get("products", [])

def rep_barcode(html):
    """상품 HTML에서 대표 옵션(representFlag:true)의 표준코드(바코드)를 뽑는다."""
    s = html.replace('\\"', '"')               # RSC 이스케이프(\") 해제
    idx = s.find('"representFlag":true')
    if idx >= 0:                               # 대표 옵션 직전의 표준코드
        win = s[max(0, idx - 500):idx]
        ms = re.findall(r'"standardCode":"(\d{8,14})"', win)
        if ms:
            return ms[-1]
    m = re.search(r'"standardCode":"(\d{8,14})"', s)   # 폴백: 첫 표준코드
    return m.group(1) if m else None

def read_count(page, goods_no):
    for attempt in range(2):                   # 페이지 로드 최대 2번 시도
        try:
            page.goto(URL.format(goods_no), wait_until="domcontentloaded", timeout=40000)
        except Exception:
            page.wait_for_timeout(2000)
            continue

        # 바코드는 서버 HTML(초기 렌더)에 들어있음 → 최대 3번만 짧게 재확인
        barcode = None
        for _ in range(3):
            barcode = rep_barcode(page.content())
            if barcode:
                break
            page.wait_for_timeout(1500)
        if not barcode:
            continue                           # 바코드 못 찾으면 다음 시도

        # 브라우저 안에서 실시간 조회자 API 호출(올영 페이지가 쓰는 그 호출)
        try:
            cnt = page.evaluate(
                """async ([bc, cuid]) => {
                    try {
                        const r = await fetch(
                          `https://rts.ai.oliveyoung.co.kr/api/stat?itemid=${bc}&type=view&cuid=${cuid}`,
                          { headers: { 'accept': 'application/json' } });
                        if (!r.ok) return null;
                        const j = await r.json();
                        return (j && j.item && typeof j.item.count !== 'undefined')
                                 ? j.item.count : null;
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

        # 폴백: 혹시 화면에 숫자가 렌더됐으면 텍스트에서라도 긁어본다(짧게)
        try:
            page.wait_for_timeout(2000)
            m = VIEW_RE.search(page.inner_text("body"))
            if m:
                return int(m.group(1).replace(",", ""))
        except Exception:
            pass
        return None
    return None

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

    try:
        r = requests.post(WEBHOOK_URL, json={"token": TOKEN, "rows": rows}, timeout=30)
        print("시트 전송:", r.status_code, r.text[:200])
    except Exception as e:
        print("시트 전송 실패:", e)

if __name__ == "__main__":
    main()

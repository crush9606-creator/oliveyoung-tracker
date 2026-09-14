# -*- coding: utf-8 -*-
import os, re, time
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
from playwright.sync_api import sync_playwright

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
TOKEN       = os.environ.get("TOKEN", "")

URL = "https://www.oliveyoung.co.kr/store/goods/getGoodsDetail.do?goodsNo={}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
VIEW_RE = re.compile(r"([\d,]+)\s*명이\s*보고\s*있어요")

def now_seoul():
    return datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S")

def get_products():
    """시트의 '제품설정' 탭에서 제품 목록을 읽어온다."""
    r = requests.get(WEBHOOK_URL, params={"token": TOKEN}, timeout=30)
    return r.json().get("products", [])

def read_count(page, goods_no):
    for attempt in range(2):                 # 페이지 로드 최대 2번 시도
        try:
            page.goto(URL.format(goods_no), wait_until="domcontentloaded", timeout=40000)
        except Exception:
            page.wait_for_timeout(2000)
            continue
        for _ in range(5):                    # 로드 후 최대 5번(약 15초) 재확인
            page.wait_for_timeout(3000)
            try:
                m = VIEW_RE.search(page.inner_text("body"))
            except Exception:
                continue
            if m:
                return int(m.group(1).replace(",", ""))
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

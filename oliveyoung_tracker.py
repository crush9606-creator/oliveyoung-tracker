# -*- coding: utf-8 -*-
import os, re, time
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
from playwright.sync_api import sync_playwright

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
TOKEN       = os.environ.get("TOKEN", "")

PRODUCTS = [
    {"name": "크림랩핑마스크 5+1",          "goodsNo": "A000000240462"},
    {"name": "메디힐 시트 마스크",           "goodsNo": "A000000223414"},
    {"name": "바이오던스 7+1",              "goodsNo": "A000000261842"},
    {"name": "TXA 크림",                   "goodsNo": "A000000253592"},
    {"name": "에스네이처 스쿠알란 수분크림",   "goodsNo": "A000000263782"},
    {"name": "달바 미스트",                 "goodsNo": "A000000259555"},
    {"name": "선크림 50+50",               "goodsNo": "A000000254726"},
    {"name": "메디힐 선세럼 50+50",         "goodsNo": "A000000232672"},
    {"name": "구달 어성초 수분선크림 50+50",  "goodsNo": "A000000263555"},
]

URL = "https://www.oliveyoung.co.kr/store/goods/getGoodsDetail.do?goodsNo={}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
VIEW_RE = re.compile(r"([\d,]+)\s*명이\s*보고\s*있어요")

def now_seoul():
    return datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S")

def read_count(page, goods_no):
    page.goto(URL.format(goods_no), wait_until="domcontentloaded", timeout=40000)
    for _ in range(3):
        page.wait_for_timeout(2500)
        m = VIEW_RE.search(page.inner_text("body"))
        if m:
            return int(m.group(1).replace(",", ""))
    return None

def main():
    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(user_agent=UA, locale="ko-KR", viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        for prod in PRODUCTS:
            cnt = None
            try:
                cnt = read_count(page, prod["goodsNo"])
            except Exception as e:
                print(f"[{prod['name']}] 오류: {e}")
            print(f"[{prod['name']}] {cnt}")
            rows.append({"time": now_seoul(), "name": prod["name"], "goodsNo": prod["goodsNo"], "count": cnt if cnt is not None else ""})
            time.sleep(1)
        browser.close()
    if WEBHOOK_URL.startswith("http"):
        try:
            r = requests.post(WEBHOOK_URL, json={"token": TOKEN, "rows": rows}, timeout=30)
            print("시트 전송:", r.status_code, r.text[:200])
        except Exception as e:
            print("시트 전송 실패:", e)
    else:
        print("WEBHOOK_URL 미설정")

if __name__ == "__main__":
    main()

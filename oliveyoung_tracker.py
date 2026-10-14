# -*- coding: utf-8 -*-
import os, re, time, uuid, json, random
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
from playwright.sync_api import sync_playwright

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
TOKEN       = os.environ.get("TOKEN", "")

URL = "https://www.oliveyoung.co.kr/store/goods/getGoodsDetail.do?goodsNo={}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
CACHE_FILE = "barcodes.json"
CUID = str(uuid.uuid4())

# [핵심] 올영이 상세페이지를 JS 렌더링으로 바꿔 "N명이 보고 있어요" 숫자가 HTML에 없음.
#  → 실시간 조회자 API(rts)를 직접 호출해서 가져온다. API는 바코드(표준코드)로 조회.
#  → 바코드는 상품마다 고정이라 barcodes.json에 저장해두고 재사용.
#    (평상시엔 무거운 상품페이지를 안 긁고 가벼운 rts API만 호출 → 올영 차단 회피)

# 상품 HTML에서 대표 옵션 바코드만 추출 (바코드 캐시에 없을 때만 1회 사용)
JS_BARCODE = r"""
async (gno) => {
  try {
    const h = await fetch(
      `https://www.oliveyoung.co.kr/store/goods/getGoodsDetail.do?goodsNo=${gno}`,
      { credentials: 'include' }).then(r => r.text());
    const s = h.split('\\"').join('"');
    const i = s.indexOf('"representFlag":true');
    if (i >= 0) {
      const win = s.slice(Math.max(0, i - 500), i);
      const ms = [...win.matchAll(/"standardCode":"(\d{8,14})"/g)];
      if (ms.length) return ms[ms.length - 1][1];
    }
    const m = s.match(/"standardCode":"(\d{8,14})"/);
    return m ? m[1] : null;
  } catch (e) { return null; }
}
"""

# 바코드로 실시간 조회자 수 호출 (가벼움)
JS_RTS = r"""
async ([bc, cuid]) => {
  try {
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

def load_cache():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_cache(cache):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=0, sort_keys=True)
        print("바코드 캐시 저장:", len(cache), "개")
    except Exception as e:
        print("바코드 캐시 저장 실패:", e)

def rts_count(page, barcode):
    try:
        v = page.evaluate(JS_RTS, [barcode, CUID])
    except Exception:
        v = None
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        return None

def resolve_barcode(page, goods_no):
    try:
        return page.evaluate(JS_BARCODE, goods_no)
    except Exception:
        return None

def pause(page, base, jitter):
    page.wait_for_timeout(base + random.randint(0, jitter))

def main():
    if not WEBHOOK_URL.startswith("http"):
        print("WEBHOOK_URL 미설정"); return

    products = get_products()
    print(f"제품설정에서 {len(products)}개 불러옴")
    if not products:
        print("제품 목록이 비어있음 (제품설정 탭 확인)"); return

    # 같은 상품번호는 한 번만 → 여러 이름(블록)에 같은 값 기록
    by_goods = {}
    for p in products:
        g = p.get("goodsNo")
        if not g:
            continue
        by_goods.setdefault(g, []).append(p["name"])

    cache = load_cache()
    cache = {g: b for g, b in cache.items() if g in by_goods}  # 사라진 제품 정리
    dirty = False

    ts = now_seoul()
    counts = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(user_agent=UA, locale="ko-KR", viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        # 최초 1회: 실제 페이지 로드로 올영 봇차단(클라우드플레어) 통과(쿠키)
        first = next(iter(by_goods), None)
        if first:
            try:
                page.goto(URL.format(first), wait_until="domcontentloaded", timeout=40000)
                page.wait_for_timeout(3000)
            except Exception:
                pass

        cached   = [g for g in by_goods if cache.get(g)]
        uncached = [g for g in by_goods if not cache.get(g)]

        # A단계: 바코드 이미 아는 제품 → 가벼운 rts만 (평상시 대부분 여기)
        print(f"A단계(캐시된 {len(cached)}개): rts 호출")
        for g in cached:
            c = rts_count(page, cache[g])
            if c is not None:
                counts[g] = c
            print(f"[{g}] {c}  -> {', '.join(by_goods[g])}")
            pause(page, 1500, 1000)

        # B단계: 바코드 모르는 제품(신규/워밍업) → 상품페이지로 바코드 해석 후 rts
        if uncached:
            print(f"B단계(신규 {len(uncached)}개): 바코드 해석 + rts")
        for g in uncached:
            bc = resolve_barcode(page, g)
            if bc:
                cache[g] = bc; dirty = True
                c = rts_count(page, bc)
                if c is not None:
                    counts[g] = c
                print(f"[{g}] bc={bc} {c}  -> {', '.join(by_goods[g])}")
            else:
                print(f"[{g}] 바코드 해석 실패(다음 실행 재시도)  -> {', '.join(by_goods[g])}")
            pause(page, 3000, 1500)

        # C단계: 캐시는 있는데 rts가 비어 돌아온 것만 쿨다운 후 1번 재시도
        missing = [g for g in by_goods if g not in counts and cache.get(g)]
        if missing:
            print(f"C단계: {len(missing)}개 재시도(30초 쿨다운)")
            page.wait_for_timeout(30000)
            for g in missing:
                c = rts_count(page, cache[g])
                if c is not None:
                    counts[g] = c
                    print(f"[{g}] (재시도) {c}  -> {', '.join(by_goods[g])}")
                pause(page, 1500, 1000)

        browser.close()

    if dirty:
        save_cache(cache)

    print(f"수집 {len(counts)}/{len(by_goods)} | 바코드 캐시 {len(cache)}/{len(by_goods)}")

    rows = []
    for goods_no, names in by_goods.items():
        c = counts.get(goods_no)
        for name in names:
            rows.append({"time": ts, "name": name, "goodsNo": goods_no,
                         "count": c if c is not None else ""})

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

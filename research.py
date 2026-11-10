#!/usr/bin/env python3
"""일회성 상권 조사: 우리 가게 메뉴·후기 화면과 온수리(전등사 앞) 주변 가게 목록을 네이버 플레이스 공개 화면에서 읽어 research/ 에 저장."""
import json, re, time, urllib.parse, os
from collect import http_text, _apollo, log, load_config, _searchad_headers, http_json, _qc

OUT = "research"
os.makedirs(OUT, exist_ok=True)
PID = "21202659"
ONSU = ("126.4893", "37.6387")   # 길상면 온수리 읍내
num = lambda x: int(re.sub(r"[^0-9]", "", str(x))) if x not in (None, "") and re.sub(r"[^0-9]", "", str(x)) else None


def dump(name, obj):
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def by_type(st):
    t = {}
    for k, v in (st or {}).items():
        if isinstance(v, dict) and v.get("__typename"):
            t.setdefault(v["__typename"], []).append(v)
    return t


def deref(st, v, depth=0):
    if depth > 3:
        return v
    if isinstance(v, dict):
        if "__ref" in v and len(v) == 1:
            return deref(st, st.get(v["__ref"], {}), depth + 1)
        return {k: deref(st, x, depth + 1) for k, x in v.items()}
    if isinstance(v, list):
        return [deref(st, x, depth + 1) for x in v]
    return v


def store_pages(pid, pages=("home", "menu/list", "review/visitor", "information")):
    res = {"types": {}}
    for p in pages:
        try:
            html = http_text(f"https://m.place.naver.com/restaurant/{pid}/{p}")
            st = _apollo(html) or {}
            ty = by_type(st)
            res["types"][p] = {k: len(v) for k, v in ty.items()}
            for tn, items in ty.items():
                if re.search(r"Menu|Review|Base|Detail|Hour|Conven|Keyword|Theme|Stat|Description|Info", tn):
                    res.setdefault(tn, [])
                    for it in items:
                        d = deref(st, it)
                        s = json.dumps(d, ensure_ascii=False)
                        if len(s) < 6000 and d not in res[tn]:
                            res[tn].append(d)
        except Exception as e:
            res.setdefault("err", []).append(f"{p}: {type(e).__name__} {str(e)[:80]}")
        time.sleep(0.6)
    return res


def place_list(q, kind="restaurant"):
    html = http_text(f"https://m.place.naver.com/{kind}/list?" + urllib.parse.urlencode({"query": q, "x": ONSU[0], "y": ONSU[1], "level": "top"}))
    st = _apollo(html) or {}
    root = st.get("ROOT_QUERY", {})
    refs, total = [], None
    for k, v in root.items():
        if k.startswith(("restaurants(", "restaurantList(", "places(", "placeList(")):
            v = v or {}
            items = v.get("items") or (v.get("businesses") or {}).get("items") or []
            total = (v.get("businesses") or {}).get("total") or v.get("total") or total
            if len(items) > len(refs):
                refs = items
    out = []
    for r in refs:
        o = st.get(r.get("__ref", ""), r) if isinstance(r, dict) else None
        if not o or not o.get("name"):
            continue
        out.append({"name": o.get("name"), "id": str(o.get("id") or ""), "category": o.get("category"),
                    "addr": o.get("commonAddress") or o.get("address"), "road": o.get("roadAddress"), "full": o.get("fullAddress") or o.get("address"),
                    "visitor": num(o.get("visitorReviewCount")), "blog": num(o.get("blogCafeReviewCount")), "score": o.get("visitorReviewScore"),
                    "x": o.get("x"), "y": o.get("y"), "dist": o.get("distance"), "status": o.get("businessHours") or o.get("newBusinessHours"),
                    "price": o.get("priceCategory"), "tv": o.get("broadcastInfo"), "new": o.get("newOpening"), "ad": o.get("adId") or o.get("isAd")})
    return {"total": total, "items": out, "keys": sorted((st.get(refs[0].get("__ref", ""), {}) if refs and isinstance(refs[0], dict) else {}).keys())[:90]}


def main():
    res = {}
    log("우리 가게 화면")
    dump("store.json", store_pages(PID))
    qs = ["전등사 맛집", "온수리 맛집", "길상면 맛집", "전등사 카페", "온수리 카페", "전등사 칼국수", "온수리 칼국수", "온수리 국밥", "온수리 분식",
          "온수리 치킨", "온수리 중국집", "온수리 고기", "온수리 술집", "온수리 빵집", "온수리 김밥", "온수리 백반", "온수리 해장국", "전등사 산채비빔밥",
          "온수리 반찬", "온수리 돈까스", "온수리 국수", "온수리 족발", "온수리 피자", "온수리 떡볶이", "길상면 카페", "강화 젓국갈비", "온수리 한식", "온수리 포장"]
    for q in qs:
        try:
            res[q] = place_list(q)
            log(f"{q}: {len(res[q]['items'])}곳 / 전체 {res[q]['total']}")
        except Exception as e:
            res[q] = {"err": f"{type(e).__name__} {str(e)[:80]}"}
        time.sleep(0.6)
    dump("lists.json", res)
    # 온수리 주소인 가게 상세(메뉴·가격·후기 수)
    seen, det = {}, {}
    for q, v in res.items():
        for it in v.get("items", []):
            a = " ".join(str(it.get(k) or "") for k in ("addr", "road", "full"))
            if it["id"] and ("온수리" in a or "전등사로" in a or "온수길" in a or "삼랑성길" in a or "길상로" in a):
                seen.setdefault(it["id"], it)
    log(f"상세 조회 대상 {len(seen)}곳")
    for pid, it in list(seen.items())[:60]:
        try:
            d = store_pages(pid, pages=("home",))
            keep = {k: v for k, v in d.items() if re.search(r"Menu|Base|types|err", k)}
            det[pid] = {"list": it, "detail": keep}
        except Exception as e:
            det[pid] = {"list": it, "err": str(e)[:80]}
    dump("onsu_detail.json", det)
    # 검색량(네이버 검색광고 키워드 도구)
    kws = ["온수리맛집", "전등사맛집", "전등사카페", "온수리카페", "길상면맛집", "전등사칼국수", "강화도국밥", "강화도분식", "강화도빵집", "강화도베이커리",
           "강화도브런치", "강화도막국수", "강화도보리밥", "강화도산채비빔밥", "강화도해장국", "강화도짬뽕", "강화도돈까스", "강화도치킨", "강화도김밥", "강화도반찬",
           "강화도떡", "강화도도넛", "강화도파스타", "강화도피자", "강화도삼겹살", "강화도냉면", "강화도추어탕", "강화도순대국", "강화도한정식", "강화도두부",
           "강화도막걸리", "강화도술집", "강화도포장", "강화도배달", "초지대교맛집", "강화루지맛집", "강화도쌈밥", "강화도백반", "강화도아침식사", "강화도고기집",
           "강화도한우", "강화도숯불갈비", "강화도닭백숙", "강화도오리", "강화도생선구이", "강화도게장", "강화도해물탕", "강화도조개구이", "강화도회", "강화도대하구이",
           "전등사", "강화루지", "동검도", "강화도아이스크림", "강화도젤라또", "강화도디저트", "강화도쑥", "강화도인삼", "강화도속노랑고구마", "강화도기념품"]
    vol = {}
    if os.getenv("NAVER_AD_API_KEY"):
        uri = "/keywordstool"
        for i in range(0, len(kws), 5):
            try:
                r = http_json("https://api.searchad.naver.com" + uri + "?" + urllib.parse.urlencode({"hintKeywords": ",".join(kws[i:i + 5]), "showDetail": "1"}), _searchad_headers("GET", uri))
                for it in r.get("keywordList", []):
                    k = it["relKeyword"]
                    if k in kws or any(w in k for w in ("전등사", "온수리", "길상", "루지", "초지")):
                        vol[k] = _qc(it.get("monthlyPcQcCnt")) + _qc(it.get("monthlyMobileQcCnt"))
            except Exception as e:
                vol[f"err{i}"] = str(e)[:80]
            time.sleep(0.5)
    dump("volume.json", dict(sorted(vol.items(), key=lambda x: -x[1] if isinstance(x[1], int) else 0)))


if __name__ == "__main__":
    main()

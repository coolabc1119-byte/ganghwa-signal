#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
강화 시그널 수집기 (v0.1)

매일 1회 실행되어 data/ 폴더에 JSON을 만든다. 대시보드(index.html)는 이 JSON만 읽는다.

데이터 출처
  1) 네이버 데이터랩 검색어 트렌드  → 키워드별 일별 상대 검색량 (0~100)
  2) 네이버 검색광고 키워드도구     → 월간 절대 검색량(PC/모바일) + 연관 키워드 발굴
  3) 카카오 로컬 API                → 강화도 안 업체 수 (메뉴별 경쟁 강도)
  4) 한국관광공사 관광빅데이터       → 강화군 일별 방문자 수
  5) 네이버 뉴스 검색               → 급등일 설명용 뉴스 로그 (매일 쌓아 둠)

환경변수(API 키)가 하나도 없으면 샘플 모드로 동작한다. 일부만 있으면 있는 것만 실제 데이터를 쓴다.

환경변수
  NAVER_CLIENT_ID, NAVER_CLIENT_SECRET          (네이버 개발자센터 — 데이터랩·뉴스)
  NAVER_AD_API_KEY, NAVER_AD_SECRET, NAVER_AD_CUSTOMER_ID   (네이버 검색광고)
  KAKAO_REST_KEY                                (카카오 개발자)
  DATA_GO_KR_KEY                                (공공데이터포털 — 한국관광공사)
"""

import base64
import hashlib
import hmac
import json
import math
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
ERRORS = {}  # 출처별 실패 메시지(키 값 미포함)
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
DAYS = 485  # 120일 추이 + 364일 전 겹쳐보기까지 커버 (데이터랩은 2016년부터 제공)

# 강화도 전체를 덮는 사각형 (경도1,위도1,경도2,위도2) — 카카오 로컬 rect 파라미터용
GANGHWA_RECT = "126.25,37.55,126.62,37.83"
GANGHWA_AREA_CD = "28"      # 인천광역시
GANGHWA_SIGNGU_CD = "28710"  # 강화군


def log(msg):
    print(f"[collect] {msg}", flush=True)


def load_config():
    with open(os.path.join(ROOT, "keywords.json"), encoding="utf-8") as f:
        return json.load(f)


def save(name, obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    log(f"saved data/{name}")


def load_existing(name, default):
    p = os.path.join(DATA_DIR, name)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return default


def http_json(url, headers=None, method="GET", body=None, timeout=30):
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def date_range(days):
    end = datetime.now(KST).date() - timedelta(days=1)  # 데이터랩은 전날까지 제공
    start = end - timedelta(days=days - 1)
    return start, end


def ymd_list(start, end):
    out, d = [], start
    while d <= end:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# 1) 네이버 데이터랩 — 일별 상대 검색량
# ---------------------------------------------------------------------------
def fetch_datalab(cfg, start, end):
    """
    데이터랩은 한 번 호출에 최대 5개 그룹까지만 비교되고, 호출이 다르면 척도(0~100)가 달라진다.
    그래서 모든 호출에 '앵커 키워드'(강화도 맛집)를 함께 넣고, 앵커 값을 기준으로 전체를 한 척도로 다시 맞춘다.
    """
    cid, csec = os.getenv("NAVER_CLIENT_ID"), os.getenv("NAVER_CLIENT_SECRET")
    if not (cid and csec):
        return None
    anchor = cfg["anchor_keyword"]
    all_kw = []
    for g, kws in cfg["groups"].items():
        for k in kws:
            if k not in all_kw:
                all_kw.append(k)
    others = [k for k in all_kw if k != anchor]

    headers = {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": csec}
    url = "https://openapi.naver.com/v1/datalab/search"
    series = {}
    anchor_ref = None
    for i in range(0, len(others), 4):  # 앵커 1 + 4개 = 5그룹
        chunk = others[i:i + 4]
        body = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "timeUnit": "date",
            "keywordGroups": [{"groupName": anchor, "keywords": [anchor]}]
            + [{"groupName": k, "keywords": [k]} for k in chunk],
        }
        res = http_json(url, headers, "POST", body)
        got = {}
        for r in res.get("results", []):
            got[r["title"]] = {d["period"]: d["ratio"] for d in r.get("data", [])}
        a = got.get(anchor, {})
        a_mean = (sum(a.values()) / len(a)) if a else 0
        if anchor_ref is None:
            anchor_ref = a_mean or 1
            series[anchor] = a
        scale = (anchor_ref / a_mean) if a_mean else 1
        for k in chunk:
            series[k] = {d: round(v * scale, 2) for d, v in got.get(k, {}).items()}
        time.sleep(0.3)
    dates = ymd_list(start, end)
    return {
        "dates": dates,
        "series": {k: [series.get(k, {}).get(d, 0) for d in dates] for k in all_kw},
        "note": "네이버 데이터랩 상대 검색량. 앵커 키워드 기준으로 한 척도로 맞춤 (최댓값 100은 앵커 기준).",
    }


# ---------------------------------------------------------------------------
# 2) 네이버 검색광고 키워드도구 — 월간 절대 검색량 + 연관 키워드
# ---------------------------------------------------------------------------
def _searchad_headers(method, uri):
    key, sec, cust = os.getenv("NAVER_AD_API_KEY"), os.getenv("NAVER_AD_SECRET"), os.getenv("NAVER_AD_CUSTOMER_ID")
    ts = str(int(time.time() * 1000))
    sig = base64.b64encode(hmac.new(sec.encode(), f"{ts}.{method}.{uri}".encode(), hashlib.sha256).digest()).decode()
    return {"X-Timestamp": ts, "X-API-KEY": key, "X-Customer": cust, "X-Signature": sig}


def _qc(v):
    # "< 10" 같은 문자열이 올 수 있음
    if isinstance(v, (int, float)):
        return int(v)
    try:
        return int(str(v).replace("<", "").replace(",", "").strip())
    except Exception:
        return 0


def fetch_keyword_tool(cfg):
    if not (os.getenv("NAVER_AD_API_KEY") and os.getenv("NAVER_AD_SECRET") and os.getenv("NAVER_AD_CUSTOMER_ID")):
        return None
    tracked = {k.replace(" ", "") for kws in cfg["groups"].values() for k in kws}
    hints = list(dict.fromkeys(cfg["discovery_hints"] + sorted(tracked)))
    uri = "/keywordstool"
    rows = {}
    for i in range(0, len(hints), 5):
        chunk = [h.replace(" ", "") for h in hints[i:i + 5]]
        q = urllib.parse.urlencode({"hintKeywords": ",".join(chunk), "showDetail": "1"})
        res = http_json("https://api.searchad.naver.com" + uri + "?" + q, _searchad_headers("GET", uri))
        for it in res.get("keywordList", []):
            k = it["relKeyword"]
            pc, mo = _qc(it.get("monthlyPcQcCnt")), _qc(it.get("monthlyMobileQcCnt"))
            rows[k] = {
                "keyword": k,
                "pc": pc,
                "mobile": mo,
                "total": pc + mo,
                "comp": it.get("compIdx", ""),
                "tracked": k.replace(" ", "") in tracked,
            }
        time.sleep(0.5)
    out = sorted(rows.values(), key=lambda r: -r["total"])
    # 강화 관련 키워드만 남김
    out = [r for r in out if "강화" in r["keyword"] or "전등사" in r["keyword"] or "석모도" in r["keyword"]]
    return {"rows": out[:300], "note": "네이버 검색광고 키워드도구. 최근 한 달 PC/모바일 검색수 합계."}


# ---------------------------------------------------------------------------
# 3) 카카오 로컬 — 강화도 안 업체 수
# ---------------------------------------------------------------------------
def fetch_competitors(cfg):
    key = os.getenv("KAKAO_REST_KEY")
    if not key:
        return None
    headers = {"Authorization": f"KakaoAK {key}"}
    out = {}
    for q in cfg["competitor_queries"]:
        params = urllib.parse.urlencode({"query": q, "category_group_code": "FD6" if q != "카페" else "CE7", "rect": GANGHWA_RECT, "size": 15})
        res = http_json("https://dapi.kakao.com/v2/local/search/keyword.json?" + params, headers)
        out[q] = {
            "count": res.get("meta", {}).get("total_count", 0),
            "samples": [d["place_name"] for d in res.get("documents", [])[:5]],
        }
        time.sleep(0.2)
    return {"queries": out, "note": "카카오 로컬 API, 강화도 범위 안 음식점 수."}


# ---------------------------------------------------------------------------
# 4) 한국관광공사 관광빅데이터 — 강화군 일별 방문자
# ---------------------------------------------------------------------------
def fetch_visitors(start, end):
    key = os.getenv("DATA_GO_KR_KEY")
    if not key:
        return None
    base = "https://apis.data.go.kr/B551011/DataLabService/locgoRegnVisitrDDList"
    params = {
        "serviceKey": key, "numOfRows": 1000, "pageNo": 1, "MobileOS": "ETC", "MobileApp": "ganghwa-signal",
        "startYmd": start.strftime("%Y%m%d"), "endYmd": end.strftime("%Y%m%d"),
        "areaCd": GANGHWA_AREA_CD, "signguCd": GANGHWA_SIGNGU_CD, "_type": "json",
    }
    res = http_json(base + "?" + urllib.parse.urlencode(params, safe="%"), timeout=60)
    items = res.get("response", {}).get("body", {}).get("items", {}).get("item", [])
    by_day = {}
    for it in items:
        d = str(it.get("baseYmd", ""))
        if len(d) != 8:
            continue
        iso = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        div = str(it.get("touDivCd", ""))  # 1 현지인, 2 외지인, 3 외국인
        by_day.setdefault(iso, {"local": 0, "visitor": 0, "foreign": 0})
        n = float(it.get("touNum", 0) or 0)
        by_day[iso]["local" if div == "1" else "foreign" if div == "3" else "visitor"] += n
    dates = sorted(by_day)
    return {
        "dates": dates,
        "visitor": [round(by_day[d]["visitor"]) for d in dates],
        "local": [round(by_day[d]["local"]) for d in dates],
        "foreign": [round(by_day[d]["foreign"]) for d in dates],
        "note": "한국관광공사 관광빅데이터(통신사 기반) 강화군 일별 방문자. 원천 데이터가 며칠~몇 주 늦게 올라옴.",
    }


# ---------------------------------------------------------------------------
# 5) 네이버 뉴스 — 매일 로그 적재
# ---------------------------------------------------------------------------
def fetch_news_log(cfg):
    cid, csec = os.getenv("NAVER_CLIENT_ID"), os.getenv("NAVER_CLIENT_SECRET")
    if not (cid and csec):
        return None
    existing = load_existing("news_log.json", {"items": []})
    existing["items"] = [i for i in existing["items"] if not str(i.get("link", "")).startswith("https://example.com/")]  # 샘플 제거
    headers = {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": csec}
    seen = {it["link"] for it in existing["items"]}
    for q in cfg["news_queries"]:
        params = urllib.parse.urlencode({"query": q, "display": 30, "sort": "date"})
        res = http_json("https://openapi.naver.com/v1/search/news.json?" + params, headers)
        for it in res.get("items", []):
            if it["link"] in seen:
                continue
            try:
                pub = datetime.strptime(it["pubDate"], "%a, %d %b %Y %H:%M:%S %z").astimezone(KST).date().isoformat()
            except Exception:
                pub = datetime.now(KST).date().isoformat()
            title = it["title"].replace("<b>", "").replace("</b>", "").replace("&quot;", '"').replace("&amp;", "&")
            existing["items"].append({"date": pub, "title": title, "link": it["link"], "query": q})
            seen.add(it["link"])
        time.sleep(0.2)
    cutoff = (datetime.now(KST).date() - timedelta(days=400)).isoformat()
    existing["items"] = sorted([i for i in existing["items"] if i["date"] >= cutoff], key=lambda i: i["date"], reverse=True)
    return existing


# ---------------------------------------------------------------------------
# 분석: 주간 변화, 급등일, 기회 키워드
# ---------------------------------------------------------------------------

def _avg(vals, a, b):
    seg = vals[a:b]
    return (sum(seg) / len(seg)) if seg else 0.0


def _pct(cur, base):
    if base > 0:
        return round((cur - base) / base * 100, 1)
    return None


def compare(trend):
    """
    키워드별 비교 지표. 데이터랩은 한 호출 안에서는 척도가 같으므로 같은 시계열 안의 비교(전주·전월·전년)는 유효함.
      week:  최근 7일 평균  vs 직전 7일 평균
      month: 최근 30일 평균 vs 직전 30일 평균
      yoy:   최근 30일 평균 vs 1년 전 같은 30일 평균(364일 전 — 요일을 맞추기 위해 52주)
    """
    n = len(trend["dates"])
    out = []
    for k, v in trend["series"].items():
        w_cur, w_prev = _avg(v, n - 7, n), _avg(v, n - 14, n - 7)
        m_cur, m_prev = _avg(v, n - 30, n), _avg(v, n - 60, n - 30)
        y_prev = _avg(v, n - 30 - 364, n - 364) if n >= 394 else 0
        out.append({
            "keyword": k,
            "week": {"cur": round(w_cur, 1), "prev": round(w_prev, 1), "pct": _pct(w_cur, w_prev)},
            "month": {"cur": round(m_cur, 1), "prev": round(m_prev, 1), "pct": _pct(m_cur, m_prev)},
            "yoy": {"cur": round(m_cur, 1), "prev": round(y_prev, 1), "pct": _pct(m_cur, y_prev) if y_prev else None},
        })
    return out


def monthly_table(trend):
    """월별 평균 — 올해/작년 같은 달을 나란히 보기 위한 표."""
    by = {}
    for d, i in zip(trend["dates"], range(len(trend["dates"]))):
        ym = d[:7]
        for k, v in trend["series"].items():
            by.setdefault(k, {}).setdefault(ym, []).append(v[i])
    return {k: {ym: round(sum(x) / len(x), 1) for ym, x in m.items()} for k, m in by.items()}


def weekly_change(trend):
    dates, out = trend["dates"], []
    n = len(dates)
    if n < 14:
        return out
    for k, vals in trend["series"].items():
        this_w = sum(vals[n - 7:]) / 7
        last_w = sum(vals[n - 14:n - 7]) / 7
        prev4 = sum(vals[max(0, n - 35):n - 7]) / max(1, len(vals[max(0, n - 35):n - 7]))
        pct = ((this_w - last_w) / last_w * 100) if last_w > 0 else (100 if this_w > 0 else 0)
        out.append({"keyword": k, "this_week": round(this_w, 1), "last_week": round(last_w, 1), "avg_4w": round(prev4, 1), "change_pct": round(pct, 1)})
    return sorted(out, key=lambda r: -r["change_pct"])


def detect_spikes(trend, rule, news_log, broadcast_log):
    ratio, min_v, lb = rule["ratio"], rule["min_value"], rule["lookback_weeks"]
    dates = trend["dates"]
    news_by_date = {}
    for it in (news_log or {}).get("items", []):
        news_by_date.setdefault(it["date"], []).append(it)
    bc_by_date = {b["date"]: b for b in broadcast_log}
    events = []
    for k, vals in trend["series"].items():
        for i in range(7 * lb, len(vals)):
            base = [vals[i - 7 * w] for w in range(1, lb + 1)]
            base_mean = sum(base) / len(base)
            v = vals[i]
            if v >= min_v and base_mean > 0 and v / base_mean >= ratio:
                d = dates[i]
                events.append({
                    "date": d, "keyword": k, "value": round(v, 1), "baseline": round(base_mean, 1), "ratio": round(v / base_mean, 2),
                    "broadcast": bc_by_date.get(d), "news": news_by_date.get(d, [])[:5],
                })
    events.sort(key=lambda e: (e["date"], -e["ratio"]), reverse=True)
    return events


def opportunity(volume, competitors, cfg):
    """월 검색량 ÷ 업체 수 — 처남의 '젓국갈비' 논리: 찾는 사람은 있는데 파는 집이 적은 메뉴."""
    if not (volume and competitors):
        return []
    vol = {r["keyword"].replace(" ", ""): r["total"] for r in volume["rows"]}
    out = []
    for q, info in competitors["queries"].items():
        cands = [f"강화도{q}", f"강화{q}", f"강화도{q}맛집"]
        total = sum(vol.get(c, 0) for c in cands)
        cnt = info["count"]
        out.append({"menu": q, "monthly_search": total, "shops": cnt, "per_shop": round(total / cnt, 1) if cnt else None,
                    "mine": q in cfg["store"]["menus"]})
    return sorted(out, key=lambda r: -(r["per_shop"] or 0))


# ---------------------------------------------------------------------------
# 샘플 데이터 (API 키 없을 때)
# ---------------------------------------------------------------------------
def sample_trend(cfg, start, end, seed=7):
    rnd = random.Random(seed)
    dates = ymd_list(start, end)
    base_level = {
        "강화도 맛집": 62, "강화도 가볼만한곳": 55, "강화도 여행": 45, "강화도 카페": 40, "강화도 당일치기": 30,
        "강화도 드라이브": 25, "강화도 펜션": 35, "전등사 맛집": 22, "강화도 장어": 30, "강화도 밴댕이": 14,
        "강화도 순무": 10, "강화도 꽃게탕": 26, "강화도 간장게장": 12, "강화도 낙지": 13, "강화도 연포탕": 6,
        "강화도 젓국갈비": 9, "강화 젓국갈비": 5,
    }
    bc_dates = {b["date"] for b in cfg["broadcast_log"]}
    series = {}
    for g, kws in cfg["groups"].items():
        for k in kws:
            if k in series:
                continue
            lvl = base_level.get(k, 15)
            vals = []
            for d in dates:
                dt = date.fromisoformat(d)
                wd = dt.weekday()
                # 주말(금·토 검색 피크), 꽃게는 가을 성수기, 젓국갈비는 완만한 상승 추세
                w = 1.0 + (0.35 if wd in (4, 5) else 0.15 if wd == 6 else 0)
                yd = dt.timetuple().tm_yday
                # 연간 성장(전년 대비): 꽃게·낙지는 소폭 감소, 젓국갈비는 큰 폭 증가, 나머지는 소폭 증가
                years = (dt - date.fromisoformat(dates[0])).days / 365
                growth = {"꽃게": 0.93, "게장": 0.95, "낙지": 0.97, "젓국갈비": 1.9}.get(next((t for t in ("꽃게", "게장", "낙지", "젓국갈비") if t in k), ""), 1.08) ** years
                season = 1.0 + 0.15 * math.cos((yd - 290) / 58)  # 가을 성수기 공통
                if "꽃게" in k or "게장" in k:
                    season += 0.5 * math.exp(-((yd - 290) / 30) ** 2) + 0.25 * math.exp(-((yd - 120) / 25) ** 2)  # 가을 + 봄 암꽃게
                if "펜션" in k or "드라이브" in k or "여행" in k:
                    season += 0.3 * math.exp(-((yd - 215) / 25) ** 2)  # 여름휴가
                if "순무" in k:
                    season += 0.6 * math.exp(-((yd - 325) / 20) ** 2)  # 김장철
                v = lvl * w * season * growth * rnd.uniform(0.85, 1.15)
                # 샘플 방송 효과: 방송일 + 다음 2일, 전등사/젓국갈비/맛집 키워드
                for bd in bc_dates:
                    gap = (dt - date.fromisoformat(bd)).days
                    if 0 <= gap <= 2 and any(t in k for t in ("전등사", "젓국갈비", "강화도 맛집")):
                        v *= (2.6, 1.9, 1.3)[gap]
                vals.append(round(min(v, 100), 1))
            series[k] = vals
    return {"dates": dates, "series": series, "note": "⚠ 샘플 데이터 — API 키 연결 전 화면 확인용"}


def sample_volume(cfg):
    rnd = random.Random(3)
    rows = [
        ("강화도맛집", 48200), ("강화도가볼만한곳", 41500), ("강화도여행", 22100), ("강화도카페", 31800), ("강화도펜션", 15400),
        ("강화도당일치기", 7200), ("강화도드라이브", 5100), ("전등사맛집", 6800), ("강화도장어", 9900), ("강화도밴댕이", 4300),
        ("강화도순무", 2600), ("강화도꽃게탕", 8100), ("강화도간장게장", 3900), ("강화도낙지", 4100), ("강화도연포탕", 1700),
        ("강화도젓국갈비", 3300), ("강화젓국갈비", 1200), ("강화도밴댕이회무침", 1900), ("강화도장어맛집", 5200),
        ("강화도꽃게탕맛집", 3100), ("강화도회", 2800), ("강화도조개구이", 2200), ("강화도칼국수", 2600), ("강화도한정식", 2100),
        ("강화도인삼", 6400), ("강화도섬쌀", 1500), ("강화도속노랑고구마", 3200), ("강화도순무김치", 2900), ("강화도빵집", 2400),
        ("강화도루지", 19800), ("강화도보문사", 7600), ("강화도동막해수욕장", 8200), ("강화도마니산", 9400), ("석모도맛집", 3700),
        ("강화도젓국갈비맛집", 900), ("강화도밴댕이맛집", 2300), ("강화도뷰맛집", 1800), ("강화도아이와가볼만한곳", 4600),
    ]
    tracked = {k.replace(" ", "") for kws in cfg["groups"].values() for k in kws}
    out = []
    for k, t in rows:
        mo = int(t * rnd.uniform(0.72, 0.85))
        out.append({"keyword": k, "pc": t - mo, "mobile": mo, "total": t, "comp": rnd.choice(["낮음", "중간", "높음"]), "tracked": k in tracked})
    return {"rows": sorted(out, key=lambda r: -r["total"]), "note": "⚠ 샘플 데이터"}


def sample_competitors(cfg):
    counts = {"꽃게탕": 41, "간장게장": 23, "낙지": 37, "연포탕": 12, "젓국갈비": 9, "밴댕이": 28, "장어": 44, "순무": 6, "카페": 312}
    return {"queries": {q: {"count": counts.get(q, 20), "samples": []} for q in cfg["competitor_queries"]}, "note": "⚠ 샘플 데이터"}


def sample_visitors(cfg, start, end):
    rnd = random.Random(11)
    dates = ymd_list(start, end - timedelta(days=10))  # 실제처럼 열흘 늦게 끝남
    vis, loc = [], []
    bc_dates = {b["date"] for b in cfg["broadcast_log"]}
    for d in dates:
        dt = date.fromisoformat(d)
        wd = dt.weekday()
        base = 52000 * (1.55 if wd == 5 else 1.45 if wd == 6 else 1.0)
        base *= 1.0 + 0.25 * math.exp(-((dt.timetuple().tm_yday - 292) / 25) ** 2)  # 가을 단풍
        for bd in bc_dates:
            gap = (dt - date.fromisoformat(bd)).days
            if 0 <= gap <= 6:
                base *= 1.0 + 0.12 * (1 - gap / 7)
        vis.append(round(base * rnd.uniform(0.92, 1.08)))
        loc.append(round(38000 * rnd.uniform(0.95, 1.05)))
    return {"dates": dates, "visitor": vis, "local": loc, "foreign": [round(v * 0.02) for v in vis], "note": "⚠ 샘플 데이터"}


def sample_news(cfg):
    items = []
    for b in cfg["broadcast_log"]:
        items.append({"date": b["date"], "title": f"(샘플) 강화도 전등사 앞 맛집, 방송 소개 뒤 주말 대기 줄", "link": "https://example.com/sample-1", "query": "강화도 방송"})
        nd = (date.fromisoformat(b["date"]) + timedelta(days=1)).isoformat()
        items.append({"date": nd, "title": "(샘플) 가을 꽃게 제철… 강화 어판장 시세 소폭 상승", "link": "https://example.com/sample-2", "query": "강화도 꽃게"})
    return {"items": items}


# ---------------------------------------------------------------------------
def main():
    cfg = load_config()
    start, end = date_range(DAYS)
    sources = {}

    def run(name, fn, sample_fn):
        try:
            res = fn()
        except Exception as e:  # 한 출처가 죽어도 나머지는 계속
            log(f"{name} 실패: {e}")
            ERRORS[name] = f"{type(e).__name__}: {str(e)[:200]}"
            res = None
        if res is None:
            sources[name] = "sample"
            return sample_fn()
        sources[name] = "live"
        return res

    trend = run("datalab", lambda: fetch_datalab(cfg, start, end), lambda: sample_trend(cfg, start, end))
    volume = run("keyword_tool", lambda: fetch_keyword_tool(cfg), lambda: sample_volume(cfg))
    comp = run("kakao_local", lambda: fetch_competitors(cfg), lambda: sample_competitors(cfg))
    visitors = run("tour_api", lambda: fetch_visitors(start, end), lambda: sample_visitors(cfg, start, end))
    news = run("naver_news", lambda: fetch_news_log(cfg), lambda: sample_news(cfg))

    save("trend_daily.json", trend)
    save("volume_monthly.json", volume)
    save("competitors.json", comp)
    save("visitors.json", visitors)
    save("news_log.json", news)
    save("weekly_change.json", weekly_change(trend))
    save("compare.json", compare(trend))
    save("monthly.json", monthly_table(trend))
    save("events.json", detect_spikes(trend, cfg["spike_rule"], news, cfg["broadcast_log"]))
    save("opportunity.json", opportunity(volume, comp, cfg))
    save("meta.json", {
        "project": cfg["project"], "version": cfg["version"],
        "updated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        "range": [start.isoformat(), end.isoformat()],
        "sources": sources,
        # 키 값은 절대 기록하지 않음 — 등록돼 있는지(길이>0)만 표시
        "keys_present": {k: bool(os.getenv(k, "").strip()) for k in (
            "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET", "NAVER_AD_API_KEY", "NAVER_AD_SECRET",
            "NAVER_AD_CUSTOMER_ID", "KAKAO_REST_KEY", "DATA_GO_KR_KEY")},
        "errors": ERRORS,
        "mode": "live" if all(v == "live" for v in sources.values()) else "partial" if any(v == "live" for v in sources.values()) else "sample",
        "store": cfg["store"], "groups": cfg["groups"], "broadcast_log": cfg["broadcast_log"],
    })
    log("완료: " + json.dumps(sources, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
강화 시그널 수집기 (v0.2)

매일 1회 실행되어 data/ 폴더에 JSON을 만든다. 대시보드(index.html)는 이 JSON만 읽는다.

데이터 출처
  1) 네이버 데이터랩 검색어 트렌드  → 키워드별 일별 상대 검색량 (0~100)
  2) 네이버 검색광고 키워드도구     → 월간 절대 검색량(PC/모바일) + 연관 키워드 발굴
  4) 한국관광공사 관광빅데이터       → 강화군 일별 방문자 수
  5) 네이버 뉴스 검색               → 급등일 설명용 뉴스 로그 (매일 쌓아 둠)

환경변수(API 키)가 하나도 없으면 샘플 모드로 동작한다. 일부만 있으면 있는 것만 실제 데이터를 쓴다.

환경변수
  NAVER_CLIENT_ID, NAVER_CLIENT_SECRET          (네이버 개발자센터 — 데이터랩·뉴스)
  NAVER_AD_API_KEY, NAVER_AD_SECRET, NAVER_AD_CUSTOMER_ID   (네이버 검색광고)
  DATA_GO_KR_KEY                                (공공데이터포털 — 한국관광공사)
"""

import re
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
PLACE_RE = re.compile(r"강화(도|군|읍|섬|산|\s?(화도|길상|선두|동막|낙조|추젓|젓국|꽃게|순무|인삼|평화|나들길|외포|후포|어시장|풍물))|전등사|석모도|교동도|마니산|보문사")
TV_RE = re.compile(r"생생정보|오늘\s?N|생활의\s?달인|6시\s?내\s?고향|백반기행|맛있는\s?녀석들|줄서는\s?식당|전지적\s?참견|나혼자|1박\s?2일|놀면\s?뭐하니|VJ특공대|생방송\s?투데이|2TV\s?저녁|저녁\s?생생|토요일\s?저녁|신상출시|수요미식회|맛있는\s?이야기|고향\s?민국|한국인의\s?밥상|EBS|KBS|MBC|SBS|tvN|JTBC|채널A|MBN|TV조선")
for _k in ("NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET", "NAVER_AD_API_KEY", "NAVER_AD_SECRET",
           "NAVER_AD_CUSTOMER_ID", "DATA_GO_KR_KEY"):
    if _k in os.environ:
        os.environ[_k] = os.environ[_k].strip().strip('"').strip("'").strip()
ERRORS = {}  # 출처별 실패 메시지(키 값 미포함)
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
DAYS = 485  # 120일 추이 + 364일 전 겹쳐보기까지 커버 (데이터랩은 2016년부터 제공)

GANGHWA_AREA_CD = "28"      # 인천광역시
GANGHWA_SIGNGU_CD = "28710"  # 강화군


def log(msg):
    print(f"[collect] {msg}", flush=True)


def load_config():
    with open(os.path.join(ROOT, "keywords.json"), encoding="utf-8") as f:
        return json.load(f)


def trim_empty_tail(obj):
    """데이터랩은 가장 최근 하루가 아직 집계 전이면 모든 키워드를 0으로 돌려줌 → '데이터 없음'이므로 잘라냄"""
    if not isinstance(obj, dict) or not obj.get("dates") or not isinstance(obj.get("series"), dict):
        return obj
    ser = obj["series"]
    n = len(obj["dates"])
    while n > 0 and all((v[n - 1] if n - 1 < len(v) else None) in (0, 0.0, None) for v in ser.values()):
        n -= 1
    if n < len(obj["dates"]):
        obj = dict(obj)
        obj["dates"] = obj["dates"][:n]
        obj["series"] = {k: v[:n] for k, v in ser.items()}
    return obj


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

    headers = {"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": csec, "X-Naver-Client-Id": cid, "X-Naver-Client-Secret": csec}
    url = "https://naverapihub.apigw.ntruss.com/search-trend/v1/search"
    series = {}
    anchor_ref = None
    for i in range(0, len(others), 4):  # 앵커 1 + 4개 = 5그룹
        chunk = others[i:i + 4]
        body = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "timeUnit": "date",
            "keywordGroups": [{"groupName": anchor, "keywords": [anchor]}]
            + [{"groupName": k, "keywords": cfg.get("aliases", {}).get(k, [k])[:20]} for k in chunk],
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
            series[k] = {d: round(v * scale, 5) for d, v in got.get(k, {}).items()}
        time.sleep(0.3)
    dates = ymd_list(start, end)
    return {
        "dates": dates,
        "series": {k: [series.get(k, {}).get(d, 0) for d in dates] for k in all_kw},
        "note": "네이버 데이터랩 상대 검색량. 앵커 키워드 기준으로 한 척도로 맞춤 (최댓값 100은 앵커 기준).",
    }


def fetch_discovery_daily(volume, days=70, top_n=60):
    """
    키워드 발굴용: 검색광고 상위 키워드(강화 관련 top_n개)의 최근 days일 일별 검색 추정 횟수.
    데이터랩은 한 호출에 5개까지 → 5개씩 묶어 호출. 각 키워드는 자기 자신의 '한 달 실제 검색 횟수'로 환산하므로
    호출 간 척도 차이는 결과에 영향 없음.
      일별 추정 횟수 = 데이터랩 일별 상대값 × (검색광고 한 달 횟수 ÷ 최근 30일 상대값 합)
    """
    cid, csec = os.getenv("NAVER_CLIENT_ID"), os.getenv("NAVER_CLIENT_SECRET")
    if not (cid and csec) or not volume:
        return None
    rows = [r for r in volume.get("rows", []) if r.get("total", 0) >= 100][:top_n]
    end = datetime.now(KST).date() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    dates = ymd_list(start, end)
    # 메인 데이터랩과 같은 방식(네이버 클라우드 API 게이트웨이)
    headers = {"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": csec, "X-Naver-Client-Id": cid, "X-Naver-Client-Secret": csec}
    series, errs = {}, []
    anchor_vol = next((r["total"] for r in rows if r["keyword"].replace(" ", "") == "강화도맛집"), 0)
    rows = [r for r in rows if r["keyword"].replace(" ", "") != "강화도맛집"]
    for i in range(0, len(rows), 4):
        chunk = rows[i:i + 4]
        body = {"startDate": start.isoformat(), "endDate": end.isoformat(), "timeUnit": "date",
                "keywordGroups": [{"groupName": "강화도 맛집", "keywords": ["강화도 맛집"]}]
                + [{"groupName": r["keyword"], "keywords": [r["keyword"]]} for r in chunk]}
        res, last = None, None
        for attempt in range(3):
            try:
                res = http_json("https://naverapihub.apigw.ntruss.com/search-trend/v1/search", headers, "POST", body)
                break
            except Exception as e:
                last = e
                body_txt = ""
                if hasattr(e, "read"):
                    try:
                        body_txt = e.read().decode("utf-8", "ignore")[:120]
                    except Exception:
                        pass
                errs.append(f"묶음{i//4+1} 시도{attempt+1}: {type(e).__name__} {str(e)[:40]} {body_txt}")
                time.sleep(1.5 * (attempt + 1))
        if res is None:
            continue
        for g in res.get("results", []):
            name = g["title"]
            if name == "강화도 맛집":
                name = "강화도맛집"
                if name in series:
                    continue
            m = {d["period"]: d["ratio"] for d in g.get("data", [])}
            rel = [m.get(d, 0) for d in dates]
            rel30 = sum(rel[-30:])
            vol = anchor_vol if name == "강화도맛집" else next((r["total"] for r in chunk if r["keyword"] == name), 0)
            if rel30 > 0 and vol:
                sc = vol / rel30
                series[name] = [round(x * sc, 1) for x in rel]
        time.sleep(0.6)
    if errs:
        ERRORS["discovery_chunks"] = errs[:8]
    if not series:
        raise RuntimeError("발굴 키워드 데이터랩 응답 없음")
    return {"dates": dates, "series": series, "note": "검색광고 상위 키워드의 일별 추정 검색 횟수(회/일)", "live": True}


def sample_discovery(volume, days=70):
    rnd = random.Random(9)
    end = datetime.now(KST).date() - timedelta(days=1)
    dates = ymd_list(end - timedelta(days=days - 1), end)
    series = {}
    for r in (volume or {}).get("rows", [])[:60]:
        base = r["total"] / 30
        series[r["keyword"]] = [round(base * (1.4 if date.fromisoformat(d).weekday() >= 5 else 0.85) * rnd.uniform(.7, 1.3), 1) for d in dates]
    return {"dates": dates, "series": series, "note": "⚠ 샘플", "live": False}


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
    aliases = {k.replace(" ", ""): list(dict.fromkeys(a.replace(" ", "") for a in v)) for k, v in cfg.get("aliases", {}).items()}
    alias_all = {a for v in aliases.values() for a in v}
    tracked |= alias_all
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
            under10 = "<" in str(it.get("monthlyPcQcCnt")) + str(it.get("monthlyMobileQcCnt"))
            rows[k] = {
                "keyword": k,
                "pc": pc,
                "mobile": mo,
                "total": pc + mo,
                "comp": it.get("compIdx", ""),
                "tracked": k.replace(" ", "") in tracked,
                "under10": under10 and pc + mo < 20,
            }
        time.sleep(0.5)
    out = sorted(rows.values(), key=lambda r: -r["total"])
    # 강화 관련 키워드만 남김 (추적 키워드는 순위와 무관하게 항상 포함)
    out = [r for r in out if "강화" in r["keyword"] or "전등사" in r["keyword"] or "석모도" in r["keyword"] or r["tracked"]]
    keep = out[:1000] + [r for r in out[1000:] if r["tracked"]]
    seen = {r["keyword"].replace(" ", "") for r in keep}
    for t in sorted(tracked):  # 키워드도구가 아예 돌려주지 않은 추적 키워드 = 월 10회 미만으로 간주
        if t not in seen:
            keep.append({"keyword": t, "pc": 0, "mobile": 0, "total": 0, "comp": "", "tracked": True, "under10": True})
    # 별칭(띄어쓰기·지역명 붙인 표기)을 대표 키워드 하나로 합산
    byk = {r["keyword"].replace(" ", ""): r for r in keep}
    for canon, al in aliases.items():
        got = [byk[a] for a in al if a in byk]
        tot = sum(r["total"] for r in got)
        row = {"keyword": canon, "pc": sum(r["pc"] for r in got), "mobile": sum(r["mobile"] for r in got), "total": tot,
               "comp": "", "tracked": True, "under10": all(r.get("under10") for r in got) if got else True, "aliases": al}
        keep = [r for r in keep if r["keyword"].replace(" ", "") not in set(al) | {canon}] + [row]
    return {"rows": keep, "note": "네이버 검색광고 키워드도구. 최근 한 달 PC/모바일 검색수 합계."}


# ---------------------------------------------------------------------------
# 4) 한국관광공사 관광빅데이터 — 강화군 일별 방문자
# ---------------------------------------------------------------------------
def fetch_visitors(start, end):
    key = os.getenv("DATA_GO_KR_KEY")
    if not key:
        return None
    base = "https://apis.data.go.kr/B551011/DataLabService/locgoRegnVisitrDDList"
    by_day, raw_head = {}, ""
    # 이전에 받은 실데이터가 있으면 이어서(최근 14일만 다시) 받음 — 공공데이터 일일 호출 한도 절약
    prev = load_existing("visitors.json", {})
    if prev.get("live") and prev.get("dates"):
        for d, a, b, c in zip(prev["dates"], prev["visitor"], prev["local"], prev["foreign"]):
            if d >= start.isoformat():
                by_day[d] = {"visitor": a, "local": b, "foreign": c}
        have = set(prev["dates"])
        recent_cut = (end - timedelta(days=14)).isoformat()
    # 최근 날짜부터 거꾸로 받음 — 시간 제한에 걸려도 브리핑에 필요한 최근 데이터는 먼저 확보
    chunks, c0 = [], start
    while c0 <= end:
        chunks.append((c0, min(end, c0 + timedelta(days=29))))
        c0 += timedelta(days=30)
    chunks.reverse()
    ly_center = datetime.now(KST).date() - timedelta(days=364)
    chunks.sort(key=lambda c: (0 if abs((c[0] - ly_center).days) <= 75 else 1, -c[0].toordinal()))
    seen_days = set()
    calls, chunk_errs = 0, []
    t_start = time.time()
    BUDGET = 660  # 관광공사는 해외 접속이 느림 — 11분 넘으면 받은 만큼만 쓰고 다음 실행에서 이어받음
    page_size = [5000]
    # 이 API에는 지역 필터가 없음 — 전국 시군구(하루 약 740행)를 받아 강화군만 골라냄.
    # 첫 실행(약 16개월 백필)은 300회 안팎 호출, 이후 매일은 10회 안팎.
    if prev.get("live") and prev.get("dates"):
        chunks = [(a, b) for a, b in chunks
                  if b.isoformat() >= recent_cut or any((a + timedelta(days=i)).isoformat() not in have for i in range((b - a).days + 1))]
    for s0, e0 in chunks:
        if calls >= 450 or time.time() - t_start >= BUDGET:
            break
        page, total = 1, None
        while calls < 450 and time.time() - t_start < BUDGET:
            params = {
                "serviceKey": key, "numOfRows": page_size[0], "pageNo": page, "MobileOS": "ETC", "MobileApp": "ganghwasignal",
                "startYmd": s0.strftime("%Y%m%d"), "endYmd": e0.strftime("%Y%m%d"), "_type": "json",
            }
            calls += 1
            req = urllib.request.Request(base + "?" + urllib.parse.urlencode(params))
            txt, last_err = None, None
            for attempt in range(2):  # 공공데이터포털은 해외(GitHub 서버)에서 접속이 느릴 때가 있어 재시도
                try:
                    with urllib.request.urlopen(req, timeout=75) as r:
                        txt = r.read().decode("utf-8", "ignore")
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(3)
            if txt is None:
                chunk_errs.append(f"{s0}~{e0}: 접속 실패 {type(last_err).__name__}: {str(last_err)[:80]}")
                if len(chunk_errs) >= 2 and not by_day:
                    raise RuntimeError("관광공사 서버 접속 반복 실패: " + str(last_err)[:120])
                break
            if not raw_head:
                raw_head = txt[:300]
            try:
                res = json.loads(txt)
            except Exception:
                chunk_errs.append(f"{s0}~{e0}: JSON 아님 {txt[:120]}")
                break
            head = res.get("response", {}).get("header", {}) or {}
            code = str(head.get("resultCode", res.get("resultCode", "")))
            if code not in ("0000", "00", "0", "") and page_size[0] > 1000:
                page_size[0] = 1000  # 큰 페이지가 거부되면 1000으로 낮춰 같은 구간 재시도
                continue
            if code not in ("0000", "00", "0", ""):
                chunk_errs.append(f"{s0}~{e0}: {code} {head.get('resultMsg', res.get('resultMsg', ''))}")
                break
            body = res.get("response", {}).get("body", {})
            items = body.get("items") or {}
            items = items.get("item", []) if isinstance(items, dict) else []
            if isinstance(items, dict):
                items = [items]
            for it in items:
                code_ = str(it.get("signguCode", it.get("signguCd", "")))
                name = str(it.get("signguNm", ""))
                if code_ != GANGHWA_SIGNGU_CD and "강화" not in name:
                    continue
                d = str(it.get("baseYmd", ""))
                if len(d) != 8:
                    continue
                iso = f"{d[:4]}-{d[4:6]}-{d[6:]}"
                div = str(it.get("touDivCd", ""))  # 1 현지인, 2 외지인, 3 외국인
                if iso not in seen_days:  # 이어받기 구간은 새로 덮어씀
                    by_day[iso] = {"local": 0, "visitor": 0, "foreign": 0}
                    seen_days.add(iso)
                n = float(it.get("touNum", 0) or 0)
                by_day[iso]["local" if div == "1" else "foreign" if div == "3" else "visitor"] += n
            total = int(body.get("totalCount", 0) or 0)
            if page * page_size[0] >= total or not items:
                break
            page += 1
            time.sleep(0.1)
    if chunk_errs:
        ERRORS["tour_api_chunks"] = chunk_errs[:6]
    ERRORS["tour_api_calls"] = calls
    if not by_day:
        raise RuntimeError("관광공사 응답에 강화군 데이터 없음. 응답 앞부분: " + raw_head[:250])
    dates = sorted(by_day)
    return {
        "dates": dates,
        "visitor": [round(by_day[d]["visitor"]) for d in dates],
        "local": [round(by_day[d]["local"]) for d in dates],
        "foreign": [round(by_day[d]["foreign"]) for d in dates],
        "note": "한국관광공사 관광빅데이터(통신사 기반) 강화군 일별 방문자. 원천 데이터가 며칠~몇 주 늦게 올라옴.",
        "live": True,
    }


# ---------------------------------------------------------------------------
# 4b) 날씨 — Open-Meteo (무료, 키 불필요). 강화군 길상면 기준. 과거 500일 + 앞으로 7일
# ---------------------------------------------------------------------------
GANGHWA_LAT, GANGHWA_LON = 37.63, 126.48  # 길상면 선두리 부근


def fetch_weather(start, end):
    daily = "weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,windspeed_10m_max"
    out = {}
    # 과거(archive): 어제까지
    q = urllib.parse.urlencode({"latitude": GANGHWA_LAT, "longitude": GANGHWA_LON, "start_date": start.isoformat(),
                                "end_date": (datetime.now(KST).date() - timedelta(days=2)).isoformat(),
                                "daily": "weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum", "timezone": "Asia/Seoul"})
    try:
        res = http_json("https://archive-api.open-meteo.com/v1/archive?" + q, timeout=60)
        d = res.get("daily", {})
        for i, day in enumerate(d.get("time", [])):
            out[day] = {"code": d["weathercode"][i], "tmax": d["temperature_2m_max"][i], "tmin": d["temperature_2m_min"][i],
                        "rain": d["precipitation_sum"][i], "pop": None, "src": "past"}
    except Exception as e:
        ERRORS["weather_archive"] = f"{type(e).__name__}: {str(e)[:120]}"
    # 예보: 최근 7일(과거 보정) + 앞 7일
    q = urllib.parse.urlencode({"latitude": GANGHWA_LAT, "longitude": GANGHWA_LON, "daily": daily, "timezone": "Asia/Seoul",
                                "past_days": 7, "forecast_days": 8})
    res = None
    for attempt in range(4):  # Open-Meteo가 가끔 503(과부하) → 재시도
        try:
            res = http_json("https://api.open-meteo.com/v1/forecast?" + q, timeout=60)
            break
        except Exception:
            if attempt == 3:
                raise
            time.sleep(8 * (attempt + 1))
    d = res.get("daily", {})
    today = datetime.now(KST).date().isoformat()
    for i, day in enumerate(d.get("time", [])):
        out[day] = {"code": d["weathercode"][i], "tmax": d["temperature_2m_max"][i], "tmin": d["temperature_2m_min"][i],
                    "rain": d["precipitation_sum"][i], "pop": (d.get("precipitation_probability_max") or [None] * 99)[i],
                    "wind": (d.get("windspeed_10m_max") or [None] * 99)[i], "src": "forecast" if day >= today else "past"}
    if not out:
        raise RuntimeError("날씨 응답 비어 있음")
    return {"days": out, "note": "Open-Meteo 일별 날씨 (강화군 길상면 기준). 과거는 관측 재분석, 앞 7일은 예보.", "live": True}


def sample_weather(start, end):
    rnd = random.Random(5)
    out = {}
    d = start
    today = datetime.now(KST).date()
    while d <= today + timedelta(days=7):
        yd = d.timetuple().tm_yday
        tmax = 4 + 24 * math.exp(-((yd - 210) / 95) ** 2) + rnd.uniform(-3, 3)
        rain = rnd.choice([0, 0, 0, 0, 0.5, 3, 12, 30]) * (2 if 180 < yd < 240 else 1)
        code = 0 if rain == 0 and rnd.random() < .6 else 2 if rain == 0 else 61 if rain < 10 else 63
        out[d.isoformat()] = {"code": code, "tmax": round(tmax, 1), "tmin": round(tmax - 8, 1), "rain": rain,
                              "pop": None if d < today else rnd.choice([10, 20, 30, 60, 80]), "src": "past" if d < today else "forecast"}
        d += timedelta(days=1)
    return {"days": out, "note": "⚠ 샘플 날씨", "live": False}


# ---------------------------------------------------------------------------
# 5) 네이버 뉴스 — 매일 로그 적재
# ---------------------------------------------------------------------------
def fetch_news_log(cfg):
    cid, csec = os.getenv("NAVER_CLIENT_ID"), os.getenv("NAVER_CLIENT_SECRET")
    if not (cid and csec):
        return None
    existing = load_existing("news_log.json", {"items": []})
    existing["items"] = [i for i in existing["items"] if not str(i.get("link", "")).startswith("https://example.com/")]  # 샘플 제거
    headers = {"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": csec, "X-Naver-Client-Id": cid, "X-Naver-Client-Secret": csec}
    seen = {it["link"] for it in existing["items"]}
    for q in cfg["news_queries"]:
        params = urllib.parse.urlencode({"query": q, "display": 30, "sort": "date"})
        res = http_json("https://naverapihub.apigw.ntruss.com/search/v1/news?" + params, headers)
        for it in res.get("items", []):
            if it["link"] in seen:
                continue
            try:
                pub = datetime.strptime(it["pubDate"], "%a, %d %b %Y %H:%M:%S %z").astimezone(KST).date().isoformat()
            except Exception:
                pub = datetime.now(KST).date().isoformat()
            title = it["title"].replace("<b>", "").replace("</b>", "").replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            if not PLACE_RE.search(title):
                continue
            tv = TV_RE.search(title)
            existing["items"].append({"date": pub, "title": title, "link": it["link"], "query": q, "tv": tv.group(0) if tv else None})
            seen.add(it["link"])
        time.sleep(0.2)
    cutoff = (datetime.now(KST).date() - timedelta(days=400)).isoformat()
    existing["items"] = sorted([i for i in existing["items"] if i["date"] >= cutoff and PLACE_RE.search(i["title"])], key=lambda i: i["date"], reverse=True)
    for i in existing["items"]:
        m = TV_RE.search(i["title"])
        i["tv"] = m.group(0) if m else None
    return existing


UPCOMING_RE = re.compile(r"예정|예고|방송된다|방영된다|전파를\s?탄다|공개된다|출연한다|찾아간다|소개된다|오늘\s?\(|오늘\s?밤|오늘\s?저녁|내일|이번\s?주")
AIR_RE = re.compile(r"(?:오는\s?)?(\d{1,2})일\s*(?:\([월화수목금토일]\)\s*)?(?:오전|오후|저녁|밤)?\s*(?:\d{1,2}시\s*(?:\d{1,2}분)?\s*)?(?:에\s*)?(?:방송|방영|전파|공개)")


def _clean(t):
    return re.sub(r"<[^>]+>", "", t or "").replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&apos;", "'")


def _air_date(text, pub):
    """기사 본문 요약에서 방송 날짜를 추정. 못 찾으면 None"""
    if re.search(r"오늘\s?(?:\(|밤|저녁|오후)", text):
        return pub
    if "내일" in text:
        return pub + timedelta(days=1)
    m = AIR_RE.search(text) or re.search(r"오늘\s?\((\d{1,2})일\)", text)
    if not m:
        return None
    d = int(m.group(1))
    if not 1 <= d <= 31:
        return None
    y, mo = pub.year, pub.month
    if d < pub.day - 3:  # 이미 지난 날짜면 다음 달
        mo += 1
        if mo == 13:
            y, mo = y + 1, 1
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def fetch_upcoming(cfg):
    """방송 예고 기사(최근 10일)에서 강화 관련 예정 방송을 찾음 — 공식 편성표 API가 없어 기사 기반"""
    cid, csec = os.getenv("NAVER_CLIENT_ID"), os.getenv("NAVER_CLIENT_SECRET")
    if not (cid and csec):
        return []
    headers = {"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": csec, "X-Naver-Client-Id": cid, "X-Naver-Client-Secret": csec}
    today = datetime.now(KST).date()
    out, seen = [], set()
    for q in cfg.get("broadcast_queries", []):
        params = urllib.parse.urlencode({"query": q, "display": 50, "sort": "date"})
        try:
            res = http_json("https://naverapihub.apigw.ntruss.com/search/v1/news?" + params, headers)
        except Exception as e:
            log(f"방송 예정 검색 실패 {q}: {e}")
            continue
        for it in res.get("items", []):
            try:
                pub = datetime.strptime(it["pubDate"], "%a, %d %b %Y %H:%M:%S %z").astimezone(KST).date()
            except Exception:
                continue
            if (today - pub).days > 10:
                continue
            title, desc = _clean(it["title"]), _clean(it.get("description"))
            text = title + " " + desc
            tv = TV_RE.search(text)
            if not (PLACE_RE.search(text) and tv and UPCOMING_RE.search(text)):
                continue
            air = _air_date(text, pub)
            if air and air < today - timedelta(days=1):
                continue
            key = (tv.group(0), air.isoformat() if air else title[:20])
            if key in seen or it["link"] in seen:
                continue
            seen.add(key); seen.add(it["link"])
            out.append({"air": air.isoformat() if air else None, "pub": pub.isoformat(), "program": tv.group(0), "title": title,
                        "link": it.get("originallink") or it["link"], "ours": "만선" in text})
        time.sleep(0.2)
    return sorted(out, key=lambda x: (x["air"] or "9999", x["pub"]))


# ---------------------------------------------------------------------------
# 분석: 주간 변화, 급등일, 기회 키워드
# ---------------------------------------------------------------------------

def to_counts(trend, volume):
    """
    데이터랩 상대지수 → 추정 검색 횟수(회/일).
    검색광고 키워드도구의 '최근 한 달 실제 검색 횟수'(PC+모바일)를 데이터랩 최근 30일 모양에 맞춰 날짜별로 나눔.
      배율 = 한 달 실제 횟수 ÷ 최근 30일 상대지수 합   →   날짜별 추정 횟수 = 상대지수 × 배율
    검색광고에 없는 키워드는 '강화도 맛집' 배율을 그대로 씀(같은 척도로 맞춰 둔 값이라 비례 관계 유지).
    """
    if not volume or trend.get("unit") == "count":
        return trend
    vol = {r["keyword"].replace(" ", ""): r["total"] for r in volume.get("rows", [])}
    n = len(trend["dates"])
    scales, src = {}, {}
    for k, v in trend["series"].items():
        rel30 = sum(x for x in v[n - 30:n] if x)
        mv = vol.get(k.replace(" ", ""))
        if mv and rel30 > 0:
            scales[k] = mv / rel30
            src[k] = "direct"
    anchor_scale = scales.get("강화도 맛집") or (sorted(scales.values())[len(scales) // 2] if scales else None)
    if anchor_scale is None:
        return trend
    out = {}
    for k, v in trend["series"].items():
        sc = scales.get(k, anchor_scale)
        src.setdefault(k, "anchor")
        out[k] = [round(x * sc, 1) if x is not None else None for x in v]
    trend = dict(trend)
    trend["series"] = out
    trend["unit"] = "count"
    trend["scale_source"] = src
    trend["note"] = "네이버 검색 추정 횟수(회/일) = 데이터랩 일별 등락 × 검색광고 최근 한 달 실제 검색 횟수로 환산. PC+모바일 합계."
    return trend


def _avg(vals, a, b):
    seg = vals[a:b]
    return (sum(seg) / len(seg)) if seg else 0.0


def _pct(cur, base):
    if base > 0:
        return round((cur - base) / base * 100, 1)
    return None


def compare(trend, volume=None):
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
        tiny = _avg(v, max(0, n - 60), n) < (3 if trend.get("unit") == "count" else 0.05)  # 하루 3회 미만 = 검색량 너무 적음
        if volume:
            vm = {r["keyword"].replace(" ", ""): r["total"] for r in volume.get("rows", [])}
            mv = vm.get(k.replace(" ", ""))
            if mv is not None and mv < 200:
                tiny = True
        if tiny:
            out.append({"keyword": k, "tiny": True,
                        "week": {"cur": round(w_cur, 3), "prev": round(w_prev, 3), "pct": None},
                        "month": {"cur": round(m_cur, 3), "prev": round(m_prev, 3), "pct": None},
                        "yoy": {"cur": round(m_cur, 3), "prev": round(y_prev, 3), "pct": None}})
            continue
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


def menu_demand(volume, cfg):
    """메뉴별 월 검색 수요(네이버 검색광고 절대값). '강화도+메뉴' 계열 키워드를 합산."""
    if not volume:
        return []
    vol = {r["keyword"].replace(" ", ""): r for r in volume["rows"]}
    out = []
    for q in cfg["menu_queries"]:
        cands = [f"강화도{q}", f"강화{q}", f"강화도{q}맛집", f"강화{q}맛집"]
        rows = [vol[c] for c in cands if c in vol]
        total = sum(r["total"] for r in rows)
        out.append({"menu": q, "monthly_search": total, "keywords": [r["keyword"] for r in rows],
                    "mine": q in cfg["store"]["menus"], "under10": total == 0 and any(r.get("under10") for r in rows)})
    return sorted(out, key=lambda r: -r["monthly_search"])


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
            body = ""
            if hasattr(e, "read"):
                try:
                    body = e.read().decode("utf-8", "ignore")[:200]
                except Exception:
                    pass
            ERRORS[name] = f"{type(e).__name__}: {str(e)[:120]} {body}".strip()
            res = None
        if res is None:
            # 실패하면 샘플 대신 직전에 받은 실데이터를 그대로 사용 (가짜 수치가 예측에 섞이지 않게)
            prev_file = {"keyword_tool": "volume_monthly.json", "datalab": "trend_daily_raw.json", "discovery": "discovery_daily.json",
                         "tour_api": "visitors.json", "naver_news": "news_log.json", "weather": "weather.json"}.get(name)
            prev_meta = load_existing("meta.json", {}).get("sources", {})
            if prev_file and prev_meta.get(name) in ("live", "stale") and os.path.exists(os.path.join(DATA_DIR, prev_file)):
                sources[name] = "stale"
                log(f"{name}: 직전 실데이터 재사용")
                return load_existing(prev_file, None)
            sources[name] = "sample"
            return sample_fn()
        sources[name] = "live"
        return res

    volume = run("keyword_tool", lambda: fetch_keyword_tool(cfg), lambda: sample_volume(cfg))
    trend = run("datalab", lambda: fetch_datalab(cfg, start, end), lambda: sample_trend(cfg, start, end))
    trend = trim_empty_tail(trend)
    disc = trim_empty_tail(run("discovery", lambda: fetch_discovery_daily(volume), lambda: sample_discovery(volume)))
    save("discovery_daily.json", disc)
    # 방문자는 월별·요일별 연간 비교를 위해 2024-01-01부터 보관(매일 빈 구간을 조금씩 채움)
    v_start = min(start, date(2024, 1, 1))
    visitors = run("tour_api", lambda: fetch_visitors(v_start, end), lambda: sample_visitors(cfg, start, end))
    news = run("naver_news", lambda: fetch_news_log(cfg), lambda: sample_news(cfg))
    try:
        news = dict(news or {})
        news["upcoming"] = fetch_upcoming(cfg)
        news["upcoming_checked"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    except Exception as e:
        ERRORS["upcoming"] = f"{type(e).__name__}: {str(e)[:120]}"
    weather = run("weather", lambda: fetch_weather(start, end), lambda: sample_weather(start, end))
    save("weather.json", weather)

    if sources.get("datalab") == "live":
        save("trend_daily_raw.json", trend)
    trend = to_counts(trend, volume)
    save("trend_daily.json", trend)
    save("volume_monthly.json", volume)
    save("visitors.json", visitors)
    save("news_log.json", news)
    save("weekly_change.json", weekly_change(trend))
    save("compare.json", compare(trend, volume))
    save("monthly.json", monthly_table(trend))
    _ev = detect_spikes(trend, cfg["spike_rule"], news, cfg["broadcast_log"])
    for _x in _ev:
        _x["holiday"] = cfg.get("holidays", {}).get(_x["date"])
    save("events.json", _ev)
    save("menu_demand.json", menu_demand(volume, cfg))
    try:
        import brief
        save("brief.json", brief.build(cfg, visitors, compare(trend, volume), news, weather))
    except Exception as e:
        log(f"brief 실패: {e}")
        ERRORS["brief"] = f"{type(e).__name__}: {str(e)[:200]}"
    save("meta.json", {
        "project": cfg["project"], "version": cfg["version"],
        "updated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        "range": [start.isoformat(), end.isoformat()],
        "sources": sources,
        # 키 값은 절대 기록하지 않음 — 등록돼 있는지(길이>0)만 표시
        "keys_present": {k: bool(os.getenv(k, "").strip()) for k in (
            "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET", "NAVER_AD_API_KEY", "NAVER_AD_SECRET",
            "NAVER_AD_CUSTOMER_ID", "DATA_GO_KR_KEY")},
        "key_lengths": {k: len(os.getenv(k, "")) for k in (
            "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET", "NAVER_AD_API_KEY", "NAVER_AD_SECRET",
            "NAVER_AD_CUSTOMER_ID", "DATA_GO_KR_KEY")},
        "errors": ERRORS,
        "mode": "live" if all(v in ("live", "stale") for v in sources.values()) else "partial" if any(v == "live" for v in sources.values()) else "sample",
        "store": cfg["store"], "groups": cfg["groups"], "broadcast_log": cfg["broadcast_log"], "holidays": cfg.get("holidays", {}),
    })
    log("완료: " + json.dumps(sources, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())

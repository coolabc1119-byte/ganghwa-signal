# -*- coding: utf-8 -*-
"""
주간 준비 브리핑 — "이번 주 재료를 얼마나 받고, 사람을 며칠 더 부를지"

입력: data/visitors.json(강화군 외지인 방문자), data/compare.json(키워드 비교), keywords.json(가게·재료·연휴·방송 기록),
      (선택) data/store_sales.csv — 날짜,매출 또는 날짜,테이블수. 있으면 "외지인 1만 명당 우리 가게 손님" 비율을 계산해 숫자 예측에 씀.
출력: data/brief.json

예측 방식(단순하고 설명 가능하게):
  1) 최근 8주 요일별 외지인 방문자 중앙값 = 요일 기본값
  2) 추세 보정 = 최근 4주 평균 ÷ 그 전 4주 평균 (최대 ±25%)
  3) 연휴 = 토요일 기본값, 연휴 전날 = 금요일 기본값
  4) 방송 기록이 최근 10일 안에 있으면 첫 주말 ×1.3
  5) 등급 = 예상치 ÷ 전체 중앙값 → 한산(<0.85) / 평소 / 많음(>1.15) / 매우 많음(>1.5)
"""
import csv
import json
import os
import statistics
from datetime import date, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
WD = ["월", "화", "수", "목", "금", "토", "일"]


def _median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else 0


def _in_window(d, win):
    if not win:
        return False
    (m1, d1), (m2, d2) = win
    a, b = (d.month, d.day), (m1, d1)
    c = (m2, d2)
    return b <= a <= c if b <= c else (a >= b or a <= c)


def load_sales(path):
    """날짜,값 CSV. 헤더 유무 무관. 값은 매출이든 테이블 수든 비율 계산에만 쓰임."""
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            if len(row) < 2:
                continue
            try:
                d = datetime.strptime(row[0].strip()[:10].replace(".", "-").replace("/", "-"), "%Y-%m-%d").date().isoformat()
                out[d] = float(str(row[1]).replace(",", ""))
            except Exception:
                continue
    return out


def build(cfg, visitors, compare, today=None):
    today = today or datetime.now(KST).date()
    rules = cfg.get("brief_rules", {})
    lb = rules.get("lookback_weeks", 8)
    lv = rules.get("levels", {"quiet": 0.85, "normal": 1.15, "busy": 1.5})
    holidays = cfg.get("holidays", {})
    bc = cfg.get("broadcast_log", [])
    sample = not visitors.get("live")

    dates, vis = visitors.get("dates", []), visitors.get("visitor", [])
    by = dict(zip(dates, vis))
    # 연휴·연휴 전날은 기준값 계산에서 제외(추석 같은 연휴가 평일 기준을 부풀리지 않게)
    hol = set(holidays)
    hol |= {(date.fromisoformat(h) - timedelta(days=1)).isoformat() for h in holidays}
    recent = [(date.fromisoformat(d), v) for d, v in zip(dates, vis) if d not in hol][-7 * lb:]
    by_wd = {i: _median([v for d, v in recent if d.weekday() == i]) for i in range(7)}
    overall = _median([v for _, v in recent]) or 1
    last4 = [v for d, v in recent[-28:]]
    prev4 = [v for d, v in recent[-56:-28]]
    trend = (sum(last4) / len(last4)) / (sum(prev4) / len(prev4)) if last4 and prev4 else 1.0
    trend = max(0.9, min(1.1, trend))  # 한 달 이상 늦게 올라오는 데이터라 추세는 ±10%까지만 반영
    last_data = dates[-1] if dates else None

    # 작년 같은 요일(364일 전) 대비 — 참고용
    def yoy(d):
        a, b = by.get((d - timedelta(days=364)).isoformat()), by.get((d - timedelta(days=371)).isoformat())
        return a or b

    # 가게 매출 비율(선택)
    sales = load_sales(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "store_sales.csv"))
    capture = None
    if sales:
        pairs = [(by[d], s) for d, s in sales.items() if d in by and by[d] > 0]
        if len(pairs) >= 14:
            capture = statistics.median([s / v * 10000 for v, s in pairs])  # 외지인 1만 명당 가게 값

    recent_bc = [b for b in bc if 0 <= (today - date.fromisoformat(b["date"])).days <= rules.get("broadcast_boost_days", 10)]

    days = []
    for i in range(7):
        d = today + timedelta(days=i)
        wd = d.weekday()
        tags = []
        base_wd = wd
        if d.isoformat() in holidays:
            base_wd = 5
            tags.append(holidays[d.isoformat()])
        elif (d + timedelta(days=1)).isoformat() in holidays and wd not in (4, 5):
            base_wd = 4
            tags.append("연휴 전날")
        est = by_wd.get(base_wd, overall) * trend
        if recent_bc and wd in (5, 6):
            est *= rules.get("broadcast_boost", 1.3)
            tags.append("방송 후 첫 주말")
        ratio = est / overall
        vs_wd = (est / by_wd[wd] - 1) * 100 if by_wd.get(wd) else 0  # 평소 이 요일 대비
        level = "quiet" if ratio < lv["quiet"] else "normal" if ratio <= lv["normal"] else "busy" if ratio <= lv["busy"] else "peak"
        days.append({
            "date": d.isoformat(), "wd": WD[wd], "est_visitors": round(est), "ratio": round(ratio, 2), "level": level,
            "yoy_visitors": yoy(d), "tags": tags, "vs_weekday_pct": round(vs_wd),
            "est_store": round(est / 10000 * capture) if capture else None,
        })

    # 재료 신호
    cmp = {r["keyword"]: r for r in compare}
    weekend_levels = [x["level"] for x in days if x["wd"] in ("토", "일") or x["tags"]]
    busy_week = any(l in ("busy", "peak") for l in weekend_levels)
    ingredients = []
    for name, ing in cfg["store"].get("ingredients", {}).items():
        kws = [cmp[k] for k in ing.get("keywords", []) if k in cmp and not cmp[k].get("tiny")]
        wk = [k["week"]["pct"] for k in kws if k["week"]["pct"] is not None]
        mo = [k["month"]["pct"] for k in kws if k["month"]["pct"] is not None]
        wk_pct = sum(wk) / len(wk) if wk else None
        mo_pct = sum(mo) / len(mo) if mo else None
        se = ing.get("season", {})
        reasons, action = [], "keep"
        if _in_window(today, se.get("closed")):
            action = "frozen"
            reasons.append("금어기 — 생물 수급 어려움, 냉동·비축분으로 운영")
        else:
            score = 0
            if _in_window(today, se.get("peak")):
                score += 1; reasons.append("제철(성수기)")
            if wk_pct is not None and wk_pct >= 15:
                score += 1; reasons.append(f"검색 수요 전주 대비 +{wk_pct:.0f}%")
            elif wk_pct is not None and wk_pct <= -15:
                score -= 1; reasons.append(f"검색 수요 전주 대비 {wk_pct:.0f}%")
            if mo_pct is not None and mo_pct >= 20:
                score += 1; reasons.append(f"검색 전월 대비 +{mo_pct:.0f}%")
            elif mo_pct is not None and mo_pct <= -20:
                score -= 1; reasons.append(f"검색 전월 대비 {mo_pct:.0f}% (성수기 끝물)")
            if not kws:
                reasons.append("검색량이 너무 적어 검색 신호 없음 — 꽃게 손님 연동으로 판단")
            if busy_week:
                score += 1; reasons.append("주말·연휴 손님 많음 예상")
            if recent_bc:
                score += 1; reasons.append("방송 효과 기간")
            action = "more" if score >= 2 else "less" if score <= -1 else "keep"
        ingredients.append({"name": name, "menus": ing.get("menus", []), "action": action, "reasons": reasons[:3], "note": ing.get("note", ""),
                            "week_pct": None if wk_pct is None else round(wk_pct, 1), "month_pct": None if mo_pct is None else round(mo_pct, 1)})

    # 인력 신호
    staff = []
    for x in days:
        if x["level"] == "peak":
            staff.append({"date": x["date"], "wd": x["wd"], "advice": "홀 +1, 주방 +1", "why": ", ".join(x["tags"]) or "매우 많음 예상"})
        elif x["level"] == "busy":
            staff.append({"date": x["date"], "wd": x["wd"], "advice": "홀 +1", "why": ", ".join(x["tags"]) or "많음 예상"})

    # 주의 알림 (앞으로 30일)
    alerts = []
    for i in range(30):
        d = today + timedelta(days=i)
        if d.isoformat() in holidays:
            alerts.append({"date": d.isoformat(), "wd": WD[d.weekday()], "text": holidays[d.isoformat()] + " — 토요일 수준 손님 예상"})
    for name, ing in cfg["store"].get("ingredients", {}).items():
        se = ing.get("season", {})
        for key, label in (("closed", "금어기 시작"), ("peak", "제철 시작")):
            w = se.get(key)
            if not w:
                continue
            start = date(today.year, w[0][0], w[0][1])
            if start < today:
                start = date(today.year + 1, w[0][0], w[0][1])
            if 0 <= (start - today).days <= 30:
                alerts.append({"date": start.isoformat(), "wd": WD[start.weekday()], "text": f"{name} {label} ({ing.get('note','')[:40]})"})
    for b in bc:
        bd = date.fromisoformat(b["date"])
        if 0 <= (bd - today).days <= 30:
            alerts.append({"date": b["date"], "wd": WD[bd.weekday()], "text": f"📺 {b['program']} 방송 예정 — 다음 주말 1.3배 준비"})
    alerts.sort(key=lambda a: a["date"])

    # 한 줄 요약
    peak_days = [x for x in days if x["level"] in ("busy", "peak")]
    if peak_days:
        summary = "이번 주 손님 많은 날: " + ", ".join(f"{x['wd']}({x['date'][5:].replace('-', '/')})" for x in peak_days)
    else:
        summary = "이번 주는 평소 수준 — 주말 기본 준비만"
    more = [i["name"] for i in ingredients if i["action"] == "more"]
    if more:
        summary += " · 더 받을 재료: " + ", ".join(more)

    return {
        "generated": today.isoformat(), "sample": sample, "last_visitor_data": last_data,
        "trend_factor": round(trend, 3), "weekday_median": {WD[i]: round(by_wd[i]) for i in range(7)}, "overall_median": round(overall),
        "capture_per_10k": round(capture, 2) if capture else None, "sales_days": len(sales),
        "summary": summary, "days": days, "ingredients": ingredients, "staff": staff, "alerts": alerts[:12],
        "broadcast_recent": recent_bc,
        "note": "방문자 = 한국관광공사(통신사) 강화군 외지인. 요일 중앙값×추세×연휴·방송 보정. 가게 매출 CSV(data/store_sales.csv)를 넣으면 가게 기준 숫자로 바뀜.",
    }


if __name__ == "__main__":
    root = os.path.dirname(os.path.abspath(__file__))
    cfg = json.load(open(os.path.join(root, "keywords.json"), encoding="utf-8"))
    vis = json.load(open(os.path.join(root, "data", "visitors.json"), encoding="utf-8"))
    cmp = json.load(open(os.path.join(root, "data", "compare.json"), encoding="utf-8"))
    out = build(cfg, vis, cmp)
    json.dump(out, open(os.path.join(root, "data", "brief.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps({k: out[k] for k in ("summary", "trend_factor", "weekday_median")}, ensure_ascii=False))
    for d in out["days"]:
        print(d["date"], d["wd"], d["est_visitors"], d["level"], d["tags"])
    for i in out["ingredients"]:
        print(i["name"], i["action"], i["reasons"])
    print(out["staff"]); print(out["alerts"])

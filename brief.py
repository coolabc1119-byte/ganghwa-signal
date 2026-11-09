# -*- coding: utf-8 -*-
"""
주간 준비 브리핑 v2 — 사장님의 세 가지 질문에 답한다.
  1) 이번 주 며칠이 얼마나 바쁠까?           → 날짜별 예상 외지인 + 평소 같은 요일 대비 %
  2) 재료를 평소보다 얼마나 더/덜 받을까?      → 재료별 "평소 대비 배율" + 기준량을 넣으면 kg 환산
  3) 사람을 며칠 더 부를까?                    → 평소 같은 요일 대비 20%↑ 날만 표시
  + 앞으로 4주 전망(작년 같은 주 + 연휴 + 계절)  → 미리 발주·휴무·알바 계획

예측 = 요일 기본값(최근 8주 중앙값, 연휴 제외)
       × 계절(작년 같은 시기 변화, 0.7~1.3)
       × 추세(최근 4주 ÷ 그 전 4주, ±10%)
       × 연휴(연휴=토요일, 전날=금요일)
       × 날씨(비·눈 예보 — 과거 데이터에서 비 오는 날 실제로 얼마나 줄었는지 계산해 적용)
       × 방송(최근 10일 내 강화 맛집 방송 → 첫 주말 1.3배)
"""
import csv
import json
import os
import statistics
from datetime import date, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
WD = ["월", "화", "수", "목", "금", "토", "일"]

WX = {0: "맑음", 1: "맑음", 2: "구름", 3: "흐림", 45: "안개", 48: "안개", 51: "이슬비", 53: "이슬비", 55: "이슬비",
      61: "비", 63: "비", 65: "큰비", 66: "진눈깨비", 67: "진눈깨비", 71: "눈", 73: "눈", 75: "큰눈", 77: "눈",
      80: "소나기", 81: "소나기", 82: "폭우", 85: "눈", 86: "큰눈", 95: "뇌우", 96: "뇌우", 99: "뇌우"}


def _median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else 0


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _in_window(d, win):
    if not win:
        return False
    (m1, d1), (m2, d2) = win
    a, b, c = (d.month, d.day), (m1, d1), (m2, d2)
    return b <= a <= c if b <= c else (a >= b or a <= c)


def load_sales(path):
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


def rain_level(w):
    """0 없음 / 1 약간(1~5mm 또는 확률 40~59%) / 2 비(5mm↑ 또는 확률 60%↑) / 3 많은 비·눈(20mm↑ 또는 눈)"""
    if not w:
        return 0
    rain, pop, code = w.get("rain") or 0, w.get("pop"), w.get("code") or 0
    snow = code in (71, 73, 75, 77, 85, 86, 66, 67)
    if snow or rain >= 20:
        return 3
    if rain >= 5 or (pop is not None and pop >= 60):
        return 2
    if rain >= 1 or (pop is not None and pop >= 40):
        return 1
    return 0


def build(cfg, visitors, compare, news=None, weather=None, today=None):
    today = today or datetime.now(KST).date()
    rules = cfg.get("brief_rules", {})
    lb = rules.get("lookback_weeks", 8)
    holidays = cfg.get("holidays", {})
    bc = cfg.get("broadcast_log", [])
    wx = (weather or {}).get("days", {})
    compare = compare or []
    visitors = visitors or {}
    sample = not visitors.get("live")

    dates, vis = visitors.get("dates", []), visitors.get("visitor", [])
    by = {d: v for d, v in zip(dates, vis) if v is not None}
    hol = set(holidays) | {(date.fromisoformat(h) - timedelta(days=1)).isoformat() for h in holidays}
    recent = [(date.fromisoformat(d), v) for d, v in zip(dates, vis) if v is not None and d not in hol][-7 * lb:]
    by_wd = {i: _median([v for d, v in recent if d.weekday() == i]) for i in range(7)}
    weekday_base = _median([v for d, v in recent if d.weekday() < 4]) or 1
    last4 = [v for d, v in recent[-28:]]
    prev4 = [v for d, v in recent[-56:-28]]
    trend = (sum(last4) / len(last4)) / (sum(prev4) / len(prev4)) if last4 and prev4 else 1.0
    trend = max(0.9, min(1.1, trend))
    last_data = dates[-1] if dates else None
    base_center = recent[len(recent) // 2][0] if recent else today

    # ---- 날씨 효과: 과거에서 "비 온 날 ÷ 그 주변(±4주) 같은 요일 비 안 온 날" 실측 ----
    #  주변 날짜와 비교해야 계절 차이(여름 vs 가을)가 비 효과에 섞이지 않음
    def local_dry_base(dt):
        xs = []
        for k in (-28, -21, -14, -7, 7, 14, 21, 28):
            o = (dt + timedelta(days=k)).isoformat()
            if o in by and o not in hol and o in wx and rain_level(wx[o]) == 0:
                xs.append(by[o])
        return statistics.median(xs) if len(xs) >= 2 else None

    def wx_effect():
        eff = {(k, l): [] for k in ("we", "wd") for l in (1, 2, 3)}
        for d, v in by.items():
            if d in hol or d not in wx:
                continue
            dt = date.fromisoformat(d)
            lvl = rain_level(wx[d])
            if lvl == 0:
                continue
            base = local_dry_base(dt)
            if not base:
                continue
            eff[("we" if dt.weekday() >= 5 else "wd", lvl)].append(v / base)
        default = {1: 0.95, 2: 0.80, 3: 0.65}
        out = {}
        for (k, lvl), xs in eff.items():
            out[f"{k}{lvl}"] = {"factor": round(max(0.4, min(1.05, statistics.median(xs))), 3) if len(xs) >= 4 else default[lvl],
                                "n": len(xs), "measured": len(xs) >= 4}
        return out
    wxe = wx_effect()

    def wx_factor(d):
        w = wx.get(d.isoformat())
        lvl = rain_level(w)
        if lvl == 0:
            return 1.0, None, w
        k = ("we" if d.weekday() >= 5 else "wd") + str(lvl)
        return wxe[k]["factor"], {1: "비 조금", 2: "비 예보", 3: "많은 비·눈"}[lvl], w

    # ---- 계절 ----
    def ly_avg(wd, center, half=14):
        xs = [by[(center + timedelta(days=k)).isoformat()] for k in range(-half, half + 1)
              if (center + timedelta(days=k)).weekday() == wd and (center + timedelta(days=k)).isoformat() in by
              and (center + timedelta(days=k)).isoformat() not in hol]
        return (_mean(xs), len(xs))

    def seasonal(wd, target):
        a, na = ly_avg(wd, target - timedelta(days=364))
        b, nb = ly_avg(wd, base_center - timedelta(days=364))
        if a and b and na >= 2 and nb >= 2:
            return max(0.7, min(1.3, a / b))
        return 1.0

    # ---- 실제값 비교 ----
    true_hol = set(holidays)

    def month_wd_avg(ym, wd):
        # 연휴·연휴 전날 제외 → 없으면 연휴 당일만 제외하고 다시
        xs = [v for d, v in by.items() if d.startswith(ym) and date.fromisoformat(d).weekday() == wd and d not in hol]
        if not xs:
            xs = [v for d, v in by.items() if d.startswith(ym) and date.fromisoformat(d).weekday() == wd and d not in true_hol]
        return (_mean(xs), len(xs))

    def ym_shift(d, k):
        y, m = d.year, d.month - k
        while m <= 0:
            m += 12
            y -= 1
        return f"{y}-{m:02d}"

    def cmp_block(d, est):
        out = {}
        for key, k in (("prev_month", 1), ("yoy_month", 12)):
            ym = ym_shift(d, k)
            a, n = month_wd_avg(ym, d.weekday())
            out[key] = {"base": round(a) if a else None, "n": n, "pct": round((est / a - 1) * 100, 1) if a else None, "ym": ym}
        return out

    # ---- 가게 매출 비율(선택) ----
    sales = load_sales(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "store_sales.csv"))
    capture = None
    if sales:
        pairs = [(by[d], s) for d, s in sales.items() if d in by and by[d] > 0]
        if len(pairs) >= 14:
            capture = statistics.median([s / v * 10000 for v, s in pairs])

    # ---- 방송 ----
    auto_bc = []
    for it in (news or {}).get("items", []):
        if it.get("tv") and any(w in it["title"] for w in ("맛집", "꽃게", "낙지", "젓국", "식당", "밥상", "먹방", "게장", "칼국수")):
            auto_bc.append({"date": it["date"], "program": it["tv"], "note": it["title"][:60], "link": it.get("link"), "auto": True})
    seen = {b["date"] for b in bc}
    all_bc = bc + [b for b in auto_bc if b["date"] not in seen and not seen.add(b["date"])]
    recent_bc = [b for b in all_bc if 0 <= (today - date.fromisoformat(b["date"])).days <= rules.get("broadcast_boost_days", 10)]

    # ---- 날짜별 예측 ----
    def forecast_day(d, detail=True):
        wd = d.weekday()
        tags, base_wd = [], wd
        if d.isoformat() in holidays:
            base_wd = 5
            tags.append(holidays[d.isoformat()])
        elif (d + timedelta(days=1)).isoformat() in holidays and wd not in (4, 5):
            base_wd = 4
            tags.append("연휴 전날")
        sf = seasonal(base_wd, d)
        est = by_wd.get(base_wd, weekday_base) * trend * sf
        wf, wtag, w = wx_factor(d)
        if detail and wtag:
            est *= wf
            tags.append(wtag)
        if recent_bc and wd in (5, 6) and detail:
            est *= rules.get("broadcast_boost", 1.3)
            tags.append("방송 후 첫 주말")
        vs_wd = (est / by_wd[wd] - 1) * 100 if by_wd.get(wd) else 0
        x_weekday = est / weekday_base
        level = "quiet" if vs_wd <= -20 else "peak" if vs_wd >= 20 else "busy" if vs_wd >= 8 else "normal"
        row = {"date": d.isoformat(), "wd": WD[wd], "est_visitors": round(est), "level": level,
               "vs_weekday_pct": round(vs_wd), "x_weekday": round(x_weekday, 2), "tags": tags,
               "weather": None if not w else {"code": w.get("code"), "text": WX.get(w.get("code"), ""), "tmax": w.get("tmax"), "tmin": w.get("tmin"),
                                              "rain": w.get("rain"), "pop": w.get("pop"), "src": w.get("src")}}
        if detail:
            row["cmp"] = cmp_block(d, est)
            row["est_store"] = round(est / 10000 * capture) if capture else None
            row["seasonal"] = round(sf, 3)
            row["weather_factor"] = round(wf, 3)
        return row

    days = [forecast_day(today + timedelta(days=i)) for i in range(7)]

    # ---- 4주 전망 ----
    outlook = []
    for wk in range(4):
        ws = today + timedelta(days=7 * wk)
        rows = [forecast_day(ws + timedelta(days=i), detail=(wk == 0)) for i in range(7)]
        sat = next((r for r in rows if r["wd"] == "토"), None)
        sun = next((r for r in rows if r["wd"] == "일"), None)
        wkd = [r for r in rows if r["wd"] in ("월", "화", "수", "목")]
        hols = [t for r in rows for t in r["tags"] if t in holidays.values()]
        ly = [by.get((ws + timedelta(days=i) - timedelta(days=364)).isoformat()) for i in range(7)]
        ly = [x for x in ly if x]
        outlook.append({"start": ws.isoformat(), "end": (ws + timedelta(days=6)).isoformat(),
                        "sat": sat["est_visitors"] if sat else None, "sun": sun["est_visitors"] if sun else None,
                        "weekday": round(_mean([r["est_visitors"] for r in wkd])) if wkd else None,
                        "week_total": sum(r["est_visitors"] for r in rows),
                        "last_year_total": round(sum(ly)) if len(ly) >= 5 else None,
                        "holidays": hols, "weather_applied": wk == 0})

    # ---- 재료 ----
    cmp = {r["keyword"]: r for r in compare}
    we = [d for d in days if d["wd"] in ("토", "일") or (d["tags"] and d["tags"][0] in holidays.values())]
    we_ratio = _mean([1 + d["vs_weekday_pct"] / 100 for d in we]) or 1.0
    wk_ratio = _mean([1 + d["vs_weekday_pct"] / 100 for d in days if d not in we]) or 1.0
    cold = any((d["weather"] or {}).get("tmax") is not None and d["weather"]["tmax"] <= 5 for d in days)
    ingredients = []
    for name, ing in cfg["store"].get("ingredients", {}).items():
        kws = [cmp[k] for k in ing.get("keywords", []) if k in cmp and not cmp[k].get("tiny")]
        wk_pct = _mean([k["week"]["pct"] for k in kws if k["week"]["pct"] is not None])
        mo_pct = _mean([k["month"]["pct"] for k in kws if k["month"]["pct"] is not None])
        se = ing.get("season", {})
        reasons = []
        mult = we_ratio
        if we_ratio >= 1.08:
            reasons.append(f"주말 손님 평소보다 +{(we_ratio-1)*100:.0f}%")
        elif we_ratio <= 0.92:
            reasons.append(f"주말 손님 평소보다 {(we_ratio-1)*100:.0f}%")
        if wk_pct is not None and abs(wk_pct) >= 15:
            mult *= 1 + max(-0.15, min(0.15, wk_pct / 100 * 0.5))
            reasons.append(f"검색 전주 대비 {wk_pct:+.0f}%")
        if mo_pct is not None and mo_pct <= -25:
            reasons.append(f"검색 전월 대비 {mo_pct:.0f}%" + (" (제철 끝물)" if se.get("peak") and _in_window(today, se.get("peak")) else ""))
        if cold and ing.get("hot_menu"):
            mult *= 1.08
            reasons.append("추위 예보 — 탕 수요 ↑")
        if recent_bc:
            reasons.append("방송 효과 기간")
        action = "keep"
        if _in_window(today, se.get("closed")):
            action = "frozen"
            reasons.insert(0, "금어기 — 생물 수급 어려움, 냉동·비축분 운영")
        elif mult >= 1.10:
            action = "more"
        elif mult <= 0.90:
            action = "less"
        if not kws:
            reasons.append("검색량 적어 손님 수만으로 판단")
        ingredients.append({"name": name, "menus": ing.get("menus", []), "action": action, "mult_weekend": round(mult, 2), "mult_weekday": round(wk_ratio, 2),
                            "reasons": reasons[:3], "note": ing.get("note", ""), "in_season": _in_window(today, se.get("peak")),
                            "week_pct": None if wk_pct is None else round(wk_pct, 1), "month_pct": None if mo_pct is None else round(mo_pct, 1)})

    # ---- 인력 ----
    staff = []
    for x in days:
        is_hol = bool(x["tags"]) and x["tags"][0] in holidays.values()
        if x["vs_weekday_pct"] >= 20 or is_hol:
            staff.append({"date": x["date"], "wd": x["wd"], "advice": "홀 +1, 주방 +1" if x["x_weekday"] >= 2.5 else "홀 +1",
                          "why": ", ".join(x["tags"]) or f"평소 {x['wd']}요일보다 +{x['vs_weekday_pct']}%"})
        elif x["vs_weekday_pct"] <= -25:
            staff.append({"date": x["date"], "wd": x["wd"], "advice": "인원 줄여도 됨", "why": ", ".join(x["tags"]) or f"평소보다 {x['vs_weekday_pct']}%"})

    # ---- 알림 30일 ----
    alerts = []
    for i in range(30):
        d = today + timedelta(days=i)
        if d.isoformat() in holidays:
            alerts.append({"date": d.isoformat(), "wd": WD[d.weekday()], "kind": "holiday", "text": holidays[d.isoformat()] + " — 토요일 수준 손님 예상"})
    for name, ing in cfg["store"].get("ingredients", {}).items():
        for key, label in (("closed", "금어기 시작"), ("peak", "제철 시작")):
            w = ing.get("season", {}).get(key)
            if not w:
                continue
            start = date(today.year, w[0][0], w[0][1])
            if start < today:
                start = date(today.year + 1, w[0][0], w[0][1])
            if 0 <= (start - today).days <= 30:
                alerts.append({"date": start.isoformat(), "wd": WD[start.weekday()], "kind": "season", "text": f"{name} {label}"})
    for b in bc:
        bd = date.fromisoformat(b["date"])
        if 0 <= (bd - today).days <= 30:
            alerts.append({"date": b["date"], "wd": WD[bd.weekday()], "kind": "tv", "text": f"📺 {b['program']} 방송 예정 — 다음 주말 1.3배 준비"})
    alerts.sort(key=lambda a: a["date"])

    # ---- 한 줄 요약 ----
    sat = next((d for d in days if d["wd"] == "토"), None)
    sun = next((d for d in days if d["wd"] == "일"), None)
    parts = []
    for lab, x in (("토요일", sat), ("일요일", sun)):
        if not x:
            continue
        p = x["vs_weekday_pct"]
        txt = f"{lab} 평소보다 {p:+d}%" if abs(p) >= 8 else f"{lab} 평소 수준"
        if x["tags"]:
            txt += f" ({', '.join(x['tags'])})"
        parts.append(txt)
    more = [i["name"] for i in ingredients if i["action"] == "more"]
    less = [i["name"] for i in ingredients if i["action"] == "less"]
    if more:
        parts.append("더 받기: " + ", ".join(more))
    if less:
        parts.append("줄이기: " + ", ".join(less))
    summary = " · ".join(parts) if parts else "이번 주는 평소 수준 — 주말 기본 준비"

    return {
        "generated": today.isoformat(), "sample": sample, "last_visitor_data": last_data, "weather_live": bool((weather or {}).get("live")),
        "trend_factor": round(trend, 3), "weekday_median": {WD[i]: round(by_wd[i]) for i in range(7)}, "weekday_base": round(weekday_base),
        "weather_effect": wxe, "capture_per_10k": round(capture, 2) if capture else None, "sales_days": len(sales),
        "summary": summary, "days": days, "outlook": outlook, "ingredients": ingredients, "staff": staff, "alerts": alerts[:12],
        "weekend_ratio": round(we_ratio, 3), "weekday_ratio": round(wk_ratio, 3),
        "broadcast_recent": recent_bc, "broadcast_auto": sorted(auto_bc, key=lambda b: b["date"], reverse=True)[:10],
    }


if __name__ == "__main__":
    root = os.path.dirname(os.path.abspath(__file__))

    def L(n):
        p = os.path.join(root, "data", n)
        return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None
    cfg = json.load(open(os.path.join(root, "keywords.json"), encoding="utf-8"))
    out = build(cfg, L("visitors.json"), L("compare.json"), L("news_log.json"), L("weather.json"))
    json.dump(out, open(os.path.join(root, "data", "brief.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(out["summary"])
    print("weather_effect", out["weather_effect"])
    for d in out["days"]:
        print(d["date"], d["wd"], d["est_visitors"], d["level"], d["vs_weekday_pct"], d["tags"], (d["weather"] or {}).get("text"))
    for o in out["outlook"]:
        print(o)
    for i in out["ingredients"]:
        print(i["name"], i["action"], i["mult_weekend"], i["reasons"])
    print(out["staff"])

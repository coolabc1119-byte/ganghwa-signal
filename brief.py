# -*- coding: utf-8 -*-
"""
주간 준비 브리핑 v4 — 사장님의 질문에 답한다.
  1) 오늘·이번 주 며칠이 얼마나 바쁠까?   → 날짜별 예상 외지인 + 이맘때 보통 같은 요일 대비 % + 오차 범위
  2) 재료를 더 받을까 덜 받을까?          → 손님 수 신호 × 메뉴 인기 신호
  3) 앞으로 6주 흐름, 연휴는 얼마나 붐빌까 → 작년 실제 + 예상
  4) 이 예측을 얼마나 믿어도 되나?        → 과거를 같은 조건으로 다시 맞혀 본 성적표

예측 식은 forecast.py 참고.
"""
import json
import os
import statistics as st
from datetime import date, datetime, timedelta, timezone

import forecast
from forecast import Model, rain_level, TYPE_LABEL, SAT_BASED

KST = timezone(timedelta(hours=9))
WD = ["월", "화", "수", "목", "금", "토", "일"]
I = lambda d: d.isoformat()

WX = {0: "맑음", 1: "맑음", 2: "구름", 3: "흐림", 45: "안개", 48: "안개", 51: "이슬비", 53: "이슬비", 55: "이슬비",
      61: "비", 63: "비", 65: "큰비", 66: "진눈깨비", 67: "진눈깨비", 71: "눈", 73: "눈", 75: "큰눈", 77: "눈",
      80: "소나기", 81: "소나기", 82: "폭우", 85: "눈", 86: "큰눈", 95: "뇌우", 96: "뇌우", 99: "뇌우"}
RAIN_TAG = {1: "비 조금", 2: "비", 3: "많은 비·눈"}


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _in_window(d, win):
    if not win:
        return False
    (m1, d1), (m2, d2) = win
    a, b, c = (d.month, d.day), (m1, d1), (m2, d2)
    return b <= a <= c if b <= c else (a >= b or a <= c)


def _q(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    return xs[min(len(xs) - 1, max(0, int(round(p * (len(xs) - 1)))))]


def backtest(by, H, wx, lag, span=365):
    """방문자 통계가 lag일 늦게 나오는 조건 그대로, 지난 span일을 하루씩 다시 맞혀 본다."""
    last = date.fromisoformat(max(by))
    rows, cache = [], {}
    d = last - timedelta(days=span - 1)
    while d <= last:
        s = I(d)
        if s in by:
            cut = I(d - timedelta(days=lag))
            if cut not in cache:
                if len(cache) > 2:
                    cache.pop(next(iter(cache)))
                cache[cut] = Model(by, H, wx, cutoff=cut)
            p = cache[cut].predict(d)
            if p:
                rows.append({"date": s, "act": by[s], "est": round(p["est"]), "type": p["type"], "wd": d.weekday(), "rl": p["rain_level"]})
        d += timedelta(1)

    def stat(sel):
        r = [x for x in rows if sel(x)]
        if len(r) < 5:
            return None
        e = [x["act"] / x["est"] - 1 for x in r]
        a = [abs(v) for v in e]
        return {"n": len(r), "mape": round(st.mean(a) * 100, 1), "median": round(st.median(a) * 100, 1), "bias": round(-st.mean(e) * 100, 1),
                "p10": round(_q(e, 0.1), 3), "p90": round(_q(e, 0.9), 3),
                "within10": round(sum(1 for v in a if v <= 0.10) / len(a) * 100), "within20": round(sum(1 for v in a if v <= 0.20) / len(a) * 100)}
    cats = {"all": stat(lambda x: True), "weekday": stat(lambda x: x["type"] == "normal" and x["wd"] < 5),
            "weekend": stat(lambda x: x["type"] == "normal" and x["wd"] >= 5), "holiday": stat(lambda x: x["type"] != "normal"),
            "rain": stat(lambda x: x["type"] == "normal" and x["rl"] >= 2)}
    return {"lag": lag, "from": rows[0]["date"] if rows else None, "to": rows[-1]["date"] if rows else None, "cats": cats,
            "recent": [{"date": x["date"], "act": x["act"], "est": x["est"], "type": x["type"]} for x in rows[-56:]]}


def menu_share(trend, kws, ref="강화도 맛집"):
    """최근 7일 메뉴 비중(메뉴 검색 ÷ 강화도 맛집 검색)이 그 앞 4주보다 얼마나 높은가"""
    if not trend or not trend.get("series") or ref not in trend["series"]:
        return None
    ser, n = trend["series"], len(trend["dates"])
    while n > 0 and not any((v[n - 1] or 0) for v in ser.values()):
        n -= 1
    kws = [k for k in kws if k in ser]
    if not kws or n < 40:
        return None
    s = lambda k, a, b: sum((ser[k][i] or 0) for i in range(max(0, a), b))
    cur_m, cur_r = sum(s(k, n - 7, n) for k in kws), s(ref, n - 7, n)
    use_m, use_r = sum(s(k, n - 35, n - 7) for k in kws), s(ref, n - 35, n - 7)
    if not cur_r or not use_r or not use_m or cur_m / 7 < 3:
        return None
    now, usual = cur_m / cur_r * 100, use_m / use_r * 100
    return {"now": round(now, 2), "usual": round(usual, 2), "pct": round((now / usual - 1) * 100, 1), "per_day": round(cur_m / 7, 1),
            "from": trend["dates"][n - 7], "to": trend["dates"][n - 1]}


def build(cfg, visitors, compare=None, news=None, weather=None, today=None, trend=None):
    today = today or datetime.now(KST).date()
    rules = cfg.get("brief_rules", {})
    H = cfg.get("holidays", {})
    bc = cfg.get("broadcast_log", [])
    wx = (weather or {}).get("days", {})
    visitors = visitors or {}
    dates, vis = visitors.get("dates", []), visitors.get("visitor", [])
    by = {d: v for d, v in zip(dates, vis) if v is not None}
    last_data = max(by) if by else None
    lag = (today - date.fromisoformat(last_data)).days if last_data else 30
    lag = max(7, min(45, lag))

    m = Model(by, H, wx)
    acc = backtest(by, H, wx, lag)
    cats = acc["cats"]

    def band(t, wd):
        c = cats.get("holiday" if t != "normal" else "weekend" if wd >= 5 else "weekday") or cats.get("all") or {"p10": -0.15, "p90": 0.2}
        return c["p10"], c["p90"]

    # ---- 실제값 비교(지난달·작년 같은 달 같은 요일) ----
    def month_wd_avg(ym, wd):
        xs = [v for d, v in by.items() if d.startswith(ym) and date.fromisoformat(d).weekday() == wd and m.dtype(date.fromisoformat(d)) == "normal"]
        return _mean(xs), len(xs)

    def ym_shift(d, k):
        y, mo = d.year, d.month - k
        while mo <= 0:
            mo += 12
            y -= 1
        return f"{y}-{mo:02d}"

    def cmp_block(d, est):
        """보통날이 2일 이상 있는 가장 최근 달 / 작년 같은 달의 같은 요일 실제 평균과 비교"""
        out = {}
        for key, ks in (("prev_month", (1, 2, 3)), ("yoy_month", (12,))):
            out[key] = {"base": None, "n": 0, "pct": None, "ym": ym_shift(d, ks[0])}
            for k in ks:
                ym = ym_shift(d, k)
                a, n = month_wd_avg(ym, d.weekday())
                if a and n >= 2:
                    out[key] = {"base": round(a), "n": n, "pct": round((est / a - 1) * 100, 1), "ym": ym}
                    break
        return out

    # ---- 방송 ----
    auto_bc = []
    for it in (news or {}).get("items", []):
        if it.get("tv") and any(w in it["title"] for w in ("맛집", "꽃게", "낙지", "젓국", "식당", "밥상", "먹방", "게장", "칼국수")):
            auto_bc.append({"date": it["date"], "program": it["tv"], "note": it["title"][:60], "link": it.get("link"), "auto": True})

    # ---- 날짜별 예측 (14일) ----
    def day_row(d, i):
        use_wx = i < 8 and I(d) in wx          # 8일 뒤부터는 예보가 자주 바뀌어 날씨를 반영하지 않음
        p = m.predict(d, use_weather=use_wx)
        if not p:
            return None
        wd, t = d.weekday(), p["type"]
        w = wx.get(I(d))
        # 보통 = 이맘때 같은 요일, 연휴 아님, 비 안 오는 날
        normal = (p["normal_same_wd"] or p["base"]) * (m.dry_boost["we" if wd >= 5 else "wd"] if use_wx else 1.0)
        est = p["est"]
        lo, hi = band(t, wd)
        vs = (est / normal - 1) * 100 if normal else 0
        tags = []
        if I(d) in H:
            tags.append(H[I(d)])
        if TYPE_LABEL.get(t) and TYPE_LABEL[t] not in tags:
            tags.append(TYPE_LABEL[t])
        if p["rain_level"]:
            tags.append(RAIN_TAG[p["rain_level"]])
        if p.get("cold"):
            tags.append("갑자기 추움")
        level = "quiet" if vs <= -15 else "peak" if vs >= 25 else "busy" if vs >= 10 else "normal"
        sat_x = None
        if t in SAT_BASED:
            sat_x = round(p["type_mult"], 2)
        return {"date": I(d), "wd": WD[wd], "type": t, "type_label": TYPE_LABEL.get(t, ""), "holiday": H.get(I(d)),
                "est": round(est), "lo": round(est * (1 + lo)), "hi": round(est * (1 + hi)), "normal": round(normal) if normal else None,
                "vs_normal_pct": round(vs), "level": level, "tags": tags, "sat_x": sat_x,
                "wx_pct": round((p["wx_factor"] / (m.dry_boost["we" if m.off(d) else "wd"]) - 1) * 100) if use_wx else None,
                "rain_level": p["rain_level"], "wx_applied": use_wx,
                "weather": None if not w else {"code": w.get("code"), "text": WX.get(w.get("code"), ""), "tmax": w.get("tmax"), "tmin": w.get("tmin"),
                                               "rain": w.get("rain"), "pop": w.get("pop"), "wind": w.get("wind"), "src": w.get("src")},
                "last_year": by.get(I(d - timedelta(days=364))),
                "cmp": cmp_block(d, est)}

    days = [r for r in (day_row(today + timedelta(days=i), i) for i in range(14)) if r]
    week = days[:7]

    # ---- 재료: 손님 수 신호 × 메뉴 인기 신호 ----
    def ratio(rows):
        a, b = sum(r["est"] for r in rows), sum(r["normal"] or r["est"] for r in rows)
        return (a / b - 1) * 100 if b else 0
    is_peak_day = lambda r: r["wd"] in ("토", "일") or r["type"] in SAT_BASED
    we_rows = [r for r in week if is_peak_day(r)]
    wk_rows = [r for r in week if not is_peak_day(r)]
    we_pct, wk_pct = ratio(we_rows) if we_rows else 0, ratio(wk_rows) if wk_rows else 0
    cold_days = [r for r in week if (r["weather"] or {}).get("tmax") is not None and r["weather"]["tmax"] <= 5]
    prep = []
    for name, ing in cfg["store"].get("ingredients", {}).items():
        sh = menu_share(trend, ing.get("keywords", []))
        se = ing.get("season", {})
        score, reasons = 0, []
        if we_pct >= 10:
            score += 1
        elif we_pct <= -10:
            score -= 1
        if sh:
            if sh["pct"] >= 15:
                score += 1
            elif sh["pct"] <= -15:
                score -= 1
        verdict = "more" if score >= 1 else "less" if score <= -1 else "keep"
        mixed = bool(sh) and ((we_pct >= 10 and sh["pct"] <= -15) or (we_pct <= -10 and sh["pct"] >= 15))
        if _in_window(today, se.get("closed")):
            verdict = "frozen"
            reasons.append("금어기 — 생물 수급 어려움, 냉동·비축분 운영")
        if _in_window(today, se.get("peak")):
            reasons.append("제철")
        if cold_days and ing.get("hot_menu"):
            reasons.append(f"최고 5℃ 이하 {len(cold_days)}일 — 탕·면 찾는 손님이 늘 수 있음")
        if not sh:
            reasons.append("메뉴 검색이 적어 손님 수만으로 판단")
        prep.append({"name": name, "menus": ing.get("menus", []), "verdict": verdict, "mixed": mixed, "cust_pct": round(we_pct), "share": sh,
                     "reasons": reasons, "note": ing.get("note", ""), "in_season": _in_window(today, se.get("peak"))})

    # ---- 앞으로 6주 흐름 (주말·평일 하루 평균: 예상 vs 작년 같은 주 실제) ----
    outlook = []
    mon = today - timedelta(days=today.weekday())
    for k in range(6):
        ws = mon + timedelta(days=7 * k)
        rows = []
        for i in range(7):
            d = ws + timedelta(i)
            p = m.predict(d, use_weather=False)
            if p:
                rows.append((d, p))
        we = [p["est"] for d, p in rows if m.off(d)]
        wk = [p["est"] for d, p in rows if not m.off(d)]
        ly = [(ws + timedelta(i - 364)) for i in range(7)]
        ly_we = [by[I(d)] for d in ly if I(d) in by and m.off(d)]
        ly_wk = [by[I(d)] for d in ly if I(d) in by and not m.off(d)]
        hol = sorted({H[I(d)] for d, _ in rows if I(d) in H})
        outlook.append({"start": I(ws), "end": I(ws + timedelta(6)), "we": round(_mean(we)) if we else None, "wk": round(_mean(wk)) if wk else None,
                        "ly_we": round(_mean(ly_we)) if ly_we else None, "ly_wk": round(_mean(ly_wk)) if ly_wk else None,
                        "week_total": round(sum(p["est"] for _, p in rows)), "holidays": hol, "current": k == 0})

    # ---- 알림 45일 ----
    alerts = []
    for i in range(45):
        d = today + timedelta(days=i)
        if I(d) in H:
            t = m.dtype(d)
            mult = m.type_mult.get(t)
            txt = H[I(d)]
            if mult and t in SAT_BASED:
                txt += f" — {TYPE_LABEL[t]}, 과거 {mult['n']}번 평균 보통 토요일의 {mult['x']:.2f}배"
            alerts.append({"date": I(d), "wd": WD[d.weekday()], "kind": "holiday", "text": txt})
    for name, ing in cfg["store"].get("ingredients", {}).items():
        for key, label in (("closed", "금어기 시작"), ("peak", "제철 시작")):
            w = ing.get("season", {}).get(key)
            if not w:
                continue
            for edge, lab in ((0, label), (1, label.replace("시작", "끝"))):
                dd = date(today.year, w[edge][0], w[edge][1])
                if dd < today:
                    dd = date(today.year + 1, w[edge][0], w[edge][1])
                if 0 <= (dd - today).days <= 45:
                    alerts.append({"date": I(dd), "wd": WD[dd.weekday()], "kind": "season", "text": f"{name} {lab}"})
    for b in bc:
        bd = date.fromisoformat(b["date"])
        if 0 <= (bd - today).days <= 45:
            alerts.append({"date": b["date"], "wd": WD[bd.weekday()], "kind": "tv", "text": f"📺 {b.get('program', '방송')} 방송 예정"})
    alerts.sort(key=lambda a: a["date"])

    # ---- 한 줄 결론 ----
    def phrase(r):
        p = r["vs_normal_pct"]
        s = "보통 수준" if abs(p) < 8 else f"보통보다 {p:+d}%"
        why = [t for t in r["tags"]]
        return s + (f" ({', '.join(why[:2])})" if why and abs(p) >= 8 else "")
    sat = next((r for r in week if r["wd"] == "토"), None)
    sun = next((r for r in week if r["wd"] == "일"), None)
    parts = [f"{lab} {phrase(r)}" for lab, r in (("토요일", sat), ("일요일", sun)) if r]
    more = [p["name"] for p in prep if p["verdict"] == "more"]
    less = [p["name"] for p in prep if p["verdict"] == "less"]
    summary = " · ".join(parts)

    lv = m.level
    return {
        "version": 4, "generated": I(today), "last_visitor_data": last_data, "lag_days": lag, "weather_live": bool((weather or {}).get("live")),
        "summary": summary, "more": more, "less": less, "weekend_pct": round(we_pct), "weekday_pct": round(wk_pct),
        "days": days, "prep": prep, "outlook": outlook, "alerts": alerts[:14], "accuracy": acc,
        "model": {"level_vs_last_year": {"weekend": round((lv["we"][0] - 1) * 100, 1) if lv["we"][0] else None,
                                          "weekday": round((lv["wd"][0] - 1) * 100, 1) if lv["wd"][0] else None},
                  "type_mult": m.type_mult, "type_label": TYPE_LABEL, "dry_boost": m.dry_boost, "cold": m.cold_eff,
                  "recent_from": I(min(m.recent)) if m.recent else None, "recent_to": I(max(m.recent)) if m.recent else None},
        "weather_effect": m.wx_eff,
        "broadcast_auto": sorted(auto_bc, key=lambda b: b["date"], reverse=True)[:10],
    }


if __name__ == "__main__":
    root = os.path.dirname(os.path.abspath(__file__))

    def L(n):
        p = os.path.join(root, "data", n)
        return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None
    cfg = json.load(open(os.path.join(root, "keywords.json"), encoding="utf-8"))
    out = build(cfg, L("visitors.json"), L("compare.json"), L("news_log.json"), L("weather.json"), trend=L("trend_daily.json"))
    json.dump(out, open(os.path.join(root, "data", "brief.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(out["summary"], "| 주말", out["weekend_pct"], "평일", out["weekday_pct"], "| 더", out["more"], "덜", out["less"])
    for d in out["days"]:
        print(d["date"], d["wd"], d["est"], f"({d['lo']}~{d['hi']})", "보통", d["normal"], f"{d['vs_normal_pct']:+d}%", d["level"], d["tags"], (d["weather"] or {}).get("text"), "작년", d["last_year"])
    for p in out["prep"]:
        print(p["name"], p["verdict"], p["cust_pct"], p["share"], p["reasons"])
    for o in out["outlook"]:
        print(o)
    print(out["alerts"])
    print(json.dumps(out["accuracy"]["cats"], ensure_ascii=False))
    print(out["model"]["level_vs_last_year"], out["model"]["cold"])

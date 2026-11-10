# -*- coding: utf-8 -*-
"""
방문자 예측 엔진 (v4) — brief.py가 사용.

예상 = 기준선(작년·재작년 같은 시기 같은 요일 모양 × 최근 8주 수준)
       × 날 유형(연휴 첫날/가운데/마지막, 명절, 평일 공휴일, 징검다리 …  과거 실측 배율)
       × 날씨(비 단계별 과거 실측 배율)

모든 배율은 과거 실제값에서 매번 다시 계산한다(고정 상수 아님).
"""
import statistics as st
from datetime import date, timedelta

I = lambda d: d.isoformat()
TYPE_LABEL = {"normal": "", "hol_weekday": "평일 공휴일", "newyear": "신정", "long_first": "연휴 첫날", "long_mid": "연휴 가운데",
              "long_last": "연휴 마지막 날", "mj_first": "명절 연휴 첫날", "mj_mid": "명절 연휴", "mj_day": "명절 당일",
              "mj_last": "명절 연휴 마지막 날", "bridge": "징검다리 평일", "eve_long": "연휴 전날"}
# 과거 사례가 3번 미만일 때 쓰는 기본 배율 (토요일 기준 / 평일형은 같은 요일 기준)
TYPE_DEFAULT = {"hol_weekday": 0.92, "newyear": 1.5, "long_first": 1.24, "long_mid": 1.35, "long_last": 0.91, "mj_first": 1.14,
                "mj_mid": 1.47, "mj_day": 1.97, "mj_last": 1.11, "bridge": 1.20, "eve_long": 1.22}
SAT_BASED = {"hol_weekday", "newyear", "long_first", "long_mid", "long_last", "mj_first", "mj_mid", "mj_day", "mj_last"}


def rain_level(w):
    """0 없음 / 1 약간(1~5mm 또는 확률 40~59%) / 2 비(5mm↑ 또는 확률 60%↑) / 3 많은 비·눈(20mm↑ 또는 눈)"""
    if not w:
        return 0
    rain, pop, code = w.get("rain") or 0, w.get("pop"), w.get("code") or 0
    snow = code in (71, 73, 75, 77, 85, 86, 66, 67)
    if snow or rain >= 20:
        return 3
    if rain >= 5 or (pop is not None and pop >= 60 and w.get("src") == "forecast" and rain >= 1):
        return 2
    if rain >= 1 or (pop is not None and pop >= 40 and w.get("src") == "forecast"):
        return 1
    return 0


class Model:
    def __init__(self, by, holidays, wx=None, cutoff=None, lookback_weeks=8):
        """by: {날짜: 방문자}, cutoff: 이 날짜까지의 실제값만 사용(백테스트용)"""
        self.H = holidays
        self.wx = wx or {}
        self.cut = cutoff or max(by)
        self.by = {d: v for d, v in by.items() if d <= self.cut and v is not None}
        self.lb = lookback_weeks
        self._types = {}
        self._fit()

    # ---------- 날 유형 ----------
    def off(self, d):
        return d.weekday() >= 5 or I(d) in self.H

    def run(self, d):
        if not self.off(d):
            return 0, 0
        a = b = d
        while self.off(a - timedelta(1)):
            a -= timedelta(1)
        while self.off(b + timedelta(1)):
            b += timedelta(1)
        return (b - a).days + 1, (d - a).days + 1

    def dtype(self, d):
        s = I(d)
        if s in self._types:
            return self._types[s]
        name = self.H.get(s, "")
        L, pos = self.run(d)
        t = "normal"
        if name in ("설날", "추석"):
            t = "mj_day"
        elif L >= 3:
            near = "".join(self.H.get(I(d + timedelta(k)), "") for k in range(-3, 4))
            pre = "mj_" if ("설" in near or "추석" in near) else "long_"
            t = pre + ("first" if pos == 1 else "last" if pos == L else "mid")
        elif name == "신정":
            t = "newyear"
        elif s in self.H and d.weekday() < 5:
            t = "hol_weekday"
        elif not self.off(d):
            nxt, prv = d + timedelta(1), d - timedelta(1)
            if self.off(nxt) and self.run(nxt)[0] >= 3 and d.weekday() != 4:
                t = "eve_long"
            elif self.off(prv) and self.off(nxt):
                t = "bridge"
        self._types[s] = t
        return t

    # ---------- 기준선 ----------
    def _ref(self, d, wd=None):
        """작년·재작년 같은 시기(±14일) 같은 요일 보통날 평균. 연도별 값을 반환"""
        wd = d.weekday() if wd is None else wd
        out = []
        for yb in (1, 2):
            c = d - timedelta(days=364 * yb)
            xs = []
            for k in range(-14, 15):
                o = c + timedelta(k)
                if o.weekday() == wd and I(o) in self.by and self.dtype(o) == "normal":
                    xs.append(self.by[I(o)])
            out.append(sum(xs) / len(xs) if len(xs) >= 2 else None)
        return out

    def _fit(self):
        cut = date.fromisoformat(self.cut)
        self.recent = [(cut - timedelta(k)) for k in range(0, 7 * self.lb) if I(cut - timedelta(k)) in self.by and self.dtype(cut - timedelta(k)) == "normal"]
        # 최근 수준: 올해 ÷ 작년(같은 시기 같은 요일), 올해 ÷ 재작년
        lv = {"all": [[], []], "we": [[], []], "wd": [[], []]}
        for d in self.recent:
            r = self._ref(d)
            for i in (0, 1):
                if r[i]:
                    q = self.by[I(d)] / r[i]
                    lv["all"][i].append(q)
                    lv["we" if d.weekday() >= 5 else "wd"][i].append(q)
        self.level = {}
        for k, pair in lv.items():
            self.level[k] = [st.median(x) if len(x) >= (10 if k != "we" else 6) else None for x in pair]
        for k in ("we", "wd"):  # 표본이 모자라면 전체 수준으로
            self.level[k] = [self.level[k][i] if self.level[k][i] else self.level["all"][i] for i in (0, 1)]
        self.wd_med = {w: (st.median([self.by[I(d)] for d in self.recent if d.weekday() == w]) if any(d.weekday() == w for d in self.recent) else None) for w in range(7)}
        self._fit_types()
        self._fit_weather()
        self._fit_cold()

    def base(self, d, wd=None):
        """보통날이라면 몇 명일까"""
        wd = d.weekday() if wd is None else wd
        r = self._ref(d, wd)
        lv = self.level["we" if wd >= 5 else "wd"]
        est = [r[i] * lv[i] for i in (0, 1) if r[i] and lv[i]]
        if est:
            return sum(est) / len(est)
        return self.wd_med.get(wd)

    def _local_base(self, d, wd):
        xs = []
        first = d + timedelta((wd - d.weekday()))
        for k in (-28, -21, -14, -7, 0, 7, 14, 21, 28):
            o = first + timedelta(k)
            if o != d and I(o) in self.by and self.dtype(o) == "normal":
                xs.append(self.by[I(o)])
        return st.median(xs) if len(xs) >= 3 else None

    def _fit_types(self):
        g = {}
        for s, v in self.by.items():
            d = date.fromisoformat(s)
            t = self.dtype(d)
            if t == "normal":
                continue
            b = self._local_base(d, 5 if t in SAT_BASED else d.weekday())
            if b:
                g.setdefault(t, []).append(v / b)
        self.type_mult = {}
        for t, dflt in TYPE_DEFAULT.items():
            xs = g.get(t, [])
            self.type_mult[t] = {"x": round(st.median(xs), 3) if len(xs) >= 3 else dflt, "n": len(xs), "lo": round(min(xs), 2) if xs else None, "hi": round(max(xs), 2) if xs else None}

    def _fit_weather(self):
        eff = {(k, l): [] for k in ("we", "wd") for l in (1, 2, 3)}
        for s, v in self.by.items():
            if s not in self.wx:
                continue
            d = date.fromisoformat(s)
            if self.dtype(d) != "normal":
                continue
            lvl = rain_level(self.wx[s])
            if lvl == 0:
                continue
            xs = []
            for k in (-28, -21, -14, -7, 7, 14, 21, 28):
                o = I(d + timedelta(k))
                if o in self.by and self.dtype(d + timedelta(k)) == "normal" and o in self.wx and rain_level(self.wx[o]) == 0:
                    xs.append(self.by[o])
            if len(xs) >= 2:
                eff[("we" if d.weekday() >= 5 else "wd", lvl)].append(v / st.median(xs))
        default = {1: 0.95, 2: 0.85, 3: 0.75}
        self.wx_eff = {}
        for (k, lvl), xs in eff.items():
            ok = len(xs) >= 5
            self.wx_eff[f"{k}{lvl}"] = {"factor": round(max(0.4, min(1.05, st.median(xs))), 3) if ok else default[lvl], "n": len(xs), "measured": ok}
        # 기준선 자체가 '비 온 날 포함 평균'이라, 맑은 날은 평균보다 조금 높다 → 보정값
        self.dry_boost = {}
        for k in ("we", "wd"):
            tot = n = 0
            for s in self.by:
                d = date.fromisoformat(s)
                if s in self.wx and self.dtype(d) == "normal" and (d.weekday() >= 5) == (k == "we"):
                    lvl = rain_level(self.wx[s])
                    tot += 1.0 if lvl == 0 else self.wx_eff[f"{k}{lvl}"]["factor"]
                    n += 1
            self.dry_boost[k] = round(n / tot, 4) if tot else 1.0

    def cold_snap(self, d):
        """그날 최고기온이 직전 14일 평균보다 5℃ 이상 낮은가"""
        w = self.wx.get(I(d))
        if not w or w.get("tmax") is None:
            return False
        prev = [self.wx[I(d - timedelta(k))]["tmax"] for k in range(1, 15) if I(d - timedelta(k)) in self.wx and self.wx[I(d - timedelta(k))].get("tmax") is not None]
        return len(prev) >= 10 and w["tmax"] - sum(prev) / len(prev) <= -5

    def _fit_cold(self):
        xs = []
        for s, v in self.by.items():
            d = date.fromisoformat(s)
            if s not in self.wx or self.dtype(d) != "normal" or rain_level(self.wx[s]) > 0 or not self.cold_snap(d):
                continue
            base = []
            for k in (-28, -21, -14, -7, 7, 14, 21, 28):
                o = d + timedelta(k)
                if I(o) in self.by and self.dtype(o) == "normal" and I(o) in self.wx and rain_level(self.wx[I(o)]) == 0 and not self.cold_snap(o):
                    base.append(self.by[I(o)])
            if len(base) >= 2:
                xs.append(v / st.median(base))
        ok = len(xs) >= 10
        self.cold_eff = {"factor": round(max(0.7, min(1.0, st.median(xs))), 3) if ok else 1.0, "n": len(xs), "measured": ok}

    def wx_factor(self, d, use_weather=True):
        w = self.wx.get(I(d))
        if not use_weather or w is None:
            return 1.0, 0
        k = "we" if self.off(d) else "wd"
        lvl = rain_level(w)
        f = self.dry_boost[k] * (1.0 if lvl == 0 else self.wx_eff[f"{k}{lvl}"]["factor"])
        if lvl == 0 and self.cold_snap(d):
            f *= self.cold_eff["factor"]
        return f, lvl

    # ---------- 예측 ----------
    def predict(self, d, use_weather=True):
        t = self.dtype(d)
        if t == "normal":
            b, m = self.base(d), 1.0
        elif t in SAT_BASED:
            sat = d + timedelta((5 - d.weekday()) % 7) if d.weekday() <= 5 else d - timedelta(1)
            b, m = self.base(sat, 5), self.type_mult[t]["x"]
        else:
            b, m = self.base(d), self.type_mult[t]["x"]
        if b is None:
            return None
        wf, lvl = self.wx_factor(d, use_weather)
        return {"est": b * m * wf, "base": b, "type": t, "type_mult": m, "wx_factor": wf, "rain_level": lvl, "cold": lvl == 0 and use_weather and self.cold_snap(d), "normal_same_wd": self.base(d)}

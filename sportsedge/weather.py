"""Game-time weather for outdoor NFL games, from Open-Meteo (free, no key).

How it moves the projection (NFL totals only). These are rules of thumb from
betting research, not fitted to this app's own data (old forecasts aren't kept):
  - wind over 12 mph: about 0.3 fewer points per mph, up to 6
  - rain at kickoff (0.05"+ an hour, or 60%+ chance): 1.5 fewer points
  - snow at kickoff: 3 fewer points
  - 20 F or colder: 1 fewer point
Domes and closed roofs get nothing.
"""
import json
from datetime import timedelta

import requests

from . import db as D
from .util import now_utc, parse_iso

GEO = "https://geocoding-api.open-meteo.com/v1/search"
FCST = "https://api.open-meteo.com/v1/forecast"
UA = {"User-Agent": "SportsEdge/1.0"}


def _geocode(con, city, state):
    key = f"geo2:{city},{state}"
    hit = D.get_meta(con, key)
    if hit:
        return json.loads(hit)
    params = {"name": city, "count": 10, "language": "en"}
    if state:   # US stadiums have a state; games abroad (London, Munich...) don't
        params["countryCode"] = "US"
    r = requests.get(GEO, params=params, headers=UA, timeout=20)
    r.raise_for_status()
    res = r.json().get("results") or []
    pick = None
    for x in res:
        if state and (x.get("admin1") or "").lower() == state.lower():
            pick = x
            break
    pick = pick or (res[0] if res else None)
    if not pick:
        return None
    ll = [pick["latitude"], pick["longitude"]]
    D.set_meta(con, key, json.dumps(ll))
    return ll


def update_weather(con, league, days=8):
    if league.key != "nfl":
        return 0
    now = now_utc()
    games = [dict(r) for r in con.execute(
        "SELECT * FROM games WHERE league=? AND completed=0 AND date_utc>=? AND date_utc<=?",
        (league.key, (now - timedelta(hours=3)).isoformat(), (now + timedelta(days=days)).isoformat()))]
    n = 0
    for g in games:
        if g.get("indoor") == 1 or not g.get("venue_city"):
            continue
        ll = _geocode(con, g["venue_city"], g.get("venue_state"))
        if not ll:
            continue
        r = requests.get(FCST, params={
            "latitude": ll[0], "longitude": ll[1], "timezone": "UTC", "forecast_days": 10,
            "hourly": "temperature_2m,precipitation_probability,precipitation,snowfall,wind_speed_10m,wind_gusts_10m",
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "precipitation_unit": "inch"},
            headers=UA, timeout=20)
        r.raise_for_status()
        h = r.json().get("hourly") or {}
        times = h.get("time") or []
        ko = parse_iso(g["date_utc"])
        want = [(ko + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00") for i in range(3)]   # the 3 game hours
        idx = [times.index(t) for t in want if t in times]
        if not idx:
            continue
        pick = lambda k, f=max: f((h.get(k) or [None])[i] or 0 for i in idx)
        avg = lambda k: sum((h.get(k) or [0])[i] or 0 for i in idx) / len(idx)
        con.execute("INSERT OR REPLACE INTO weather VALUES (?,?,?,?,?,?,?,?)",
                    (g["id"], avg("temperature_2m"), avg("wind_speed_10m"), pick("wind_gusts_10m"),
                     pick("precipitation_probability"), pick("precipitation"), pick("snowfall"),
                     now.isoformat(timespec="seconds")))
        n += 1
    D.set_meta(con, f"{league.key}:weather_pulled", json.dumps({"at": now.isoformat(timespec="seconds"), "games": n}))
    con.commit()
    return n


def adjustment(w):
    """(points off the total, short reasons) for one game's forecast row, or (0, [])."""
    if not w:
        return 0.0, []
    adj, why = 0.0, []
    wind = w.get("wind_mph") or 0
    if wind > 12:
        a = min(6.0, 0.3 * (wind - 12))
        adj -= a
        why.append(f"{wind:.0f} mph wind")
    snow = (w.get("snow_in") or 0) > 0.02
    rain = not snow and ((w.get("precip_in") or 0) >= 0.05 or (w.get("precip_prob") or 0) >= 60)
    if snow:
        adj -= 3.0
        why.append("snow")
    elif rain:
        adj -= 1.5
        why.append("rain")
    if (w.get("temp_f") if w.get("temp_f") is not None else 60) <= 20:
        adj -= 1.0
        why.append(f"{w['temp_f']:.0f}°F")
    return adj, why

#!/usr/bin/env python3
"""Irkutsk Flight Watch - early-warning monitor for flight disruption at Irkutsk (IKT).

Pulls scheduled arrivals/departures (with status) from AeroDataBox via RapidAPI,
stores a compact daily record per airport, compares the target airport against
its own trailing baseline AND against regional control airports, and pushes an
alert via ntfy.sh when Irkutsk diverges.

Standard library only. Commands:
  python monitor.py daily [--date YYYY-MM-DD]   fetch yesterday (Irkutsk local), analyse, alert
  python monitor.py backfill --days N           fetch missing past days to build the baseline
  python monitor.py intraday                    quick +/-6h check (if enabled in config.json)
  python monitor.py health                      per-airport data-feed health (free endpoint)
  python monitor.py analyze [--alert]           recompute from stored data, no API calls
  python monitor.py test-alert                  send a test push notification

Environment:
  RAPIDAPI_KEY         your RapidAPI key (required for real API calls)
  NTFY_TOPIC           your private ntfy topic name (alerts are printed if unset)
  NTFY_SERVER          optional, default https://ntfy.sh
  NTFY_TOKEN           optional, for a protected ntfy topic
  DASHBOARD_URL        optional, link attached to notifications
  FLIGHTWATCH_FAKE=1   use synthetic data instead of the API (for testing)
"""

import argparse
import datetime as dt
import json
import os
import random
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "docs" / "data"
DAYS_DIR = DATA_DIR / "days"
STATE_PATH = ROOT / "state" / "state.json"

API_HOST = "aerodatabox.p.rapidapi.com"
UNITS_PER_FIDS_CALL = 2
OPERATED = {"departed", "enroute", "approaching", "arrived", "diverted"}


# ---------------------------------------------------------------- utilities

def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj, compact=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, separators=(",", ":")) if compact else json.dumps(obj, indent=2)
    path.write_text(text + "\n", encoding="utf-8")


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def local_now(cfg, code):
    return utcnow() + dt.timedelta(hours=cfg["airports"][code]["utc_offset"])


def is_cancelled(status):
    return "cancel" in (status or "").lower()


def is_uncertain_cancel(status):
    return (status or "").lower() == "canceleduncertain"


# ------------------------------------------------------------------- budget

def budget_month(state):
    month = utcnow().strftime("%Y-%m")
    b = state.setdefault("budget", {})
    if b.get("month") != month:
        b.update({"month": month, "units_used": 0})
    return b


def can_spend(state, cfg, units):
    b = budget_month(state)
    return b["units_used"] + units <= cfg["monthly_unit_budget"]


# ---------------------------------------------------------------- API layer

class BudgetExceeded(Exception):
    pass


def api_get(path, params, state, cfg, units):
    """GET an AeroDataBox endpoint. Returns parsed JSON ({} for 204 No Content)."""
    if units and not can_spend(state, cfg, units):
        raise BudgetExceeded(f"monthly unit budget ({cfg['monthly_unit_budget']}) would be exceeded")
    key = os.environ.get("RAPIDAPI_KEY")
    if not key:
        sys.exit("RAPIDAPI_KEY is not set (or set FLIGHTWATCH_FAKE=1 to test with synthetic data)")
    url = f"https://{API_HOST}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "X-RapidAPI-Key": key,
        "X-RapidAPI-Host": API_HOST,
        "User-Agent": "irkutsk-flight-watch/1.0",
    })
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read()
                headers = {k.lower(): v for k, v in resp.headers.items() if k.lower().startswith("x-ratelimit")}
                if headers:
                    state["rate_limit_headers"] = headers
                if units:
                    budget_month(state)["units_used"] += units
                time.sleep(1.2)  # Basic plan allows 1 request/second
                return json.loads(body) if resp.status != 204 and body.strip() else {}
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            detail = e.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"HTTP {e.code} for {path}: {detail}") from None
        except urllib.error.URLError:
            if attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            raise


def fids_params():
    return {
        "withLeg": "false", "direction": "Both", "withCancelled": "true",
        "withCodeshared": "false", "withCargo": "false", "withPrivate": "false",
        "withLocation": "false",
    }


def fetch_fids_range(code, from_local, to_local, state, cfg):
    """from_local/to_local: 'YYYY-MM-DDTHH:MM' in the airport's local time (max 12h apart)."""
    if os.environ.get("FLIGHTWATCH_FAKE"):
        budget_month(state)["units_used"] += UNITS_PER_FIDS_CALL
        return fake_fids(code, from_local, cfg)
    path = f"/flights/airports/iata/{code}/{from_local}/{to_local}"
    return api_get(path, fids_params(), state, cfg, UNITS_PER_FIDS_CALL)


def parse_fids(data):
    """Turn an AeroDataBox FIDS response into compact, de-duplicated flight records."""
    out, seen = [], set()
    for direction, tag in (("departures", "D"), ("arrivals", "A")):
        for f in data.get(direction) or []:
            if f.get("isCargo") or f.get("codeshareStatus") == "IsCodeshared":
                continue
            mv = f.get("movement") or f.get("departure" if tag == "D" else "arrival") or {}
            sched = (mv.get("scheduledTime") or {}).get("utc") or mv.get("scheduledTimeUtc") or ""
            number = (f.get("number") or "").strip()
            key = (number, tag, sched)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "n": number,
                "d": tag,
                "t": sched,
                "s": f.get("status") or "Unknown",
                "o": (mv.get("airport") or {}).get("iata") or (mv.get("airport") or {}).get("name") or "",
                "al": (f.get("airline") or {}).get("name") or "",
            })
    return out


def summarize(code, date, flights):
    cancelled = [f for f in flights if is_cancelled(f["s"])]
    return {
        "airport": code,
        "date": date,
        "fetched_at": utcnow().isoformat(timespec="seconds"),
        "total": len(flights),
        "departures": sum(1 for f in flights if f["d"] == "D"),
        "arrivals": sum(1 for f in flights if f["d"] == "A"),
        "cancelled": len(cancelled),
        "cancelled_uncertain": sum(1 for f in cancelled if is_uncertain_cancel(f["s"])),
        "operated": sum(1 for f in flights if f["s"].lower() in OPERATED),
        "unconfirmed": sum(1 for f in flights if not is_cancelled(f["s"]) and f["s"].lower() not in OPERATED),
        "flights": flights,
    }


def day_path(code, date):
    return DAYS_DIR / code / f"{date}.json"


def fetch_day(code, date, state, cfg):
    """Fetch one full local day for an airport (two 12-hour windows = 4 units)."""
    if not can_spend(state, cfg, 2 * UNITS_PER_FIDS_CALL):
        raise BudgetExceeded(f"not enough budget left to fetch {code} {date}")
    flights = []
    for start, end in (("00:00", "11:59"), ("12:00", "23:59")):
        data = fetch_fids_range(code, f"{date}T{start}", f"{date}T{end}", state, cfg)
        flights.extend(parse_fids(data))
    # de-dup across the two windows
    uniq = {(f["n"], f["d"], f["t"]): f for f in flights}
    summary = summarize(code, date, sorted(uniq.values(), key=lambda f: f["t"]))
    save_json(day_path(code, date), summary, compact=True)
    print(f"  {code} {date}: {summary['total']} flights, {summary['cancelled']} cancelled")
    return summary


# ----------------------------------------------------------------- analysis

def load_history(code):
    hist = {}
    for p in sorted((DAYS_DIR / code).glob("*.json")):
        s = load_json(p, None)
        if s:
            s.pop("flights", None)
            hist[s["date"]] = s
    return hist


def cancel_rate(s):
    return s["cancelled"] / s["total"] if s["total"] else 0.0


def baseline_stats(history, date, th):
    prior = [history[d] for d in sorted(history) if d < date and history[d]["total"] > 0]
    prior = prior[-th["baseline_days"]:]
    if len(prior) < th["min_baseline_days"]:
        return None
    rates = [cancel_rate(p) for p in prior]
    med_rate = statistics.median(rates)
    return {
        "days": len(prior),
        "median_total": statistics.median(p["total"] for p in prior),
        "median_rate": med_rate,
        "mad_rate": statistics.median(abs(r - med_rate) for r in rates),
    }


def assess_day(summary, base, th):
    """Compare one airport-day with its own baseline. Returns flags + numbers."""
    if summary is None:
        return {"status": "missing", "flags": []}
    if base is None:
        return {"status": "learning", "flags": [], "total": summary["total"], "cancelled": summary["cancelled"]}
    total, cancelled = summary["total"], summary["cancelled"]
    rate = cancel_rate(summary)
    vol_ratio = total / base["median_total"] if base["median_total"] else None
    # noise floor: robust spread of past days, but never below binomial sampling error,
    # so 3 cancellations at a 24-flight airport isn't treated as a 10-sigma event
    p = max(base["median_rate"], 0.02)
    binom_se = (p * (1 - p) / max(total, 1)) ** 0.5
    z = (rate - base["median_rate"]) / max(1.4826 * base["mad_rate"], binom_se, 0.02)
    flags, severe = [], False
    if total == 0 and base["median_total"] >= 5:
        flags.append("no_flights")
        severe = True
    elif vol_ratio is not None and vol_ratio < th["volume_drop_ratio"]:
        flags.append("volume_drop")
        severe |= vol_ratio < th["severe_volume_drop_ratio"]
    if (cancelled >= th["min_cancellations"]
            and rate - base["median_rate"] >= th["cancel_rate_excess"]
            and z >= th["cancel_z"]):
        flags.append("cancel_spike")
        severe |= rate >= th["severe_cancel_rate"]
    return {
        "status": "anomalous" if flags else "normal",
        "flags": flags,
        "severe": severe,
        "total": total,
        "cancelled": cancelled,
        "cancel_rate": round(rate, 3),
        "baseline_total": base["median_total"],
        "baseline_rate": round(base["median_rate"], 3),
        "volume_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
        "cancel_z": round(z, 1),
    }


def corroborates(target, control):
    """Does a control airport show the same kind of disruption, at a comparable size?

    A control only 'explains away' an Irkutsk anomaly if it has the same flag and at
    least half the magnitude - a couple of cancellations at Ulan-Ude shouldn't
    downgrade a 40%-cancelled day at Irkutsk to 'probably weather'.
    """
    if "no_flights" in target["flags"]:
        return "no_flights" in control["flags"]
    if "cancel_spike" in target["flags"] and "cancel_spike" in control["flags"]:
        t_excess = target["cancel_rate"] - target["baseline_rate"]
        c_excess = control["cancel_rate"] - control["baseline_rate"]
        if c_excess >= 0.5 * t_excess:
            return True
    if "volume_drop" in target["flags"] and control.get("volume_ratio") is not None:
        t_drop = 1 - target["volume_ratio"]
        if 1 - control["volume_ratio"] >= 0.5 * t_drop:
            return True
    return False


def classify(target, controls, consecutive):
    """Combine the target's assessment with control airports into an alert level.

    Levels: learning | normal | watch (Irkutsk anomalous but the region is too,
    e.g. weather) | alert (Irkutsk-specific) | data (looks like a data problem).
    """
    if target["status"] in ("missing", "learning"):
        return {"level": target["status"], "priority": 0, "reason": "baseline still building" if target["status"] == "learning" else "no data for target"}
    if not target["flags"]:
        return {"level": "normal", "priority": 0, "reason": "within baseline"}
    usable = {c: a for c, a in controls.items() if a["status"] in ("normal", "anomalous")}
    disrupted = [c for c, a in usable.items() if corroborates(target, a)]
    regional = bool(usable) and len(disrupted) / len(usable) >= 0.5
    if "no_flights" in target["flags"]:
        if regional and all("no_flights" in usable[c]["flags"] for c in disrupted):
            return {"level": "data", "priority": 2, "reason": "no flights returned for any airport - probably an API/data gap"}
        return {"level": "alert", "priority": 5, "reason": "Irkutsk returned ZERO flights while controls look normal - schedule wiped or data gap; check manually"}
    if regional:
        return {"level": "watch", "priority": 3, "reason": f"Irkutsk anomalous but so are {', '.join(disrupted)} - likely regional (weather/airspace)"}
    priority = 5 if target["severe"] or consecutive >= 1 else 4
    note = f"; anomalous {consecutive + 1} days running" if consecutive else ""
    return {"level": "alert", "priority": priority, "reason": "Irkutsk-specific disruption, controls normal" + note}


def analyze_all(cfg, days_back=90):
    th = cfg["thresholds"]
    codes = list(cfg["airports"])
    target = cfg["target"]
    histories = {c: load_history(c) for c in codes}
    dates = sorted(histories[target])[-days_back:]
    results = []
    consecutive = 0
    for date in dates:
        assess = {c: assess_day(histories[c].get(date), baseline_stats(histories[c], date, th), th) for c in codes}
        verdict = classify(assess[target], {c: a for c, a in assess.items() if c != target}, consecutive)
        consecutive = consecutive + 1 if assess[target]["flags"] and verdict["level"] in ("alert", "watch") else 0
        results.append({"date": date, "verdict": verdict, "airports": assess})
    return results, histories


# ------------------------------------------------------------------ alerting

def notify(title, message, priority=3, tags=None):
    topic = os.environ.get("NTFY_TOPIC")
    print(f"[notify p{priority}] {title}\n{message}\n")
    if not topic:
        print("  (NTFY_TOPIC not set - printed only)")
        return
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {"Title": title.encode("ascii", "replace").decode(), "Priority": str(priority)}
    if tags:
        headers["Tags"] = ",".join(tags)
    if os.environ.get("DASHBOARD_URL"):
        headers["Click"] = os.environ["DASHBOARD_URL"]
    if os.environ.get("NTFY_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['NTFY_TOKEN']}"
    req = urllib.request.Request(f"{server}/{topic}", data=message.encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30):
        pass


def send_once(state, key, title, message, priority, tags):
    sent = state.setdefault("sent_alerts", {})
    if key in sent:
        print(f"  (already sent: {key})")
        return False
    notify(title, message, priority, tags)
    sent[key] = utcnow().isoformat(timespec="seconds")
    cutoff = (utcnow() - dt.timedelta(days=45)).isoformat()
    state["sent_alerts"] = {k: v for k, v in sent.items() if v >= cutoff}
    log = state.setdefault("alert_log", [])
    log.append({"at": sent[key], "key": key, "title": title, "priority": priority})
    state["alert_log"] = log[-50:]
    return True


def describe(code, a):
    if a["status"] in ("missing", "learning"):
        return f"{code}: {a['status']}"
    vr = f"{a['volume_ratio']:.0%}" if a.get("volume_ratio") is not None else "n/a"
    return (f"{code}: {a['total']} flights ({vr} of usual {a['baseline_total']:.0f}), "
            f"{a['cancelled']} cancelled ({a['cancel_rate']:.0%} vs usual {a['baseline_rate']:.0%})"
            + (f" [{', '.join(a['flags'])}]" if a["flags"] else ""))


def alert_for(result, cfg, state):
    v = result["verdict"]
    if v["level"] not in ("alert", "watch", "data"):
        return
    lines = [v["reason"], ""] + [describe(c, a) for c, a in result["airports"].items()]
    tags = {"alert": ["rotating_light", "airplane"], "watch": ["warning", "airplane"], "data": ["grey_question"]}[v["level"]]
    title = f"{v['level'].upper()}: Irkutsk flights {result['date']}"
    send_once(state, f"daily:{result['date']}:{v['level']}", title, "\n".join(lines), v["priority"], tags)


def heartbeat(result, results, cfg, state):
    week = results[-7:]
    target = cfg["target"]
    totals = [r["airports"][target].get("total", 0) for r in week]
    cancels = [r["airports"][target].get("cancelled", 0) for r in week]
    b = budget_month(state)
    msg = (f"Monitor alive. Last {len(week)} days at {target}: avg {statistics.mean(totals):.0f} flights/day, "
           f"{sum(cancels)} cancellations total. Latest status: {result['verdict']['level']}.\n"
           f"API units used this month: {b['units_used']}/{cfg['monthly_unit_budget']}.")
    send_once(state, f"heartbeat:{result['date']}", "Irkutsk flight watch: weekly check-in", msg, 1, ["white_check_mark"])


# ------------------------------------------------------------ dashboard data

def write_summary(cfg, results, histories, state):
    airports = {}
    by_date = {r["date"]: r for r in results}
    for code, meta in cfg["airports"].items():
        days = []
        for date in sorted(histories[code])[-90:]:
            s = histories[code][date]
            a = by_date.get(date, {}).get("airports", {}).get(code, {})
            days.append({k: s[k] for k in ("date", "total", "cancelled", "cancelled_uncertain", "operated", "unconfirmed")}
                        | {"flags": a.get("flags", []), "baseline_total": a.get("baseline_total"), "baseline_rate": a.get("baseline_rate")})
        airports[code] = {"name": meta["name"], "days": days}
    latest = results[-1] if results else None
    save_json(DATA_DIR / "summary.json", {
        "generated_at": utcnow().isoformat(timespec="seconds"),
        "target": cfg["target"],
        "latest": latest,
        "verdicts": [{"date": r["date"], **r["verdict"]} for r in results[-90:]],
        "airports": airports,
        "intraday": load_json(DATA_DIR / "intraday.json", None),
        "alert_log": state.get("alert_log", []),
        "budget": budget_month(state) | {"limit": cfg["monthly_unit_budget"]},
        "thresholds": cfg["thresholds"],
    })


# ------------------------------------------------------------------ commands

def cmd_daily(cfg, state, args):
    target = cfg["target"]
    date = args.date or (local_now(cfg, target).date() - dt.timedelta(days=1)).isoformat()
    print(f"Daily run for {date}")
    errors = []
    for code in cfg["airports"]:
        if day_path(code, date).exists() and not args.refetch:
            print(f"  {code} {date}: already stored")
            continue
        try:
            fetch_day(code, date, state, cfg)
        except BudgetExceeded as e:
            errors.append(f"{code}: {e}")
            print(f"  ! {e}")
        except Exception as e:  # keep going so one bad control doesn't block the target
            errors.append(f"{code}: {e}")
            print(f"  ! {code} failed: {e}")
    results, histories = analyze_all(cfg)
    write_summary(cfg, results, histories, state)
    today = next((r for r in results if r["date"] == date), None)
    if today:
        print(f"Verdict: {today['verdict']['level']} - {today['verdict']['reason']}")
        alert_for(today, cfg, state)
        if dt.date.fromisoformat(date).weekday() == cfg.get("heartbeat_weekday", 6):
            heartbeat(today, results, cfg, state)
    if errors:
        send_once(state, f"errors:{date}", "Irkutsk flight watch: fetch problems",
                  "\n".join(errors), 3 if any(e.startswith(target) for e in errors) else 2, ["warning"])
        if any(e.startswith(target) for e in errors):
            return 1
    return 0


def cmd_backfill(cfg, state, args):
    target = cfg["target"]
    end = local_now(cfg, target).date() - dt.timedelta(days=1)
    dates = [(end - dt.timedelta(days=i)).isoformat() for i in range(args.days, 0, -1)] + [end.isoformat()]
    missing = [(c, d) for d in dates for c in cfg["airports"] if not day_path(c, d).exists()]
    need = len(missing) * 2 * UNITS_PER_FIDS_CALL
    b = budget_month(state)
    print(f"Backfill: {len(missing)} airport-days missing, needs {need} units "
          f"({b['units_used']}/{cfg['monthly_unit_budget']} used this month)")
    for code, date in missing:
        try:
            fetch_day(code, date, state, cfg)
        except BudgetExceeded as e:
            print(f"  stopping: {e}")
            break
        except Exception as e:
            print(f"  ! {code} {date} failed: {e}")
    results, histories = analyze_all(cfg)
    write_summary(cfg, results, histories, state)
    return 0


def cmd_intraday(cfg, state, args):
    icfg = cfg.get("intraday", {})
    if not icfg.get("enabled") and not args.force:
        print("Intraday checks disabled in config.json (intraday.enabled = false); nothing to do.")
        return 0
    th = cfg["thresholds"]
    target = cfg["target"]
    out = {"checked_at": utcnow().isoformat(timespec="seconds"), "airports": {}}
    for code in icfg.get("airports", [target]):
        now = local_now(cfg, code)
        frm = (now - dt.timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M")
        to = (now + dt.timedelta(hours=6) - dt.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M")
        try:
            flights = parse_fids(fetch_fids_range(code, frm, to, state, cfg))
        except Exception as e:
            print(f"  ! {code} failed: {e}")
            continue
        base = baseline_stats(load_history(code), "9999-99-99", th)
        cancelled = sum(1 for f in flights if is_cancelled(f["s"]))
        rate = cancelled / len(flights) if flights else 0.0
        base_rate = base["median_rate"] if base else 0.05
        flagged = cancelled >= th["intraday_min_cancellations"] and rate - base_rate >= th["intraday_cancel_rate_excess"]
        out["airports"][code] = {"window": [frm, to], "total": len(flights), "cancelled": cancelled,
                                 "cancel_rate": round(rate, 3), "baseline_rate": round(base_rate, 3), "flagged": flagged,
                                 "cancelled_flights": [f for f in flights if is_cancelled(f["s"])][:30]}
        print(f"  {code}: {len(flights)} flights in window, {cancelled} cancelled" + (" [FLAGGED]" if flagged else ""))
    save_json(DATA_DIR / "intraday.json", out)
    t = out["airports"].get(target)
    if t and t["flagged"]:
        controls = {c: a for c, a in out["airports"].items() if c != target}
        regional = any(a["flagged"] for a in controls.values())
        level = "watch" if regional else "alert"
        lines = [("Regional disruption too - likely weather/airspace." if regional else
                  "Irkutsk-specific: controls not showing the same."), ""]
        lines += [f"{c}: {a['cancelled']}/{a['total']} cancelled ({a['cancel_rate']:.0%} vs usual {a['baseline_rate']:.0%})"
                  for c, a in out["airports"].items()]
        lines += ["", "Cancelled at IKT: " + ", ".join(f"{f['n']} {'to' if f['d'] == 'D' else 'from'} {f['o']}" for f in t["cancelled_flights"][:10])]
        bucket = t["cancelled"] // 5  # re-alert only if the count grows substantially
        send_once(state, f"intraday:{local_now(cfg, target).date()}:{level}:{bucket}",
                  f"{level.upper()} (live): Irkutsk cancellations", "\n".join(lines),
                  4 if level == "alert" else 3, ["rotating_light" if level == "alert" else "warning", "airplane"])
    results, histories = analyze_all(cfg)
    write_summary(cfg, results, histories, state)
    return 0


def cmd_health(cfg, state, args):
    out = {"checked_at": utcnow().isoformat(timespec="seconds"), "airports": {}}
    for code, meta in cfg["airports"].items():
        try:
            out["airports"][code] = api_get(f"/health/services/airports/{meta['icao']}/feeds", None, state, cfg, 0)
        except Exception as e:
            out["airports"][code] = {"error": str(e)}
        print(f"{code} ({meta['icao']}): {json.dumps(out['airports'][code], indent=2)}")
    save_json(DATA_DIR / "health.json", out)
    return 0


def cmd_analyze(cfg, state, args):
    results, histories = analyze_all(cfg)
    write_summary(cfg, results, histories, state)
    for r in results[-14:]:
        v = r["verdict"]
        print(f"{r['date']}  {v['level']:<9} {v['reason']}")
    if args.alert and results:
        alert_for(results[-1], cfg, state)
    return 0


def cmd_test_alert(cfg, state, args):
    notify("Irkutsk flight watch: test", "If you can read this on your phone, alerts are working.", 3, ["test_tube"])
    return 0


# --------------------------------------------------------- synthetic data

def fake_fids(code, from_local, cfg):
    """Deterministic synthetic FIDS response for testing without an API key.

    FLIGHTWATCH_FAKE_SCENARIO = normal | ikt_cancel | ikt_volume | regional
    FLIGHTWATCH_FAKE_EVENT_DATE = YYYY-MM-DD (scenario applies on/after this date)
    """
    rnd = random.Random(f"{code}|{from_local}")
    date = from_local[:10]
    scenario = os.environ.get("FLIGHTWATCH_FAKE_SCENARIO", "normal")
    event = date >= os.environ.get("FLIGHTWATCH_FAKE_EVENT_DATE", "9999-12-31")
    per_window = {"IKT": 32, "KJA": 38, "UUD": 12, "OVB": 60}.get(code, 20)
    n = max(0, per_window + rnd.randint(-3, 3))
    cancel_p = 0.03
    if event and scenario == "ikt_volume" and code == cfg["target"]:
        n //= 2
    if event and scenario == "ikt_cancel" and code == cfg["target"]:
        cancel_p = 0.4
    if event and scenario == "regional":
        cancel_p = 0.3
    offset = cfg["airports"][code]["utc_offset"]
    start = dt.datetime.fromisoformat(from_local)
    resp = {"departures": [], "arrivals": []}
    for i in range(n):
        local = start + dt.timedelta(minutes=rnd.randint(0, 719))
        utc = local - dt.timedelta(hours=offset)
        r = rnd.random()
        dep = i % 2 == 0
        status = ("Canceled" if r < cancel_p else "Unknown" if r < cancel_p + 0.02 else
                  "Departed" if dep else "Arrived")
        resp["departures" if dep else "arrivals"].append({
            "number": f"{rnd.choice(['S7', 'SU', 'U6', 'IO', '2G'])} {rnd.randint(100, 999)}",
            "status": status,
            "codeshareStatus": "IsOperator",
            "isCargo": False,
            "airline": {"name": "Synthetic Air"},
            "movement": {"airport": {"iata": rnd.choice(["SVO", "DME", "OVB", "VVO", "KHV", "PEK"])},
                         "scheduledTime": {"utc": utc.strftime("%Y-%m-%d %H:%MZ"), "local": local.strftime("%Y-%m-%d %H:%M")}},
        })
    return resp


# ------------------------------------------------------------------ main

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("daily")
    d.add_argument("--date")
    d.add_argument("--refetch", action="store_true")
    b = sub.add_parser("backfill")
    b.add_argument("--days", type=int, default=14)
    i = sub.add_parser("intraday")
    i.add_argument("--force", action="store_true")
    sub.add_parser("health")
    a = sub.add_parser("analyze")
    a.add_argument("--alert", action="store_true")
    sub.add_parser("test-alert")
    args = p.parse_args(argv)

    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        sys.exit(f"cannot read {CONFIG_PATH}")
    state = load_json(STATE_PATH, {})
    commands = {"daily": cmd_daily, "backfill": cmd_backfill, "intraday": cmd_intraday,
                "health": cmd_health, "analyze": cmd_analyze, "test-alert": cmd_test_alert}
    try:
        return commands[args.cmd](cfg, state, args)
    finally:
        save_json(STATE_PATH, state)


if __name__ == "__main__":
    sys.exit(main())

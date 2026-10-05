#!/usr/bin/env python3
"""
Separat, snäll kontroll av Proshop (1 anrop per timme).

Skillnader mot bevaka.py:
  - Presenterar sig ärligt (User-Agent säger vad det är) i stället för att
    se ut som en webbläsare, och använder inte curl_cffi.
  - Läser robots.txt först och slutar om sidan är avstängd för automatiska anrop.
  - Försöker INTE ta sig förbi någon blockering. Får vi 403 loggas det bara,
    så du ser när och om Proshop säger nej.

Resultatet loggas i proshop_logg.csv (tid, resultat). Notiser skickas av samma
logik som bevaka.py (ntfy via secret NTFY_TOPIC).
"""
from __future__ import annotations

import csv
import os
import sys
import urllib.error
import urllib.request
import urllib.robotparser
from datetime import datetime

import bevaka

URL = "https://www.proshop.se/Spelkonsol/Sony-PlayStation-5-Pro/3417363"
WATCH = "inte emot beställningar"
UA = "ps5-bevakning/1.0 (personlig lagerbevakning, 1 anrop/timme)"

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "proshop_state.json")
LOG = os.path.join(HERE, "proshop_logg.csv")


def honest_fetch(url: str, timeout: int = 25):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "text/html", "Accept-Language": "sv-SE,sv;q=0.9"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, bevaka._decode(r.read(), r.headers)
    except urllib.error.HTTPError as e:
        try:
            body = bevaka._decode(e.read(), e.headers)
        except Exception:
            body = ""
        return e.code, body
    except Exception as e:
        print(f"  nätverksfel: {e}", file=sys.stderr)
        return 0, ""


def allowed_by_robots(url: str) -> bool:
    status, body = honest_fetch("https://www.proshop.se/robots.txt")
    if status != 200:
        return True  # ingen robots.txt att följa
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(body.splitlines())
    return rp.can_fetch(UA, url)


def log(result: str, detail: str = "") -> None:
    new = not os.path.exists(LOG)
    with open(LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["tid", "resultat", "detalj"])
        w.writerow([datetime.now(bevaka.TZ).strftime("%Y-%m-%d %H:%M"), result, detail])


def main() -> int:
    if not allowed_by_robots(URL):
        log("robots.txt förbjuder", "kontrollen stoppad")
        print("robots.txt tillåter inte automatiska anrop – avbryter")
        return 0

    bevaka.fetch = honest_fetch  # check_generic använder bevaka.fetch
    items = [{"name": "Proshop", "url": URL, "watch": WATCH}]
    before = bevaka.load_state(STATE)
    state = bevaka.run(items, before, pause=0)
    s = state.get(URL, {})
    if s.get("fails", 0):
        log("blockerad/fel", str(s.get("error", "")))
    else:
        log(bevaka.LABEL[s.get("status", 0)], s.get("detail", ""))
    bevaka.save_state(state, STATE)
    return 0


if __name__ == "__main__":
    sys.exit(main())

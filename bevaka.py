#!/usr/bin/env python3
"""
PS5 Pro-bevakning för svenska butiker.

Läser produktsidorna i bevaka.txt, avgör lagerstatus och skickar en pushnotis
till iPhone (via ntfy och/eller Telegram) när något blir bättre:

  I LAGER       – går att köpa nu
  FÖRBOKA       – går att beställa/förboka
  LEVERANS      – butiken visar ett datum för nästa leverans / bekräftad inleverans
  ÄNDRAD        – butikens "slut"-text har försvunnit (kolla sidan!)
  SLUT          – inget av ovan

Körs var 10:e minut av GitHub Actions (se .github/workflows/bevaka.yml),
men går också att köra lokalt:  python3 bevaka.py   (eller --test-notis)

Bara Pythons standardbibliotek krävs. Om paketet curl_cffi finns installerat
används det för att se ut som en vanlig Chrome-webbläsare, vilket gör att
färre butiker blockerar kontrollen.
"""

from __future__ import annotations

import gzip
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zlib
from datetime import date, datetime, timezone

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Europe/Stockholm")
except Exception:  # pragma: no cover - saknar tidszonsdata
    TZ = timezone.utc

try:  # valfritt: bättre "webbläsar-fingeravtryck" mot botskydd
    from curl_cffi import requests as cffi_requests  # type: ignore
except Exception:  # pragma: no cover
    cffi_requests = None

HERE = os.path.dirname(os.path.abspath(__file__))
LIST_FILE = os.path.join(HERE, "bevaka.txt")
STATE_FILE = os.path.join(HERE, "state.json")
STATUS_FILE = os.path.join(HERE, "STATUS.md")

# Antal misslyckade kontroller i rad innan du får en (enda) varning om att en
# butik inte går att läsa. Med körning var 10:e minut ≈ 1 timme.
FAIL_WARN_AFTER = 6

# --- Status --------------------------------------------------------------------
OUT, CHANGED, INCOMING, ORDERABLE, IN_STOCK = 0, 1, 2, 3, 4
LABEL = {
    OUT: "SLUT",
    CHANGED: "ÄNDRAD",
    INCOMING: "LEVERANS",
    ORDERABLE: "FÖRBOKA",
    IN_STOCK: "I LAGER",
}
EMOJI = {OUT: "⚪", CHANGED: "👀", INCOMING: "🚚", ORDERABLE: "🟡", IN_STOCK: "🟢"}

# schema.org/availability -> status
AVAILABILITY = {
    "instock": IN_STOCK,
    "limitedavailability": IN_STOCK,
    "onlineonly": IN_STOCK,
    "instoreonly": IN_STOCK,
    "in stock": IN_STOCK,  # Open Graph-varianter
    "available for order": ORDERABLE,
    "preorder": ORDERABLE,
    "presale": ORDERABLE,
    "backorder": ORDERABLE,
    "outofstock": OUT,
    "out of stock": OUT,
    "soldout": OUT,
    "discontinued": OUT,
    "pending": OUT,
}
AV_SV = {
    "instock": "i lager", "in stock": "i lager", "limitedavailability": "få kvar",
    "onlineonly": "i lager (bara online)", "instoreonly": "i lager (bara i butik)",
    "available for order": "går att beställa", "preorder": "förbokning öppen",
    "presale": "förköp öppet", "backorder": "går att beställa (restorder)",
    "outofstock": "slut", "out of stock": "slut", "soldout": "slutsåld",
    "discontinued": "utgått", "pending": "inte släppt",
}

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
    "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
}

BLOCK_MARKERS = (
    "just a moment...",
    "cf-chl-",
    "challenge-platform",
    "attention required! | cloudflare",
    "access denied",
    "are you a robot",
    "captcha-delivery",
    "px-captcha",
    "request unsuccessful. incapsula",
)

# Text som måste finnas på sidan för att vi ska lita på att det är rätt
# produktsida (och inte en felsida/botvägg) innan "slut-texten är borta" larmar.
PRODUCT_HINT = re.compile(r"(ps5|playstation\W{0,3}5)\W{0,3}pro", re.I)

# --- Leveransdatum i sidtext ---------------------------------------------------
_MONTHS = r"(?:jan|feb|mar|apr|maj|jun|jul|aug|sep|okt|nov|dec)[a-zåäö]*\.?"
_DATE = (
    r"(?:20\d\d-\d\d-\d\d"  # 2026-10-15
    r"|\d{1,2}/\d{1,2}(?:/\d{2,4})?"  # 15/10 eller 15/10/2026
    r"|\d{1,2}\s+" + _MONTHS + r"(?:\s+20\d\d)?"  # 15 oktober (2026)
    r"|(?:vecka|v\.?)\s?\d{1,2})"  # vecka 42 / v.42
)
_KEYWORDS = (
    r"(?:förväntas|beräknas|beräknad|väntas|preliminär\w*|nästa\s+leverans\w*"
    r"|leveransdatum|inleverans\w*|inkommande|åter\s+i\s+lager|lagerdatum"
    r"|tillbaka\s+i\s+lager|i\s+lager\s+igen)"
)
DELIVERY_RE = re.compile(_KEYWORDS + r"[^0-9<>]{0,45}?(" + _DATE + r")", re.I)
# Samma sak i inbäddad JSON (t.ex. Next.js-data)
JSON_DATE_RE = re.compile(
    r'"(?:nextDeliveryDate|expectedDeliveryDate|expectedStockDate|restockDate'
    r'|incomingDate|nextIncomingDate|expectedInStockDate)"\s*:\s*"(20\d\d-\d\d-\d\d)',
    re.I,
)


# ==============================================================================
# Hämtning
# ==============================================================================
def fetch(url: str, timeout: int = 25) -> tuple[int, str]:
    """Returnerar (http-status, text). Status 0 = nätverksfel."""
    if cffi_requests is not None:
        try:
            r = cffi_requests.get(
                url,
                impersonate="chrome",
                timeout=timeout,
                headers={"Accept-Language": HEADERS["Accept-Language"]},
            )
            return r.status_code, r.text
        except Exception as e:  # faller tillbaka på urllib
            print(f"  curl_cffi-fel ({e}), provar urllib", file=sys.stderr)

    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _decode(resp.read(), resp.headers)
    except urllib.error.HTTPError as e:
        try:
            body = _decode(e.read(), e.headers)
        except Exception:
            body = ""
        return e.code, body
    except Exception as e:
        print(f"  nätverksfel: {e}", file=sys.stderr)
        return 0, ""


def _decode(raw: bytes, headers) -> str:
    enc = (headers.get("Content-Encoding") or "").lower()
    if "gzip" in enc:
        raw = gzip.decompress(raw)
    elif "deflate" in enc:
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    charset = headers.get_content_charset() if hasattr(headers, "get_content_charset") else None
    return raw.decode(charset or "utf-8", errors="replace")


# ==============================================================================
# Tolkning
# ==============================================================================
def looks_blocked(status: int, body: str) -> bool:
    if status in (0, 401, 403, 407, 429) or status >= 500:
        return True
    head = body[:6000].lower()
    return any(m in head for m in BLOCK_MARKERS)


def visible_text(body: str) -> str:
    body = re.sub(r"(?is)<(script|style|noscript|template)\b.*?</\1>", " ", body)
    body = re.sub(r"(?s)<!--.*?-->", " ", body)
    body = re.sub(r"<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", html.unescape(body)).strip()


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def structured_availability(body: str) -> tuple[int | None, str, str]:
    """Läser schema.org (JSON-LD/microdata) och Open Graph.
    Returnerar (status eller None, rå availability-text, pris)."""
    found: list[tuple[int, str]] = []
    price = ""

    # JSON-LD: använd BARA själva produkten (inte relaterade/liknande produkter,
    # tillbehör eller andra konsoler som butiken listar på samma sida).
    products = []
    for m in re.finditer(
        r'(?is)<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', body
    ):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        for node in _walk(data):
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if "Product" in types or "ProductGroup" in types:
                products.append(node)
    named = [p for p in products if PRODUCT_HINT.search(str(p.get("name", "")))]
    chosen = named[0] if named else (products[0] if len(products) == 1 else None)
    if chosen is not None:
        for node in _walk(chosen.get("offers")):
            av = node.get("availability")
            if isinstance(av, str):
                key = av.rstrip("/").rsplit("/", 1)[-1].strip().lower()
                if key in AVAILABILITY:
                    found.append((AVAILABILITY[key], key))
                    if not price and node.get("price") not in (None, ""):
                        price = _fmt_price(node.get("price"), node.get("priceCurrency"))

    # microdata (bara om JSON-LD inte gav något; bara första träffen = huvudprodukten)
    if not found:
        m = re.search(
            r'itemprop=["\']availability["\'][^>]*?(?:href|content)=["\']([^"\']+)', body, re.I
        )
        if m:
            key = m.group(1).rstrip("/").rsplit("/", 1)[-1].strip().lower()
            if key in AVAILABILITY:
                found.append((AVAILABILITY[key], key))
    # Open Graph: <meta property="product:availability" content="in stock">
    for m in re.finditer(
        r'property=["\'](?:product|og):availability["\'][^>]*content=["\']([^"\']+)', body, re.I
    ):
        key = m.group(1).strip().lower()
        if key in AVAILABILITY:
            found.append((AVAILABILITY[key], key))
    if not found:
        return None, "", price
    best = max(found, key=lambda t: t[0])
    return best[0], best[1], price


def _fmt_price(value, currency) -> str:
    try:
        n = float(str(value).replace(",", "."))
        s = f"{n:,.0f}".replace(",", " ")
        return f"{s} kr" if (currency or "SEK").upper() == "SEK" else f"{s} {currency}"
    except Exception:
        return str(value)


def find_delivery(text: str, raw: str) -> tuple[str, str]:
    """Returnerar (textutdrag att visa, själva datumet som jämförelsenyckel)."""
    m = DELIVERY_RE.search(text)
    if m:
        snippet = text[m.start() : m.end()].strip()
        snippet = snippet[:1].upper() + snippet[1:]
        return _mark_past(m.group(1), snippet), m.group(1).lower()
    m = JSON_DATE_RE.search(raw)
    if m:
        return _mark_past(m.group(1), f"leveransdatum {m.group(1)}"), m.group(1)
    return "", ""


def _mark_past(datestr: str, snippet: str) -> str:
    try:
        d = date.fromisoformat(datestr)
        if d < datetime.now(TZ).date():
            return snippet + " (datumet har passerat)"
    except ValueError:
        pass
    return snippet


def check_generic(url: str, watch: str) -> dict:
    status, body = fetch(url)
    if looks_blocked(status, body):
        why = "nätverksfel" if not status else ("botskydd" if status == 200 else f"HTTP {status}")
        return {"ok": False, "error": why}

    text = visible_text(body)
    st, raw_av, price = structured_availability(body)
    delivery, key = find_delivery(text, body)
    is_product_page = bool(PRODUCT_HINT.search(text) or PRODUCT_HINT.search(body[:200000]))
    watch_gone = bool(watch) and is_product_page and watch.lower() not in text.lower()

    status_ = OUT if st is None else st
    detail = []
    if raw_av:
        detail.append(f"butiken anger: {AV_SV.get(raw_av, raw_av)}")
    if delivery and status_ < INCOMING:
        status_ = INCOMING
    if delivery:
        detail.append(delivery)
    if watch_gone and status_ < CHANGED:
        status_ = CHANGED
        detail.append(f'texten "{watch}" finns inte längre på sidan')
    if price:
        detail.append(price)
    if st is None and not delivery and not watch:
        detail.append("hittade ingen lagerinfo – lägg till en slut-text i bevaka.txt")
    return {"ok": True, "status": status_, "detail": " · ".join(detail), "key": key}


def check_webhallen(url: str, watch: str) -> dict:
    m = re.search(r"/product/(\d+)", url)
    if not m:
        return check_generic(url, watch)
    status, body = fetch(f"https://www.webhallen.com/api/product/{m.group(1)}")
    if status != 200:
        return check_generic(url, watch)
    try:
        product = json.loads(body).get("product", {})
        stock = product.get("stock") or {}
    except Exception:
        return check_generic(url, watch)

    def num(v) -> int:
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    web = num(stock.get("web"))
    stores = sum(num(v) for k, v in stock.items() if str(k).isdigit())
    inc = stock.get("incoming") or {}
    inc_qty, inc_date, inc_conf = num(inc.get("quantity")), inc.get("deliveryDate"), bool(inc.get("confirmed"))

    price = ""
    p = product.get("price") or {}
    if isinstance(p, dict) and p.get("price"):
        price = _fmt_price(p.get("price"), "SEK")

    detail = []
    if web > 0:
        st = IN_STOCK
        detail.append(f"{web} st i webblager")
    elif stores > 0:
        st = IN_STOCK
        detail.append(f"{stores} st i butik (ej webblager)")
    elif inc_conf or inc_date:
        st = INCOMING
        d = str(inc_date)[:10] if inc_date else "datum ej satt"
        detail.append(f"bekräftad inleverans: {inc_qty} st, {d}" if inc_conf else f"inleverans {d}, {inc_qty} st")
    else:
        st = OUT
        if inc_qty:
            detail.append(f"{inc_qty} st inkommande (ej bekräftat)")
    if price:
        detail.append(price)
    key = f"{str(inc_date)[:10] if inc_date else ''}|{inc_conf}" if st == INCOMING else ""
    return {"ok": True, "status": st, "detail": " · ".join(detail), "key": key}


def check(url: str, watch: str) -> dict:
    if "webhallen.com" in url:
        return check_webhallen(url, watch)
    return check_generic(url, watch)


# ==============================================================================
# Notiser
# ==============================================================================
def notify(title: str, message: str, url: str = "", priority: int = 3, tags=None) -> None:
    sent = False
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic:
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        payload = {"topic": topic, "title": title, "message": message, "priority": priority}
        if url:
            payload["click"] = url
            payload["actions"] = [{"action": "view", "label": "Öppna butiken", "url": url}]
        if tags:
            payload["tags"] = tags
        headers = {"Content-Type": "application/json"}
        if os.environ.get("NTFY_TOKEN"):
            headers["Authorization"] = "Bearer " + os.environ["NTFY_TOKEN"]
        sent |= _post(server, json.dumps(payload).encode(), headers, "ntfy")

    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    if token and chat:
        text = f"{title}\n{message}" + (f"\n{url}" if url else "")
        body = json.dumps({"chat_id": chat, "text": text, "disable_web_page_preview": True}).encode()
        sent |= _post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            body,
            {"Content-Type": "application/json"},
            "Telegram",
        )

    if not sent:
        print(f"[NOTIS – ingen kanal konfigurerad]\n  {title}\n  {message}\n  {url}")


def _post(url: str, data: bytes, headers: dict, name: str) -> bool:
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = 200 <= r.status < 300
    except Exception as e:
        print(f"  kunde inte skicka via {name}: {e}", file=sys.stderr)
        return False
    print(f"  notis skickad via {name}")
    return ok


# ==============================================================================
# Lista, state och status-fil
# ==============================================================================
def read_list(path: str = LIST_FILE) -> list[dict]:
    items = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 2 or not parts[1].startswith("http"):
                print(f"  hoppar över rad (fel format): {line}", file=sys.stderr)
                continue
            items.append({"name": parts[0], "url": parts[1], "watch": parts[2] if len(parts) > 2 else ""})
    return items


def load_state(path: str = STATE_FILE) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        return {}


def save_state(state: dict, path: str = STATE_FILE) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def write_status(items: list[dict], state: dict, path: str = STATUS_FILE) -> None:
    rows = ["# PS5 Pro – lagerstatus", "", "Uppdateras automatiskt när något ändras.", ""]
    rows += ["| | Butik | Status | Detaljer | Sedan |", "|---|---|---|---|---|"]
    for it in items:
        s = state.get(it["url"], {})
        st = s.get("status", OUT)
        flag = " ⚠️ kan inte läsas" if s.get("fails", 0) >= FAIL_WARN_AFTER else ""
        rows.append(
            f"| {EMOJI[st]} | [{it['name']}]({it['url']}) | {LABEL[st]}{flag} | "
            f"{(s.get('detail') or '').replace('|', '/')} | {s.get('since', '')} |"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(rows) + "\n")


# ==============================================================================
# Huvudlogik
# ==============================================================================
def run(items: list[dict], state: dict | None, checker=check, notifier=notify, pause: float = 1.5) -> dict:
    first_run = state is None
    state = dict(state or {})
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    summary = []

    for i, it in enumerate(items):
        if i and pause:
            time.sleep(pause)
        url, name = it["url"], it["name"]
        prev = state.get(url)
        print(f"- {name}: ", end="", flush=True)
        try:
            res = checker(url, it["watch"])
        except Exception as e:  # en trasig butik ska inte stoppa resten
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}

        if not res["ok"]:
            s = dict(prev or {"status": OUT, "detail": "", "since": now})
            s["fails"] = s.get("fails", 0) + 1
            print(f"FEL ({res['error']}), {s['fails']} i rad")
            if s["fails"] == FAIL_WARN_AFTER and not first_run:
                notifier(
                    f"⚠️ Kan inte läsa {name}",
                    f"Sidan har inte gått att läsa på ~{FAIL_WARN_AFTER * 10} min ({res['error']}). "
                    "Butiken blockerar troligen automatiska kontroller – använd butikens "
                    "'Meddela mig'-knapp eller Prisjakt för just den.",
                    url,
                    priority=2,
                    tags=["warning"],
                )
            state[url] = s
            summary.append(f"❔ {name}: kunde inte läsas")
            continue

        new_st, detail, key = res["status"], res["detail"], res.get("key", "")
        print(f"{LABEL[new_st]} {('– ' + detail) if detail else ''}")
        old_st = prev.get("status", OUT) if prev else OUT
        old_key = prev.get("key", "") if prev else ""

        better = new_st > old_st and new_st >= CHANGED
        new_date = (
            new_st == old_st == INCOMING and key != old_key and "datumet har passerat" not in detail
        )
        if not first_run and (better or new_date):
            if new_st >= ORDERABLE:
                title, prio, tags = f"🚨 PS5 Pro {LABEL[new_st]} hos {name}!", 5, ["rotating_light"]
            elif new_st == INCOMING:
                title = f"🚚 {name}: {'nytt leveransdatum' if new_date else 'leverans på väg'}"
                prio, tags = 4, ["truck"]
            else:
                title, prio, tags = f"👀 {name}: sidan har ändrats", 4, ["eyes"]
            notifier(title, detail or "Kolla sidan nu.", url, priority=prio, tags=tags)

        since = prev.get("since", now) if prev and new_st == old_st else now
        state[url] = {"name": name, "status": new_st, "detail": detail, "key": key, "since": since, "fails": 0}
        summary.append(f"{EMOJI[new_st]} {name}: {LABEL[new_st]}" + (f" ({detail})" if detail else ""))

    if first_run:
        best = max((state[i["url"]].get("status", OUT) for i in items), default=OUT)
        notifier(
            "✅ PS5 Pro-bevakningen är igång",
            "\n".join(summary) + "\n\nDu får en notis när något ändras.",
            priority=5 if best >= ORDERABLE else 3,
            tags=["white_check_mark"],
        )

    # rensa bort butiker som tagits bort ur listan
    keep = {i["url"] for i in items}
    return {k: v for k, v in state.items() if k in keep}


def main() -> int:
    if "--test-notis" in sys.argv:
        notify("🔔 Testnotis", "Funkar! Så här kommer PS5 Pro-larmen att se ut.",
               "https://www.prisjakt.nu", priority=5, tags=["bell"])
        return 0
    items = read_list()
    if not items:
        print("bevaka.txt är tom")
        return 1
    print(f"Kollar {len(items)} sidor{' (med curl_cffi)' if cffi_requests else ''}…")
    state = run(items, load_state())
    save_state(state)
    write_status(items, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())

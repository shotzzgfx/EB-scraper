import csv
import random
import json
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from curl_cffi import requests as cffiRequests
from bs4 import BeautifulSoup

URLS_FILE = "urls.txt"
OUTPUT_CSV = "listing_data.csv"
DELAY = 2.0
CHROME_VERSIONS = [149, 148, 147, 146]
MAC_VERSIONS = ["10_15_7", "12_6", "13_5", "14_2", "14_4"]
FIELDNAMES = [
    "Listing Title", "Sold Value", "Sold (OutOfStock)", "Seller Name",
    "Years Fit", "Vehicle Make", "Vehicle Model",
    "OEM Part Number", "Interchange Part Number", "Other Part Number",
    "PartOut ID", "Manufacturer Part Number",
    "Seller Notes", "DescriptionPartCondition", "Part Condition",
    "Listing URL", "Description URL",
    "Shipping Method", "Shipping Cost", "Shipping Days", "Item Location",
    "Listing Description",
]

session = None

def makeHeaders() -> dict:
    chrome = random.choice(CHROME_VERSIONS)
    mac = random.choice(MAC_VERSIONS)

    ua = (
        f"Mozilla/5.0 (Macintosh; Intel Mac OS X {mac}) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{chrome}.0.0.0 Safari/537.36"
    )

    return {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "accept-encoding": "gzip, deflate, br, zstd",
        "accept-language": "en-GB,en;q=0.5",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "priority": "u=0, i",
        "sec-ch-ua": f'"Brave";v="{chrome}", "Chromium";v="{chrome}", "Not)A;Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
        "sec-gpc": "1",
        "upgrade-insecure-requests": "1",
        "user-agent": ua,
    }

def fetchListing(url: str) -> tuple[str, str]:
    resp = session.get(url, impersonate="chrome")
    resp.raise_for_status()

    html = resp.text
    m = re.search(r'<iframe[^>]+id=["\']?desc_ifr["\']?[^>]+src=([^\s>]+)', html)

    if not m:
        m = re.search(r'src=([^\s>]+)[^>]*id=["\']?desc_ifr["\']?', html)

    descUrl = m.group(1).strip("\"'") if m else ""

    return html, descUrl

def fetchDescription(descUrl: str) -> str:
    if not descUrl:
        return ""

    resp = session.get(descUrl, impersonate="chrome")
    resp.raise_for_status()

    return resp.text

def fetchShipping(itemId: str) -> dict:
    url = (
        f"https://www.ebay.com/itemmodules/{itemId}?module_groups=GET_RATES_MODAL&co=0&isGetRates=1&rt=nc&quantity=&shipToCountryCode=USA&shippingZipCode=10001"
    )

    resp = session.get(url, impersonate="chrome")
    resp.raise_for_status()

    return resp.json()

def getSpecifics(soup: BeautifulSoup) -> dict:
    labels = soup.select(".ux-labels-values__labels")
    values = soup.select(".ux-labels-values__values")

    out = {}
    for lbl, val in zip(labels, values):
        key = lbl.get_text(strip=True).rstrip(":").strip()
        if not key:
            continue

        if key.lower() == "condition":
            firstSpan = val.select_one("span")
            raw = firstSpan.get_text(strip=True) if firstSpan else val.get_text(strip=True)
            out[key] = re.split(r"[:\u2013\u2014]|\s{2,}", raw)[0].strip()
        else:
            out[key] = val.get_text(strip=True)

    return out

def findSpecific(specifics: dict, *candidates: str) -> str:
    def norm(s):
        return re.sub(r"[\s_/]", "", s).lower()

    index = {norm(k): v for k, v in specifics.items()}

    for c in candidates:
        hit = index.get(norm(c))
        if hit:
            return hit

    return ""

def estimateToDays(raw: str) -> str:
    today = date.today()
    dateStrs = re.findall(r"[A-Za-z]+,\s+[A-Za-z]+\s+\d+", raw)

    days = []
    for ds in dateStrs:
        for fmt in ("%a, %b %d %Y", "%A, %B %d %Y"):
            try:
                parsed = datetime.strptime(f"{ds} {today.year}", fmt)
                diff = (parsed.date() - today).days

                if diff < 0:
                    parsed = datetime.strptime(f"{ds} {today.year + 1}", fmt)
                    diff = (parsed.date() - today).days

                days.append(diff)
                break
            except ValueError:
                pass

    if len(days) == 2:
        return f"{days[0]}-{days[1]} days"
    if len(days) == 1:
        return f"{days[0]} days"

    return raw

def parseShippingJson(data: dict) -> dict:
    try:
        items = (data["states"][0]["state"]["model"]
                 ["SHIPPING_SECTION_MODULE"]["sections"]["shipping"]["dataItems"])
    except (KeyError, IndexError) as exc:
        raise ValueError(f"unexpected JSON structure: {exc}") from exc

    def spans(obj):
        return "".join(s.get("text", "") for s in obj.get("textSpans", []))

    dv = items.get("deliveryto", {})
    vals = dv.get("values", [])
    method = spans(vals[0]) if len(vals) > 0 else ""
    estRaw = spans(vals[1]) if len(vals) > 1 else ""
    costRaw = spans(vals[2]) if len(vals) > 2 else ""

    if re.search(r"free", costRaw, re.I):
        cost = "Free"
    else:
        m = re.search(r"US\s*\$[\d,.]+", costRaw)
        cost = m.group(0) if m else costRaw

    locVals = items.get("itemLocation", {}).get("values", [])
    location = spans(locVals[0]) if locVals else ""

    return {
        "shippingMethod": method,
        "shippingCost": cost,
        "shippingDays": estimateToDays(estRaw),
        "itemLocation": location,
    }

def parseDescription(descHtml: str) -> str:
    if not descHtml:
        return ""

    soup = BeautifulSoup(descHtml, "html.parser")
    container = soup.select_one(".x-item-description-child")

    if not container:
        for tag in soup(["script", "style", "head"]):
            tag.decompose()
        return re.sub(r"\n{3,}", "\n\n", soup.get_text(separator="\n", strip=True)).strip()

    for sel in ["#content1", "#content2", "#content3", "#content4", "#content5",
                ".estorepower-header", ".estorepower-footer", ".footer-mob"]:
        for el in container.select(sel):
            el.decompose()

    for tag in container(["script", "style"]):
        tag.decompose()

    return re.sub(r"\n{3,}", "\n\n", container.get_text(separator="\n", strip=True)).strip()

def extract(listingHtml: str, descHtml: str, shippingJson: dict, descUrl: str) -> dict:
    soup = BeautifulSoup(listingHtml, "html.parser")
    sp = getSpecifics(soup)

    def field(*keys):
        return findSpecific(sp, *keys)

    title = (soup.select_one("h1") or type("", (), {"get_text": lambda self, **k: ""})()).get_text(strip=True)
    priceEl = soup.select_one(".x-price-primary")
    price = (lambda m: m.group(0) if m else priceEl.get_text(strip=True))(re.search(r"US\s*\$[\d,]+\.?\d*", priceEl.get_text(strip=True))) if priceEl else ""
    sellerEl = soup.select_one(".x-sellercard-atf__info__about-seller") or soup.select_one(".mbg-nw")
    seller = sellerEl.get_text(strip=True) if sellerEl else ""
    canonical = soup.find("link", rel="canonical")
    url = canonical.get("href", "") if canonical else ""

    sold = ""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if data.get("@type") == "Product":
                sold = "Yes" if "OutOfStock" in data.get("offers", {}).get("availability", "") else "No"
        except Exception:
            pass

    yearsFit = ""
    m = re.search(r"(\d{4}[-\u2013]\d{2,4})", title)
    if m:
        raw = m.group(1)
        parts = re.split(r"[-\u2013]", raw)
        if len(parts[1]) == 2:
            parts[1] = parts[0][:2] + parts[1]
        yearsFit = f"{parts[0]}-{parts[1]}"
    else:
        yearsFit = field("Year", "Years", "YearsFit")

    ship = parseShippingJson(shippingJson)
    sellerNotes = field("Seller Notes").strip(' *\u201c\u201d"')

    return {
        "Listing Title": title,
        "Sold Value": price,
        "Sold (OutOfStock)": sold,
        "Seller Name": seller,
        "Years Fit": yearsFit,
        "Vehicle Make": field("Make"),
        "Vehicle Model": field("Model"),
        "OEM Part Number": field("OE/OEM Part Number", "OEM Part Number"),
        "Interchange Part Number": field("Interchange Part Number"),
        "Other Part Number": field("Other Part Number"),
        "PartOut ID": field("PartOut ID"),
        "Manufacturer Part Number": field("Manufacturer Part Number"),
        "Seller Notes": sellerNotes,
        "DescriptionPartCondition": field("DescriptionPartCondition"),
        "Part Condition": field("Condition"),
        "Listing URL": url,
        "Description URL": descUrl,
        "Shipping Method": ship["shippingMethod"],
        "Shipping Cost": ship["shippingCost"],
        "Shipping Days": ship["shippingDays"],
        "Item Location": ship["itemLocation"],
        "Listing Description": parseDescription(descHtml),
    }

def itemIdFromUrl(url: str) -> str:
    m = re.search(r"/itm/(\d+)", url)
    return m.group(1) if m else ""

def run():
    urlsPath = Path(URLS_FILE)
    if not urlsPath.exists():
        sys.exit(f"[!] {URLS_FILE} not found")

    urls = [u.strip() for u in urlsPath.read_text().splitlines() if u.strip()]
    print(f"[+] loaded {len(urls)} urls")

    outPath = Path(OUTPUT_CSV)
    writeHeader = not outPath.exists() or outPath.stat().st_size == 0

    with open(outPath, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        if writeHeader:
            writer.writeheader()

        for i, url in enumerate(urls, 1):
            print(f"\n[?] [{i}/{len(urls)}] {url}")
            try:
                global session
                session = cffiRequests.Session()
                session.headers = makeHeaders()
                itemId = itemIdFromUrl(url)

                listingHtml, descUrl = fetchListing(url)
                print(f"[+] listing fetched ({descUrl[:55] if descUrl else 'no desc url'})")

                descHtml = fetchDescription(descUrl)
                print(f"[+] description fetched ({len(descHtml)} chars)")

                shippingJson = fetchShipping(itemId)
                print(f"[+] shipping fetched")

                row = extract(listingHtml, descHtml, shippingJson, descUrl)
                writer.writerow(row)
                fh.flush()
                print(f"[+] saved: {row['Listing Title'][:70]}")
            except Exception as exc:
                print(f"[-] failed: {exc}")

            if i < len(urls):
                time.sleep(DELAY)

    print(f"\n[+] done -> {OUTPUT_CSV}")

if __name__ == "__main__":
    run()

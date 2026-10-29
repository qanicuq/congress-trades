#!/usr/bin/env python3
"""Congress trades tracker: House, Senate, Executive (OGE) PTRs."""
import json
import os
import re
import sys
import time
import zipfile
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import pdfplumber
import requests
from bs4 import BeautifulSoup
import links

USER_AGENT = "congress-trades-tracker (github.com/qanicuq)"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
if not NTFY_TOPIC:
    print("ERROR: NTFY_TOPIC environment variable (GitHub secret) is not set.")
    sys.exit(1)
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"
STATE_FILE = Path("state/seen_ids.json")
RETRY_DELAY = 10
MAX_RETRIES = 3
SENATE_DELAY = 2
LATE_DAYS = 45  # STOCK Act: PTR due no later than 45 days after the transaction; late = filed more than 45 days after

def load_seen():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return set(json.load(f))
    return set()

def save_seen(seen):
    STATE_FILE.parent.mkdir(exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=2)

def ntfy(title, body, click=None):
    try:
        title = title.encode("ascii", "ignore").decode("ascii").strip() or "Congress trade filing"
        headers = {"Title": title}
        if click:
            headers["Click"] = click
        if len(body) > 4000:
            parts = []
            while body:
                parts.append(body[:3800])
                body = body[3800:]
            for i, part in enumerate(parts, 1):
                headers["Title"] = f"{title} ({i}/{len(parts)})"
                r = requests.post(NTFY_URL, data=part.encode(), headers=headers, timeout=30)
                r.raise_for_status()
                time.sleep(1)
        else:
            r = requests.post(NTFY_URL, data=body.encode(), headers=headers, timeout=30)
            r.raise_for_status()
            time.sleep(1)
        return True
    except Exception as e:
        print(f"ntfy send failed (continuing): {e}")
        return False

def fetch(url, method="GET", **kwargs):
    headers = kwargs.pop("headers", {})
    headers["User-Agent"] = USER_AGENT
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.request(method, url, headers=headers, timeout=60, **kwargs)
            r.raise_for_status()
            return r
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(RETRY_DELAY)

def oge_fetch(url, **kwargs):
    """Polite fetch for OGE: 30s timeout, at most 2 retries."""
    headers = kwargs.pop("headers", {})
    headers["User-Agent"] = USER_AGENT
    for attempt in range(3):  # 1 initial + 2 retries
        try:
            r = requests.request("GET", url, headers=headers, timeout=30, **kwargs)
            r.raise_for_status()
            return r
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(RETRY_DELAY)
    return None

def check_late(filing_str, texts):
    try:
        fd = datetime.strptime(filing_str.strip(), "%m/%d/%Y")
    except Exception:
        return False
    text = " ".join(texts) if isinstance(texts, (list, tuple)) else str(texts)
    for ds in re.findall(r"(\d{1,2}/\d{1,2}/\d{4})", text):
        try:
            td = datetime.strptime(ds, "%m/%d/%Y")
            if (fd - td).days > LATE_DAYS:
                return True
        except Exception:
            continue
    return False

HOUSE_TYPES = {"P": "BUY", "S": "SELL", "S (partial)": "SELL (partial)", "E": "EXCHANGE"}
HOUSE_OWNERS = {"SP": "Spouse", "JT": "Joint", "DC": "Child"}
HOUSE_ROW = re.compile(
    r"^(?P<asset>.+?)\s(?P<type>S \(partial\)|P|S|E)\s(?P<date>\d{2}/\d{2}/\d{4})\s\d{2}/\d{2}/\d{4}\s"
    r"(?P<amt>Over \$[\d,]+|\$[\d,]+(?:\s*-\s*\$[\d,]+)?)"
)

def parse_house_text(text):
    # House e-filed PTRs: "[owner] Asset (TICKER) [ST] P 10/09/2026 10/15/2026 $1,001 - $15,000".
    # Field labels in these PDFs contain NUL characters; skip those lines.
    lines = [l.strip() for l in text.splitlines() if "\x00" not in l and l.strip()]
    rows = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.endswith("-") and i + 1 < len(lines) and lines[i + 1].startswith("$"):
            line += " " + lines[i + 1]  # amount range wrapped onto the next line
            i += 1
        m = HOUSE_ROW.match(line)
        i += 1
        if not m:
            continue
        asset = m.group("asset")
        # asset name wrapped onto the next line, e.g. "(FITB) [ST]"
        if i < len(lines) and "[" in lines[i] and not HOUSE_ROW.match(lines[i]) and not lines[i].startswith("*"):
            asset += " " + lines[i]
            i += 1
        owner = ""
        first, _, rest = asset.partition(" ")
        if first in HOUSE_OWNERS and rest:
            owner, asset = f" ({HOUSE_OWNERS[first]})", rest
        amt = re.sub(r"\s*-\s*", " - ", m.group("amt"))
        rows.append(f"{HOUSE_TYPES[m.group('type')]} {asset}{owner} | {m.group('date')} | {amt}")
    return rows

def parse_house_pdf(pdf_bytes):
    rows = []
    try:
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        if not text.strip():
            return None  # scanned paper filing: no text to read
        rows = parse_house_text(text)
        if not rows:  # other layouts (e.g. OGE 278-T) spell out Purchase/Sale
            for line in text.splitlines():
                if re.search(r"\$[\d,]+\s*-\s*\$[\d,]+", line) and any(x in line for x in ["Purchase", "Sale", "Partial", "Exchange", "S (", "P ("]):
                    rows.append(line.strip())
    except Exception:
        pass
    return rows

def process_house(seen, seed=False):
    new_reports = []
    year = datetime.now().year
    years = [year]
    if datetime.now().month == 1:
        years.append(year - 1)
    for y in years:
        zip_url = f"https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{y}FD.zip"
        r = fetch(zip_url)
        z = zipfile.ZipFile(BytesIO(r.content))
        xml = z.read(f"{y}FD.xml").decode("utf-8-sig")
        members = re.findall(r"<Member>.*?</Member>", xml, re.DOTALL)
        for m in members:
            ftype = re.search(r"<FilingType>([^<]+)</FilingType>", m)
            if not ftype or ftype.group(1) != "P":
                continue
            docid = re.search(r"<DocID>([^<]+)</DocID>", m).group(1)
            sid = f"house:{docid}"
            if sid in seen:
                continue
            last = re.search(r"<Last>([^<]*)</Last>", m).group(1)
            first = re.search(r"<First>([^<]*)</First>", m).group(1)
            state = re.search(r"<StateDst>([^<]*)</StateDst>", m).group(1)
            fdate = re.search(r"<FilingDate>([^<]+)</FilingDate>", m).group(1)
            is_amend = "amend" in m.lower()
            pdf_url = f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{y}/{docid}.pdf"
            if seed:
                title = f"{'AMENDMENT ' if is_amend else ''}HOUSE trade filing: {first} {last} ({state})"
                new_reports.append((sid, title, "", pdf_url))
                continue
            try:
                pr = fetch(pdf_url)
                time.sleep(1)
                rows = parse_house_pdf(pr.content)
                if rows is None:
                    rows = []
                    body = f"SCANNED PAPER FILING - trades not readable, open the report\n{first} {last}\nFiled {fdate}"
                elif not rows:
                    body = f"NO ROWS FOUND — open the report\n{first} {last}\nFiled {fdate}"
                else:
                    FILING_TICKERS[sid] = links.tickers_from_house_rows(rows)
                    body = "\n".join(rows[:20])
                    if len(rows) > 20:
                        body += f"\n+{len(rows)-20} more — open the report"
                    body += f"\nFiled {fdate}"
                late = check_late(fdate, rows)
                prefix = "LATE " if late else ""
                if is_amend:
                    prefix += "AMENDMENT "
                title = f"{prefix}HOUSE trade filing: {first} {last} ({state})"
                new_reports.append((sid, title, body, pdf_url))
            except Exception as e:
                body = f"PARSE ERROR — open the report\n{first} {last}\nFiled {fdate}\n{e}"
                title = f"{'AMENDMENT ' if is_amend else ''}HOUSE trade filing: {first} {last} ({state})"
                new_reports.append((sid, title, body, pdf_url))
    return new_reports

def process_senate(seen, seed=False):
    new_reports = []
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Referer": "https://efdsearch.senate.gov/search/home/",
    })
    home = s.get("https://efdsearch.senate.gov/search/home/")
    if home.status_code == 403:
        raise Exception("site sent HTTP 403 (home page)")
    csrf = re.search(r'csrfmiddlewaretoken" value="([^"]+)"', home.text).group(1)
    s.post(
        "https://efdsearch.senate.gov/search/home/",
        data={"prohibition_agreement": "1", "csrfmiddlewaretoken": csrf},
        headers={"Referer": "https://efdsearch.senate.gov/search/home/"},
    )
    time.sleep(SENATE_DELAY)
    start = 0
    length = 50
    cutoff = datetime.now() - timedelta(days=60)
    logged_columns = False
    while True:
        data = {
            "draw": "1",
            "start": str(start),
            "length": str(length),
            "report_types": "[11]",
            "filer_types": "[]",
            "order[0][column]": "4",
            "order[0][dir]": "desc",
        }
        r = s.post(
            "https://efdsearch.senate.gov/search/report/data/",
            data=data,
            headers={
                "X-CSRFToken": csrf,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://efdsearch.senate.gov/search/home/",
            },
        )
        try:
            j = r.json()
        except ValueError:
            raise Exception(f"site sent HTTP {r.status_code} {r.headers.get('Content-Type', '')}, not data: {r.text[:80]!r}")
        rows_data = j.get("data", [])
        if not logged_columns and rows_data:
            first = rows_data[0]
            print(f"Senate columns: {len(first)}; first row cells: {first}")
            logged_columns = True
        for row in rows_data:
            link_html = row[3]
            m = re.search(r'href="([^"]+)"', link_html)
            if not m:
                continue
            rel = m.group(1)
            uuid = rel.split("/")[-2] if rel.endswith("/") else rel.split("/")[-1]
            sid = f"senate:{uuid}"
            if sid in seen:
                continue
            name = row[2]
            filed = row[4]
            try:
                filed_dt = datetime.strptime(filed.strip(), "%m/%d/%Y")
            except Exception:
                continue
            if filed_dt < cutoff:
                return new_reports
            full_url = urljoin("https://efdsearch.senate.gov", rel)
            is_paper = "/paper/" in rel
            is_amend = "amendment" in link_html.lower()
            title_prefix = "AMENDMENT " if is_amend else ""
            if seed:
                title = f"{title_prefix}SENATE trade filing: {name}"
                new_reports.append((sid, title, "", full_url))
                continue
            if is_paper:
                body = f"SCANNED — open the report\n{name}\nFiled {filed}"
                title = f"{title_prefix}SENATE trade filing: {name}"
                new_reports.append((sid, title, body, full_url))
            else:
                time.sleep(SENATE_DELAY)
                pr = s.get(full_url)
                soup = BeautifulSoup(pr.text, "html.parser")
                table = soup.find("table")
                rows = []
                if table:
                    ticker_pairs = []
                    for tr in table.find_all("tr")[1:]:
                        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
                        if len(cells) >= 6:
                            row_text = " ".join(cells)
                            rows.append(row_text)
                            if cells[3] != "--":
                                ticker_pairs.append((cells[3], row_text))
                    if ticker_pairs:
                        FILING_TICKERS[sid] = ticker_pairs
                if not rows:
                    body = f"SCANNED/UNPARSED — open the report\n{name}\nFiled {filed}"
                else:
                    body = "\n".join(rows[:20])
                    if len(rows) > 20:
                        body += f"\n+{len(rows)-20} more — open the report"
                    body += f"\nFiled {filed}"
                late = check_late(filed, rows)
                if late:
                    title_prefix = "LATE " + title_prefix
                title = f"{title_prefix}SENATE trade filing: {name}"
                new_reports.append((sid, title, body, full_url))
        if len(rows_data) < length:
            break
        start += length
        time.sleep(SENATE_DELAY)
    return new_reports

OGE_API = "https://extapps2.oge.gov/201/Presiden.nsf/API.xsp/v3/rest"
OGE_COLS = ["docDate", "title", "type", "name", "agency", "level"]

def oge_params():
    # Same DataTables query the OGE page sends, filtered to "278 Transaction", newest first.
    p = {"draw": 1, "start": 0, "length": 100, "search[value]": "278 Transaction",
         "search[regex]": "false", "order[0][column]": 0, "order[0][dir]": "desc"}
    for i, c in enumerate(OGE_COLS):
        p.update({f"columns[{i}][data]": c, f"columns[{i}][name]": "",
                  f"columns[{i}][searchable]": "true", f"columns[{i}][orderable]": "true",
                  f"columns[{i}][search][value]": "", f"columns[{i}][search][regex]": "false"})
    return p

def process_oge(seen, seed=False):
    # OGE "Officials' Individual Disclosures Search Collection" feed (President, VP,
    # Senate-confirmed officials). Newest 100 "278 Transaction" (exec-branch PTR) entries.
    data = oge_fetch(OGE_API, params=oge_params()).json()["data"]
    new_reports = []
    counts = {}
    for d in data:
        if "278 Transaction" not in d.get("type", ""):
            continue
        base = f"oge:{d['name']}|{d['title']}|{d['docDate']}"
        counts[base] = counts.get(base, 0) + 1
        sid = base if counts[base] == 1 else f"{base}#{counts[base]}"
        if sid in seen:
            continue
        link_m = re.search(r"href='([^']+)'", d["type"])
        link = link_m.group(1) if link_m else "https://www.oge.gov/web/OGE.nsf/Officials%20Individual%20Disclosures%20Search%20Collection?OpenForm"
        is_pdf = "$FILE" in link
        added = d["docDate"][:10]
        title = f"{'AMENDMENT ' if d.get('amended') else ''}EXEC trade filing: {d['name']}"
        head = f"{d['title']}, {d['agency']}\nAdded to OGE {added}"
        if seed:
            # Include date in body for first-run recent check
            new_reports.append((sid, title, added, link))
            continue
        if not is_pdf:
            body = f"{head}\nNot posted online - OGE sends it on request (tap link to request)"
            new_reports.append((sid, title, body, link))
            continue
        try:
            pr = fetch(link)
            time.sleep(1)
            rows = parse_house_pdf(pr.content)
            if rows is None:
                body = f"Scanned report - trades not readable, open the report\n{head}"
            elif not rows:
                body = f"No trades found - open the report\n{head}"
            else:
                body = "\n".join(rows[:20])
                if len(rows) > 20:
                    body += f"\n+{len(rows)-20} more - open the report"
                body += f"\n{head}"
        except Exception as e:
            short = str(e)[:80]
            body = f"Could not read report - open it ({short})\n{head}"
        new_reports.append((sid, title, body, link))
    return new_reports

DIGEST_DIR = Path("digests")
DIGEST_URL = "https://github.com/qanicuq/congress-trades/blob/main/digests/{}.md"
PUSH_LIMIT = 3800  # stay under ntfy's 4096-byte limit so the digest is ONE push
FILING_TICKERS = {}  # filing id -> list of (ticker, row text)

def short_lines(title, body):
    """One-line label plus up to 2 trade lines for the phone push."""
    label = title.replace(" trade filing", "")
    lines = body.splitlines()
    trades = [l for l in lines if "$" in l]
    more = sum(int(m) for m in re.findall(r"^\+(\d+) more", body, re.M))
    total = len(trades) + more
    if total:
        label += f" - {total} trade{'s' if total != 1 else ''}"
    elif lines:
        label += f" - {lines[0][:60]}"
    return [label] + ["   " + t[:90] for t in trades[:2]]

def send_digest(today, all_new, counts, seeded, errors, notes=()):
    n = len(all_new)
    head = (f"{n} new filing{'s' if n != 1 else ''} (House {counts['House']}, "
            f"Senate {counts['Senate']}, Exec {counts['Executive']})" if n else "No new trade filings today.")
    push = [head]
    full = [f"# Trade filings digest - {today}", "", head, ""]
    links_result = {}
    try:
        links_result = links.find_links(all_new, FILING_TICKERS)
    except Exception as e:
        push.append(f"Links check failed: {str(e)[:100]}")
    # Possible links for push
    link_lines = []
    total_flagged = 0
    for sid, title, body, link in all_new:
        if sid not in links_result:
            continue
        member_part = title.split(":")[-1].strip().split("(")[0].strip()
        for line in links_result[sid]:
            if line == "member not matched":
                continue
            # short form like "Boozman, John: MU - on Agriculture"
            parts = line.split(":")
            if len(parts) >= 2:
                ticker_name = parts[0].strip()
                reason = parts[1].strip().split(";")[0].strip()
                short = f"{member_part}: {ticker_name.split()[0]} - {reason}"
                link_lines.append(short)
                total_flagged += 1
    if link_lines:
        links_header = f"Possible links ({total_flagged}):"
        push.append(links_header)
        added = 0
        for ll in link_lines:
            test_push = push + [ll]
            if added >= 5 or len("\n".join(test_push).encode()) > PUSH_LIMIT - 60:  # cap so trades still fit
                remaining = len(link_lines) - added
                if remaining > 0:
                    push.append(f"+{remaining} more links - tap for the full list")
                break
            push.append(ll)
            added += 1
    # Group for sections
    house_new = [x for x in all_new if "HOUSE" in x[1]]
    senate_new = [x for x in all_new if "SENATE" in x[1]]
    exec_new = [x for x in all_new if "EXEC" in x[1]]
    for sid, title, body, link in all_new:
        full += [f"## {title}", "", "```", body or "(no details)", "```", f"[Open the report]({link})", ""]
        if sid in links_result:
            full.append("Possible links:")
            for line in links_result[sid]:
                full.append(f"- {line}")
            full.append("")
    for note in notes:
        push.append(note); full.append(note)
    for name, k in seeded:
        msg = f"{name} tracking started ({k} existing filings marked as seen)."
        push.append(msg); full.append(msg)
    # Executive branch section
    exec_error = next((e for e in errors if e.startswith("Executive")), None)
    if exec_error:
        err_text = exec_error[len("Executive: "):] if exec_error.startswith("Executive: ") else exec_error
        exec_section = "Executive branch: check failed - " + err_text[:120]
        full.append(f"ERROR {exec_error}")
    elif not exec_new:
        exec_section = "Executive branch: no new reports"
    else:
        exec_lines = []
        shown_exec = 0
        for sid, title, body, link in exec_new:
            label = "PDF: " if "$FILE" in link else "Request form: "
            chunk = short_lines(title, body) + [f"   {label}{link}"]
            # Same byte budget as House/Senate, so the digest can never spill into a second push.
            if shown_exec >= 5 or len("\n".join(push + ["Executive branch:"] + exec_lines + chunk).encode()) > PUSH_LIMIT - 60:
                break
            exec_lines.extend(chunk)
            shown_exec += 1
        exec_section = "Executive branch:\n" + "\n".join(exec_lines)
        if len(exec_new) > shown_exec:
            exec_section += f"\n+{len(exec_new)-shown_exec} more exec - tap for the full list"
    push.append(exec_section)
    if exec_error or not exec_new:
        full.append(exec_section)  # exec reports are already listed in full above
    for e in errors:
        if not e.startswith("Executive"):
            push.append(f"ERROR {e[:150]}"); full.append(f"ERROR {e}")
    shown = 0
    for sid, title, body, link in house_new + senate_new:
        chunk = short_lines(title, body)
        if len("\n".join(push + chunk).encode()) > PUSH_LIMIT - 60:
            break
        push += chunk
        shown += 1
    if shown < len(house_new + senate_new):
        push.append(f"+{len(house_new + senate_new) - shown} more - tap for the full list")
    DIGEST_DIR.mkdir(exist_ok=True)
    (DIGEST_DIR / f"{today}.md").write_text("\n".join(full) + "\n")
    title = f"Trade filings {today}" + (" (with errors)" if errors else "")
    return ntfy(title, "\n".join(push), click=DIGEST_URL.format(today))

def main():
    print("LATE flag: filed more than 45 days after the transaction (STOCK Act deadline).")
    seen = load_seen()
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    marker = f"digest:{today}"
    sources = [
        ("House", process_house, "house:"),
        ("Senate", process_senate, "senate:"),
        ("Executive", process_oge, "oge:"),
    ]
    counts = {"House": 0, "Senate": 0, "Executive": 0}
    errors = []
    seeded = []
    notes = []
    all_new = []
    for name, fn, prefix in sources:
        seed = not any(s.startswith(prefix) for s in seen)
        try:
            news = fn(seen, seed=seed)
            if seed:
                if name == "Executive":
                    # First run: seed all except reports dated in the last 7 days (alert on those)
                    to_seed = []
                    to_alert = []
                    for sid, title, body, link in news:
                        try:
                            doc_dt = datetime.strptime(body[:10], "%Y-%m-%d") if body else datetime.min
                            if (datetime.now() - doc_dt).days <= 7:
                                to_alert.append((sid, title, body, link))
                            else:
                                to_seed.append((sid, title, body, link))
                        except Exception:
                            to_seed.append((sid, title, body, link))
                    seen.update(sid for sid, _, _, _ in to_seed)
                    seeded.append((name, len(to_seed)))
                    counts[name] = len(to_alert)
                    all_new.extend(to_alert)
                else:
                    seen.update(sid for sid, _, _, _ in news)
                    seeded.append((name, len(news)))
            else:
                counts[name] = len(news)
                all_new.extend(news)
        except Exception as e:
            print(f"ERROR {name}: {e}")
            if name == "Senate" and "HTTP 403" in str(e):
                # Senate eFD started blocking GitHub's servers (Akamai) on Oct 23 2026; retried every run.
                notes.append("Senate: paused - the Senate site is blocking automated access (retried daily)")
                continue
            errors.append(f"{name}: {e}")
    if marker in seen:
        # Today's digest already went out; new finds wait for tomorrow's digest (one push per day).
        print(f"Digest already sent for {today}; holding {len(all_new)} new filings for tomorrow.")
        save_seen(seen)
    elif send_digest(today, all_new, counts, seeded, errors, notes):
        seen.update(sid for sid, _, _, _ in all_new)
        seen = {s for s in seen if not s.startswith("digest:")} | {marker}
        save_seen(seen)
    else:
        save_seen(seen)  # keep seeds; unsent filings retry next run
    if errors:
        raise SystemExit(1)

if __name__ == "__main__":
    main()
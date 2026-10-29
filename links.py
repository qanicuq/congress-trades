#!/usr/bin/env python3
"""Possible links checks for Congress trades: state, district, committee."""
import json
import os
import re
import time
import unicodedata
from pathlib import Path

import requests

USER_AGENT = f"congress-trades-tracker {os.environ.get('SEC_EMAIL', '')}"
SEC_DELAY = 0.2

FIPS_TO_STATE = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT",
    "10": "DE", "11": "DC", "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL",
    "18": "IN", "19": "IA", "20": "KS", "21": "KY", "22": "LA", "23": "ME", "24": "MD",
    "25": "MA", "26": "MI", "27": "MN", "28": "MS", "29": "MO", "30": "MT", "31": "NE",
    "32": "NV", "33": "NH", "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND",
    "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD",
    "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA", "54": "WV",
    "55": "WI", "56": "WY", "72": "PR",
}

# Cache for company lookups during one run
_company_cache = {}
_sources = None

def load_sources():
    global _sources
    if _sources:
        return _sources
    # A: legislators
    r = requests.get("https://unitedstates.github.io/congress-legislators/legislators-current.json", timeout=60)
    r.raise_for_status()
    legislators = r.json()
    # C: committees
    r = requests.get("https://unitedstates.github.io/congress-legislators/committee-membership-current.json", timeout=60)
    r.raise_for_status()
    committees = r.json()
    # D: tickers
    r = requests.get("https://www.sec.gov/files/company_tickers.json", headers={"User-Agent": USER_AGENT}, timeout=60)
    r.raise_for_status()
    tickers = r.json()
    # F: Census CD-ZCTA (local file expected, or download if missing)
    census_path = Path("tab20_cd11920_zcta520_natl.txt")
    if not census_path.exists():
        r = requests.get("https://www2.census.gov/geo/docs/maps-data/data/rel2020/cd-sld/tab20_cd11920_zcta520_natl.txt", timeout=120)
        r.raise_for_status()
        census_path.write_bytes(r.content)
    # Load committee industries
    with open("committee_industries.json") as f:
        industries = json.load(f)
    _sources = {
        "legislators": legislators,
        "committees": committees,
        "tickers": tickers,
        "census_path": census_path,
        "industries": industries,
    }
    return _sources

def normalize_name(name):
    if not name:
        return ""
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    return name.lower().strip()

def find_member(title):
    sources = load_sources()
    legislators = sources["legislators"]
    if "HOUSE" in title:
        m = re.search(r"\(([A-Z]{2})(\d{2}|AL|00)\)", title)
        if not m:
            return None
        state = m.group(1)
        dist_str = m.group(2)
        if dist_str in ("AL", "00"):
            district = 0
        else:
            district = int(dist_str)
        for leg in legislators:
            terms = leg.get("terms", [])
            if not terms:
                continue
            last = terms[-1]
            if (last.get("type") == "rep" and last.get("state") == state and
                last.get("district") == district):
                return leg
        return None
    elif "SENATE" in title:
        m = re.search(r"SENATE trade filing: ([^,]+), ([^(]+)\(", title)
        if not m:
            return None
        last_name = normalize_name(m.group(1))
        first_name = normalize_name(m.group(2))
        matches = []
        for leg in legislators:
            terms = leg.get("terms", [])
            if not terms:
                continue
            last = terms[-1]
            if last.get("type") != "sen":
                continue
            leg_last = normalize_name(leg.get("name", {}).get("last", ""))
            if leg_last != last_name:
                continue
            leg_first = normalize_name(leg.get("name", {}).get("first", ""))
            leg_nick = normalize_name(leg.get("name", {}).get("nickname", ""))
            if first_name and (first_name in leg_first or first_name in leg_nick or
                               leg_first.startswith(first_name) or leg_nick.startswith(first_name)):
                matches.append(leg)
        if len(matches) == 1:
            return matches[0]
        # fallback: exactly one with that last name
        last_only = [leg for leg in legislators
                     if normalize_name(leg.get("name", {}).get("last", "")) == last_name
                     and leg.get("terms", [{}])[-1].get("type") == "sen"]
        if len(last_only) == 1:
            return last_only[0]
        return None
    return None

def tickers_from_house_rows(rows):
    pairs = []
    for row in rows:
        m = re.search(r"\(([A-Z0-9.\-]+)\)\s*\[(?:ST|OP)\]", row)
        if m:
            pairs.append((m.group(1), row))
    return pairs

def clean_ticker(t):
    if not t:
        return ""
    return t.upper().replace(" ", "").replace(".", "-")

def company(ticker):
    ticker = clean_ticker(ticker)
    if not ticker or ticker == "--":
        return None
    if ticker in _company_cache:
        return _company_cache[ticker]
    sources = load_sources()
    tickers_data = sources["tickers"]
    cik = None
    for entry in tickers_data.values():
        if entry.get("ticker") == ticker:
            cik = str(entry["cik_str"]).zfill(10)
            break
    if not cik:
        _company_cache[ticker] = None
        return None
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    time.sleep(SEC_DELAY)
    data = r.json()
    sic = data.get("sic")
    if not sic:
        _company_cache[ticker] = None
        return None
    name = data.get("name", "")
    sic_desc = data.get("sicDescription", "")
    addr = data.get("addresses", {}).get("business", {})
    state = addr.get("stateOrCountry", "")
    zipcode = addr.get("zipCode", "")[:5]
    result = {
        "name": name,
        "sic": sic,
        "sicDescription": sic_desc,
        "state": state,
        "zip": zipcode,
    }
    _company_cache[ticker] = result
    return result

def check(member, ticker):
    reasons = []
    if not member:
        return reasons
    comp = company(ticker)
    if not comp:
        return reasons
    sources = load_sources()
    last_term = member.get("terms", [{}])[-1]
    member_state = last_term.get("state")
    if comp["state"] == member_state:
        reasons.append(f"HQ in {member_state} (their state)")
    # District check for House only
    if last_term.get("type") == "rep":
        district = last_term.get("district", 0)
        zipcode = comp["zip"]
        if zipcode and len(zipcode) == 5:
            # Load census lazily and build index if needed
            if "census_index" not in sources:
                index = {}
                with open(sources["census_path"]) as f:
                    header = f.readline()  # skip header
                    for line in f:
                        parts = line.strip().split("|")
                        if len(parts) < 9:
                            continue
                        geoid_cd = parts[1]  # GEOID_CD119_20
                        geoid_zcta = parts[8]  # GEOID_ZCTA5_20
                        if geoid_zcta and geoid_cd:
                            index.setdefault(geoid_zcta, set()).add(geoid_cd)
                sources["census_index"] = index
            zips_cds = sources["census_index"].get(zipcode, set())
            state_fips = None
            for fips, st in FIPS_TO_STATE.items():
                if st == member_state:
                    state_fips = fips
                    break
            if state_fips:
                dist_str = f"{district:02d}"
                target = state_fips + dist_str
                if target in zips_cds:
                    reasons.append(f"HQ zip {zipcode} is in their district")
    # Committee checks
    bioguide = member.get("id", {}).get("bioguide")
    if not bioguide:
        return reasons
    industries = {item["code"]: item for item in sources["industries"]}
    committees = sources["committees"]
    try:
        sic_num = int(comp["sic"])
    except (ValueError, TypeError):
        return reasons
    for code, members_list in committees.items():
        if len(code) != 4:
            continue
        if code not in industries:
            continue
        if not any(m.get("bioguide") == bioguide for m in members_list):
            continue
        for first, last in industries[code]["sic_ranges"]:
            if first <= sic_num <= last:
                reasons.append(f"on {industries[code]['name']} ({comp['sicDescription']})")
                break
    return reasons

def find_links(all_new, filing_tickers):
    result = {}
    for sid, title, body, link in all_new:
        if "HOUSE" not in title and "SENATE" not in title:
            continue
        member = find_member(title)
        tickers = filing_tickers.get(sid, [])
        lines = []
        if member is None:
            lines.append("member not matched")
        else:
            for ticker, row_text in tickers:
                reasons = check(member, ticker)
                if reasons:
                    comp = company(ticker)
                    name = comp["name"] if comp else ticker
                    line = f"{ticker} {name}: " + "; ".join(reasons)
                    lines.append(line)
        if lines:
            result[sid] = lines
    return result

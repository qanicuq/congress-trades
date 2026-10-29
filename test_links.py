#!/usr/bin/env python3
"""Test possible links checks on sample filings. No ntfy."""
import links

samples = [
    ("SENATE trade filing: Boozman, John (Senator)", ["MU", "CSCO", "APTV", "O", "VEA", "TLH"]),
    ("SENATE trade filing: Scott, Tim (Senator)", ["SCCO"]),
    ("SENATE trade filing: Scott, Rick (Senator)", ["SCCO"]),
    ("HOUSE trade filing: James A. Himes (CT04)", ["SYF", "BRK.B"]),
]

for title, tickers in samples:
    print(f"=== {title} ===")
    member = links.find_member(title)
    if member:
        name = f"{member['name'].get('first', '')} {member['name'].get('last', '')}"
        last_term = member['terms'][-1]
        state = last_term.get('state')
        district = last_term.get('district')
        print(f"Matched: {name}, state={state}, district={district}")
        # committees
        sources = links.load_sources()
        bioguide = member.get('id', {}).get('bioguide')
        comm_codes = [code for code, mems in sources['committees'].items()
                      if len(code) == 4 and any(m.get('bioguide') == bioguide for m in mems)]
        print(f"Committee codes: {comm_codes}")
    else:
        print("Matched: None")
    for t in tickers:
        reasons = links.check(member, t) if member else []
        if reasons:
            for r in reasons:
                print(f"  {t}: {r}")
        else:
            print(f"  {t}: no links")
    print()

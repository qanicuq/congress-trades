# Congress Trades Tracker

Daily tracker for House, Senate, and Executive branch (OGE Form 278-T) periodic transaction reports.

This project is for personal, non-commercial use under 5 U.S.C. 13107(c).

## Possible links

For House and Senate trades with a stock ticker, the tracker checks three things and flags a "possible link" if any is true:

- **STATE**: the company's headquarters is in the member's state.
- **DISTRICT**: (House members only) the company's headquarters zip code is in the member's congressional district.
- **COMMITTEE**: the member sits on a committee whose area covers the company's industry (via SIC code).

Data sources:
- [Legislators current](https://unitedstates.github.io/congress-legislators/legislators-current.json)
- [Committee membership current](https://unitedstates.github.io/congress-legislators/committee-membership-current.json)
- [SEC company tickers](https://www.sec.gov/files/company_tickers.json)
- [SEC company submissions](https://data.sec.gov/submissions/CIK##########.json)
- [Census 119th CD to 2020 ZCTA relationship file](https://www2.census.gov/geo/docs/maps-data/data/rel2020/cd-sld/tab20_cd11920_zcta520_natl.txt)

A flag only means "worth a look". Only headquarters locations are checked. The Census file is for the 119th Congress and must be swapped for the 120th Congress file in January 2027.

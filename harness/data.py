"""Seed the harness SQLite file with ~200 synthetic customers.

Shape the later items rely on:
- CUSTOMER (app1) and CUSTMAST (app2) describe the same people, linked by the CUST_XREF crosswalk.
- ADDRESS.CUST_REF points at CUSTMAST.CIF_NO by column name only: no declared FK (I-15 finds it).
- A few customers share a full name (duplicates the bot has to choose between).
- Customer 4471 is JOHN SMITH with account 0048812231; customer 90218 is another real row.
All values are generated from a fixed seed; nothing here is real.
"""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path

DDL = """
CREATE TABLE CUSTOMER (
  CUST_ID     INTEGER PRIMARY KEY,
  FULL_NAME   VARCHAR(60) NOT NULL,
  TAX_ID      VARCHAR(9),
  CITY        VARCHAR(40),
  SEGMENT     VARCHAR(10),
  UPDATED_AT  VARCHAR(30)
);
CREATE TABLE ACCT (
  ACCT_NO     VARCHAR(10) PRIMARY KEY,
  CUST_ID     INTEGER NOT NULL REFERENCES CUSTOMER(CUST_ID),
  ACCT_TYPE   VARCHAR(10),
  BALANCE     NUMERIC(12,2),
  OPENED_DT   VARCHAR(10),
  UPDATED_AT  VARCHAR(30)
);
CREATE TABLE CUSTMAST (
  CIF_NO      VARCHAR(8) PRIMARY KEY,
  FIRST_NM    VARCHAR(30) NOT NULL,
  LAST_NM     VARCHAR(30) NOT NULL,
  SSN         VARCHAR(11),
  BIRTH_DT    VARCHAR(10),
  EMAIL       VARCHAR(80),
  LAST_MOD_TS VARCHAR(30)
);
CREATE TABLE CUST_XREF (
  CUST_ID     INTEGER NOT NULL REFERENCES CUSTOMER(CUST_ID),
  CIF_NO      VARCHAR(8) NOT NULL REFERENCES CUSTMAST(CIF_NO),
  PRIMARY KEY (CUST_ID, CIF_NO)
);
CREATE TABLE PHONE (
  PHONE_ID    INTEGER PRIMARY KEY,
  CIF_NO      VARCHAR(8) NOT NULL REFERENCES CUSTMAST(CIF_NO),
  PHONE_TYPE  VARCHAR(10),
  NUMBER      VARCHAR(12)
);
CREATE TABLE ADDRESS (
  ADDRESS_ID  INTEGER PRIMARY KEY,
  CUST_REF    VARCHAR(8) NOT NULL,
  ADDR_TYPE   VARCHAR(10),
  LINE1       VARCHAR(60),
  CITY        VARCHAR(40),
  STATE       VARCHAR(2),
  ZIP         VARCHAR(5)
);
CREATE TABLE LOAN (
  LOAN_NO        INTEGER PRIMARY KEY,
  ACCT_NO        VARCHAR(10) NOT NULL REFERENCES ACCT(ACCT_NO),
  LOAN_AMT       NUMERIC(12,2) NOT NULL,
  TERM_MONTHS    INTEGER NOT NULL,
  PURPOSE_CD     VARCHAR(10) NOT NULL,
  SUB_PURPOSE_CD VARCHAR(10),
  COSIGNER_NM    VARCHAR(60),
  STATUS_CD      VARCHAR(10) NOT NULL DEFAULT 'PENDING',
  CREATED_TS     VARCHAR(30) NOT NULL,
  NOTES          VARCHAR(200),
  UPDATED_AT     VARCHAR(30)
);
CREATE INDEX IX_CUSTOMER_NAME ON CUSTOMER(FULL_NAME);
CREATE INDEX IX_ADDRESS_REF ON ADDRESS(CUST_REF);
"""

FIRST = (
    "JOHN MARY JAMES PATRICIA ROBERT LINDA MICHAEL BARBARA WILLIAM ELIZABETH DAVID JENNIFER "
    "RICHARD MARIA JOSEPH SUSAN THOMAS MARGARET CHARLES DOROTHY DANIEL LISA MATTHEW NANCY "
    "ANTHONY KAREN MARK BETTY DONALD HELEN STEVEN SANDRA PAUL DONNA ANDREW CAROL JOSHUA RUTH"
).split()
LAST = (
    "SMITH JOHNSON WILLIAMS BROWN JONES GARCIA MILLER DAVIS RODRIGUEZ MARTINEZ HERNANDEZ LOPEZ "
    "GONZALEZ WILSON ANDERSON THOMAS TAYLOR MOORE JACKSON MARTIN LEE PEREZ THOMPSON WHITE HARRIS "
    "SANCHEZ CLARK RAMIREZ LEWIS ROBINSON WALKER YOUNG ALLEN KING WRIGHT SCOTT TORRES NGUYEN HILL"
).split()
STREETS = (
    "OAK MAPLE CEDAR PINE ELM WASHINGTON LAKE HILL PARK MAIN RIVER SUNSET HIGHLAND MEADOW "
    "FOREST CHURCH SPRING RIDGE VALLEY WILLOW"
).split()
SUFFIX = ["ST", "AVE", "RD", "LN", "DR", "CT"]
CITIES = [
    ("SPRINGFIELD", "IL"),
    ("RIVERTON", "WY"),
    ("FAIRVIEW", "TN"),
    ("MADISON", "WI"),
    ("GEORGETOWN", "TX"),
    ("SALEM", "OR"),
    ("CLINTON", "IA"),
    ("FRANKLIN", "MA"),
]

AUDIT_TS = "2026-11-01T09:00:00.000Z"
CUSTOMER_COUNT = 200
JOHN_SMITH_ID = 4471
OTHER_ID = 90218
TABLES = ["CUSTOMER", "ACCT", "CUSTMAST", "CUST_XREF", "PHONE", "ADDRESS", "LOAN"]


def _ssn(rng: random.Random) -> str:
    while True:
        area = rng.randint(1, 899)
        if area == 666:
            continue
        return f"{area:03d}{rng.randint(1, 99):02d}{rng.randint(1, 9999):04d}"


def build_rows(seed: int = 4471) -> dict[str, list[dict]]:
    rng = random.Random(seed)
    pool = [
        i
        for i in rng.sample(range(1000, 99999), CUSTOMER_COUNT + 2)
        if i not in (JOHN_SMITH_ID, OTHER_ID)
    ]
    ids = sorted(pool[: CUSTOMER_COUNT - 2] + [JOHN_SMITH_ID, OTHER_ID])
    rows: dict[str, list[dict]] = {t: [] for t in TABLES}
    names: dict[int, tuple[str, str]] = {cid: (rng.choice(FIRST), rng.choice(LAST)) for cid in ids}
    # Deliberate duplicates: JOHN SMITH twice (4471 + one other), MARY JONES three times.
    others = [c for c in ids if c not in (JOHN_SMITH_ID, OTHER_ID)]
    for c in others:
        if names[c] in (("JOHN", "SMITH"), ("MARY", "JONES")):
            names[c] = ("PAUL", "KING")
    names[JOHN_SMITH_ID] = ("JOHN", "SMITH")
    names[others[7]] = ("JOHN", "SMITH")
    for c in others[20:23]:
        names[c] = ("MARY", "JONES")
    names[OTHER_ID] = ("LINDA", "NGUYEN")
    phone_id, addr_id, loan_no = 500_001, 700_001, 70_000_001
    for cid in ids:
        first, last = names[cid]
        city, state = rng.choice(CITIES)
        ssn = _ssn(rng)
        cif = f"C{cid:07d}"
        rows["CUSTOMER"].append(
            {
                "CUST_ID": cid,
                "FULL_NAME": f"{first} {last}",
                "TAX_ID": ssn,
                "CITY": city,
                "SEGMENT": rng.choice(["RETAIL", "PRIVATE", "SMB"]),
                "UPDATED_AT": AUDIT_TS,
            }
        )
        dob = f"{rng.randint(1950, 2002)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
        rows["CUSTMAST"].append(
            {
                "CIF_NO": cif,
                "FIRST_NM": first,
                "LAST_NM": last,
                "SSN": f"{ssn[:3]}-{ssn[3:5]}-{ssn[5:]}",
                "BIRTH_DT": dob,
                "EMAIL": f"{first.lower()}.{last.lower()}{cid % 97}@example.com",
                "LAST_MOD_TS": AUDIT_TS,
            }
        )
        rows["CUST_XREF"].append({"CUST_ID": cid, "CIF_NO": cif})
        n_acct = 2 if cid == JOHN_SMITH_ID else rng.randint(1, 3)
        for i in range(n_acct):
            acct = f"00{rng.randint(10_000_000, 99_999_999)}"
            if cid == JOHN_SMITH_ID:
                acct = ["0048812231", "0061734408"][i]
            rows["ACCT"].append(
                {
                    "ACCT_NO": acct,
                    "CUST_ID": cid,
                    "ACCT_TYPE": rng.choice(["CHK", "SAV", "MMA"]),
                    "BALANCE": round(rng.uniform(50, 90_000), 2),
                    "OPENED_DT": f"{rng.randint(2005, 2025)}-{rng.randint(1, 12):02d}-01",
                    "UPDATED_AT": AUDIT_TS,
                }
            )
            if rng.random() < 0.25:
                rows["LOAN"].append(
                    {
                        "LOAN_NO": loan_no,
                        "ACCT_NO": acct,
                        "LOAN_AMT": rng.choice([5000, 9000]),
                        "TERM_MONTHS": rng.choice([24, 36, 60]),
                        "PURPOSE_CD": "PERSONAL",
                        "SUB_PURPOSE_CD": "OTHER",
                        "COSIGNER_NM": None,
                        "STATUS_CD": "ACTIVE",
                        "CREATED_TS": "2025-06-01T10:00:00.000Z",
                        "NOTES": None,
                        "UPDATED_AT": "2025-06-01T10:00:00.000Z",
                    }
                )
                loan_no += 1
        for kind in ["CELL", "HOME"] if rng.random() < 0.5 else ["CELL"]:
            rows["PHONE"].append(
                {
                    "PHONE_ID": phone_id,
                    "CIF_NO": cif,
                    "PHONE_TYPE": kind,
                    "NUMBER": f"{rng.randint(201, 989)}-{rng.randint(200, 999)}-"
                    f"{rng.randint(0, 9999):04d}",
                }
            )
            phone_id += 1
        kinds = ["HOME", "MAIL"] if rng.random() < 0.3 or cid == JOHN_SMITH_ID else ["HOME"]
        for kind in kinds:
            rows["ADDRESS"].append(
                {
                    "ADDRESS_ID": addr_id,
                    "CUST_REF": cif,
                    "ADDR_TYPE": kind,
                    "LINE1": f"{rng.randint(10, 9999)} {rng.choice(STREETS)} {rng.choice(SUFFIX)}",
                    "CITY": city,
                    "STATE": state,
                    "ZIP": f"{rng.randint(10000, 99999)}",
                }
            )
            addr_id += 1
    return rows


def create_db(path: Path, seed: int = 4471) -> Path:
    """(Re)create the harness DB file. Only ever writes the harness file it is given."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    try:
        con.executescript(DDL)
        for table, rs in build_rows(seed).items():
            if not rs:
                continue
            cols = list(rs[0])
            con.executemany(
                f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                [tuple(r[c] for c in cols) for r in rs],
            )
        con.commit()
    finally:
        con.close()
    return path


def ensure_db(path: Path) -> Path:
    if not Path(path).exists():
        create_db(path)
    return path

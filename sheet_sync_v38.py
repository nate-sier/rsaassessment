"""Reviewed assessment -> specific Google worksheet, guarded against duplicate dates/names."""
from __future__ import annotations

import math
import re
import unicodedata
from datetime import date, datetime

import numpy as np
import pandas as pd

SPREADSHEET_ID = "1UT2Hq5OkPHDnaB9ciwUSUHdXoM6Ev7ImXM0xiN5q_Ak"
WORKSHEET_GID = 1753811563
HEADERS = [
    "Date", "Athlete", "Best mph", "Mean mph", "Top 3 mph", "Worst mph",
    "Peak to Avg Drop %", "Best to Worst Drop %", "Set 1 Mean mph",
    "Set 2 Mean mph", "Set 3 Mean mph", "Set 3 vs 1 Decline %", "Speed CV %",
]


def normal_name(value):
    return " ".join(unicodedata.normalize("NFKC", str(value)).split()).casefold()


def normal_date(value):
    if isinstance(value, (date, datetime)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    raw = str(value).strip()
    if not raw:
        raise ValueError("Blank test date")
    # Sheets can return date display formats as well as ISO strings.
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"Unrecognized sheet date {raw!r}. Set Date column display to yyyy-mm-dd.")


def _positive_speeds(rep_frame):
    """Require one explicitly assigned, finite, positive raw GPS peak per 1..30."""
    if "Rep" not in rep_frame or "Raw max (mph)" not in rep_frame:
        raise ValueError("Reviewed repetition CSV is missing Rep or Raw max (mph)")
    rep_ids = pd.to_numeric(rep_frame["Rep"], errors="coerce")
    if len(rep_frame) != 30 or rep_ids.isna().any():
        raise ValueError("A test must contain exactly 30 numbered repetitions")
    if not (rep_ids.between(1,30) & (rep_ids % 1 == 0)).all() or rep_ids.nunique() != 30:
        raise ValueError("Repetitions must be exactly 1–30 with no duplicate or missing numbers")
    data=rep_frame.copy()
    data["Rep"] = rep_ids.astype(int)
    data=data.sort_values("Rep")
    if "Set" in data:
        set_ids=pd.to_numeric(data["Set"],errors="coerce")
        expected=(data["Rep"]-1)//10+1
        if not (set_ids.to_numpy()==expected.to_numpy()).all():
            raise ValueError("Set numbers are inconsistent with repetitions 1–30")
    speeds=pd.to_numeric(data["Raw max (mph)"],errors="coerce").to_numpy(dtype=float)
    if not np.isfinite(speeds).all() or (speeds<=0).any():
        raise ValueError("One or more repetitions lack a valid positive raw peak")
    return speeds


def compute_summary(day, athlete, reps):
    """Calculate all 13 sheet fields from post-review raw local peaks."""
    name=" ".join(str(athlete).split()).strip()
    if not name:
        raise ValueError("Athlete name is blank")
    speeds=_positive_speeds(reps)
    best=float(np.max(speeds))
    worst=float(np.min(speeds))
    mean=float(np.mean(speeds))
    s1,s2,s3=(float(np.mean(speeds[i:i+10])) for i in (0,10,20))
    metrics=[best,mean,float(np.mean(np.sort(speeds)[-3:])),worst,
             100*(best-mean)/best,100*(best-worst)/best,
             s1,s2,s3,100*(s1-s3)/s1,
             100*float(np.std(speeds,ddof=1))/mean]
    return [normal_date(day),name,*[round(v,4) for v in metrics]]


def summaries_from_csv(csv_path, day, allowed_keys):
    data=pd.read_csv(csv_path,low_memory=False)
    if "Match key" not in data or "Athlete" not in data:
        raise ValueError("Selected reviewed CSV lacks Match key / Athlete columns")
    selection=set(map(str,allowed_keys))
    rows=[]
    errors=[]
    for key, trial in data.groupby("Match key",sort=False,dropna=False):
        if str(key) not in selection:
            continue
        names=trial["Athlete"].dropna().astype(str).str.strip().unique().tolist()
        if len(names)!=1:
            errors.append(f"Trial {key}: requires one unambiguous athlete label")
            continue
        try:
            rows.append(compute_summary(day,names[0],trial))
        except ValueError as exc:
            errors.append(f"{names[0]}: {exc}")
    found={str(k) for k in data["Match key"].dropna().astype(str)}
    for missing in sorted(selection-found):
        errors.append(f"Selected trial {missing[:12]} is not present in reviewed CSV")
    # The sheet has only date + name as identity. Multiple same-name trials cannot both be stored.
    seen=set()
    for row in rows:
        key=(normal_date(row[0]),normal_name(row[1]))
        if key in seen:
            errors.append(f"More than one selected trial for {row[1]} on {row[0]}; select only one")
        seen.add(key)
    return rows,errors


def worksheet_from_secrets(secrets):
    try:
        config=dict(secrets["gcp_service_account"])
    except (KeyError, TypeError) as exc:
        raise ValueError("Add [gcp_service_account] credentials in Streamlit Secrets before syncing") from exc
    if not config.get("client_email") or not config.get("private_key"):
        raise ValueError("The Google service account is missing client_email or private_key")
    import gspread
    from google.oauth2.service_account import Credentials
    credentials=Credentials.from_service_account_info(config,scopes=["https://www.googleapis.com/auth/spreadsheets"])
    client=gspread.authorize(credentials)
    spreadsheet=client.open_by_key(SPREADSHEET_ID)
    return spreadsheet.get_worksheet_by_id(WORKSHEET_GID)


def write_rows(worksheet, rows, replace=False):
    """Fail closed on unexpected headers or multiple matching rows; return write counts."""
    grid=worksheet.get_all_values()
    if not grid:
        raise ValueError("Destination sheet is blank. Add the 13 exact column headers first")
    headers=[str(v).strip() for v in grid[0]]
    if headers!=HEADERS:
        raise ValueError("Sheet row 1 does not match the 13 required headers in A:M. No changes made")
    lookup={}
    for i, values in enumerate(grid[1:],start=2):
        if not any(str(v).strip() for v in values):
            continue
        if len(values)<2 or not str(values[0]).strip() or not str(values[1]).strip():
            raise ValueError(f"Existing row {i} has no Date or Athlete. Fix the sheet before syncing")
        key=(normal_date(values[0]), normal_name(values[1]))
        lookup.setdefault(key,[]).append(i)
    # Preflight every row before any network mutation.
    actions=[]
    batch_keys=set()
    for values in rows:
        if len(values)!=len(HEADERS):
            raise ValueError("Malformed summary row")
        key=(normal_date(values[0]),normal_name(values[1]))
        if key in batch_keys:
            raise ValueError("Duplicate athlete and date in selected export")
        batch_keys.add(key)
        if len(lookup.get(key,[]))>1:
            raise ValueError(f"Duplicate existing records for {values[1]} on {values[0]}; manually resolve first")
        if key in lookup:
            actions.append(("replace" if replace else "skip",lookup[key][0],values))
        else:
            actions.append(("add",None,values))
    for action,row_no,values in actions:
        if action=="replace":
            worksheet.update(range_name=f"A{row_no}:M{row_no}",values=[values],value_input_option="RAW")
    additions=[values for action,_,values in actions if action=="add"]
    if additions:
        worksheet.append_rows(additions,value_input_option="RAW",insert_data_option="INSERT_ROWS")
    return {"added":len(additions), "replaced":sum(a=="replace" for a,_,_ in actions),
            "skipped":sum(a=="skip" for a,_,_ in actions)}

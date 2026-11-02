#!/usr/bin/env python3
"""STATSports all-athlete 30-rep tagged assessment dashboard (v15, raw local peaks).

Run: python3 statsports_assessment_all_players_v15_raw_peaks.py --date 2026-10-20
Fix previously missing first reps OFFLINE (auto-opens updated dashboard):
    python3 statsports_assessment_all_players_v15_raw_peaks.py --date 2026-10-20 --repair-existing
Open saved dashboard with NO API requests: python3 statsports_assessment_all_players_v14_interactive_quadrants.py --date 2026-10-20 --open-dashboard
Test locally without API: python3 statsports_assessment_all_players_v12_auto_open.py --demo
Install: python3 -m pip install requests pandas numpy plotly reportlab

Interactive quadrant dividing lines (mean or median) and browser-only display-name aliases.
Raw local peak mode is the default: unsmoothed maximum API v within +/-0.2s of each smoothed detected sprint peak.
Original smoothed repetition data CSV is NEVER overwritten by report generation.
Exports separate all_30_reps_RAW.csv with raw peak as primary Peak (mph).
Refresh from saved GPS CSVs with --report-only; --quadrant-split median sets initial/PDF/CSV classification.
Finds exact drill name Assessment - 3 Sets across all athletes on chosen date.
Uses drill timestamps to limit processing, not drill KPI peak speeds.
Supports --resolve-only to find the tagged athlete name without downloading raw GPS.
Stores 0.2-second speed bins for the tagged interval, not raw pages.
No server-side GPS time filtering is documented.
Use --resolve-only to look up the athlete automatically from API metadata (no GPS request). Downloads only linked athletes\' streams,
and can stop early if pages are demonstrably chronologically ordered.
Shared devices still require confirmation that GPS samples belong to the tagged athlete.
This is an engineering implementation informed by GPS validation research;
its numeric detection thresholds are NOT validated by those studies.
API credential is never stored in this repository; supply STATSports credentials using environment variables or Streamlit secrets.
"""
import argparse
import os
import hashlib
import shutil
import subprocess
import html
import json
import math
import re
import sqlite3
import unicodedata
import sys
import time
import webbrowser
from datetime import datetime
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
import plotly.graph_objects as go
from plotly.offline import plot
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet

API_KEY = os.environ.get('STATSPORTS_API_KEY', '').strip()  # Set in Streamlit secrets; never commit credentials.
BASE_URL = 'https://statsportsproseries.com/thirdpartyapi/api/thirdPartyData'
HEADERS = {'Internal': API_KEY, 'api-version': '7', 'Content-Type': 'application/json'}
OUTPUT_ROOT = Path(__file__).resolve().parent
TZ = 'America/New_York'
# No fixed session time restrictions: the exact tagged drill determines the assessment window.
DRILL_NAME = 'Assessment - 3 Sets'
PAD_BEFORE_S = 3
PAD_AFTER_S = 3
MAX_PAGES = 12000
BIN_MS = 200                 # 0.2 s bin, speed mean/max stored; not raw observations
CADENCE_S = 17.0
MATCH_TOLERANCE_S = 1.5      # within-set reps 2-10: baseline timing tolerance
SET_OPENING_TOLERANCE_S = 7.0  # flexible FIRST rep of each set, with 8+ later matches; flagged for review

MIN_REPS_PER_SET = 8        # permits missing evidence; marks for review
REST_GAP_RANGE_S = (120, 420) # allow 2–7 min between last peak and next set's first; tighten using within-set timing
NORMAL_REST_GAP_RANGE_S = (165, 240) # outside this plausible 3-minute range is an explicit QC warning
IDEAL_GAP_S = 183.0         # ~3 min after last rep, exact definition may vary
PEAK_FLOOR_MPS = 3.8        # candidate finder only; NOT a fatigue/performance threshold
OUTPUT_NOTE = 'PROVISIONAL - check detection quality, GPS units and shared-device identity.'
MPH = 2.2369362921

HTTP = requests.Session()
HTTP.mount('https://', HTTPAdapter(pool_connections=4, pool_maxsize=4))


def api(endpoint, payload):
    if not API_KEY:
        raise RuntimeError('Missing STATSports API credential. Configure STATSPORTS_API_KEY in Streamlit secrets or environment before pulling GPS.')
    for attempt in range(5):
        try:
            r = HTTP.post(f'{BASE_URL}/{endpoint}', json=payload, headers=HEADERS,
                          timeout=(30, 180))
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f'Transient HTTP {r.status_code}', response=r)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and data.get('success') is False:
                raise ValueError(f'API error: {str(data)[:300]}')
            return data
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
            if attempt == 4 or (isinstance(exc, requests.HTTPError) and
                getattr(exc.response,'status_code', None) not in (429,500,502,503,504)):
                raise RuntimeError(f'{endpoint} failed: {exc}') from exc
            delay = min(40,3*2**attempt)
            print(f'    Retry {attempt+1}/5 in {delay}s: {str(exc)[:120]}')
            time.sleep(delay)


def get_field(obj, *fields):
    if not isinstance(obj, dict): return None
    for f in fields:
        x=obj.get(f)
        if x is not None and x != '': return x
    return None


def unwrap_gps(body):
    if isinstance(body,list):return body
    if isinstance(body,dict):
        for k in ('gpsData','sessionGpsData','rawData','rows','records','items','data','result','values'):
            v=body.get(k)
            if isinstance(v,list) and (not v or isinstance(v[0],dict)): return v
            if isinstance(v,dict):
                arr=unwrap_gps(v)
                if arr:return arr
    return []


def normalize_gps(rows):
    """Parse timestamp/speed and drop impossible readings; GPS 'v' assumed m/s."""
    if not rows:return pd.DataFrame(columns=['timestamp','speed_mps'])
    df=pd.DataFrame(rows)
    tk=next((x for x in ('t','timestamp','time','gpsTime','utcTimestamp','dateTime') if x in df),None)
    vk=next((x for x in ('v','speed','speedMps','velocity','speedMs') if x in df),None)
    if tk is None or vk is None:
        raise ValueError(f'GPS timestamp or speed column missing; available: {list(df.columns)}')
    raw_t=df[tk]
    if pd.api.types.is_numeric_dtype(raw_t) or (len(raw_t) and
          pd.to_numeric(raw_t,errors='coerce').notna().mean()>.95):
        nums=pd.to_numeric(raw_t,errors='coerce')
        med=nums.dropna().abs().median()
        unit='ns' if med>1e17 else 'ms' if med>1e11 else 's'
        t=pd.to_datetime(nums,unit=unit,utc=True,errors='coerce')
    else:
        t=pd.to_datetime(raw_t,utc=True,errors='coerce')
    v=pd.to_numeric(df[vk],errors='coerce')
    out=pd.DataFrame({'timestamp':t,'speed_mps':v}).dropna()
    return out[out.speed_mps.between(0,16)]




def normalize_name(value):
    return re.sub(r'\s+', ' ', str(value or '').replace('–','-').replace('—','-')).strip().casefold()


def interpret_drill_timestamps(drill, clock='auto'):
    """Use the complete athlete-tagged drill at ANY local time of day.

    All actual API drill timestamps observed for this test are naive (no UTC
    offset). The GPS rows expose UTC times, so in auto mode the drill times
    are *provisionally* interpreted as UTC, consistently with the other
    successful STATSports assessments. That is an assumption, not proof.
    An operator can explicitly override this with --drill-clock local.

    We do NOT use previously supplied testing-day hours to select a clock:
    rejecting early or late tests would silently lose correctly tagged drills.
    """
    raw_start=drill.get('start')
    raw_end=drill.get('end')
    if not raw_start or not raw_end:
        raise ValueError('Tagged drill is missing start/end timestamps.')
    a,b=pd.Timestamp(raw_start),pd.Timestamp(raw_end)
    if pd.isna(a) or pd.isna(b):
        raise ValueError(f'Invalid drill timestamps: {raw_start!r} - {raw_end!r}')
    if (a.tzinfo is None) != (b.tzinfo is None):
        raise RuntimeError('Drill timestamp mismatch: one date has a UTC offset and the other does not.')
    if a.tzinfo is not None:
        if b<=a:
            raise RuntimeError('Drill end timestamp precedes start.')
        return a.tz_convert('UTC'),b.tz_convert('UTC'),'Timezone offsets provided by STATSports API; no time-of-day restrictions.'
    if b<=a:
        raise RuntimeError('Drill end timestamp precedes start.')
    if clock not in ('auto','utc','local'):
        raise ValueError(f'Unrecognized --drill-clock value: {clock}')
    if clock=='auto':
        clock='utc'
        msg=('Timezone absent from STATSports drill metadata; treating timestamps as UTC '
             'by default, consistent with known matching drills. NO time-of-day cutoff. '
             'If GPS does not overlap, verify the drill rawDataId and Sonra times.')
    else:
        msg=(f'Timezone absent from STATSports drill metadata; explicitly interpreted as '
             f'{clock.upper()} (--drill-clock). NO time-of-day cutoff.')
    zone='UTC' if clock=='utc' else TZ
    start=a.tz_localize(zone).tz_convert('UTC')
    end=b.tz_localize(zone).tz_convert('UTC')
    return start,end,msg


def drill_window(drill, alerts, clock='auto'):
    a, b, note = interpret_drill_timestamps(drill, clock)
    alerts.append(note)
    print('  Timestamp interpretation:', note)
    print(f'  Tagged drill in Eastern time: {a.tz_convert(TZ):%Y-%m-%d %I:%M:%S %p} '
          f'to {b.tz_convert(TZ):%I:%M:%S %p}')
    return (a.value-int(PAD_BEFORE_S*1e9), b.value+int(PAD_AFTER_S*1e9))


def safe_athlete_name(name):
    """Permit normal name characters only; used in PDF, HTML, and filenames."""
    label = ' '.join(str(name or '').split()).strip()
    if not label or len(label) > 100 or label.lower() in ('unknown player', 'unknown'):
        raise ValueError('Provide a real athlete name (not Unknown player).')
    if any(not (ch.isalnum() or ch in " .'-") for ch in label):
        raise ValueError("Athlete name may contain letters, numbers, spaces, periods, apostrophes, or hyphens.")
    return label


def profile_name(profile):
    if not isinstance(profile, dict):
        return None
    for field in ('displayName', 'playerName', 'fullName', 'shortName', 'name'):
        value = profile.get(field)
        if isinstance(value, str) and value.strip() and value.strip().lower() not in ('unknown', 'unknown player'):
            return ' '.join(value.split())
    first, last = profile.get('firstName'), profile.get('lastName')
    full = ' '.join(str(x).strip() for x in (first, last) if x)
    return full or None


def _exact_playerdetails_name(value):
    """Return a name only when the session-player's *own* playerDetails contains one.

    Do not search arbitrary session/drill labels or choose among multiple
    potentially unrelated profiles. playerDetails may be a dict, a single-item
    list, or a serialized JSON object. A free-text value is never treated as a
    verified player name.
    """
    if isinstance(value, str):
        try:
            value=json.loads(value)
        except (ValueError, TypeError):
            return None, None
    if isinstance(value,list):
        if len(value)!=1:return None,None
        value=value[0]
    if not isinstance(value,dict):return None,None

    def named_profile(obj,path,depth=0):
        if depth>4 or not isinstance(obj,dict):return []
        fields={re.sub(r'[^a-z0-9]','',str(k).lower()):v for k,v in obj.items()}
        direct={}
        for norm,dest in [('displayname','displayName'),('playername','playerName'),
                          ('fullname','fullName'),('shortname','shortName'),
                          ('firstname','firstName'),('lastname','lastName')]:
            if norm in fields:direct[dest]=fields[norm]
        # A plain 'name' field in playerDetails is acceptable only when
        # attached to this player, unlike a generic session 'name'.
        if 'name' in fields:direct['name']=fields['name']
        candidates=[]
        name=profile_name(direct)
        if name and name.casefold() not in ('unknown','unknown player','none','null'):
            candidates.append((name,path))
        # Only traverse likely player profile wrappers, not arbitrarily nested
        # arrays or drill metadata.
        for k,v in obj.items():
            if re.sub(r'[^a-z0-9]','',str(k).lower()) in ('player','profile','playerprofile','details','playerdetails','data'):
                if isinstance(v,dict):candidates.extend(named_profile(v,path+'.'+str(k),depth+1))
                elif isinstance(v,list) and len(v)==1 and isinstance(v[0],dict):
                    candidates.extend(named_profile(v[0],path+'.'+str(k)+'[0]',depth+1))
        return candidates

    names=named_profile(value,'playerDetails')
    unique={n.casefold(): (n,path) for n,path in names}
    if len(unique)!=1:return None,None
    return next(iter(unique.values()))


def _sessionplayer_identity(node):
    """Resolve a profile attached to this exact SessionPlayerData record."""
    if not isinstance(node,dict):return None,None,None
    details=node.get('playerDetails')
    from_details,path=_exact_playerdetails_name(details)
    embedded=node.get('player') if isinstance(node.get('player'),dict) else None
    from_player=profile_name(embedded) if embedded is not None else None
    from_parent=None
    if any(k in node for k in ('displayName','firstName','lastName','playerName','fullName')):
        from_parent=profile_name(node)
    choices=[(n,src) for n,src in ((from_details,path),(from_player,'player'),(from_parent,'sessionPlayer')) if n]
    labels={n.casefold():n for n,_ in choices}
    if len(labels)>1:return None,None,'conflicting names attached to same session player'
    if choices:
        name,src=choices[0]
        return name,src,None
    return None,None,None


def _sessionplayer_keys(node):
    """Use only IDs on the same session-player container (not a sibling)."""
    keys=set()
    for fld in ('id','sessionPlayerDataId'):
        k=_canonical_join_key('session_player_id',node.get(fld))
        if k:keys.add(k)
    for fld in ('rawDataId','sessionPlayerRawDataId','session_player_rawDataId','dataId'):
        k=_canonical_join_key('raw_id',node.get(fld))
        if k:keys.add(k)
    details=node.get('playerDetails')
    if isinstance(details,dict):
        k=_canonical_join_key('player_id',get_field(details,'id','playerId'))
        if k:keys.add(k)
    player=node.get('player')
    if isinstance(player,dict):
        k=_canonical_join_key('player_id',get_field(player,'id','playerId'))
        if k:keys.add(k)
    return keys


def recover_player_by_id(match, day):
    """Query public v7 getPlayerDetails only when we have an exact player profile ID.

    Never assign the only profile returned to a session merely by date: shared
    devices and different session records make that an unsafe join.
    """
    target = match.get('player_id')
    if not target:
        return None
    try:
        body = api('getPlayerDetails', {
            'thirdPartyApiId': API_KEY, 'sessionDate': f'{day}T00:00:00'})
    except Exception as exc:
        print('  Player-profile lookup unavailable:', type(exc).__name__)
        return None
    names = set()
    def walk(obj, depth=0):
        if depth > 12: return
        if isinstance(obj, list):
            for item in obj: walk(item,depth+1)
        elif isinstance(obj, dict):
            if str(obj.get('id')) == str(target):
                label=profile_name(obj)
                if label: names.add(label)
            for v in obj.values():
                if isinstance(v,(list,dict)): walk(v,depth+1)
    walk(body)
    if len(names)==1: return next(iter(names))
    if len(names)>1: print('  Conflicting profile names found for ID; no automatic attribution.')
    return None



# Player identity is per-SessionPlayerData (v7), not per GPS device or drill title.
# The HTTP endpoint getPlayerDetails has no player-id argument in the published
# ThirdPartyDto schema. Its output must therefore be matched via an actual ID.
def _identity_id(value):
    return str(value).strip().casefold() if value is not None and str(value).strip() else None


def _canonical_join_key(key, value):
    value = _identity_id(value)
    return (key, value) if value else None


def _profile_nodes(body, source):
    """Collect verified names and typed IDs, including nested playerDetails.

    Session-player records with attached playerDetails are linkable by their
    session player ID or raw ID; standalone player profiles may only join on
    a matching player ID. Never infer a name by roster order or device ID.
    """
    records=[]
    def walk(obj, depth=0):
        if depth>18:return
        if isinstance(obj,list):
            for x in obj:walk(x,depth+1)
            return
        if not isinstance(obj,dict):return
        if 'playerDetails' in obj or ('drills' in obj and 'id' in obj):
            name,field,conflict=_sessionplayer_identity(obj)
            if conflict:
                print('  WARNING: Inconsistent player names attached to a session-player record; skipping.')
                return  # do not accidentally recover one of the conflicting nested profiles
            if name and not conflict:
                keys=_sessionplayer_keys(obj)
                if keys:records.append({'name':name,'keys':keys,'source':source+' ('+str(field)+')'})
        # Standalone player profiles (getPlayerDetails) can be used only when
        # there is an actual player_id on the chosen drill. No shared-session
        # shortcut is allowed.
        elif any(k in obj for k in ('displayName','firstName','lastName','playerName','fullName')):
            name=profile_name(obj)
            k=_canonical_join_key('player_id',get_field(obj,'id','playerId'))
            if name and k:records.append({'name':name,'keys':{k},'source':source+' (standalone profile)'})
        for key,value in obj.items():
            if isinstance(value,(dict,list)) and key!='drills':walk(value,depth+1)
    walk(body)
    return records


def _resolve_profile_matches(match, sources):
    """Only return a name when the selected drill links uniquely to that name."""
    target_keys=set()
    for field,kind in [('player_id','player_id'),('session_player_id','session_player_id'),
                       ('raw_id','raw_id')]:
        k=_canonical_join_key(kind,match.get(field))
        if k:target_keys.add(k)
    # SessionId alone is insufficient when many people are part of the same session.
    profiles=[item for body,label in sources for item in _profile_nodes(body,label)]
    matches=[]
    for p in profiles:
        shared = p['keys'] & target_keys
        if shared:
            matches.append((p,shared))
    names={p['name'].casefold():p['name'] for p,_ in matches}
    if len(names)==1:
        candidate=next(iter(names.values()))
        priority={'player_id':0,'session_player_id':1,'raw_id':2}
        pairs=sorted([(priority.get(k[0],9),k[0],p['source']) for p,s in matches for k in s])
        _,key_used,src=pairs[0]
        return candidate,f'API verified {key_used} ({src})',profiles
    if len(names)>1:
        print('  API identity conflict: multiple names linked to the selected player/recording; refusing to guess.')
        for v in sorted(names.values()):print('   Candidate:',v)
    return None,None,profiles


def resolve_name_from_api(match, day, refresh=False):
    """Resolve athlete from session/roster API responses WITHOUT GPS requests."""
    sources=[]
    if MATCH_METADATA is not None and not refresh:
        sources.append((MATCH_METADATA,'getFullSessionsByDateRange'))
    elif refresh:
        try:sources.append((get_metadata(day),'getFullSessionsByDateRange'))
        except Exception as ex: print('  Session range lookup failed:',type(ex).__name__)
    name,src,profiles=_resolve_profile_matches(match,sources)
    if name:return name,src
    # v4 short-circuited at a matching drill and never checked getFullSession.
    try:
        full=api('getFullSession',{'thirdPartyApiId':API_KEY,'sessionDate':f'{day}T00:00:00'})
        sources.append((full,'getFullSession'))
    except Exception as ex:
        print('  getFullSession name lookup unavailable:',str(ex)[:140])
    name,src,profiles=_resolve_profile_matches(match,sources)
    if name:return name,src
    # v7 getPlayerDetails returns PlayerDataV6; schema accepts ThirdPartyDto,
    # NOT a player ID request. Only use it if its player.id matches selected id.
    try:
        detail=api('getPlayerDetails',{'thirdPartyApiId':API_KEY,'sessionDate':f'{day}T00:00:00'})
        sources.append((detail,'getPlayerDetails'))
    except Exception as ex:
        print('  getPlayerDetails name lookup unavailable:',str(ex)[:140])
    name,src,profiles=_resolve_profile_matches(match,sources)
    if name:return name,src
    # Diagnostic: no athlete PII written to disk or the console by default.
    print('  Could not establish a unique athlete name from STATSports API metadata.')
    print('  Exact IDs found: player_id=',bool(match.get('player_id')),
          '| session_player_id=',bool(match.get('session_player_id')),
          '| raw_id=',bool(match.get('raw_id')))
    print('  Named profiles available via API:',len(profiles),
          '| None confidently linked to this tagged drill.')
    print('  An API export/identity-link issue is likely; use --identity-audit to inspect metadata structure.')
    print('  In Sonra, share/export the player(s) WITH the session using Share Data > Third-Party API.')
    return None,None


def refresh_saved_reports_with_api_name(day, drill_clock='auto'):
    """Resolve and correct v4 reports using only STATSports player/session metadata."""
    print('Resolving the player via STATSports API; no raw GPS requests will be made.')
    matches,names=find_drill_with_diagnostics(day)
    chosen=select_drill(matches,names)
    if not chosen:return None
    drill_window(chosen,[],drill_clock)
    player,source=resolve_name_from_api(chosen,day)
    if player:
        outdir=OUTPUT_ROOT/f'statsports_tagged_assessment_{day}'
        path=outdir/'detection_review.json'
        if not path.is_file():
            print('  Verified:',player,'but no existing report to relabel.')
            return None
        previous=json.loads(path.read_text(encoding='utf-8')).get('selected',{})
        # The folder stores only ONE selected athlete for each date. When
        # multiple athletes have this tag, never overwrite another's report.
        for fld in ('session_player_id','raw_id','drill_id','start','end'):
            a,b=previous.get(fld),chosen.get(fld)
            if not a or not b or str(a)!=str(b):
                print('  API resolved:',player,'but existing report belongs to a different or unverified tagged drill.')
                print('  Refusing to overwrite. Select the drill that originally generated the saved report.')
                return None
        print('  Verified:',player,'|',source)
        return relabel_saved_report(day,player,source)
    print('  Existing reports left unchanged; no player identity could be verified.')
    return None

def relabel_saved_report(day, athlete, name_source="operator-confirmed"):
    """Fix existing PDF/HTML/CSV offline, no metadata or GPS API requests."""
    athlete=safe_athlete_name(athlete)
    outdir=OUTPUT_ROOT/f'statsports_tagged_assessment_{day}'
    stem=outdir/f'assessment_3sets_{day}'
    csv, pdf, htm = stem.with_suffix('.csv'), stem.with_suffix('.pdf'), stem.with_suffix('.html')
    review_path=outdir/'detection_review.json'
    if not csv.is_file() or not review_path.is_file():
        raise RuntimeError('Existing report missing. Run the normal assessment script first, or check --date.')
    df=pd.read_csv(csv)
    old_names=set(str(x) for x in df.get('Recording', pd.Series([],dtype='str')).dropna().unique())
    if len(old_names)>1:
        raise RuntimeError('Multiple recording identities found: refusing to relabel more than one athlete.')
    info=json.loads(review_path.read_text(encoding='utf-8'))
    prior=next(iter(old_names), 'Unknown player')
    for col in ('Athlete','Recording'):
        if col in df: df[col]=athlete
    warnings=[w for w in info.get('warnings',[])
              if 'Athlete identity not exposed' not in str(w) and 'Athlete name supplied manually' not in str(w)]
    if name_source.startswith('API'):
        warnings.append('Athlete identified from exact STATSports API identity link. Confirm shared-device assignments for physical wearer.')
    else:
        warnings.append('Athlete label supplied manually by operator; verify against Sonra and shared-device records.')
    reviews=info.get('blocks',[])
    for item in reviews: item['recording']=athlete
    if isinstance(info.get('selected'), dict):
        info['selected']['player']=athlete
        info['selected']['name_source']=name_source
    info['warnings']=warnings
    info['blocks']=reviews
    traces={}
    cache_folder=outdir/'cache'
    sqlite_files=list(cache_folder.glob('*.sqlite')) if cache_folder.is_dir() else []
    if len(sqlite_files)==1:
        db=sqlite3.connect(sqlite_files[0])
        try:
            ident=json.loads(dict(db.execute('SELECT key,value FROM metadata')).get('identity','{}'))
            lo,hi=ident.get('start_ns'),ident.get('end_ns')
            if lo is not None and hi is not None:
                trace,_=load_trace(db,(int(lo),int(hi)))
                events,sets,blocks,options=analyze_one(trace,athlete)
                traces={athlete:(trace,events,blocks)}
        finally:
            db.close()
    # Regenerate all three outputs from the existing CSV and cached compact trace.
    df.to_csv(csv,index=False)
    make_pdf(df,reviews,pdf,day)
    make_dashboard(df,reviews,warnings,traces,htm,pdf.name,csv.name,day)
    review_path.write_text(json.dumps(info,indent=2,default=str),encoding='utf-8')
    print('No API requests made. Reused original 30-rep data.')
    print('Athlete:',athlete, f'({name_source})')
    print('Dashboard:',htm,'\nPDF:',pdf,'\nCSV:',csv)
    return df

def get_matches(data):
    """Search session-player drills, tolerating slightly different API wrappers.

    Never guess a player from drill names. A match without a player/raw ID is
    reported but not downloaded.
    """
    matches, all_names = [], []
    seen = set()
    def walk(node, depth=0):
        if depth > 18: return
        if isinstance(node, list):
            for item in node: walk(item, depth+1)
        elif isinstance(node, dict):
            drills = node.get('drills')
            if isinstance(drills, list):
                p = node.get('player') if isinstance(node.get('player'),dict) else {}
                pname, _, identity_conflict=_sessionplayer_identity(node)
                if identity_conflict:
                    print('  WARNING: Conflicting name fields in a tagged athlete; leaving unidentified.')
                pname=pname if not identity_conflict and pname else 'Unknown player'
                details=node.get('playerDetails')
                details=details if isinstance(details,dict) else {}
                for drill in drills:
                    if not isinstance(drill,dict): continue
                    dname = get_field(drill,'name','drillName','title')
                    if not dname: continue
                    all_names.append((str(pname),str(dname)))
                    if normalize_name(dname) != normalize_name(DRILL_NAME): continue
                    assoc = drill.get('sessionPlayerDataId')
                    # STATSports v7 can specify association ID; matching this
                    # guards against attributing another athlete's GPS stream.
                    if assoc and node.get('id') and str(assoc) != str(node['id']): continue
                    raw = get_field(node,'session_player_rawDataId',
                         'sessionPlayerRawDataId','rawDataId','dataId')
                    if not raw: raw=get_field(p,'rawDataId','session_player_rawDataId')
                    match={'player':str(pname),'player_id':p.get('id') or details.get('id') or details.get('playerId') or node.get('playerId'),
                        'session_player_id':node.get('id'),'session_id':node.get('sessionId') or drill.get('sessionId'),
                        'raw_id':raw,
                        'raw_is_dataid':raw is not None and raw==node.get('dataId'),
                        'drill_id':drill.get('id'),
                        'start':get_field(drill,'startTime','start'),
                        'end':get_field(drill,'endTime','end'),
                        'name':str(dname)}
                    key=(str(match['session_player_id']),str(match['drill_id']),str(match['start']),str(match['end']))
                    if key not in seen: seen.add(key); matches.append(match)
            for k, v in node.items():
                if k!='drills':walk(v,depth+1)
    walk(data)
    return matches,sorted(set(all_names))


def metadata_report(body, label):
    """Print a minimal safe schema summary; no raw GPS calls or API key output."""
    info={'session_objects':0,'session_names':[], 'players':0,
          'drill_lists':0, 'drill_none':0,'drills':0,'drill_labels':[]}
    def scan(x,depth=0):
        if depth>18:return
        if isinstance(x,list):
            for obj in x:scan(obj,depth+1)
        elif isinstance(x,dict):
            if isinstance(x.get('session'),dict) and isinstance(x.get('players'),list):
                info['session_objects']+=1
                info['session_names'].append(str(x.get('name') or '(unnamed)'))
            if isinstance(x.get('players'),list):info['players']+=len(x['players'])
            if 'drills' in x:
                if isinstance(x['drills'],list):
                    info['drill_lists']+=1
                    info['drills']+=len(x['drills'])
                    for d in x['drills']:
                        if isinstance(d,dict):
                            name=get_field(d,'name','drillName','title',
                                           'primaryLabel','secondaryLabel','freeText')
                            if name:info['drill_labels'].append(str(name))
                elif x['drills'] is None:info['drill_none']+=1
            for k,v in x.items():
                if k!='drills':scan(v,depth+1)
    scan(body)
    print(f'  {label}: {info["session_objects"]} session objects; {info["players"]} player entries; '
          f'{info["drills"]} drills in {info["drill_lists"]} populated drill lists; '
          f'{info["drill_none"]} null drill lists')
    if info['session_names']:
        print('  Session names:',', '.join(info['session_names'][:8]))
    if info['drill_labels']:
        uniq=sorted(set(info['drill_labels']))
        print('  Visible drill names/labels (first 30):')
        for name in uniq[:30]:print('   -',name)
    else:
        print('  No drill names/labels returned with this metadata.')
    return info


MATCH_METADATA = None  # retained only in memory for exact identity joins

def find_drill_with_diagnostics(day):
    global MATCH_METADATA
    print('Checking session-range metadata (no GPS download)...')
    first=get_metadata(day)
    MATCH_METADATA = first
    metadata_report(first,'Date-range API')
    matches,names=get_matches(first)
    if matches:return matches,names
    print('Checking single-session endpoint as a fallback (no GPS download)...')
    try:
        second=api('getFullSession', {'thirdPartyApiId':API_KEY,
                                     'sessionDate':f'{day}T00:00:00'})
        metadata_report(second,'Single-session API')
        alt,alt_names=get_matches(second)
        if alt:return alt,alt_names
        names=sorted(set(names + alt_names))
    except Exception as ex:
        print(f'  Single-session endpoint unavailable: {type(ex).__name__}: {str(ex)[:180]}')
    return [],names


def get_metadata(day):
    return api('getFullSessionsByDateRange',{
        'thirdPartyApiId':API_KEY,
        'sessionStartDate':f'{day}T00:00:00',
        'sessionEndDate':f'{day}T23:59:59'})


def select_drill(matches, all_names, interactive=True):
    if not matches:
        print(f'\nNo exact {DRILL_NAME!r} drill is visible in API metadata for this date.')
        if all_names:
            print('Drills found (first 30, with player names):')
            for athlete,name in all_names[:30]:print(f'  {athlete}: {name}')
        else:
            print('The API returned no player-associated drills under the expected fields.')
        print('\nCheck in STATSports Sonra:')
        print('  1. Confirm this is a DRILL TITLE, not only a drill label.')
        print('  2. Confirm the drill includes the correct athlete and was saved/calculated.')
        print('  3. Re-share/re-export the October 20 session to the third-party API;')
        print('     if Sonra Cloud, allow synchronization to finish.')
        print('  4. Confirm you are accessing the account/API key that received this session.')
        print('No raw GPS downloaded.')
        return None
    for i,x in enumerate(matches,1):
        print(f'  [{i}] {x["player"]} | {x["name"]} | {x["start"]} - {x["end"]}')
    if len(matches)==1:return matches[0]
    if not interactive:raise RuntimeError('More than one matching drill; choose interactively to avoid mixing players.')
    while True:
        value=input('Select the ONE tagged athlete/drill number: ').strip()
        if value.isdigit() and 1<=int(value)<=len(matches):return matches[int(value)-1]
        print('Enter one of the listed numbers.')




def open_db(path, drill, window):
    db=sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS speed_bins (bin INTEGER PRIMARY KEY, n INTEGER NOT NULL, sum_speed REAL NOT NULL, max_speed REAL NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    ident=json.dumps({'rawid':str(drill['raw_id']),'spid':str(drill.get('session_player_id')),
      'start_ns':int(window[0]),'end_ns':int(window[1]),'bin_ms':BIN_MS,'protocol':'nextPage-v7'},sort_keys=True)
    vals=dict(db.execute('SELECT key,value FROM metadata'))
    if vals.get('identity') not in (None,ident):
        db.close();raise RuntimeError('Existing checkpoint does not match the selected drill. Delete the corresponding SQLite cache only after reviewing it.')
    if 'identity' not in vals:
        with db:db.execute('INSERT INTO metadata VALUES (?,?)',('identity',ident))
    return db


def load_trace(db, window):
    arr=db.execute('SELECT bin,n,sum_speed,max_speed FROM speed_bins ORDER BY bin').fetchall()
    meta=dict(db.execute('SELECT key,value FROM metadata'))
    if not arr:return pd.DataFrame(columns=['sec','mean','rawmax','count']),meta
    ids=np.array([r[0] for r in arr],dtype=np.int64)
    n=np.array([r[1] for r in arr],dtype=int)
    sums=np.array([r[2] for r in arr],dtype=float)
    mx=np.array([r[3] for r in arr],dtype=float)
    lo,hi=window
    grid=np.arange(lo//(BIN_MS*1_000_000), (hi-1)//(BIN_MS*1_000_000)+1, dtype=np.int64)
    loc=np.searchsorted(grid,ids)
    valid=(loc<len(grid));ids=ids[valid];n=n[valid];sums=sums[valid];mx=mx[valid];loc=loc[valid]
    valid2=grid[loc]==ids
    loc=loc[valid2];n=n[valid2];sums=sums[valid2];mx=mx[valid2]
    mean=np.full(len(grid),np.nan);rawmax=np.full(len(grid),np.nan);counts=np.zeros(len(grid),dtype=int)
    mean[loc]=sums/n;rawmax[loc]=mx;counts[loc]=n
    return pd.DataFrame({'sec':grid*(BIN_MS/1000.0),'mean':mean,'rawmax':rawmax,'count':counts}),meta


def next_cursor(body, old):
    if not isinstance(body,dict):return None
    val=body.get('nextPage')
    if val is None or val=='':return None
    try:num=int(val)
    except (TypeError,ValueError):raise RuntimeError(f'Invalid STATSports nextPage cursor: {val!r}')
    if num<=0:return None
    if old is not None and num==old:raise RuntimeError('API returned repeated nextPage cursor; preventing infinite download.')
    return num


def fetch_drill_trace(drill,window,outdir,fast_stop=True,gps_shift_hours=0.0,force_refresh=False):
    if not drill['raw_id']:
        raise RuntimeError('Matching drill was found but no raw GPS ID is present in its player record. We need to inspect the metadata field names before pulling GPS.')
    # Unique even if many athletes share the same drill ID and tag timestamps.
    cache_identity='|'.join(str(drill.get(k) or '') for k in
        ('raw_id','session_player_id','drill_id','start','end'))
    if gps_shift_hours:
        cache_identity+='|gps_shift_hours='+str(gps_shift_hours)
    key=hashlib.sha256(cache_identity.encode('utf-8')).hexdigest()[:24]
    target=outdir/'cache'/f'{key}.sqlite'
    if force_refresh and target.exists():
        stamped=target.with_suffix('.sqlite.backup_'+datetime.now().strftime('%Y%m%d_%H%M%S'))
        target.rename(stamped)
        print('  Previous GPS cache backed up before forced re-download:',stamped.name)
    target.parent.mkdir(parents=True,exist_ok=True)
    # Reuse a compatible v7 single-athlete cache, if the earlier script already
    # downloaded it. Require exact stream AND window IDs to avoid mixing athletes.
    if not target.exists() and not gps_shift_hours and not force_refresh:
        olddir=OUTPUT_ROOT / f'statsports_tagged_assessment_{pd.Timestamp(window[0],unit="ns",tz="UTC").tz_convert(TZ).date()}' / 'cache'
        if olddir.exists():
            expected={'rawid':str(drill['raw_id']),'spid':str(drill.get('session_player_id')),
                 'start_ns':int(window[0]),'end_ns':int(window[1]),'bin_ms':BIN_MS}
            for oldfile in sorted(olddir.glob('*.sqlite')):
                try:
                    old=sqlite3.connect(f'file:{oldfile}?mode=ro',uri=True)
                    try:
                        vals=dict(old.execute('SELECT key,value FROM metadata'))
                        ident=json.loads(vals.get('identity','{}'))
                        if all(ident.get(k)==v for k,v in expected.items()):
                            new=sqlite3.connect(str(target))
                            try:old.backup(new)
                            finally:new.close()
                            print('  Reused compatible older single-athlete GPS cache.')
                            break
                    finally:old.close()
                except (sqlite3.Error,ValueError,TypeError):
                    continue
    db=open_db(target,drill,window)
    vals=dict(db.execute('SELECT key,value FROM metadata'))
    if vals.get('done')=='yes':
        print('Using completed local cache (no API GPS requests).')
        trace,meta=load_trace(db,window);db.close();return trace,meta
    cursor=int(vals['cursor']) if vals.get('cursor') not in (None,'') else None
    count=int(vals.get('pages',0));scanned=int(vals.get('rows',0))
    lo,hi=window
    monotone=True;previous_max=float(vals.get('last_max_ns','-inf'))
    if vals.get('monotone')=='false':monotone=False
    streak=int(vals.get('ordered_streak','0'))
    stream_first_ns=float(vals.get('stream_first_ns','inf'))
    stream_last_ns=float(vals.get('stream_last_ns','-inf'))
    seen=set()
    for page in range(count+1,MAX_PAGES+1):
        payload={'thirdPartyApiId':API_KEY,'rawDataId':drill['raw_id']}
        if cursor is not None:payload['nextPage']=cursor
        body=api('getSessionGpsData',payload)
        if not isinstance(body,dict):
            raise RuntimeError('GPS response not a dictionary with data/nextPage; check API schema.')
        batch=unwrap_gps(body)
        if not batch:
            with db:db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('done','yes'))
            print('No more GPS data.');break
        frame=normalize_gps(batch)
        if gps_shift_hours and not frame.empty:
            # Explicit, user-controlled correction. NEVER infer and silently shift time.
            frame['timestamp']=frame['timestamp']+pd.to_timedelta(gps_shift_hours,unit='h')
        if not frame.empty:
            ns=frame.timestamp.astype('int64').to_numpy()
            mi=float(np.nanmin(ns));ma=float(np.nanmax(ns))
            if np.any(np.diff(ns)<0) or mi<previous_max:monotone=False
            previous_max=max(previous_max,ma)
            stream_first_ns=min(stream_first_ns,mi)
            stream_last_ns=max(stream_last_ns,ma)
            streak=streak+1 if monotone else 0
        else:mi=ma=float('nan');streak=0
        entries=batch_to_bins(frame,window)
        next_c=next_cursor(body,cursor)
        # Save bins and cursor in one transaction. Safe to resume without double-counting.
        with db:
            db.executemany('''INSERT INTO speed_bins (bin,n,sum_speed,max_speed) VALUES (?,?,?,?)
             ON CONFLICT(bin) DO UPDATE SET n=n+excluded.n,sum_speed=sum_speed+excluded.sum_speed,
              max_speed=MAX(max_speed,excluded.max_speed)''', entries)
            count+=1;scanned+=len(batch)
            props={'cursor':'' if next_c is None else str(next_c),'pages':str(count),'rows':str(scanned),
              'monotone':str(monotone).lower(),'ordered_streak':str(streak),'last_max_ns':str(previous_max)}
            if np.isfinite(mi):
                props['stream_first_ns']=str(int(stream_first_ns))
                props['stream_last_ns']=str(int(stream_last_ns))
                props['stream_first_page_after_tag']='yes' if count==1 and mi>hi else vals.get('stream_first_page_after_tag','no')
            db.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)',props.items())
        if page==1 or page%10==0 or entries:
            print(f'GPS page {page} | {scanned:,} rows | {len(entries)} time-bins in drill window')
        if next_c is None:
            with db:db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('done','yes'))
            print('Reached end of GPS recording.');break
        if fast_stop and streak>=3 and np.isfinite(mi) and mi>hi:
            with db:
                db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('done','yes'))
                db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('early_stop','yes'))
            print('Stopped early: multiple sequential GPS pages confirmed past tagged drill end.')
            break
        if next_c in seen:raise RuntimeError('Duplicate nextPage cursor within run; halted.')
        seen.add(next_c);cursor=next_c
    else:print('WARNING: page limit reached; data incomplete.')
    trace,meta=load_trace(db,window);db.close()
    return trace,meta

def batch_to_bins(frame, window):
    if frame.empty:return []
    lo,hi=window
    ts=frame.timestamp.astype('int64').to_numpy()
    mask=(ts>=lo)&(ts<hi)
    if not mask.any():return []
    tiny=pd.DataFrame({'bin':ts[mask]//(BIN_MS*1_000_000),
                       'v':frame.speed_mps.to_numpy()[mask]})
    grouped=tiny.groupby('bin',sort=False)['v'].agg(['size','sum','max'])
    return [(int(idx),int(r['size']),float(r['sum']),float(r['max'])) for idx,r in grouped.iterrows()]


def extract_events(trace):
    """Find short acceleration-to-peak efforts; no hard 1.5s above-threshold cutoff.

    Steps: 0.6s median denoise, 0.8s local max, prior low-speed context,
    0.4-3.4s rise time, post-peak fall, 5s refractory. Parameters heuristic.
    """
    if trace.empty:return []
    raw=trace['mean'].to_numpy(dtype=float)
    sm=pd.Series(raw).rolling(3,center=True,min_periods=2).median().to_numpy()
    t=trace.sec.to_numpy(dtype=float)
    events=[]
    ix=np.flatnonzero(np.isfinite(sm)&(sm>=PEAK_FLOOR_MPS))
    for i in ix:
        lb=max(0,i-4);rb=min(len(sm),i+5)
        v=sm[i]
        if np.nanmax(sm[lb:rb])>v+.03:continue
        if np.isfinite(sm[i-1]) and sm[i-1]>v+.01:continue
        pre=sm[max(0,i-19):max(0,i-2)]
        prior=np.flatnonzero(np.isfinite(pre))
        if len(prior)<2:continue
        low=float(np.nanmin(pre))
        rise=v-low
        if rise<1.8:continue
        # Determine preceding trough and final speed-rise crossing, not total time above threshold.
        pre_start=max(0,i-19)
        eligible=np.where(np.isfinite(sm[pre_start:i]) &
                    (sm[pre_start:i]<=min(low+max(.35,rise*.22),2.6)))[0]
        if not len(eligible):continue
        onset_idx=pre_start+eligible[-1]
        rise_s=t[i]-t[onset_idx]
        if not .4<=rise_s<=3.4:continue
        after=sm[i+1:min(len(sm),i+17)]
        post_fall=(np.nanmin(after) if np.isfinite(after).any() else v)
        has_fall=(v-post_fall)>=1.0
        if not has_fall:continue
        adjacent_max=trace.rawmax.iloc[max(0,i-1):min(len(trace),i+2)].max()
        spike=max(0.0,float(adjacent_max-v)) if np.isfinite(adjacent_max) else 0.0
        quality=round(max(0.0,1.0 - max(0,rise_s-2.5)*.15 - max(0,spike-.8)*.18),3)
        events.append({'peak_time':float(t[i]),'onset_time':float(t[onset_idx]),
                       'peak_mps':float(v),'raw_peak_mps':float(adjacent_max),
                       'rise_s':float(rise_s),'rise_mps':float(rise),'quality':quality,
                       'spike_flag':spike>1.5})
    # Non-maximum suppression: a weaker walk/jog peak just before a sprint
    # must NOT suppress the actual sprint (this occurred in synthetic tests).
    events.sort(key=lambda e:-(e['peak_mps']*e['quality']))
    kept=[]
    for candidate in events:
        if any(abs(candidate['peak_time']-ev['peak_time'])<5.0 for ev in kept):
            continue
        kept.append(candidate)
    return sorted(kept,key=lambda e:e['peak_time'])


def find_sets(events):
    """Fit 10-slot 17s time lattice to peaks, tolerate ≤2 missing samples.
    Rep assignment from pattern, not independent first-30 candidate ranking.
    """
    if len(events)<MIN_REPS_PER_SET:return []
    events=sorted(events,key=lambda e:e['peak_time'])
    t=np.array([e['peak_time'] for e in events]);
    seeds=set()
    for tt in t:
        for k in range(10):seeds.add(round((tt-k*CADENCE_S)*5)/5)
    found={}
    for start in seeds:
        if start < t[0]-9*CADENCE_S-2 or start>t[-1]:continue
        matches={};errs=[];quality=0.0;used=set()
        for k in range(10):
            target=start+k*CADENCE_S
            lo=np.searchsorted(t,target-MATCH_TOLERANCE_S)
            hi=np.searchsorted(t,target+MATCH_TOLERANCE_S,side='right')
            options=[j for j in range(lo,hi) if j not in used]
            if not options:continue
            j=min(options,key=lambda x:(abs(t[x]-target)-.16*events[x]['quality']))
            used.add(j);matches[k+1]=j
            errs.append(abs(t[j]-target));quality+=events[j]['quality']
        if len(matches)<MIN_REPS_PER_SET:continue
        # A coach may initiate the first repetition of a set independently
        # of the 17-second cadence used for repetitions 2-10. If 8+ of those
        # later repetitions tightly agree, give the set-opening peak a wider
        # *provisional* search window. This does not relax reps 2-10.
        relaxed_first=False
        first_offset_s=0.0
        if 1 not in matches and sum(2 <= slot <= 10 for slot in matches) >= 8:
            lo=np.searchsorted(t,start-SET_OPENING_TOLERANCE_S)
            hi=np.searchsorted(t,start+SET_OPENING_TOLERANCE_S,side='right')
            options=[j for j in range(lo,hi) if j not in used]
            if options:
                j=min(options,key=lambda x:(abs(t[x]-start)-.16*events[x]['quality']))
                # Leave unusual peaks for QC, not automatic acceptance as truth.
                matches[1]=j
                used.add(j)
                first_offset_s=float(t[j]-start)
                errs.append(abs(first_offset_s))
                quality+=events[j]['quality']
                relaxed_first=True
        error=float(np.median(errs))
        if error>0.85:continue
        # Do not claim confidence from speed fatigue monotonicity.
        # An isolated first-rep phase offset is less concerning than a drift
        # across repetitions 2-10. Penalize it less, but keep the QC flag.
        weighted_error=sum(errs) - (.75*abs(first_offset_s) if relaxed_first else 0.0)
        score=len(matches)*4 + quality*.5 - weighted_error*1.5
        key=tuple(sorted(matches.items()))
        prev=found.get(key)
        obj={'start':float(start),'matches':matches,'count':len(matches),
             'median_error_s':round(error,2),'score':round(score,2),
             'first_rep_relaxed':relaxed_first,'first_rep_offset_s':round(first_offset_s,2)}
        if prev is None or score>prev['score']:found[key]=obj
    choices=sorted(found.values(),key=lambda x:(-x['score'],x['median_error_s']))
    keep=[]
    for s in choices:
        ids=set(s['matches'].values())
        if any(len(ids&set(x['matches'].values()))>=5 for x in keep):continue
        keep.append(s)
    return sorted(keep,key=lambda x:x['start'])


def assemble_blocks(sets):
    """Group full ten-rep sets with approximate rest plus restart allowance.
    Ambiguity is FLAGGED, never silently interpreted as a verified false start.
    """
    if len(sets)<3:return [],[]
    options=[]
    for i,a in enumerate(sets):
        for j in range(i+1,len(sets)):
            b=sets[j];gap1=b['start']-(a['start']+9*CADENCE_S)
            if not REST_GAP_RANGE_S[0]<=gap1<=REST_GAP_RANGE_S[1]:continue
            for k in range(j+1,len(sets)):
                c=sets[k];gap2=c['start']-(b['start']+9*CADENCE_S)
                if not REST_GAP_RANGE_S[0]<=gap2<=REST_GAP_RANGE_S[1]:continue
                count=sum(s['count'] for s in (a,b,c))
                err=sum(s['median_error_s'] for s in (a,b,c))
                lag_penalty=(abs(gap1-IDEAL_GAP_S)+abs(gap2-IDEAL_GAP_S))*.04
                score=count*2-err*2-lag_penalty
                options.append({'indices':(i,j,k),'sets':[a,b,c],
                                'count':count,'gap1':round(gap1,1),'gap2':round(gap2,1),
                                'score':round(score,2)})
    options.sort(key=lambda x:x['score'],reverse=True)
    winners=[];occupied=set()
    for item in options:
        if any(k in occupied for k in item['indices']):continue
        winners.append(item);occupied.update(item['indices'])
    for win in winners:
        alternatives=[x for x in options if x is not win and
                      len(set(x['indices'])&set(win['indices']))>=2 and
                      x['score']>=win['score']-3]
        win['ambiguous']=bool(alternatives)
        win['alternatives']=len(alternatives)
        win['possible_restart']=any(not NORMAL_REST_GAP_RANGE_S[0] <= gap <= NORMAL_REST_GAP_RANGE_S[1] for gap in (win['gap1'],win['gap2']))
        win['requires_review']=(win['ambiguous'] or win['possible_restart'] or win['count']<30
             or any(s.get('first_rep_relaxed') for s in win['sets']))
    return sorted(winners,key=lambda w:w['sets'][0]['start']),options


def result_rows(events, block, recording, block_label, trace, local_day):
    rows=[]
    for s,seq in enumerate(block['sets'],1):
        for r in range(1,11):
            i=seq['matches'].get(r)
            ev=events[i] if i is not None else None
            rep=(s-1)*10+r
            if ev:
                peak=ev['peak_mps'];ts=pd.Timestamp(ev['peak_time'],unit='s',tz='UTC').tz_convert(TZ)
            else:peak=np.nan;ts=pd.NaT
            opening_flag = bool(ev and r==1 and seq.get('first_rep_relaxed'))
            rows.append({'Athlete':recording,'Recording':recording,
                         'Block':block_label,'Set':s,'Set Rep':r,'Rep':rep,
                         'Peak (m/s)':peak,'Peak (mph)':peak*MPH,
                         'Raw max (mph)':ev['raw_peak_mps']*MPH if ev else np.nan,
                         'Peak time local':str(ts) if ev else '',
                         'Detection':'Review' if ev and (block['requires_review'] or ev['spike_flag']) else ('Candidate' if ev else 'Missing'),
                         'QC note':('Set-opening offset %.2fs: REVIEW' % seq.get('first_rep_offset_s',0)
                                    if opening_flag else ('Possible GPS spike' if ev and ev['spike_flag'] else '')),
                         'Timing error (s)':round(abs(ev['peak_time']-(seq['start']+(r-1)*CADENCE_S)),2) if ev else np.nan,
                         'Samples':0})
    df=pd.DataFrame(rows)
    df['Previous (mph)']=df.groupby(['Recording','Block','Set'])['Peak (mph)'].shift(1)
    df['Rep decrement (%)']=(df['Previous (mph)']-df['Peak (mph)'])/df['Previous (mph)']*100
    best=df.groupby(['Recording','Block','Set'])['Peak (mph)'].transform('max')
    df['Drop from set best (%)']=(best-df['Peak (mph)'])/best*100
    return df


def make_pdf(df, review, path, day):
    """Compact two-page-plus PDF: group summary, then ONE page per assessment."""
    styles=getSampleStyleSheet()
    doc=SimpleDocTemplate(str(path),pagesize=landscape(letter),leftMargin=30,
                          rightMargin=30,topMargin=24,bottomMargin=23)
    parts=[Paragraph('STATSports | Repeated Sprint GPS Review',styles['Title']),
           Paragraph(f'{day} | Tagged drill: {DRILL_NAME} | 3 sets of 10 | 17-second starts',styles['Normal']),
           Paragraph(OUTPUT_NOTE,styles['Normal']),Spacer(1,11)]
    def tab(rows, widths=None):
        t=Table(rows,repeatRows=1,colWidths=widths,hAlign='LEFT')
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#18334a')),
                 ('TEXTCOLOR',(0,0),(-1,0),colors.white),('FONTSIZE',(0,0),(-1,-1),8),
                 ('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),
                 ('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5),
                 ('GRID',(0,0),(-1,-1),.25,colors.lightgrey)]))
        return t
    grp=df.groupby('Rep')['Peak (mph)'].agg(['mean','count']).reset_index()
    parts.append(Paragraph('Average detected peak speed by repetition',styles['Heading2']))
    rows=[['Set 1 rep','Avg mph','N','Set 2 rep','Avg mph','N','Set 3 rep','Avg mph','N']]
    for r in range(10):
        cells=[]
        for set_n in range(3):
            rep=set_n*10+r+1
            d=grp[grp.Rep==rep]
            avg=float(d['mean'].iloc[0]) if len(d) else float('nan')
            n=int(d['count'].iloc[0]) if len(d) else 0
            cells.extend([str(rep),f'{avg:.2f}' if np.isfinite(avg) else '--',str(n)])
        rows.append(cells)
    parts.extend([tab(rows,[77,83,50]*3),Spacer(1,9),
       Paragraph('Only matched GPS-derived efforts contribute to means. N counts detected blocks; this is a single-athlete test.',styles['Normal'])])
    fmt=lambda x:f'{x:.2f}' if pd.notna(x) else '--'
    for (rec,blk),sub in df.groupby(['Recording','Block'],sort=True):
        parts.append(PageBreak())
        parts.append(Paragraph(html.escape(f'{rec} | {blk}'),styles['Heading1']))
        data=next((x for x in review if x['recording']==rec and x['block']==blk),None)
        if data:
            parts.append(Paragraph(f"Matched: {data['count']}/30 | Gap after set 1: {data['gap1']}s | After set 2: {data['gap2']}s | {html.escape(data['flag'])}",styles['Normal']))
        parts.append(Spacer(1,8))
        summaries=[]
        for set_n,g in sub.groupby('Set'):
            p=g['Peak (mph)'];first=p.iloc[0];last=p.iloc[-1]
            d=(first-last)/first*100 if pd.notna(first) and pd.notna(last) and first else np.nan
            summaries.append(f'Set {set_n}: mean {fmt(p.mean())}, best {fmt(p.max())}, first-last {fmt(d)}%')
        for txt in summaries:parts.append(Paragraph(txt,styles['Normal']))
        parts.append(Spacer(1,10))
        rows=[['Set 1 rep','Peak mph','Dec %','QC','Set 2 rep','Peak mph','Dec %','QC',
               'Set 3 rep','Peak mph','Dec %','QC']]
        by_set={k:v.sort_values('Set Rep') for k,v in sub.groupby('Set')}
        for r in range(10):
            cells=[]
            for s in (1,2,3):
                item=by_set[s].iloc[r]
                qc='Spike?' if item['QC note'] else ('Missing' if item['Detection']=='Missing' else ('Review' if item['Detection']=='Review' else 'Found'))
                cells.extend([str(int(item['Rep'])),fmt(item['Peak (mph)']),fmt(item['Rep decrement (%)']),qc])
            rows.append(cells)
        parts.append(tab(rows,[43,56,46,57]*3))
        parts.append(Spacer(1,10))
        parts.append(Paragraph('Speed values are robust 0.2-second-bin peaks after smoothing; they are not electronic 10-yard sprint times.',styles['Normal']))
    doc.build(parts)


def make_dashboard(df, review, warnings, traces, outfile, pdfname,csvname,day):
    content=[]
    css='''<style>body{font:14px system-ui,-apple-system,sans-serif;max-width:1160px;margin:25px auto;padding:0 20px;background:#f2f5f9;color:#152f42}h1{font-size:28px}section{background:white;border-radius:12px;padding:20px;margin:18px 0}table{border-collapse:collapse;background:white;width:100%;font-size:13px;margin:10px 0}td,th{padding:8px;border-bottom:1px solid #ddd;text-align:left}th{background:#18354b;color:white}.warning{padding:12px;background:#fff3d5;border-radius:8px}.btn{background:#18354b;color:white;border-radius:7px;padding:10px 15px;text-decoration:none;display:inline-block;margin:8px}</style>'''
    title=f'''<h1>STATSports | 30-Rep GPS Assessment</h1><p>{html.escape(day)} | Tagged drill: Assessment - 3 Sets | 10 yard reps, 17 second starts, 3 x 10</p>
    <p class="warning">{html.escape(OUTPUT_NOTE)} This is a candidate-detection dashboard, not validated sprint timing. GPS-derived peak speed is not 10-yard split time.</p>
    <p>{('<a class="btn" href="'+html.escape(pdfname)+'" download>Download PDF</a>') if pdfname else ''}{('<a class="btn" href="'+html.escape(csvname)+'" download>Download CSV</a>') if csvname else ''}</p>'''
    if review:
        content.append('<section><h2>Detection quality / possible restarts</h2>'+pd.DataFrame(review).to_html(index=False,escape=True)+'</section>')
    if warnings:
        content.append('<section><h2>Warnings</h2><ul>'+''.join('<li>'+html.escape(w)+'</li>' for w in warnings)+'</ul></section>')
    if not df.empty:
        avg=df.groupby('Rep')['Peak (mph)'].agg(['mean','count']).reset_index()
        fig=go.Figure()
        for si in (1,2,3):
            g=avg[avg.Rep.between((si-1)*10+1,si*10)]
            fig.add_scatter(x=g.Rep,y=g['mean'],mode='lines+markers',name=f'Set {si}',
                            customdata=g['count'],hovertemplate='Rep %{x}: %{y:.2f} mph | N=%{customdata}<extra></extra>')
        fig.update_layout(title='Mean peak speed by repetition (across detected blocks)',xaxis_title='Rep',yaxis_title='mph',template='plotly_white')
        content.append('<section>'+plot(fig,include_plotlyjs=True,output_type='div')+avg.to_html(index=False,float_format=lambda x:f'{x:.2f}')+'</section>')
        for (rec,blk),sub in df.groupby(['Recording','Block'],sort=True):
            fig=go.Figure()
            for si,g in sub.groupby('Set'):
                fig.add_scatter(x=g['Rep'],y=g['Peak (mph)'],mode='lines+markers',name=f'Set {si}',
                                customdata=g['Rep decrement (%)'],hovertemplate='Rep %{x} | %{y:.2f} mph | Δ %{customdata:.2f}%<extra></extra>')
            fig.update_layout(template='plotly_white',title=f'{rec} - {blk}',xaxis_title='Rep',yaxis_title='mph')
            content.append('<section><h2>'+html.escape(f'{rec} - {blk}')+'</h2>'+plot(fig,include_plotlyjs=False,output_type='div')+
                           sub[['Rep','Set','Peak (mph)','Rep decrement (%)','Timing error (s)','Detection','QC note']].to_html(index=False,na_rep='-',float_format=lambda x:f'{x:.2f}')+'</section>')
    for rec, payload in traces.items():
        trace,events,blocks=payload
        if trace.empty:continue
        fig=go.Figure()
        # 0.2s speed trace with 90min extent; downsample for smaller HTML.
        clip=trace.iloc[::max(1,len(trace)//14000)]
        t=pd.to_datetime(clip.sec,unit='s',utc=True).dt.tz_convert(TZ)
        fig.add_scatter(x=t,y=clip['mean']*MPH,mode='lines',line={'width':1},name='Binned mean speed (mph)')
        if events:
            ev_t=pd.to_datetime([x['peak_time'] for x in events],unit='s',utc=True).tz_convert(TZ)
            fig.add_scatter(x=ev_t,y=[x['peak_mps']*MPH for x in events],mode='markers',name='Candidate peaks',marker={'size':4})
        for bi,b in enumerate(blocks):
            start=min(s['start'] for s in b['sets'])-3
            end=max(s['start']+9*CADENCE_S for s in b['sets'])+3
            fig.add_vrect(x0=pd.Timestamp(start,unit='s',tz='UTC').tz_convert(TZ),
                          x1=pd.Timestamp(end,unit='s',tz='UTC').tz_convert(TZ),fillcolor='green',opacity=.08,line_width=0)
        fig.update_layout(template='plotly_white',height=320,title=f'{rec} - speed trace and proposed blocks',xaxis_title='Local time',yaxis_title='mph')
        content.append('<section>'+plot(fig,include_plotlyjs=not bool(df.shape[0]),output_type='div')+'</section>')
    if not content:content.append('<section>No assessment blocks detected. Review speed traces and warnings.</section>')
    outfile.write_text('<!doctype html><html><head><meta charset="utf-8">'+css+'</head><body>'+title+'\n'.join(content)+'</body></html>',encoding='utf-8')



def cadence_diagnostics(events, athlete, idhash, folder):
    """Write full candidate timing evidence, including gaps and 3-set clusters."""
    if not events: return
    times=np.array([e['peak_time'] for e in events],dtype=float)
    gaps=np.diff(times)
    records=[]
    for i,e in enumerate(events):
        records.append({'Athlete':athlete,'Candidate':i+1,
                        'UTC':pd.Timestamp(e['peak_time'],unit='s',tz='UTC').isoformat(),
                        'Local':pd.Timestamp(e['peak_time'],unit='s',tz='UTC').tz_convert(TZ).isoformat(),
                        'Gap from previous peak (s)':round(float(gaps[i-1]),2) if i else np.nan,
                        'Smoothed peak (mph)':round(e['peak_mps']*MPH,3),
                        'Raw peak (mph)':round(e['raw_peak_mps']*MPH,3),
                        'GPS spike warning':e.get('spike_flag',False),
                        'Quality':e.get('quality')})
    pd.DataFrame(records).to_csv(folder/f'peak_timing_audit_{idhash}.csv',index=False)


def evidence_based_3x10_fallback(events):
    """Conservative recovery if 3x10 candidates already exist but strict lattice fails.

    Does NOT manufacture peaks or fill missing slots. Requires exact 10/10/10
    candidate split, plausible cadence, 2 clear set-break gaps and uniquely
    separated repetitions. All recovered blocks are flagged for human review.
    """
    if len(events)!=30:return None, 'Need exactly 30 distinct candidate peaks; found '+str(len(events))
    ev=sorted(events,key=lambda x:x['peak_time'])
    t=np.array([e['peak_time'] for e in ev],dtype=float)
    diff=np.diff(t)
    # Breaks at rep 10->11 and rep 20->21, not based on speed rankings.
    for boundary in (9,19):
        if not 120<=diff[boundary]<=420:
            return None, f'Gap after candidate {boundary+1} is {diff[boundary]:.1f}s (expected 120-420s)'
    internal=np.concatenate((diff[0:9],diff[10:19],diff[20:29]))
    if np.any(internal<12) or np.any(internal>23):
        return None, f'Within-set interval outside 12-23s: {internal.min():.1f}-{internal.max():.1f}s'
    if abs(float(np.median(internal))-CADENCE_S)>1.5:
        return None, f'Median within-set peak interval {np.median(internal):.1f}s not close to 17s'
    # Require nearly 17s average cadence in EACH set; tolerate peak drift
    # larger than the legacy +/-1.5s exact time lattice.
    sets=[]
    for g in range(3):
        k=10*g
        sub=diff[k:k+9]
        if abs(float(np.mean(sub))-CADENCE_S)>2.0:
            return None, f'Set {g+1} mean interpeak interval {np.mean(sub):.1f}s outside 15-19s'
        if np.max(abs(sub-CADENCE_S))>6.0:
            return None, f'Set {g+1} has interval >6s from expected 17s'
        start=float(t[k])
        err=np.abs(t[k:k+10]-(start+np.arange(10)*CADENCE_S))
        sets.append({'start':start,'matches':{i+1:k+i for i in range(10)},
                     'count':10,'median_error_s':round(float(np.median(err)),2),
                     'score':0.0,'first_rep_relaxed':False,'first_rep_offset_s':0.0})
    gap1=float(diff[9]);gap2=float(diff[19]);
    block={'indices':(0,1,2),'sets':sets,'count':30,'gap1':round(gap1,1),
           'gap2':round(gap2,1),'score':0,'ambiguous':False,'alternatives':0,
           'possible_restart':not all(NORMAL_REST_GAP_RANGE_S[0]<=v<=NORMAL_REST_GAP_RANGE_S[1] for v in (gap1,gap2)),
           'requires_review':True,'fallback_method':'EXACT 30 candidates, 10 per set, cadence timing QC (PROVISIONAL)'}
    return block,'Recovered structurally plausible 3x10, all rows flagged REVIEW; confirm raw trace and athlete ID'


def analyze_one(trace, recording):
    ev=extract_events(trace)
    sets=find_sets(ev)
    blocks,options=assemble_blocks(sets)
    if not blocks:
        recovered, reason=evidence_based_3x10_fallback(ev)
        if recovered:
            print('  REVIEW-ONLY cadence recovery:',reason)
            blocks=[recovered]
        elif len(ev)>=24:
            print('  3x10 timing audit (no automatic recovery):',reason)
    return ev,sets,blocks,options



def process(day,list_only=False,no_early_stop=False,drill_clock="auto", athlete=None, no_prompt=False, allow_unknown=False):
    outdir=OUTPUT_ROOT/f'statsports_tagged_assessment_{day}'
    outdir.mkdir(parents=True,exist_ok=True)
    print(f'Looking for the exact tagged drill: {DRILL_NAME}')
    matches,names=find_drill_with_diagnostics(day)
    chosen=select_drill(matches,names)
    if chosen is None:
        return None
    alerts=[]
    window=drill_window(chosen,alerts,drill_clock)
    name_source='API player metadata'
    if chosen['player']=='Unknown player':
        recovered,recovery_source=resolve_name_from_api(chosen,day)
        if recovered:
            chosen['player']=recovered
            name_source=recovery_source
            print('  Resolved player from API:',recovered,'|',name_source)
    if athlete:
        athlete=safe_athlete_name(athlete)
        if chosen['player']!='Unknown player' and chosen['player'].casefold()!=athlete.casefold():
            print('  WARNING: Manual athlete differs from API player:',chosen['player'])
        chosen['player']=athlete
        name_source='operator-confirmed'
    chosen['name_source']=name_source if chosen['player']!='Unknown player' else 'unresolved'
    start=pd.Timestamp(window[0],unit='ns',tz='UTC').tz_convert(TZ)
    end=pd.Timestamp(window[1],unit='ns',tz='UTC').tz_convert(TZ)
    print(f'Selected athlete: {chosen["player"]}')
    print(f'Using tagged window (with 3-second buffers): {start:%H:%M:%S} - {end:%H:%M:%S} Eastern')
    if chosen.get('raw_is_dataid'):alerts.append('Raw GPS ID inferred from SessionPlayerData.dataId; STATSports docs do not verify this mapping.')
    print('  Raw GPS ID available:', 'Yes' if chosen.get('raw_id') else 'No')
    if chosen['player']=='Unknown player':
        alerts.append('Athlete identity not exposed in retrieved drill metadata. Do not attribute this report to a named athlete until verified.')
        print('  Athlete identity not provided in metadata; verify the device-to-athlete assignment before interpreting results.')
    elif name_source=='operator-confirmed':
        alerts.append('Athlete name supplied manually. Confirm physical device assignment before using results.')
        print('  Athlete label: operator-confirmed; physical device assignment still needs verification.')
    if list_only:
        print('Metadata-only mode: no GPS has been downloaded.')
        return None
    if chosen['player']=='Unknown player' and not allow_unknown:
        raise RuntimeError('No verifiable player name in STATSports API response. Refusing to download GPS or assign a false identity. In Sonra, re-share the session WITH the selected player through Third-Party API, then rerun. Use --allow-unknown only for a diagnostic unlabeled report.')
    trace,meta=fetch_drill_trace(chosen,window,outdir,not no_early_stop)
    rec=chosen['player']
    ev,sets,blocks,opts=analyze_one(trace,rec)
    print(f'{len(ev)} candidate speed peaks | {len(sets)} full-ish sets | {len(blocks)} three-set assessment blocks')
    if meta.get('done')!='yes':alerts.append('Raw GPS pages incomplete; results provisional.')
    if meta.get('early_stop')=='yes':alerts.append('Early stopped on verified chronological pages; still confirm selected GPS trace.')
    if not blocks:alerts.append('No complete 3-set sequence found within tag. Inspect the GPS speed trace for missed reps and timing differences.')
    if len(blocks)>1:alerts.append('Multiple possible assessment blocks within one tagged drill: check selected player and false starts.')
    results=[];reviews=[]
    for k,b in enumerate(blocks,1):
        blk=f'Assessment {k}'
        flag='Review - ambiguous' if b['ambiguous'] else ('Review - possible restart' if b['possible_restart'] else 'Verify against trace')
        reviews.append({'recording':rec,'block':blk,'count':b['count'],'gap1':b['gap1'],
                        'gap2':b['gap2'],'flag':flag,'competing matches':b['alternatives']})
        results.append(result_rows(ev,b,rec,blk,trace,day))
    df=pd.concat(results,ignore_index=True) if results else pd.DataFrame()
    stem=outdir/f'assessment_3sets_{day}'
    csv=stem.with_suffix('.csv');pdf=stem.with_suffix('.pdf');htm=stem.with_suffix('.html')
    (outdir/'detection_review.json').write_text(json.dumps({'selected':chosen,'warnings':alerts,
        'blocks':reviews,'matched_events':len(ev),'tag_local':[str(start),str(end)]},indent=2,default=str))
    if not df.empty:
        df.to_csv(csv,index=False)
        make_pdf(df,reviews,pdf,day)
    # When no full blocks are detected, provide usable candidate-event CSV and GPS dashboard.
    if ev:
        pd.DataFrame(ev).to_csv(outdir/'candidate_events_for_review.csv',index=False)
    make_dashboard(df,reviews,alerts,{rec:(trace,ev,blocks)},htm,pdf.name if pdf.exists() else '',
                   csv.name if csv.exists() else '',day)
    print('\nDashboard:',htm)
    if pdf.exists():print('PDF:',pdf,'\nCSV:',csv)
    if not blocks:print('No PDF generated; review candidate_events_for_review.csv and dashboard trace.')
    print('Provisional: validate 10-yard GPS peak speeds and drill timestamps against recorded activity.')
    try:webbrowser.open(htm.as_uri())
    except Exception:pass
    return df



def audit_identity_from_api(day):
    """Metadata-only inspection of athlete fields for targeted STATSports drills.

    Reports schema/field presence and counts, not raw JSON, IDs, API keys,
    player names or GPS observations. It cannot manufacture absent profiles.
    """
    print(f'\nSTATSports IDENTITY AUDIT for {day} (metadata only; no GPS calls)')
    sources=[]
    queries=(
        ('getFullSessionsByDateRange',{
            'thirdPartyApiId':API_KEY,
            'sessionStartDate':f'{day}T00:00:00',
            'sessionEndDate':f'{day}T23:59:59',
        }),
        ('getFullSession',{'thirdPartyApiId':API_KEY, 'sessionDate':f'{day}T00:00:00'}),
        ('getPlayerDetails',{'thirdPartyApiId':API_KEY, 'sessionDate':f'{day}T00:00:00'}),
    )
    for endpoint,payload in queries:
        try:
            body=api(endpoint,payload)
            sources.append((body,endpoint))
        except Exception as exc:
            print(f'  {endpoint}: request failed: {type(exc).__name__}: {str(exc)[:180]}')

    def walk(obj, callback, path='$', depth=0):
        if depth>16:return
        if isinstance(obj,list):
            for i,v in enumerate(obj[:2000]):walk(v,callback,path+'[]',depth+1)
        elif isinstance(obj,dict):
            callback(obj,path)
            for key,value in obj.items():
                if isinstance(value,(dict,list)) and key!='drills':
                    walk(value,callback,path+'.'+str(key),depth+1)

    if not sources:
        print('  None of the three metadata endpoints responded successfully.')
        return None
    for body,endpoint in sources:
        print(f'\nEndpoint: {endpoint}')
        print('  Top-level type:',type(body).__name__)
        if isinstance(body,dict):
            print('  Top-level fields:',', '.join(sorted(map(str,body.keys()))[:35]))
        sample=[]; found=[]; name_containers=[]
        status={'drill_parent_nodes':0,'missing_player':0,'null_player':0,
                'player_object':0,'other_player':0,'named_player_object':0,
                'matching_drills':0}
        def visit(node,path):
            drills=node.get('drills')
            if isinstance(drills,list):
                status['drill_parent_nodes']+=1
                if 'player' not in node:status['missing_player']+=1
                elif node['player'] is None:status['null_player']+=1
                elif isinstance(node['player'],dict):
                    status['player_object']+=1
                    if profile_name(node['player']):status['named_player_object']+=1
                else:status['other_player']+=1
                for drill in drills:
                    if not isinstance(drill,dict):continue
                    if normalize_name(str(get_field(drill,'name','drillName','title') or ''))==normalize_name(DRILL_NAME):
                        status['matching_drills']+=1
                        if len(sample)<12:
                            profile=node.get('player')
                            sample.append({
                                'path':path,
                                'container_fields':sorted(node.keys()),
                                'player_field':('missing' if 'player' not in node else
                                   ('null' if profile is None else type(profile).__name__)),
                                'player_fields':sorted(profile.keys()) if isinstance(profile,dict) else [],
                                'player_details_type':type(node.get('playerDetails')).__name__ if 'playerDetails' in node else 'MISSING',
                                'player_details_keys':sorted(node['playerDetails'].keys()) if isinstance(node.get('playerDetails'),dict) else [],
                                'player_details_name':bool(_exact_playerdetails_name(node.get('playerDetails'))[0]),
                                'drill_fields':sorted(drill.keys()),
                                'has_session_player_id':bool(node.get('id')),
                                'has_player_id':bool((profile or {}).get('id')) if isinstance(profile,dict) else bool(node.get('playerId')),
                                'has_data_id':bool(node.get('dataId')),
                            })
            if any(k in node for k in ('displayName','firstName','lastName','shortName','playerName','fullName')):
                if profile_name(node): name_containers.append((path,node))
        walk(body,visit)
        print('  Drill parent objects:',status['drill_parent_nodes'],
              '| matching assessment drills:',status['matching_drills'])
        print('  Parent player field: missing',status['missing_player'],
              '| null',status['null_player'],'| object',status['player_object'],
              '| other',status['other_player'])
        print('  Named nested player objects:',status['named_player_object'],
              '| all nodes with name fields:',len(name_containers))
        for i,item in enumerate(sample,1):
            print(f'  Matched drill {i}: object path:',item['path'])
            print('    Container keys:',', '.join(item['container_fields']))
            print('    Player field:',item['player_field'],'| player keys:',
                  ', '.join(item['player_fields']) if item['player_fields'] else '(none)')
            print('    playerDetails type:',item['player_details_type'],
                  '| name detected:',item['player_details_name'])
            print('    playerDetails field names:',
                  ', '.join(item['player_details_keys'][:35]) if item['player_details_keys'] else '(none / not a dict)')
            print('    Drill keys:',', '.join(item['drill_fields']))
            print('    IDs present? session player:',item['has_session_player_id'],
                  '| player:',item['has_player_id'],'| data:',item['has_data_id'])
        found.extend(sample)

    first=next((x for x,e in sources if e=='getFullSessionsByDateRange'),None)
    if first is not None:
        matches,_=get_matches(first)
        for i,match in enumerate(matches,1):
            name,source,profiles=_resolve_profile_matches(match,sources)
            print(f'\nTagged entry {i}:',
                  ('LINKED API PROFILE FOUND ('+str(source)+')' if name else
                   'NO LINKED API NAME'))
            print('  Link IDs present: player',bool(match.get('player_id')),
                  '| session player',bool(match.get('session_player_id')),
                  '| raw stream',bool(match.get('raw_id')))
            print('  Named profile objects from all sources:',len(profiles))
    print('\nINTERPRETATION:')
    print('  v6 overlooked playerDetails. v7 checks it, including nested fields.')
    print('  If playerDetails contains no identifiable athlete fields and no other')
    print('  exact matching named profile is available, names remain unresolved.')
    print('  In Sonra, Share Data > Third-Party API: select BOTH session(s)')
    print('  and intended player(s), send again, then rerun --resolve-only.')
    print('  If player keys exist but names still do not link, share the output')
    print('  of this audit (it intentionally omits credential, IDs, and names).')
    return sources

def main():
    parser=argparse.ArgumentParser(description='Single tagged Assessment - 3 Sets GPS sprint report (v7 playerDetails resolver)')
    parser.add_argument('--date',help='Session date YYYY-MM-DD (default: prompt)')
    parser.add_argument('--list-only',action='store_true',help='Check tagged drills without downloading GPS data')
    parser.add_argument('--drill-clock',choices=('auto','utc','local'),default='auto',
                        help='Interpret timezone-less drill timestamps (default: auto, using 11:30–13:00 Eastern window)')
    parser.add_argument('--athlete', '--player-name', dest='athlete',
                        help='Verified athlete name from Sonra (used when API metadata is blank).')
    parser.add_argument('--allow-unknown', action='store_true',
                        help='Optional diagnostic: permit GPS report even when the API cannot identify athlete. Default: require verified API identity.')
    parser.add_argument('--identity-audit', action='store_true',
                        help='Inspect the player metadata fields returned by the API (no raw GPS, no IDs or names printed).')
    parser.add_argument('--resolve-only', '--identity-only', action='store_true',
                        help='Use API session/player metadata to automatically fix saved report name; NO GPS download.')
    parser.add_argument('--relabel-only',action='store_true',
                        help='Update the existing PDF/HTML/CSV without ANY API requests or GPS download; requires --athlete.')
    parser.add_argument('--no-prompt',action='store_true',help='Never ask for athlete name interactively.')
    parser.add_argument('--no-early-stop',action='store_true',help='Download complete GPS stream instead of stopping when chronological data passes drill end')
    a=parser.parse_args()
    day=date.fromisoformat(a.date or input(f'Session date YYYY-MM-DD [{date.today()}]: ').strip() or date.today().isoformat()).isoformat()
    if a.identity_audit:
        return audit_identity_from_api(day)
    if a.resolve_only:
        return refresh_saved_reports_with_api_name(day,a.drill_clock)
    if a.relabel_only:
        if not a.athlete: raise ValueError('--relabel-only requires --athlete "Actual Name"')
        return relabel_saved_report(day,a.athlete)
    return process(day,a.list_only,a.no_early_stop,a.drill_clock,a.athlete,a.no_prompt,a.allow_unknown)


# ===========================================================================
# ALL-ATHLETE WORKFLOW: one date, all exact 'Assessment - 3 Sets' tags.
# Source extraction logic above is shared with the validated single-athlete v7.
# ===========================================================================

from reportlab.graphics.shapes import Drawing, Line, Circle, String
from reportlab.lib.colors import HexColor

ALL_REQUIRED_REPS = 30
QUADRANT_SPLIT_METHOD = 'mean'  # changed via --quadrant-split; interactive dashboard can switch live
SUMMARY_COLUMNS = [
    'Athlete', 'Detected reps', 'Expected reps', 'Best peak (mph)',
    'Average peak (mph)', 'Peak-to-average decrement (%)', 'Quadrant',
    'Worst peak (mph)', 'Best-worst difference (mph)',
    'Best-to-worst decrement (%)', 'Status'
]


def group_paths(day):
    folder = OUTPUT_ROOT / f'statsports_all_assessments_{day}'
    return folder, {
        'summary': folder / f'assessment_{day}_athlete_summary.csv',
        'reps': folder / f'assessment_{day}_all_30_reps.csv',
        'raw_reps': folder / f'assessment_{day}_all_30_reps_RAW.csv',
        'review': folder / f'assessment_{day}_quality_review.csv',
        'pdf': folder / f'assessment_{day}_team_report.pdf',
        'html': folder / f'assessment_{day}_dashboard.html',
        'manifest': folder / 'run_manifest.json'
    }


def launch_saved_dashboard(day, required=False):
    """Open the EXISTING combined HTML report with the OS default browser.

    No network requests, no GPS reprocessing, and no changes to stored reports.
    On macOS prefer the reliable `open` command over Python's browser lookup.
    """
    _, paths = group_paths(day)
    html_file = paths['html'].resolve()
    if not html_file.is_file():
        message = f'No saved HTML dashboard found: {html_file}. Run an assessment or --report-only first.'
        if required:
            raise RuntimeError(message)
        print(message)
        return False
    print('Dashboard file:', html_file)
    if sys.platform == 'darwin':
        try:
            result = subprocess.run(['open', str(html_file)], capture_output=True,
                                    text=True, check=False, timeout=15)
            if result.returncode == 0:
                print('Opened dashboard in your default browser.')
                return True
            print('macOS open returned an error:', (result.stderr or '').strip()[:250])
        except (OSError, subprocess.TimeoutExpired) as exc:
            print('macOS open was unavailable:', str(exc)[:200])
    try:
        if webbrowser.open(html_file.as_uri(), new=2):
            print('Opened dashboard in your browser.')
            return True
    except Exception as exc:
        print('Browser launch failed:', str(exc)[:200])
    print('Open this file manually, or paste this into Terminal:')
    print('open "'+str(html_file)+'"')
    return False


def strict_match_dedup(matches):
    """Keep one tag per (session-player, tag ID, start, end). Never merge streams by clock time."""
    seen, selected = set(), []
    for m in matches:
        key = tuple(str(m.get(k) or '') for k in
            ('session_player_id', 'raw_id', 'drill_id', 'start', 'end'))
        if key not in seen:
            selected.append(dict(m)); seen.add(key)
    return selected


def primary_block(blocks):
    """Choose best-supported candidate, preserve an ambiguity warning."""
    if not blocks:
        return None, False
    ordered = sorted(blocks, key=lambda b: (-b.get('count', 0),
                   -b.get('score', 0), b['sets'][0]['start']))
    ambiguity = len(ordered) > 1
    return ordered[0], ambiguity


def summarize_player(athlete, reps=None, extra_flags=()):
    flags = list(extra_flags)
    if reps is None or reps.empty:
        return {'Athlete':athlete,'Detected reps':0,'Expected reps':ALL_REQUIRED_REPS,
                'Best peak (mph)':float('nan'),'Average peak (mph)':float('nan'),
                'Worst peak (mph)':float('nan'),'Best-worst difference (mph)':float('nan'),
                'Best-to-worst decrement (%)':float('nan'),
                'Status':'; '.join(dict.fromkeys(flags or ['No complete 3-set detection']))}
    speeds = pd.to_numeric(reps['Peak (mph)'],errors='coerce').dropna()
    speeds = speeds[np.isfinite(speeds) & (speeds > 0)]
    n=int(len(speeds))
    if n < ALL_REQUIRED_REPS:
        flags.append(f'Incomplete {n}/30')
    else:
        flags.append('Provisional GPS estimates')
    if not n:
        return summarize_player(athlete,None,flags)
    hi,lo=float(speeds.max()),float(speeds.min())
    avg=float(speeds.mean())
    peak_to_avg=100*(hi-avg)/hi if hi>0 else float('nan')
    return {'Athlete':athlete,'Detected reps':n,'Expected reps':ALL_REQUIRED_REPS,
            'Best peak (mph)':hi,'Average peak (mph)':avg,
            'Peak-to-average decrement (%)':peak_to_avg,'Quadrant':'',
            'Worst peak (mph)':lo,'Best-worst difference (mph)':hi-lo,
            'Best-to-worst decrement (%)':100*(hi-lo)/hi if hi>0 else float('nan'),
            'Status':'; '.join(dict.fromkeys(flags))}


def summary_from_reps(reps, review):
    rows=[]
    names=list(dict.fromkeys(review['Athlete'].tolist())) if not review.empty else []
    if not reps.empty:
        names=list(dict.fromkeys(names + reps['Athlete'].dropna().tolist()))
    for name in names:
        item = review.loc[review['Athlete']==name] if not review.empty else pd.DataFrame()
        flags=[]
        if not item.empty:
            for flag in item['Status'].dropna().tolist():
                if flag not in ('OK','Candidate - verify trace','Provisional GPS estimates'):
                    flags.append(str(flag))
        data=reps.loc[reps['Athlete']==name] if not reps.empty else None
        rows.append(summarize_player(name,data,flags))
    out = pd.DataFrame(rows,columns=SUMMARY_COLUMNS)
    return add_quadrants(out)


def add_quadrants(summary):
    if summary is None or summary.empty:
        return pd.DataFrame(columns=SUMMARY_COLUMNS) if summary is None else summary
    out=summary.copy()
    x=pd.to_numeric(out['Best peak (mph)'],errors='coerce')
    y=pd.to_numeric(out['Peak-to-average decrement (%)'],errors='coerce')
    valid=x.notna() & y.notna() & np.isfinite(x) & np.isfinite(y)
    out['Quadrant']='Unclassified'
    if not valid.any():
        return out
    aggregator='median' if QUADRANT_SPLIT_METHOD=='median' else 'mean'
    xsplit=float(getattr(x[valid],aggregator)())
    ysplit=float(getattr(y[valid],aggregator)())
    out.attrs['quadrant_x_split']=xsplit
    out.attrs['quadrant_y_split']=ysplit
    labels=[]
    for xv,yv in zip(x,y):
        if not (pd.notna(xv) and pd.notna(yv) and np.isfinite(xv) and np.isfinite(yv)):
            labels.append('Unclassified')
            continue
        if xv >= xsplit and yv <= ysplit:
            labels.append('High peak | Low drop')
        elif xv >= xsplit and yv > ysplit:
            labels.append('High peak | High drop')
        elif xv < xsplit and yv <= ysplit:
            labels.append('Low peak | Low drop')
        else:
            labels.append('Low peak | High drop')
    out['Quadrant']=labels
    return out


def speed_plot_pdf(sub, width=700, height=152):
    """ReportLab vector plot (no matplotlib or PNG dependency)."""
    d=Drawing(width,height)
    if sub.empty:return d
    top,bottom,left,right=height-14,26,47,width-14
    p=pd.to_numeric(sub['Peak (mph)'],errors='coerce')
    p=p[np.isfinite(p)]
    if p.empty:return d
    low=max(0,float(p.min())-1.5); high=float(p.max())+1.5
    if high-low<3:high=low+3
    def xx(rep):return left+(float(rep)-1)/29*(right-left)
    def yy(speed):return bottom+(float(speed)-low)/(high-low)*(top-bottom)
    d.add(Line(left,bottom,right,bottom,strokeColor=HexColor('#a5b7c6'),strokeWidth=.6))
    for k in range(4):
        value=low+(high-low)*k/3
        y=yy(value)
        d.add(Line(left,y,right,y,strokeColor=HexColor('#e8eff3'),strokeWidth=.5))
        d.add(String(2,y-3,f'{value:.1f}',fontSize=8,fillColor=HexColor('#536779')))
    cols=['#197d91','#d1943a','#7955a5']
    for s in (1,2,3):
        g=sub[sub['Set']==s].sort_values('Rep')
        pts=[(int(r),xx(r),yy(v)) for r,v in zip(g['Rep'],g['Peak (mph)']) if pd.notna(v)]
        for (r1,x1,y1),(r2,x2,y2) in zip(pts,pts[1:]):
            if r2==r1+1:  # Never draw a continuous line across missing reps.
                d.add(Line(x1,y1,x2,y2,strokeColor=HexColor(cols[s-1]),strokeWidth=1.6))
        for _,x,y in pts:d.add(Circle(x,y,2.0,fillColor=HexColor(cols[s-1]),strokeColor=None))
    for rep in (1,5,10,11,15,20,21,25,30):
        d.add(String(xx(rep)-5,9,str(rep),fontSize=7.5,fillColor=HexColor('#526476')))
    return d


def make_team_pdf(reps, summary, review, path, day):
    styles=getSampleStyleSheet()
    doc=SimpleDocTemplate(str(path),pagesize=landscape(letter),topMargin=26,
       leftMargin=30,rightMargin=30,bottomMargin=24)
    els=[Paragraph('STATSports | 30-Rep Assessment | RAW Local Peak',styles['Title']),
         Paragraph(html.escape(f'{day}  |  {DRILL_NAME}  |  3 sets x 10 reps  |  17-second starts'),styles['Normal']),
         Spacer(1,12)]
    def table(data, widths, font=8):
        t=Table(data,colWidths=widths,repeatRows=1,hAlign='LEFT')
        t.setStyle(TableStyle([
            ('BACKGROUND',(0,0),(-1,0),HexColor('#17384c')),
            ('TEXTCOLOR',(0,0),(-1,0),colors.white),
            ('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),
            ('FONTSIZE',(0,0),(-1,-1),font),
            ('BOTTOMPADDING',(0,0),(-1,-1),3),('TOPPADDING',(0,0),(-1,-1),3),
            ('LINEBELOW',(0,1),(-1,-1),.3,HexColor('#d6e1e8')),
            ('VALIGN',(0,0),(-1,-1),'MIDDLE')
        ]))
        return t
    fmt=lambda n:f'{float(n):.2f}' if pd.notna(n) and np.isfinite(float(n)) else '--'
    els.append(Paragraph('Athlete overview',styles['Heading2']))
    header=['Player','Reps','Best mph','Avg mph','Peak→Avg %','Worst mph','Best→Worst %','Quadrant']
    entries=[header]
    for _,r in summary.sort_values('Best peak (mph)',ascending=False,na_position='last').iterrows():
        entries.append([str(r['Athlete'])[:21],f"{int(r['Detected reps'])}/30",
                  fmt(r['Best peak (mph)']),fmt(r['Average peak (mph)']),
                  fmt(r['Peak-to-average decrement (%)']),fmt(r['Worst peak (mph)']),
                  fmt(r['Best-to-worst decrement (%)']),str(r.get('Quadrant',''))[:22]])
    els.append(table(entries,[155,54,68,68,72,68,72,110],7.6))
    els.extend([Spacer(1,12),Paragraph('Group average peak speed by repetition',styles['Heading2'])])
    agg=reps.groupby('Rep')['Peak (mph)'].agg(['mean','count']) if not reps.empty else pd.DataFrame()
    rows=[['Set 1 rep','Mean mph','N','Set 2 rep','Mean mph','N','Set 3 rep','Mean mph','N']]
    for k in range(10):
        rr=[]
        for s in range(3):
            rep=s*10+k+1
            x=agg.loc[rep] if rep in agg.index else None
            rr += [str(rep),fmt(x['mean']) if x is not None else '--',str(int(x['count'])) if x is not None else '0']
        rows.append(rr)
    els.append(table(rows,[70,84,64]*3,7.7))
    els.extend([Spacer(1,8),Paragraph('Peak→Avg drop % = (best peak - average peak) / best peak x 100. Best→Worst decrement = (best peak - worst peak) / best peak x 100. '
       'Raw peak is the maximum unsmoothed speed reading within 0.6 s around each matched smoothed peak. Averages include detected reps only. Provisional; verify GPS spikes and athlete-to-device assignment.',styles['Normal'])])
    for _,s in summary.iterrows():
        name=str(s['Athlete'])
        g=reps[reps['Athlete']==name].sort_values('Rep') if not reps.empty else pd.DataFrame()
        els.append(PageBreak())
        els.append(Paragraph(html.escape(name),styles['Heading1']))
        line=(f"RAW best: {fmt(s['Best peak (mph)'])} mph  |  Average: {fmt(s['Average peak (mph)'])} mph  |  "
              f"Peak→Avg drop: {fmt(s['Peak-to-average decrement (%)'])}%  |  Worst: {fmt(s['Worst peak (mph)'])} mph  |  "
              f"Best→Worst drop: {fmt(s['Best-to-worst decrement (%)'])}%  |  Quadrant: {html.escape(str(s.get('Quadrant','')))}  |  "
              f"Detected: {int(s['Detected reps'])}/30")
        els.append(Paragraph(line,styles['Normal']))
        if not g.empty:
            els.extend([Spacer(1,8),speed_plot_pdf(g),Spacer(1,8)])
            rows=[['Set 1 rep','Peak mph','Delta %','Set 2 rep','Peak mph','Delta %','Set 3 rep','Peak mph','Delta %']]
            for k in range(10):
                rr=[]
                for sn in (1,2,3):
                    rep=10*(sn-1)+k+1
                    rg=g[g['Rep']==rep]
                    if rg.empty:rr += [str(rep),'--','--']
                    else:
                        r=rg.iloc[0]
                        rr += [str(rep),fmt(r['Peak (mph)']),fmt(r['Rep decrement (%)'])]
                rows.append(rr)
            els.append(table(rows,[65,75,74]*3,7.5))
        else:
            els.append(Paragraph('No complete three-set block detected. Review this athlete in the HTML dashboard.',styles['Normal']))
        item=review[review['Athlete']==name] if not review.empty else pd.DataFrame()
        note=str(item['Status'].iloc[0]) if not item.empty else str(s['Status'])
        els.extend([Spacer(1,9),Paragraph('QC: '+html.escape(note[:250]),styles['Normal'])])
    doc.build(els)


def team_dashboard(reps, summary, review, path, pdfname, summary_name, reps_name, day):
    """Self-contained interactive HTML dashboard; no remote Plotly CDN required."""
    esc=html.escape
    style='''<style>
    :root{font-family:Inter,-apple-system,BlinkMacSystemFont,system-ui,sans-serif;color:#17344a;background:#f2f5f8}
    *{box-sizing:border-box}body{max-width:1250px;margin:32px auto;padding:0 24px}h1{font-size:29px;margin:0 0 8px}
    .muted{color:#6b8291}.intro{margin:0 0 24px}.controls{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:18px}
    .pill{display:inline-block;border:1px solid #d4e2ea;border-radius:7px;padding:9px 15px;background:white;color:#124968;text-decoration:none}
    section{background:white;border-radius:13px;padding:22px;margin:18px 0;box-shadow:0 1px 7px #17344a0d}
    .stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}.stat{background:#f5f8fa;border-radius:9px;padding:14px}.value{font-size:25px;font-weight:700}
    table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:10px;border-bottom:1px solid #e5ecf0;text-align:left}
    th{color:#4d6779;background:#f4f7fa;position:sticky;top:0}td.num{text-align:right;font-variant-numeric:tabular-nums}
    .scroll{overflow-x:auto}details{background:#fff;border-radius:13px;margin:14px 0;padding:19px}summary{font-size:17px;font-weight:700;cursor:pointer}
    label{font-weight:600;display:block;margin-bottom:6px}select,input{padding:10px;border:1px solid #cad9e2;border-radius:7px;min-width:200px;width:100%;background:white;color:#17344a}button.pill{font:inherit;cursor:pointer}.edit-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.quad-legend{display:flex;gap:18px;flex-wrap:wrap;font-size:12px;color:#536d7e}.quad-legend span:nth-child(1){color:#1f7a8c}.quad-legend span:nth-child(2){color:#d08c2f}.quad-legend span:nth-child(3){color:#6a994e}.quad-legend span:nth-child(4){color:#bc4749}
    .qc{font-size:12px;color:#72501c}.help{color:#657986;font-size:12px}
    @media(max-width:650px){.stats{grid-template-columns:repeat(2,minmax(0,1fr))}.edit-grid{grid-template-columns:1fr}body{padding:0 10px}}
    </style>'''
    html_out=['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',style,'</head><body>']
    html_out += [f'<h1>STATSports | 30-Rep Assessment</h1>',
        f'<p class="intro muted">{esc(day)} · Tagged drill: {esc(DRILL_NAME)} · 3 sets × 10 reps · 17-second starts · <b>UNSMOOTHED RAW PEAKS</b></p>',
        '<nav class="controls">',f'<a class="pill" href="{esc(pdfname)}" download>Download PDF</a>',
        f'<a class="pill" href="{esc(summary_name)}" download>Download player summary CSV</a>',
        f'<a class="pill" href="{esc(reps_name)}" download>Download all reps CSV</a>','</nav>']
    have=int((summary['Detected reps']>0).sum()) if not summary.empty else 0
    full=int((summary['Detected reps']==30).sum()) if not summary.empty else 0
    html_out += ['<section><div class="stats">',
       f'<div class="stat"><div class="value">{len(summary)}</div><div class="muted">Tagged players</div></div>',
       f'<div class="stat"><div class="value">{have}</div><div class="muted">Detected assessments</div></div>',
       f'<div class="stat"><div class="value">{full}</div><div class="muted">30/30 rep detections</div></div>',
       f'<div class="stat"><div class="value">{int(reps["Peak (mph)"].count()) if not reps.empty else 0}</div><div class="muted">Observed sprint peaks</div></div>',
       '</div></section><section><h2>All athletes</h2><div class="scroll">']
    main_cols=['Athlete','Detected reps','Best peak (mph)','Average peak (mph)',
            'Peak-to-average decrement (%)','Quadrant','Worst peak (mph)',
            'Best-worst difference (mph)','Best-to-worst decrement (%)','Status']
    ordered=summary.sort_values('Best peak (mph)',ascending=False,na_position='last')
    html_out.append('<table id="athlete-summary-table"><thead><tr>'+''.join(
        f'<th>{esc(col)}</th>' for col in main_cols)+'</tr></thead><tbody>')
    for _,r in ordered.iterrows():
        orig=str(r['Athlete'])
        html_out.append(f'<tr data-original-name="{esc(orig,quote=True)}">')
        for col in main_cols:
            value=r[col]
            if col=='Athlete':
                html_out.append(f'<td class="summary-name">{esc(orig)}</td>')
            elif col=='Quadrant':
                html_out.append(f'<td class="summary-quadrant">{esc(str(value))}</td>')
            elif isinstance(value,(float,np.floating)):
                html_out.append(f'<td>{float(value):.2f}</td>' if pd.notna(value) else '<td>—</td>')
            else:
                html_out.append(f'<td>{esc(str(value))}</td>')
        html_out.append('</tr>')
    html_out.append('</tbody></table>')
    html_out.append('</div><p class="help">All values in this table are calculated from the maximum raw GPS reading in the ~0.6 s neighborhood around each detected peak. Peak-to-average decrement (%) = (best peak − average peak) / best peak × 100. Best-to-worst decrement remains available. Missing reps are not treated as zero; results are provisional.</p></section>')

    html_out += [
        '<section><h2>Quadrant settings &amp; display names</h2>',
        '<div class="edit-grid"><div><label for="quadrant-split">Quadrant dividing lines</label>',
        f'<select id="quadrant-split" onchange="updateDashboard()"><option value="mean" {"selected" if QUADRANT_SPLIT_METHOD=="mean" else ""}>Team mean</option><option value="median" {"selected" if QUADRANT_SPLIT_METHOD=="median" else ""}>Team median</option></select></div>',
        '<div><label for="rename-target">Choose player to relabel</label><select id="rename-target" onchange="syncAliasInput()">',
    ]
    names=summary['Athlete'].astype(str).tolist() if not summary.empty else []
    for name in names:
        html_out.append(f'<option value="{esc(name,quote=True)}">{esc(name)}</option>')
    html_out += [
        '</select></div><div><label for="rename-input">Display name / alias</label>',
        '<input id="rename-input" type="text" maxlength="100" placeholder="Enter custom name"></div></div>',
        '<div class="controls" style="margin-top:12px;margin-bottom:4px">',
        '<button type="button" class="pill" onclick="applyAlias()">Apply display name</button>',
        '<button type="button" class="pill" onclick="resetAliases()">Restore original names</button>',
        '<button type="button" class="pill" onclick="downloadViewCSV()">Download displayed summary CSV</button></div>',
        '<p class="help">Names change on this dashboard only; underlying STATSports athlete IDs and raw results remain unchanged. Aliases are saved in your browser when local storage is available. Existing PDF and original CSV files keep their original names; the download above exports the edited table.</p>',
        '</section>',
    ]

    if not summary.empty and {'Best peak (mph)','Peak-to-average decrement (%)'}.issubset(summary.columns):
        quad=summary.copy()
        qx=pd.to_numeric(quad['Best peak (mph)'],errors='coerce')
        qy=pd.to_numeric(quad['Peak-to-average decrement (%)'],errors='coerce')
        valid=qx.notna() & qy.notna() & np.isfinite(qx) & np.isfinite(qy)
        if valid.any():
            xsplit=float(qx[valid].median() if QUADRANT_SPLIT_METHOD=='median' else qx[valid].mean())
            ysplit=float(qy[valid].median() if QUADRANT_SPLIT_METHOD=='median' else qy[valid].mean())
            # One marker per athlete; browser updates marker colors and text when the
            # mean/median toggle or name overrides change. No GPS/API calls.
            qdata=quad.loc[valid]
            def base_quad_color(label):
                return {'High peak | Low drop':'#1f7a8c',
                        'High peak | High drop':'#d08c2f',
                        'Low peak | Low drop':'#6a994e',
                        'Low peak | High drop':'#bc4749'}.get(str(label),'#7b8b98')
            qfig=go.Figure()
            qfig.add_scatter(x=qdata['Best peak (mph)'],
               y=qdata['Peak-to-average decrement (%)'],mode='markers+text',
               text=qdata['Athlete'].tolist(),textposition='top center',
               marker={'size':12,'color':[base_quad_color(q) for q in qdata['Quadrant']]},
               customdata=np.stack([qdata['Average peak (mph)'].to_numpy(),qdata['Detected reps'].to_numpy(),qdata['Quadrant'].to_numpy()],axis=1),
               hovertemplate='%{text}<br>Best: %{x:.2f} mph<br>Peak→Avg drop: %{y:.2f}%<br>Average: %{customdata[0]:.2f} mph<br>Detected reps: %{customdata[1]}<br>%{customdata[2]}<extra></extra>',showlegend=False)
            qfig.add_vline(x=xsplit,line_dash='dash',line_color='#5d6d7e')
            qfig.add_hline(y=ysplit,line_dash='dash',line_color='#5d6d7e')
            qfig.update_layout(template='plotly_white',height=500,
                title='Peak speed vs. peak-to-average decrement',
                xaxis_title='Best peak speed (mph)',yaxis_title='Peak→average decrement (%)',
                margin={'l':55,'r':25,'t':60,'b':55})
            html_out.append('<section id="quadrant-section"><h2>Player performance quadrants</h2>'+plot(qfig,include_plotlyjs=True,output_type='div')+
                '<p id="quadrant-caption" class="help"></p>'+ 
                '<div class="quad-legend"><span>● Faster peak / lower drop</span><span>● Faster peak / higher drop</span><span>● Slower peak / lower drop</span><span>● Slower peak / higher drop</span></div></section>')
            include_plotly_for_group=False
        else:
            include_plotly_for_group=True
    else:
        include_plotly_for_group=True

    if not reps.empty:
        avg=reps.groupby('Rep')['Peak (mph)'].agg(['mean','count']).reindex(range(1,31)).reset_index()
        fig=go.Figure()
        palette=['#21879c','#d29237','#8163b0']
        for sn in (1,2,3):
            part=avg[(avg.Rep>=10*(sn-1)+1)&(avg.Rep<=10*sn)]
            fig.add_scatter(x=part.Rep,y=part['mean'],name=f'Set {sn}',
               customdata=part['count'],mode='lines+markers',line={'color':palette[sn-1]},
               hovertemplate='Rep %{x}: %{y:.2f} mph · N=%{customdata}<extra></extra>')
        fig.update_layout(title='Mean RAW local GPS peak by repetition',template='plotly_white',
            xaxis_title='Repetition',yaxis_title='Mean peak (mph)',height=370,margin={'l':50,'r':25,'b':45,'t':50})
        html_out.append('<section>'+plot(fig,include_plotlyjs=include_plotly_for_group,output_type='div')+'</section>')
    else:
        # Still package JS so individual graphs can render if rep data is populated in an edited report.
        html_out.append('<section><p>No complete assessments detected.</p></section>')
    html_out.append('<section><h2>Individual athlete detail</h2><label for="player">Select athlete: </label>')
    names=summary['Athlete'].astype(str).tolist() if not summary.empty else []
    html_out.append('<select id="player" onchange="showPlayer(this.value)">')
    for i,name in enumerate(names):html_out.append(f'<option value="p{i}" data-original-name="{esc(name,quote=True)}">{esc(name)}</option>')
    html_out.append('</select><p class="help">Each athlete has their own rep-by-rep chart and three-set table.</p></section>')
    for i,name in enumerate(names):
        div=f'p{i}';sub=reps[reps['Athlete']==name].sort_values('Rep') if not reps.empty else pd.DataFrame()
        rv=review[review['Athlete']==name] if not review.empty else pd.DataFrame()
        qc=rv['Status'].iloc[0] if not rv.empty else 'Review required'
        html_out.append(f'<section class="player" id="{div}" style="display:{"block" if i==0 else "none"}">')
        html_out.append(f'<h2 data-original-name="{esc(name,quote=True)}" class="player-name-heading">{esc(name)}</h2><p class="qc">Quality: {esc(str(qc))}</p>')
        if sub.empty:
            html_out.append('<p>No complete three-set block matched. See quality-review CSV for details.</p>')
        else:
            chart=go.Figure()
            for sn in (1,2,3):
                g=sub[sub['Set']==sn]
                chart.add_scatter(x=g.Rep,y=g['Peak (mph)'],mode='lines+markers',
                  name=f'Set {sn}',line={'color':palette[sn-1]},
                  hovertemplate='Rep %{x} · %{y:.2f} mph<extra></extra>')
            chart.update_layout(template='plotly_white',height=340,
                xaxis_title='Repetition',yaxis_title='Peak (mph)',margin={'l':50,'r':20,'t':20,'b':50})
            html_out.append(plot(chart,include_plotlyjs=False,output_type='div'))
            show=['Rep','Set','Peak (mph)','Smoothed peak (mph)','Rep decrement (%)','Detection','QC note']
            html_out.append('<div class="scroll">'+sub[show].to_html(index=False,escape=True,
                na_rep='—',float_format=lambda x:f'{x:.2f}')+'</div>')
        html_out.append('</section>')
    if not review.empty:
        html_out += ['<section id="review-section"><h2>Detection and review status</h2>',
            '<div class="scroll">'+review.to_html(index=False,escape=True,na_rep='—')+'</div></section>']
    html_out.append('<section><p class="help">RAW values are unsmoothed local GPS maximum readings in the 3 x 0.2-second bins surrounding the algorithm-detected smoothed peak; this does not guarantee the highest instantaneous sample across the entire 10-yard sprint. This is not an electronic 10-yard time. Confirm timing and athlete-to-device assignment before interpreting differences. Potential restarts and missing reps require review.</p></section>')
    # Browser-local aliases are display-only. No API credentials or player IDs
    # are embedded in this independent interactive control dataset.
    payload=[]
    for _,r in summary.iterrows():
        def numeric(key):
            v=pd.to_numeric(r.get(key),errors='coerce')
            return float(v) if pd.notna(v) and np.isfinite(v) else None
        payload.append({'original':str(r['Athlete']),
                        'best':numeric('Best peak (mph)'),
                        'average':numeric('Average peak (mph)'),
                        'drop':numeric('Peak-to-average decrement (%)'),
                        'reps':int(r['Detected reps'])})
    json_blob=json.dumps(payload,ensure_ascii=True,allow_nan=False).replace('<','\\u003c')
    html_out.append('<script id="quadrant-data" type="application/json">'+json_blob+'</script>')
    html_out.append(r"""<script>
(function(){
'use strict';
const data = JSON.parse(document.getElementById('quadrant-data').textContent);
const key = 'statsports-aliases:'+location.pathname;
let aliases={};
try { aliases=JSON.parse(localStorage.getItem(key)||'{}')||{}; } catch(e) { aliases={}; }
function displayName(orig) { return aliases[orig] || orig; }
function saveAliases() { try { localStorage.setItem(key,JSON.stringify(aliases)); } catch(e) {} }
function validRows() { return data.filter(r => Number.isFinite(r.best) && Number.isFinite(r.drop)); }
function threshold(vals,mode) {
  if(!vals.length) return NaN;
  if(mode==='median') {
    const sorted=[...vals].sort((a,b)=>a-b), h=Math.floor(sorted.length/2);
    return sorted.length%2 ? sorted[h] : (sorted[h-1]+sorted[h])/2;
  }
  return vals.reduce((a,b)=>a+b,0)/vals.length;
}
function quadrant(row,x,y) {
  if(!Number.isFinite(row.best)||!Number.isFinite(row.drop)) return 'Unclassified';
  return (row.best>=x?'High peak':'Low peak') + ' | ' + (row.drop<=y?'Low drop':'High drop');
}
const colors={'High peak | Low drop':'#1f7a8c','High peak | High drop':'#d08c2f',
  'Low peak | Low drop':'#6a994e','Low peak | High drop':'#bc4749'};
function paintNames() {
  document.querySelectorAll('tr[data-original-name]').forEach(row=>{
    row.querySelector('.summary-name').textContent=displayName(row.dataset.originalName);
  });
  document.querySelectorAll('.player-name-heading').forEach(heading=>{
    heading.textContent=displayName(heading.dataset.originalName);
  });
  // Keep the review table consistent with the renamed player summary.
  document.querySelectorAll('#review-section tbody tr').forEach(row=>{
    const cell=row.querySelector('td:first-child');
    if(!cell) return;
    if(!cell.dataset.originalName) cell.dataset.originalName=cell.textContent.trim();
    cell.textContent=displayName(cell.dataset.originalName);
  });
  document.querySelectorAll('#player option[data-original-name]').forEach(opt=>{
    opt.textContent=displayName(opt.dataset.originalName);
  });
  document.querySelectorAll('#rename-target option').forEach(opt=>{
    opt.textContent=displayName(opt.value) + (displayName(opt.value)!==opt.value?' ('+opt.value+')':'');
  });
}
function updateDashboard(){
  const rows=validRows(), method=document.getElementById('quadrant-split').value;
  paintNames();
  if(!rows.length) return;
  const x=threshold(rows.map(r=>r.best),method), y=threshold(rows.map(r=>r.drop),method);
  const qByName=new Map(rows.map(r=>[r.original,quadrant(r,x,y)]));
  document.querySelectorAll('tr[data-original-name]').forEach(row=>{
    row.querySelector('.summary-quadrant').textContent=qByName.get(row.dataset.originalName)||'Unclassified';
  });
  const graph=document.querySelector('#quadrant-section .plotly-graph-div');
  if(!graph||!window.Plotly) return;
  const quadLabels=rows.map(r=>quadrant(r,x,y));
  const trace={type:'scatter',mode:'markers+text',showlegend:false,
    x:rows.map(r=>r.best), y:rows.map(r=>r.drop), text:rows.map(r=>displayName(r.original)),
    textposition:'top center',marker:{size:12,color:quadLabels.map(q=>colors[q]||'#7b8b98')},
    customdata:rows.map((r,i)=>[r.average,r.reps,quadLabels[i]]),
    hovertemplate:'%{text}<br>Best: %{x:.2f} mph<br>Peak→Avg drop: %{y:.2f}%<br>Average: %{customdata[0]:.2f} mph<br>Detected reps: %{customdata[1]}<br>%{customdata[2]}<extra></extra>'};
  const layout={template:'plotly_white',height:500,
    title:'Peak speed vs. peak-to-average decrement',
    xaxis:{title:'Best peak speed (mph)',automargin:true},
    yaxis:{title:'Peak→average decrement (%)',automargin:true},
    margin:{l:55,r:25,t:60,b:55},showlegend:false,
    shapes:[{type:'line',xref:'x',yref:'paper',x0:x,x1:x,y0:0,y1:1,
       line:{color:'#5d6d7e',dash:'dash',width:1.5}},
      {type:'line',xref:'paper',yref:'y',x0:0,x1:1,y0:y,y1:y,
       line:{color:'#5d6d7e',dash:'dash',width:1.5}}]};
  Plotly.react(graph,[trace],layout,{responsive:true,displaylogo:false});
  document.getElementById('quadrant-caption').textContent =
     (method==='mean'?'Mean':'Median')+' dividing lines: best peak '+x.toFixed(2)+
     ' mph; peak-to-average drop '+y.toFixed(2)+'%. Right = higher peak speed; up = larger drop.';
}
window.updateDashboard=updateDashboard;
window.syncAliasInput=function(){
  const sel=document.getElementById('rename-target');
  document.getElementById('rename-input').value=aliases[sel.value]||'';
};
window.applyAlias=function(){
  const original=document.getElementById('rename-target').value;
  const alias=document.getElementById('rename-input').value.trim().replace(/[<>]/g,'');
  if(!original) return;
  if(!alias||alias===original) delete aliases[original]; else aliases[original]=alias;
  saveAliases(); updateDashboard();
};
window.resetAliases=function(){
  if(!confirm('Restore original STATSports names on this dashboard?')) return;
  aliases={};saveAliases();syncAliasInput();updateDashboard();
};
window.showPlayer=function(id){
  document.querySelectorAll('.player').forEach(x=>{x.style.display=x.id===id?'block':'none';});
  if(window.Plotly){const p=document.getElementById(id);
    p.querySelectorAll('.js-plotly-plot').forEach(x=>Plotly.Plots.resize(x));}
};
window.downloadViewCSV=function(){
  const table=document.getElementById('athlete-summary-table');
  const rows=[...table.querySelectorAll('tr')].map(tr=>
    [...tr.querySelectorAll('th,td')].map(cell=>cell.textContent.trim()));
  const csv=rows.map(r=>r.map(cell=>'"'+String(cell).replace(/"/g,'""')+'"').join(',')).join('\r\n');
  const blob=new Blob(['\ufeff'+csv],{type:'text/csv;charset=utf-8'});
  const url=URL.createObjectURL(blob), a=document.createElement('a');
  a.href=url; a.download='assessment_display_names_quadrants.csv';
  document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
};
syncAliasInput();updateDashboard();
})();
</script></body></html>""")

    path.write_text('\n'.join(html_out),encoding='utf-8')


def prepare_raw_local_peaks(reps):
    """Make stored unsmoothed local GPS maxima the PRIMARY speed metric.

    Historical `Raw max (mph)` is the highest original v reading in 3 adjacent
    0.2-second bins around the detected SMOOTHED speed peak (~0.6 s).
    This is NOT certified to be the maximum of an entire 10-yard sprint.
    The athlete-to-rep matching remains the smoothed-detection algorithm.
    """
    if reps.empty:
        return reps.copy()
    if 'Raw max (mph)' not in reps.columns:
        raise RuntimeError('Saved CSV is missing Raw max (mph). Cannot compute true unsmoothed local peak without saved raw speed data.')
    out=reps.copy()
    sm=pd.to_numeric(out['Smoothed peak (mph)'] if 'Smoothed peak (mph)' in out else out['Peak (mph)'], errors='coerce')
    raw=pd.to_numeric(out['Raw max (mph)'],errors='coerce')
    missing=sm.notna() & sm.gt(0) & (~np.isfinite(raw) | (raw <= 0))
    if bool(missing.any()):
        where=out.loc[missing, ['Athlete','Rep']].head(8).to_dict('records')
        raise RuntimeError('Found detected reps without a valid raw peak; refusing to silently mix smoothed/raw speeds. '+str(where))
    out['Smoothed peak (mph)']=sm
    out['Raw peak (mph)']=raw
    # Keep historical name for compatibility. In the RAW CSV it now means RAW.
    out['Peak (mph)']=raw
    if 'Peak (m/s)' in out:
        out['Peak (m/s)']=raw/MPH
    grouping=[x for x in ('Match key','Athlete','Block','Set') if x in out]
    if not grouping:
        grouping=['Athlete','Set']
    out=out.sort_values(grouping+['Rep']).copy()
    prev=out.groupby(grouping,dropna=False)['Peak (mph)'].shift(1)
    out['Previous (mph)']=prev
    out['Rep decrement (%)']=np.where(prev.gt(0),100*(prev-raw)/prev,np.nan)
    best=out.groupby(grouping,dropna=False)['Peak (mph)'].transform('max')
    out['Drop from set best (%)']=np.where(best.gt(0),100*(best-raw)/best,np.nan)
    return out



def verify_distinct_athlete_profiles(source, paths):
    """Block publication of apparently copied athlete data; never reassign streams."""
    if source.empty or 'Athlete' not in source or 'Rep' not in source:
        return
    from collections import defaultdict
    issues=[]
    # Evaluate full repetition sequences, not rounded summary scores.
    # Repetition numbers disambiguate repeated set/time points.
    vectors={}
    for athlete,df in source.groupby('Athlete',dropna=False):
        if 'Peak (mph)' not in df:continue
        d=df.sort_values(['Rep','Set'] if 'Set' in df else ['Rep'])
        vals=tuple((int(k),round(float(v),8)) for k,v in zip(d['Rep'],pd.to_numeric(d['Peak (mph)'],errors='coerce')) if pd.notna(k) and pd.notna(v))
        if len(vals)>=20:vectors[str(athlete)]=vals
    names=sorted(vectors)
    for i,first in enumerate(names):
        for second in names[i+1:]:
            if vectors[first]==vectors[second]:
                issues.append({'type':'IDENTICAL_REPETITION_PROFILE','athlete_1':first,'athlete_2':second,
                               'detail':f'{len(vectors[first])} identical repetition-speed values (8 decimal places)'})
    # Match-source metadata can detect a reused raw recording EVEN IF the processing alters values.
    try:
        if MATCH_METADATA is not None:
            matches,_=get_matches(MATCH_METADATA)
            rawmap=defaultdict(set)
            pairmap=defaultdict(set)
            for m in matches:
                a=str(m.get('player') or '').strip()
                r=m.get('raw_id')
                if a and a!='Unknown player' and r:
                    rawmap[str(r)].add(a)
                    pairmap[a].add(str(r))
            for raw,nset in rawmap.items():
                if len(nset)>1:
                    a=sorted(nset)
                    issues.append({'type':'SHARED_RAW_RECORDING_ID','athlete_1':a[0],
                                   'athlete_2':', '.join(a[1:]),'detail':'Two or more athlete tags point to same rawDataId; verify source session-player associations'})
            for a,ids in pairmap.items():
                if len(ids)>1:
                    issues.append({'type':'MULTIPLE_RECORDING_IDS_FOR_ATHLETE','athlete_1':a,'athlete_2':'',
                                   'detail':'Athlete has multiple rawDataIds on this day; verify before consolidating tags'})
    except Exception as exc:
        issues.append({'type':'METADATA_AUDIT_FAILED','athlete_1':'','athlete_2':'','detail':str(exc)[:160]})
    audit=paths['reps'].parent/'assessment_identity_duplicate_audit.csv'
    pd.DataFrame(issues,columns=['type','athlete_1','athlete_2','detail']).to_csv(audit,index=False)
    blocking=[x for x in issues if x['type'] in ('IDENTICAL_REPETITION_PROFILE','SHARED_RAW_RECORDING_ID')]
    if blocking:
        for x in blocking:print('  IDENTITY / DATA COLLISION:',x['athlete_1'],'vs',x['athlete_2'],'-',x['type'])
        raise RuntimeError('Potential cross-athlete duplicated recording or results. Reports NOT refreshed. Inspect '+str(audit))
    print('Athlete-data integrity audit:',len(vectors),'distinct analyzed profiles;',len(issues),'metadata cautions. Log:',audit)

def generate_all_reports(day, paths):
    source=pd.read_csv(paths['reps'],low_memory=False) if paths['reps'].exists() and paths['reps'].stat().st_size else pd.DataFrame()
    review=pd.read_csv(paths['review']).fillna('') if paths['review'].exists() and paths['review'].stat().st_size else pd.DataFrame(columns=['Athlete','Status'])
    if review.empty and not source.empty:
        review=pd.DataFrame([{'Athlete':n,'Status':'Provisional GPS estimates'} for n in source['Athlete'].unique()])
    verify_distinct_athlete_profiles(source,paths)
    reps=prepare_raw_local_peaks(source)
    # Export a new RAW-derived CSV, preserve the original smoothed reference CSV.
    if not reps.empty:
        temp=paths['raw_reps'].with_suffix('.tmp')
        reps.to_csv(temp,index=False)
        temp.replace(paths['raw_reps'])
    summary=summary_from_reps(reps,review)
    summary.to_csv(paths['summary'],index=False)
    make_team_pdf(reps,summary,review,paths['pdf'],day)
    team_dashboard(reps,summary,review,paths['html'],paths['pdf'].name,
                   paths['summary'].name,paths['raw_reps'].name,day)
    print('Primary dashboard speeds: RAW unsmoothed local peaks. Original smoothed CSV preserved.')
    if not reps.empty:
        print('Raw repetition CSV:',paths['raw_reps'])
    return summary


def analyze_all(day, clock='auto', list_only=False, only=None,
                no_early_stop=False, open_browser=True, gps_shift_hours=0.0, force_refresh=False):
    folder,paths=group_paths(day)
    folder.mkdir(parents=True,exist_ok=True)
    print(f'Finding all exact {DRILL_NAME!r} tags for {day}...')
    matches,names=find_drill_with_diagnostics(day)
    matches=strict_match_dedup(matches)
    # Persist the exact per-assessment API link for the manual-review UI.
    # Never infer identity from coincident drill timestamps.
    index = []
    for match in matches:
        ident = '|'.join(str(match.get(k) or '') for k in
            ('session_player_id','raw_id','drill_id','start','end'))
        row = {k:match.get(k) for k in ('player','raw_id','session_player_id','drill_id','start','end')}
        row['match_key'] = hashlib.sha256(ident.encode('utf-8')).hexdigest()[:24]
        index.append(row)
    index_path = folder / 'manual_review_identity_index.json'
    temp_index = index_path.with_suffix('.tmp')
    temp_index.write_text(json.dumps(index,indent=2,default=str),encoding='utf-8')
    temp_index.replace(index_path)
    if only:
        matches=[m for m in matches if str(m['player']).casefold()==only.casefold()]
    if not matches:
        print('No matching player-tagged drills found. No GPS downloaded.')
        return None
    print(f'Found {len(matches)} distinct tagged athlete assessments:')
    for i,m in enumerate(matches,1):
        print(f'  [{i:02d}] {m["player"]} | {m["start"]} – {m["end"]}')
    if list_only:
        print('Metadata-only mode: no GPS downloaded.')
        return None
    existing_rep=pd.read_csv(paths['reps']) if paths['reps'].exists() and paths['reps'].stat().st_size else pd.DataFrame()
    existing_review=pd.read_csv(paths['review']).fillna('') if paths['review'].exists() and paths['review'].stat().st_size else pd.DataFrame()
    # When updating only one player, retain every OTHER athlete from the previous
    # output. v8/v9 had a --only mode that accidentally rewrote the team files
    # with only the selected player. Refuse a targeted merge if identifiers are
    # absent instead of silently deleting data.
    if only and (not existing_rep.empty or not existing_review.empty):
        for table_name, frame in [('repetition',existing_rep),('quality-review',existing_review)]:
            if not frame.empty and 'Match key' not in frame.columns:
                raise RuntimeError(f'Cannot preserve previous {table_name} data: Match key missing. '
                                   'Existing reports remain unchanged.')
        backup_dir=folder / ('backup_before_targeted_update_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
        backup_dir.mkdir(parents=True,exist_ok=True)
        for backup_key in ('summary','reps','review','manifest','pdf','html'):
            src=paths[backup_key]
            if src.exists():
                shutil.copy2(src, backup_dir/src.name)
        print('  Existing reports backed up to:',backup_dir)
    # Track session-player and tagged drill separately from athlete name to avoid accidentally
    # overwriting a different recording when devices were shared.
    manifest=json.loads(paths['manifest'].read_text()) if paths['manifest'].exists() else {}
    previous=manifest.get('completed',{})
    rep_parts=[]; review_parts=[]
    if only:
        selected_keys=set()
        for selected in matches:
            identity='|'.join(str(selected.get(k) or '') for k in
                              ('session_player_id','raw_id','drill_id','start','end'))
            selected_keys.add(hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24])
        if not existing_rep.empty:
            preserved=existing_rep[~existing_rep['Match key'].astype(str).isin(selected_keys)].copy()
            if not preserved.empty:rep_parts.append(preserved)
        if not existing_review.empty:
            preserved=existing_review[~existing_review['Match key'].astype(str).isin(selected_keys)].copy()
            if not preserved.empty:review_parts.append(preserved)
        print(f'  Targeted update: preserving existing results for all other players.')
    recorded=set()
    for count,m in enumerate(matches,1):
        ident='|'.join(str(m.get(k) or '') for k in ('session_player_id','raw_id','drill_id','start','end'))
        idhash=hashlib.sha256(ident.encode('utf-8')).hexdigest()[:24]
        if idhash in recorded:continue
        recorded.add(idhash)
        athlete=str(m['player'])
        print(f'\n[{count}/{len(matches)}] {athlete}')
        if not force_refresh and not gps_shift_hours and idhash in previous and not existing_review.empty and 'Match key' in existing_review:
            r=existing_review[existing_review['Match key']==idhash]
            if not r.empty:
                print('  Reusing completed saved analysis (no GPS request).')
                review_parts.append(r)
                if not existing_rep.empty and 'Match key' in existing_rep:
                    rep_parts.append(existing_rep[existing_rep['Match key']==idhash])
                continue
        flags=[]
        df=pd.DataFrame()
        info={'Match key':idhash,'Athlete':athlete,'Status':'Not processed',
            'Matched reps':0,'Candidate peaks':0,'Blocks detected':0,
            'Timestamp start local':'','Timestamp end local':'',
            'Session player linked':'Yes' if m.get('session_player_id') else 'No'}
        try:
            if athlete=='Unknown player':
                player,src=resolve_name_from_api(m,day)
                if not player:raise RuntimeError('No verified athlete identity in STATSports metadata')
                athlete=player;info['Athlete']=athlete
            clock_notes=[]
            w=drill_window(m,clock_notes,clock)
            info['Timestamp interpretation']=' | '.join(clock_notes)
            info['Timestamp start local']=str(pd.Timestamp(w[0],unit='ns',tz='UTC').tz_convert(TZ))
            info['Timestamp end local']=str(pd.Timestamp(w[1],unit='ns',tz='UTC').tz_convert(TZ))
            if not m.get('raw_id'):raise RuntimeError('Tagged player lacks raw GPS ID')
            trace,meta=fetch_drill_trace(m,w,folder,fast_stop=not no_early_stop,
                                         gps_shift_hours=gps_shift_hours,force_refresh=force_refresh)
            if gps_shift_hours:
                flags.append(f'GPS timestamps manually shifted {gps_shift_hours:+g} h (PROVISIONAL - verify Sonra clock)')
            if meta.get('done')!='yes':flags.append('GPS stream incomplete')
            # A drill tag defines the assessment, but the separate rawDataId
            # must actually include GPS measurements in that interval.
            # Never label an empty recording as a legitimate 0/30 test.
            if trace.empty or not bool(pd.to_numeric(trace['count'],errors='coerce').fillna(0).gt(0).any()):
                first,last=meta.get('stream_first_ns'),meta.get('stream_last_ns')
                detail=''
                if first is not None and last is not None:
                    try:
                        first_loc=pd.Timestamp(int(float(first)),unit='ns',tz='UTC').tz_convert(TZ)
                        last_loc=pd.Timestamp(int(float(last)),unit='ns',tz='UTC').tz_convert(TZ)
                        detail=(f' GPS stream examined: {first_loc:%I:%M:%S %p}'
                                f' to {last_loc:%I:%M:%S %p} Eastern.')
                    except (ValueError,OverflowError,TypeError):
                        pass
                raise RuntimeError('No GPS samples overlap the exact tagged drill.'+detail+
                    ' Check the attached rawDataId/session and Sonra clock; do not invent a time offset.')
            ev,sets,blocks,opts=analyze_one(trace,athlete)
            info['Candidate peaks']=len(ev)
            info['Blocks detected']=len(blocks)
            cadence_diagnostics(ev,athlete,idhash,folder)
            selected,competing=primary_block(blocks)
            if selected is not None:
                df=result_rows(ev,selected,athlete,'Assessment 1',trace,day)
                df['Match key']=idhash
                info['Matched reps']=int(df['Peak (mph)'].count())
                if any(s.get('first_rep_relaxed') for s in selected['sets']):
                    flags.append('Set-opening peak recovered with relaxed timing (REVIEW)')
                if selected.get('fallback_method'):
                    flags.append('PROVISIONAL: relaxed 3x10 cadence recovery; verify raw trace and assignment')
                if selected.get('requires_review') or competing:
                    flags.append('Restart, missing rep or ambiguous block')
                if bool(df['QC note'].fillna('').astype(str).str.contains('spike',case=False).any()):
                    flags.append('Possible GPS peak spike')
                if competing:flags.append('Multiple possible assessment blocks')
            else:
                flags.append('No matching 3x10 pattern')
            info['Status']='; '.join(dict.fromkeys(flags)) if flags else 'Candidate - verify trace'
            # Keep lightweight evidence for later manual check; do not dump GPS rows.
            pd.DataFrame(ev).to_csv(folder / f'candidates_{idhash}.csv',index=False)
        except (ValueError,RuntimeError,requests.RequestException,sqlite3.Error) as ex:
            info['Status']='Error - '+str(ex)[:200]
            print('  WARNING:',info['Status'])
        review_parts.append(pd.DataFrame([info]))
        if not df.empty:rep_parts.append(df)
        # Checkpoint derived CSVs after each athlete. Atomic replace, resumable.
        current_reps=pd.concat(rep_parts,ignore_index=True) if rep_parts else pd.DataFrame(columns=['Athlete','Peak (mph)','Match key'])
        current_review=pd.concat(review_parts,ignore_index=True)
        tmp=paths['reps'].with_suffix('.tmp');current_reps.to_csv(tmp,index=False);tmp.replace(paths['reps'])
        tmp=paths['review'].with_suffix('.tmp');current_review.to_csv(tmp,index=False);tmp.replace(paths['review'])
        if not info['Status'].startswith('Error'):
            previous[idhash]={'athlete':athlete,'status':info['Status']}
        manifest={'date':day,'drill':DRILL_NAME,'completed':previous}
        temp=paths['manifest'].with_suffix('.tmp');temp.write_text(json.dumps(manifest,indent=2));temp.replace(paths['manifest'])
        print(f'  {info["Matched reps"]}/30 peaks; candidates {info["Candidate peaks"]}; {info["Status"]}')
    result=generate_all_reports(day,paths)
    # Re-running the all-player pipeline previously replayed its 29/30
    # checkpoints without revisiting unused candidates. Repair ONLY uniquely
    # supported missing set-opening peaks from saved candidate CSVs now.
    # This is strictly offline (no additional raw GPS calls), saves backups,
    # and marks all recovered peaks PROVISIONAL for trace verification.
    print('\nChecking offline candidates for missing first reps after set breaks...')
    review_missing_set_openings(day,apply=True)
    # Read regenerated summary after any repairs, not the stale pre-repair frame.
    result=pd.read_csv(paths['summary']) if paths['summary'].exists() else result
    print('\nPlayer summary: ',paths['summary'])
    print('All reps CSV:  ',paths['reps'])
    print('Review status: ',paths['review'])
    print('Dashboard:     ',paths['html'])
    print('PDF:           ',paths['pdf'])
    if open_browser:
        launch_saved_dashboard(day)
    return result


def _run_demo(day):
    """Offline report smoke test with arbitrary fictional values; no API calls."""
    import tempfile
    global OUTPUT_ROOT
    OUTPUT_ROOT=Path(tempfile.mkdtemp(prefix='statsports_v8_demo_'))
    directory,p=group_paths(day)
    directory.mkdir(parents=True,exist_ok=True)
    rng=np.random.default_rng(21)
    names=['Sample Athlete A','Sample Athlete B','Sample Athlete C']
    vals=[]
    reviews=[]
    for ii,name in enumerate(names):
        s={'Athlete':name,'Match key':f'sample-{ii}','Status': 'Candidate - verify trace',
           'Matched reps':30 if ii!=2 else 28,'Candidate peaks':30,'Blocks detected':1}
        if ii==2:s['Status']='Incomplete 28/30; missing reps'
        reviews.append(s)
        for rep in range(1,31):
            sn=(rep-1)//10+1
            speed=18.5+ii*.9-(rep-1)*.025+rng.normal(0,.20)
            if ii==2 and rep in (5,26):speed=np.nan
            vals.append({'Athlete':name,'Recording':name,'Block':'Assessment 1',
                'Set':sn,'Set Rep':(rep-1)%10+1,'Rep':rep,'Peak (mph)':speed,
                'Raw max (mph)': (speed+0.15 if np.isfinite(speed) else np.nan),
                'Rep decrement (%)':np.nan,'Detection':'Candidate' if np.isfinite(speed) else 'Missing',
                'QC note':'','Match key':f'sample-{ii}'})
    rdf=pd.DataFrame(vals)
    rdf['Previous']=rdf.groupby(['Athlete','Set'])['Peak (mph)'].shift(1)
    rdf['Rep decrement (%)']=100*(rdf['Previous']-rdf['Peak (mph)'])/rdf['Previous']
    rdf.to_csv(p['reps'],index=False)
    pd.DataFrame(reviews).to_csv(p['review'],index=False)
    result=generate_all_reports(day,p)
    assert int(result.loc[result.Athlete=='Sample Athlete C','Detected reps'].iloc[0])==28
    assert float(result.loc[result.Athlete=='Sample Athlete A','Best-to-worst decrement (%)'].iloc[0])>0
    print('OFFLINE DEMO outputs:',p['pdf'],p['html'])
    return p


def _as_unix_seconds(series):
    """Mixed fractional-second precision, timezone aware; NaT => actual NaN."""
    if series.empty:
        return np.array([],dtype=float)
    stamps=pd.to_datetime(series,format='mixed',utc=True,errors='coerce')
    numbers=stamps.astype('int64').to_numpy(dtype=float)/1e9
    numbers[np.asarray(stamps.isna())]=np.nan
    return numbers


def review_missing_set_openings(day, apply=False):
    """OFFLINE candidate review; no API or raw GPS download.

    Only proposes an unassigned candidate for rep 11/21 when the other 29
    are present and its timestamp supports a plausible first-rep restart.
    Writes source CSV backups before applying, and retains explicit QC flags.
    """
    folder,paths=group_paths(day)
    if not paths['reps'].exists() or not paths['review'].exists():
        raise RuntimeError('Saved rep and review CSVs not found in '+str(folder))
    reps=pd.read_csv(paths['reps'],low_memory=False)
    review=pd.read_csv(paths['review'],low_memory=False).fillna('')
    # All-empty QC note columns may be inferred as float by pandas.
    if 'QC note' in reps:
        reps['QC note']=reps['QC note'].astype('object')
    if 'Detection' in reps:
        reps['Detection']=reps['Detection'].astype('object')
    if 'Peak time local' in reps:
        reps['Peak time local']=reps['Peak time local'].astype('object')
    if 'Status' in review:
        review['Status']=review['Status'].astype('object')
    if 'Match key' not in reps or 'Peak time local' not in reps:
        raise RuntimeError('Rep CSV lacks Match key or Peak time local')
    proposals=[]
    for (athlete,key),part in reps.groupby(['Athlete','Match key'],dropna=False):
        if pd.isna(key):continue
        part=part.sort_values('Rep')
        missing=part[pd.to_numeric(part['Peak (mph)'],errors='coerce').isna()]
        # Never guess when the athlete has multiple missing/misidentified reps.
        if len(part)!=30 or len(missing)!=1:continue
        rep_no=int(missing.iloc[0]['Rep'])
        if rep_no not in (11,21):continue
        candfile=folder/f'candidates_{key}.csv'
        if not candfile.exists():continue
        candidates=pd.read_csv(candfile)
        if candidates.empty or not {'peak_time','peak_mps'}.issubset(candidates):continue
        candidates['peak_time']=pd.to_numeric(candidates['peak_time'],errors='coerce')
        candidates['peak_mps']=pd.to_numeric(candidates['peak_mps'],errors='coerce')
        candidates=candidates.dropna(subset=['peak_time','peak_mps'])
        candidates=candidates.drop_duplicates(subset='peak_time')
        if candidates.empty:continue
        peaks=_as_unix_seconds(part['Peak time local'])
        assigned=peaks[np.isfinite(peaks)]
        if len(assigned)!=29:continue
        # Candidate must be otherwise unused; 0.5 sec protects quantization.
        unused=candidates[candidates['peak_time'].map(lambda t:
          not bool(np.any(np.abs(assigned-t)<=0.5)))].copy()
        if unused.empty:continue
        # Don't assume all 30 candidates are truthful; require UNIQUE nearby
        # evidence even if there are extra isolated spikes elsewhere.
        prev=part[part['Rep']==rep_no-1]
        nxt=part[part['Rep']==rep_no+1]
        nxt2=part[part['Rep']==rep_no+2]
        if any(x.empty for x in (prev,nxt,nxt2)):continue
        tprev=float(_as_unix_seconds(prev['Peak time local'])[0])
        tnext=float(_as_unix_seconds(nxt['Peak time local'])[0])
        tnext2=float(_as_unix_seconds(nxt2['Peak time local'])[0])
        if not np.isfinite(tprev+tnext+tnext2):continue
        if abs((tnext2-tnext)-CADENCE_S)>1.8:continue
        expected=tnext-CADENCE_S
        nearby=unused[(unused['peak_time']-expected).abs()<=SET_OPENING_TOLERANCE_S].copy()
        if len(nearby)!=1:continue
        candidate=nearby.iloc[0]
        t0=float(candidate['peak_time'])
        gap=tnext-t0
        recovery=t0-tprev
        # Flexible rest is useful for restarted sets; still require a unique,
        # unused candidate near the set-opening phase and close agreement
        # between the next two repetitions. Do NOT blindly fill 30/30.
        if not (10<=gap<=24 and REST_GAP_RANGE_S[0]<=recovery<=REST_GAP_RANGE_S[1]):continue
        if 'spike_flag' in candidate and str(candidate['spike_flag']).lower() in ('true','1'):
            continue
        speed=float(candidate['peak_mps'])
        if speed<=0:continue
        proposal={'Athlete':athlete,'Match key':key,'Rep':rep_no,
                  'candidate_unix':t0,'Peak (mph)':round(speed*MPH,3),
                  'Offset from 17s (s)':round(t0-expected,2),
                  'Gap from prior set (s)':round(recovery,2),
                  'Gap to next rep (s)':round(gap,2),
                  'candidate_raw_max_mps':float(candidate.get('raw_peak_mps',speed)),
                  'Rest QC':('Review extended/shortened rest' if not NORMAL_REST_GAP_RANGE_S[0]<=recovery<=NORMAL_REST_GAP_RANGE_S[1] else 'Normal rest')}
        proposals.append(proposal)
    output=folder/f'assessment_{day}_set_opening_proposals.csv'
    pd.DataFrame(proposals,columns=['Athlete','Match key','Rep','candidate_unix',
            'Peak (mph)','Offset from 17s (s)','Gap from prior set (s)',
            'Gap to next rep (s)','candidate_raw_max_mps','Rest QC']).to_csv(output,index=False)
    print('Offline set-start review saved:',output)
    if not proposals:
        print('No uniquely supported Set 2/3 first-rep recovery. Existing reports unchanged.')
        return
    for p in proposals:
        print(f"  {p['Athlete']}: rep {p['Rep']}, {p['Peak (mph)']:.2f} mph, "
              f"offset {p['Offset from 17s (s)']:+.2f}s, "
              f"rest gap {p['Gap from prior set (s)']:.1f}s [PROVISIONAL; {p['Rest QC']}]")
    if not apply:
        print('READ ONLY. To fill supported peaks and refresh reports, use --apply-set-start-recovery')
        return
    import shutil
    backup_dir=folder / ('backup_before_setstart_recovery_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
    backup_dir.mkdir(parents=True, exist_ok=True)
    for tag in ('reps','review','summary','pdf','html'):
        oldpath=paths[tag]
        if oldpath.exists():
            shutil.copy2(oldpath,backup_dir/oldpath.name)
    print('Saved pre-repair report backups:',backup_dir)
    for p in proposals:
        mask=(reps['Athlete']==p['Athlete']) & (reps['Match key']==p['Match key']) & (reps['Rep']==p['Rep'])
        if mask.sum()!=1 or pd.notna(reps.loc[mask,'Peak (mph)'].iloc[0]):
            raise RuntimeError('Candidate slot changed between review and apply; refusing overwrite')
        pos=int(np.flatnonzero(mask.to_numpy())[0])
        peak=float(p['Peak (mph)'])
        speed=peak/MPH
        stamp=pd.Timestamp(p['candidate_unix'],unit='s',tz='UTC').tz_convert(TZ)
        reps.at[pos,'Peak (m/s)']=speed
        reps.at[pos,'Peak (mph)']=peak
        reps.at[pos,'Raw max (mph)']=p['candidate_raw_max_mps']*MPH
        reps.at[pos,'Peak time local']=str(stamp)
        reps.at[pos,'Detection']='Review'
        reps.at[pos,'QC note']=('Provisional set-opening recovery (flexible inter-set rest): timing offset '
                                f"{p['Offset from 17s (s)']:+.2f}s; confirm GPS trace")
        reps.at[pos,'Timing error (s)']=abs(p['Offset from 17s (s)'])
        m=(reps['Athlete']==p['Athlete']) & (reps['Match key']==p['Match key']) & (reps['Set']==(p['Rep']-1)//10+1)
        grp=reps.loc[m].sort_values('Rep')
        prior=grp['Peak (mph)'].shift(1)
        reps.loc[grp.index,'Previous (mph)']=prior.to_numpy()
        reps.loc[grp.index,'Rep decrement (%)']=((prior-grp['Peak (mph)'])/prior*100).to_numpy()
        best=grp['Peak (mph)'].max()
        reps.loc[grp.index,'Drop from set best (%)']=((best-grp['Peak (mph)'])/best*100).to_numpy()
        mr=(review['Athlete']==p['Athlete']) & (review['Match key']==p['Match key'])
        if mr.any():
            review.loc[mr,'Matched reps']=30
            review.loc[mr,'Status']='Provisional recovery of set-opening Rep '+str(p['Rep'])+'; VERIFY GPS TRACE'
    # Write new data atomically. All dependent reports are regenerated locally.
    for key,frame in (('reps',reps),('review',review)):
        temp=paths[key].with_suffix('.tmp')
        frame.to_csv(temp,index=False);temp.replace(paths[key])
    generate_all_reports(day,paths)
    print('Updated reports offline:',paths['html'],paths['pdf'])
    print('Every recovered rep remains explicitly marked for manual GPS validation.')


def diagnose_gps_timestamps(day,athlete,clock='auto',page_limit=4):
    """Read-only, paginated GPS audit of one selected athlete.

    Drill identity and time come from the exact Sonra tag, not preset hours.
    Inspect several pages because a page of data at noon does not prove
    the full GPS file lacks an earlier or later assessment.
    """
    if not athlete:
        raise ValueError('--diagnose-gps-times requires --only "Athlete Name".')
    matches,_=find_drill_with_diagnostics(day)
    matches=[m for m in strict_match_dedup(matches)
             if str(m['player']).casefold()==athlete.casefold()]
    if not matches:
        raise RuntimeError('No tagged assessment matched '+repr(athlete))
    for m in matches:
        print('\nGPS RECORDING AUDIT (read-only) for',m['player'])
        w=drill_window(m,[],clock)
        rawid=m.get('raw_id')
        if not rawid:
            print('No rawDataId attached to this tagged athlete. Cannot fetch GPS.')
            continue
        lo=pd.Timestamp(w[0],unit='ns',tz='UTC')
        hi=pd.Timestamp(w[1],unit='ns',tz='UTC')
        print(f'Target tagged drill: {lo.tz_convert(TZ):%I:%M:%S %p} to {hi.tz_convert(TZ):%I:%M:%S %p} Eastern')
        cursor=None
        seen=set()
        total_valid=0
        total_overlap=0
        overall_first=None
        overall_last=None
        page_count=0
        for k in range(1,page_limit+1):
            payload={'thirdPartyApiId':API_KEY,'rawDataId':rawid}
            if cursor is not None:
                payload['nextPage']=cursor
            body=api('getSessionGpsData',payload)
            batch=unwrap_gps(body)
            if not batch:
                if k>1:
                    print(f'Page {k}: empty response, although prior page advertised another page.')
                    print('  Check STATSports nextPage behavior and the recording linkage.')
                else:
                    print('First GPS page is empty. Check recording linkage and Sonra upload.')
                break
            frame=normalize_gps(batch)
            page_count+=1
            total_valid+=len(frame)
            if not frame.empty:
                left=frame.timestamp.min()
                right=frame.timestamp.max()
                overall_first=left if overall_first is None else min(overall_first,left)
                overall_last=right if overall_last is None else max(overall_last,right)
                n=int(((frame.timestamp>=lo)&(frame.timestamp<hi)).sum())
                total_overlap+=n
                print(f'Page {k}: {len(batch):,} records, '
                      f'{left.tz_convert(TZ):%I:%M:%S %p} - {right.tz_convert(TZ):%I:%M:%S %p} '
                      f'Eastern; {n:,} inside the tagged assessment')
            else:
                print(f'Page {k}: {len(batch):,} records but no parseable GPS speed and timestamps')
            nxt=next_cursor(body,cursor)
            if nxt is None:
                print('Recording ended after this page.')
                break
            if nxt in seen:
                print('Repeated pagination cursor detected. Stopping to avoid duplicate data.')
                break
            seen.add(nxt)
            cursor=nxt
        print(f'Scanned {page_count} page(s), {total_valid:,} valid samples, '
              f'{total_overlap:,} overlap the EXACT drill tag.')
        if total_overlap==0 and overall_first is not None:
            print('NO OVERLAP IN SCANNED PAGES. GPS covered '
                  f'{overall_first.tz_convert(TZ):%I:%M:%S %p} - '
                  f'{overall_last.tz_convert(TZ):%I:%M:%S %p} Eastern.')
            if overall_first>hi:
                print('Available GPS data begins AFTER the tag. This is a GPS recording/session '
                      'linkage or timebase issue, not a reason to shift the drill arbitrarily.')
            elif overall_last<lo:
                print('Available GPS data ends BEFORE the tag. Later pages may be required.')
            else:
                print('GPS pages span the tag but contain no samples during it. '
                      'Check discontinuities and the recording-to-player assignment.')
        if page_count>=page_limit and cursor is not None:
            print('Additional pages may exist; increase --gps-pages to inspect more.')
        print('No files, reports, or cache entries were modified.')



def audit_recording_linkage(day, athlete, probe=False):
    """Read-only comparison of tag/session-player IDs with available GPS identifiers.

    Never reassign a raw GPS recording automatically: shared devices and
    unrelated session exports can make superficially similar identifiers unsafe.
    """
    if not athlete:
        raise ValueError('--audit-linkage requires --only "Athlete Name".')
    matches,_ = find_drill_with_diagnostics(day)
    targets=[m for m in strict_match_dedup(matches)
             if str(m.get('player','')).casefold() == athlete.casefold()]
    if not targets:
        raise RuntimeError('No matching tagged assessment for '+repr(athlete))

    # Private opaque identifiers appear as short one-way fingerprints, not as
    # raw credentials, ids, or complete source metadata.
    def fp(v):
        if v is None or str(v).strip() in ('','None'): return 'missing'
        return hashlib.sha256(str(v).encode('utf-8')).hexdigest()[:10]

    records=[]
    def walk(node,depth=0,path='root'):
        if depth>18:return
        if isinstance(node,list):
            for i,child in enumerate(node):walk(child,depth+1,f'{path}[{i}]')
        elif isinstance(node,dict):
            if isinstance(node.get('drills'),list):
                n,_,conflict=_sessionplayer_identity(node)
                records.append((node,n if not conflict else None,path))
            for k,v in node.items():
                if k!='drills' and isinstance(v,(dict,list)):
                    walk(v,depth+1,f'{path}.{k}')
    walk(MATCH_METADATA)

    for target in targets:
        print('\n'+('='*72))
        print('RECORDING LINKAGE AUDIT for',target['player'])
        print('No data or dashboard files will be modified.')
        print('Tagged drill:',target.get('start'),'->',target.get('end'))
        print('Session-player ID fingerprint:',fp(target.get('session_player_id')))
        print('Session ID fingerprint:',fp(target.get('session_id')))
        print('Drill ID fingerprint:',fp(target.get('drill_id')))
        print('Selected rawDataId fingerprint:',fp(target.get('raw_id')))
        print('Selected rawDataId came from fallback dataId:', bool(target.get('raw_is_dataid')))

        matches_same=[(node,path) for node,n,path in records
                      if n and str(n).casefold()==athlete.casefold()]
        print('Athlete-specific session-player records:',len(matches_same))
        candidates={}
        raw_flds=('session_player_rawDataId','sessionPlayerRawDataId','rawDataId','dataId')
        for i,(node,path) in enumerate(matches_same,1):
            name=node.get('playerDetails') if isinstance(node.get('playerDetails'),dict) else {}
            print(f'  Record {i}: sessionPlayer id={fp(node.get("id"))}, session id={fp(node.get("sessionId"))}, path={path}')
            tag_count=0
            for d in node.get('drills',[]):
                if not isinstance(d,dict):continue
                if normalize_name(get_field(d,'drillName','name','title'))==normalize_name(DRILL_NAME):
                    tag_count+=1
                    print('    Tag:',str(get_field(d,'startTime','start')),'->',str(get_field(d,'endTime','end')))
                    print('    Drill linked sessionPlayer id:',fp(d.get('sessionPlayerDataId')))
            if not tag_count:print('    This record has no exact assessment tag.')
            for src,obj in [('session-player',node),('nested player',node.get('player'))]:
                if not isinstance(obj,dict):continue
                for fld in raw_flds:
                    v=obj.get(fld)
                    if v is not None and str(v).strip():
                        print(f'    {src}.{fld}: {fp(v)}')
                        candidates[str(v)]=f'{src}.{fld}'
        if target.get('raw_id') is not None:
            candidates[str(target['raw_id'])]='selected rawDataId'
        by_stream=[x['player'] for x in matches
                   if x.get('raw_id') is not None
                   and str(x['raw_id'])==str(target.get('raw_id'))]
        print('Tagged assessments using this same rawDataId:',', '.join(sorted(set(by_stream))) if by_stream else 'none')
        print('Distinct raw GPS identifiers visible for this athlete:',len(candidates))
        if not candidates:
            print('No GPS identifier found in athlete-specific metadata. Review Sonra export permissions or session assignment.')
            continue
        if len(candidates)==1:
            print('No alternate athlete-linked rawDataId in the retrieved metadata.')
            print('Re-export/re-sync the correct athlete recording or inspect the session link in Sonra; changing timing thresholds cannot supply absent GPS data.')

        if not probe:
            print('To check the actual time ranges for any other athlete-linked GPS IDs, add --probe-linked-gps.')
            continue
        possible=[]
        # This is intentionally restricted to athlete-linked recordings.
        for candidate in list(candidates)[:8]:
            print('  Probing raw GPS ID fingerprint:',fp(candidate),'(read-only)')
            cursor=None;seen=set();first=None;last=None;pages=0;valid_n=0
            for k in range(1,21):
                payload={'thirdPartyApiId':API_KEY,'rawDataId':candidate}
                if cursor is not None:payload['nextPage']=cursor
                try:
                    body=api('getSessionGpsData',payload)
                except (RuntimeError,ValueError) as exc:
                    print('    GPS request failed:',type(exc).__name__,str(exc)[:90]);break
                rows=unwrap_gps(body)
                if not rows:break
                frame=normalize_gps(rows)
                pages+=1;valid_n+=len(frame)
                if not frame.empty:
                    left=frame.timestamp.min();right=frame.timestamp.max()
                    first=left if first is None else min(first,left)
                    last=right if last is None else max(last,right)
                nxt=next_cursor(body,cursor)
                if nxt is None or nxt in seen:break
                seen.add(nxt);cursor=nxt
            if first is None:
                print('    No parseable GPS timestamps in scanned pages.')
                continue
            print(f'    {pages} page(s), {valid_n:,} valid rows; GPS spans {first.tz_convert(TZ):%I:%M:%S %p} to {last.tz_convert(TZ):%I:%M:%S %p} Eastern')
            a=pd.Timestamp(target['start']);b=pd.Timestamp(target['end'])
            for clock,zone in [('UTC','UTC'),('Eastern','America/New_York')]:
                if a.tzinfo is not None:
                    aa=a.tz_convert('UTC');bb=b.tz_convert('UTC')
                else:
                    aa=a.tz_localize(zone).tz_convert('UTC');bb=b.tz_localize(zone).tz_convert('UTC')
                extent_intersects=(first<=bb and last>=aa)
                print(f'    Tag interpreted as {clock}: GPS extent {"COULD overlap" if extent_intersects else "does not overlap"} tag ({aa.tz_convert(TZ):%I:%M}–{bb.tz_convert(TZ):%I:%M} Eastern)')
                if extent_intersects:possible.append((fp(candidate),clock))
        if possible:
            print('Candidate stream clock overlaps (NOT identity confirmation):',possible)
            print('Verify the exact recording and athlete/device assignment in Sonra before changing data attribution.')
        else:
            print('No athlete-linked raw GPS time ranges overlap either clock interpretation of this tagged drill.')
            print('The required GPS samples are not accessible through these linked IDs; check Sonra session assignment and re-export.')
    print('\nAudit finished. Existing data, caches, reports, and dashboard unchanged.')



def audit_daywide_recordings(day, page_cap=20):
    """Read-only scan of every rawDataId visible in this date's metadata.

    This can locate a *candidate* morning recording even when it is not linked
    to the tagged athlete.  It NEVER automatically attributes cross-athlete GPS.
    """
    data=get_metadata(day)
    targets, _ = get_matches(data)
    target_names={'avery ortiz','marconi german'}
    targets=[m for m in strict_match_dedup(targets)
             if str(m.get('player','')).casefold() in target_names]
    if not targets:
        raise RuntimeError('Neither Avery nor Marconi has an exact drill tag on '+day)
    tag_windows=[]
    for m in targets:
        a=pd.Timestamp(m['start']);b=pd.Timestamp(m['end'])
        if a.tzinfo is None:
            a=a.tz_localize('UTC');b=b.tz_localize('UTC')
        else:
            a=a.tz_convert('UTC');b=b.tz_convert('UTC')
        tag_windows.append((m['player'],a-pd.Timedelta(seconds=3),b+pd.Timedelta(seconds=3)))
    print('\nDAYWIDE GPS RECORDING SEARCH (READ ONLY)')
    print('Date:',day,'| Up to',page_cap,'pages per GPS stream')
    print('No athlete reports, caches, aliases, or GPS assignments will change.')
    for name,a,b in tag_windows:
        print(f'  {name}: {a.tz_convert(TZ):%I:%M:%S %p} - {b.tz_convert(TZ):%I:%M:%S %p} Eastern (tag assumed UTC)')
    streams={}
    def walk(node,depth=0):
        if depth>18:return
        if isinstance(node,list):
            for elem in node:walk(elem,depth+1)
        elif isinstance(node,dict):
            if isinstance(node.get('drills'),list):
                label,_,conflict=_sessionplayer_identity(node)
                label=label if label and not conflict else 'Unverified name'
                vals=[]
                for field in ('rawDataId','session_player_rawDataId','sessionPlayerRawDataId','dataId'):
                    rid=node.get(field)
                    if rid is not None and str(rid).strip():
                        vals.append((str(rid),field))
                # `dataId` is only a fallback candidate, not guaranteed raw GPS ID.
                for rid,field in vals:
                    record=streams.setdefault(rid, {'names':set(),'fields':set(),'records':set()})
                    record['names'].add(label)
                    record['fields'].add(field)
                    record['records'].add(str(node.get('id','unknown')))
            for k,v in node.items():
                if k!='drills' and isinstance(v,(list,dict)):
                    walk(v,depth+1)
    walk(data)
    if not streams:
        print('No recording IDs exposed by date-range metadata.')
        return
    print('\nDistinct potential recording IDs:',len(streams))
    rows=[]
    checked=0
    for idx,(rid,src) in enumerate(streams.items(),1):
        # Source IDs are withheld; short irreversible fingerprints are diagnostic only.
        fingerprint=hashlib.sha256(rid.encode()).hexdigest()[:10]
        sources=', '.join(sorted(src['names']))
        suspect=('rawDataId' not in src['fields'] and
                 'session_player_rawDataId' not in src['fields'] and
                 'sessionPlayerRawDataId' not in src['fields'])
        print(f'[{idx}/{len(streams)}] {fingerprint} | {sources}' + (' [fallback ID]' if suspect else ''))
        first=last=None
        counts={name:0 for name,_,_ in tag_windows}
        page=0;cursor=None;seen=set();fully_scanned=False;error=''
        while page<page_cap:
            params={'thirdPartyApiId':API_KEY,'rawDataId':rid}
            if cursor is not None:params['nextPage']=cursor
            try:
                body=api('getSessionGpsData',params)
                gps=unwrap_gps(body)
                if not gps:
                    fully_scanned=True
                    break
                frame=normalize_gps(gps)
                page+=1
                if not frame.empty:
                    start,end=frame.timestamp.min(),frame.timestamp.max()
                    first=start if first is None else min(first,start)
                    last=end if last is None else max(last,end)
                    for name,a,b in tag_windows:
                        counts[name]+=int(((frame.timestamp>=a)&(frame.timestamp<=b)).sum())
                nxt=next_cursor(body,cursor)
                if nxt is None:
                    fully_scanned=True
                    break
                if nxt in seen:
                    error='Repeated pagination cursor'
                    break
                seen.add(nxt)
                cursor=nxt
            except Exception as exc:
                error=f'{type(exc).__name__}: {str(exc)[:100]}'
                break
        row={'fingerprint':fingerprint,'names':sources,'pages':page,
             'first':first,'last':last,'complete':fully_scanned,
             'counts':counts,'error':error}
        rows.append(row)
        checked+=1
        if first is not None:
            print(f'    GPS: {first.tz_convert(TZ):%I:%M:%S %p} - {last.tz_convert(TZ):%I:%M:%S %p} Eastern; '+
                  ', '.join(f'{name}: {n} matching samples' for name,n in counts.items())+
                  (' [scan incomplete]' if not fully_scanned else ''))
        else:print('    No valid GPS times'+(f': {error}' if error else ''))
    hits=[r for r in rows if any(count>0 for count in r['counts'].values())]
    print('\n=== DAYWIDE RESULTS ===')
    print('Recording IDs scanned:',checked,'/',len(streams))
    if hits:
        print('POTENTIAL MATCHES IN THE EARLY ASSESSMENT WINDOW:')
        for r in hits:
            print(f'  {r["fingerprint"]} linked metadata athlete(s): {r["names"]}; overlaps: '+
                  ', '.join(f'{name}={n}' for name,n in r['counts'].items() if n))
        print('These are time matches only. They DO NOT prove the GPS belongs to Avery or Marconi.')
        print('Verify device serial number, athlete wearing unit, and session assignments in Sonra.')
    else:
        print('No returned GPS recording overlaps the morning assessment window.')
        print('Inspect other Sonra sessions, exported data permissions, and morning GPS sync status.')
    incomplete=[r for r in rows if not r['complete']]
    if incomplete:print(f'Warning: {len(incomplete)} recording(s) not scanned to the end. Increase --gps-pages or resolve errors.')
    print('No existing files, cache, reports, or athlete attributions were modified.')

def all_main():
    global QUADRANT_SPLIT_METHOD
    arg=argparse.ArgumentParser(description='STATSports v19 drill-tag driven dashboard with daywide GPS recording search')
    arg.add_argument('--date', help='Assessment date, YYYY-MM-DD')
    arg.add_argument('--quadrant-split',choices=('mean','median'),default='mean',
        help='Initial quadrant split for dashboard, PDF labels and summary CSV (default: mean). Switch live in dashboard.')
    arg.add_argument('--list-only',action='store_true',help='List all tagged players; no GPS download')
    arg.add_argument('--only',help='Update only a chosen athlete without deleting other saved results')
    arg.add_argument('--fix-juan',action='store_true',
        help='Update Juan Obispo only, interpret the timestamp as UTC, and preserve/back up the existing team report')
    arg.add_argument('--report-only',action='store_true',help='Regenerate PDF/HTML from saved CSVs and open dashboard; no API requests')
    arg.add_argument('--open-dashboard',action='store_true',help='Only open previously generated HTML dashboard. No GPS/API requests or report modifications.')
    arg.add_argument('--review-set-starts',action='store_true',help='Offline report of plausible missed set-opening reps')
    arg.add_argument('--apply-set-start-recovery',action='store_true',help='Flag and fill uniquely supported set-opening reps; offline and backs up CSVs')
    arg.add_argument('--repair-existing',action='store_true',
        help='OFFLINE: restore uniquely supported missing Rep 11/21 from cached candidate peaks; refresh PDF and dashboard, no API calls')
    arg.add_argument('--diagnose-gps-times',action='store_true',
        help='READ ONLY: inspect raw GPS page timestamps vs drill tag; never modifies reports')
    arg.add_argument('--audit-linkage',action='store_true',help='READ ONLY: inspect source session-player and rawDataId metadata; never modifies reports')
    arg.add_argument('--audit-day-recordings',action='store_true',help='READ ONLY: scan all visible rawDataIds for GPS overlapping Avery/Marconi morning assessment')
    arg.add_argument('--probe-linked-gps',action='store_true',help='With --audit-linkage, inspect GPS time ranges for linked candidate rawDataIds; read-only')
    arg.add_argument('--gps-pages',type=int,default=4,
        help='Number of GPS pages to inspect with --diagnose-gps-times (default 4; max 20)')
    arg.add_argument('--gps-time-shift-hours',type=float,default=0.,
        help='ADVANCED: shift incoming GPS timestamps by specified hours to line up with drill; only after verifying diagnostic/Sonra')
    arg.add_argument('--force-refresh',action='store_true',
        help='Bypass saved athlete analysis and back up/rebuild matching GPS cache; use with --only')
    arg.add_argument('--drill-clock',choices=('auto','utc','local'),default='auto',
        help='auto: provisionally interpret timezone-less drill tags as UTC at ANY time of day; override with utc/local if Sonra confirms')
    arg.add_argument('--no-early-stop',action='store_true')
    arg.add_argument('--no-browser',action='store_true')
    arg.add_argument('--demo',action='store_true',help='Offline sample report; no STATSports requests')
    a=arg.parse_args()
    QUADRANT_SPLIT_METHOD=a.quadrant_split
    day=date.fromisoformat(a.date or input(f'Session date YYYY-MM-DD [{date.today()}]: ').strip() or date.today().isoformat()).isoformat()
    if a.audit_day_recordings:
        return audit_daywide_recordings(day,page_cap=max(1,min(200,a.gps_pages)))
    if a.diagnose_gps_times:
        return diagnose_gps_timestamps(day,a.only,clock=a.drill_clock,page_limit=max(1,min(20,a.gps_pages)))
    if a.probe_linked_gps and not a.audit_linkage:
        raise ValueError('--probe-linked-gps requires --audit-linkage')
    if a.audit_linkage:
        return audit_recording_linkage(day,a.only,probe=a.probe_linked_gps)
    if a.gps_time_shift_hours and not a.only:
        raise ValueError('GPS clock shift requires --only "Athlete Name" to protect other players')
    if a.force_refresh and not a.only:
        raise ValueError('--force-refresh requires --only "Athlete Name" to protect other players')
    if a.demo:return _run_demo(day)
    if a.open_dashboard:
        # Immediate, local-only browser launch; intentionally skip the API.
        return launch_saved_dashboard(day,required=True)
    if a.review_set_starts or a.apply_set_start_recovery or a.repair_existing:
        result = review_missing_set_openings(day,apply=(a.apply_set_start_recovery or a.repair_existing))
        if (a.apply_set_start_recovery or a.repair_existing) and not a.no_browser:
            # Even if nothing needed repairing, open the existing dashboard.
            launch_saved_dashboard(day)
        return result
    if a.report_only:
        folder,paths=group_paths(day)
        if not paths['reps'].exists() or not paths['review'].exists():
            raise RuntimeError('No saved rep/review CSVs to regenerate. Run normal report first.')
        result=generate_all_reports(day,paths)
        print('Offline report refreshed:',paths['html'],paths['pdf'])
        if not a.no_browser:
            launch_saved_dashboard(day)
        return result
    if a.fix_juan:
        if a.only and a.only.casefold() != 'juan obispo':
            raise ValueError('--fix-juan cannot be combined with a different --only athlete')
        if a.drill_clock == 'local':
            raise ValueError("Juan's known API drill timestamps are UTC. Do not combine --fix-juan with --drill-clock local")
        print('JUAN FIX: targeting the full 12:58–1:13 PM Eastern drill, '
              'without the old 1:00 PM cutoff; preserving the rest of the team.')
        return analyze_all(day,clock='utc',list_only=a.list_only,only='Juan Obispo',
                           no_early_stop=a.no_early_stop,open_browser=not a.no_browser)
    return analyze_all(day,clock=a.drill_clock,list_only=a.list_only,only=a.only,
                       no_early_stop=a.no_early_stop,open_browser=not a.no_browser,
                       gps_shift_hours=a.gps_time_shift_hours,force_refresh=a.force_refresh)

if False:
    try:all_main()
    except (RuntimeError,ValueError) as exc:
        print('ERROR:',exc)
        sys.exit(1)


# ---- All-day single-file runner added to the preserved original v19 code ----
def run_everyone_day():
    """Run tagged assessment for all; attempt evidence-based full-stream recovery for nonoverlapping tags."""
    import argparse as _argparse
    import webbrowser as _browser
    p=_argparse.ArgumentParser(description='One-file STATSports all athletes tagged 3x10 assessment')
    p.add_argument('--date',default='2026-10-20',help='YYYY-MM-DD')
    p.add_argument('--drill-clock',choices=['utc','auto','local'],default='utc')
    p.add_argument('--no-browser',action='store_true')
    p.add_argument('--no-recovery',action='store_true',help='Do not attempt full-GPS pattern recovery for nonoverlapping tags')
    p.add_argument('--force-refresh',action='store_true',help='NOT SUPPORTED for all athletes; delete or move previous output manually after review')
    p.add_argument('--list-only',action='store_true')
    a=p.parse_args()
    if a.force_refresh: p.error('Move the dated output folder to a backup location before rerunning; --force-refresh all disabled for safety.')
    global QUADRANT_SPLIT_METHOD, OUTPUT_ROOT
    QUADRANT_SPLIT_METHOD='mean'
    OUTPUT_ROOT=Path(os.environ.get('STATSPORTS_DATA_ROOT', str(Path.home()/'Downloads'/'statsports_all_drills_and_assessments_v22'))).expanduser()
    OUTPUT_ROOT.mkdir(parents=True,exist_ok=True)
    day=date.fromisoformat(a.date).isoformat()
    print('ONE-FILE v22: tagged Assessment - 3 Sets with conservative cadence recovery and per-peak timing audits on', day)
    analyze_all(day,clock=a.drill_clock,list_only=a.list_only,open_browser=False,no_early_stop=True)
    if a.list_only:return
    folder,paths=group_paths(day)
    if a.no_recovery:
        if not a.no_browser:launch_saved_dashboard(day)
        return
    review=pd.read_csv(paths['review']).fillna('')
    reps=pd.read_csv(paths['reps'])
    errors=review[review['Status'].astype(str).str.contains('No GPS samples overlap',case=False,regex=False)].copy()
    if errors.empty:
        print('No drill-to-GPS overlap failures; all reports finished.')
        if not a.no_browser:launch_saved_dashboard(day)
        return
    matches,_=find_drill_with_diagnostics(day)
    matches=strict_match_dedup(matches)
    # Group duplicated drill tags by exact verified athlete name.  A single
    # athlete may have several tags.  Only consolidate them when they point
    # to ONE identical session-player AND raw-recording pair.
    from collections import defaultdict as _defaultdict
    grouped=_defaultdict(list)
    for m in matches:
        if m.get('player'):
            grouped[str(m['player']).strip().casefold()].append(m)
    recover_dir=folder/'full_recording_recovery';recover_dir.mkdir(parents=True,exist_ok=True)
    import sqlite3 as _sqlite3
    seen_athletes=set()
    for _,rr in errors.iterrows():
        athlete=str(rr['Athlete']); namekey=athlete.strip().casefold()
        if namekey in seen_athletes:continue
        seen_athletes.add(namekey)
        options=grouped.get(namekey,[])
        links={(str(x.get('session_player_id') or ''),str(x.get('raw_id') or ''))
               for x in options if x.get('raw_id')}
        if not options or len(links)!=1 or any(not x.get('raw_id') for x in options):
            print('SKIP recovery: multiple or missing recording links for',athlete,
                  '| distinct linked pairs:',len(links))
            continue
        m=options[0]
        print('  ',athlete,':',len(options),'tag(s), one identical verified athlete+recording link')
        print('\nFULL STREAM RECOVERY:',athlete,'(tag does not match raw GPS clock)')
        ident='|'.join(str(m.get(k) or '') for k in ('session_player_id','raw_id','drill_id','start','end'))
        key=hashlib.sha256(ident.encode()).hexdigest()[:24]
        target=recover_dir/(key+'.sqlite')
        conn=_sqlite3.connect(str(target))
        conn.execute('CREATE TABLE IF NOT EXISTS bins (bin INTEGER PRIMARY KEY,n INTEGER,sum_speed REAL,max_speed REAL)')
        conn.execute('CREATE TABLE IF NOT EXISTS info (key TEXT PRIMARY KEY,val TEXT)')
        info=dict(conn.execute('SELECT key,val FROM info'))
        if info.get('raw_id') not in (None,str(m['raw_id'])):
            conn.close();raise RuntimeError('Cache recording identity mismatch: '+athlete)
        cursor=int(info['next_page']) if info.get('next_page') not in ('',None) else None
        pages=int(info.get('pages',0));samples=int(info.get('samples',0))
        completed=info.get('completed')=='yes'
        first,last=None,None
        if not completed:
            conn.execute('INSERT OR REPLACE INTO info VALUES (?,?)',('raw_id',str(m['raw_id'])))
            conn.commit()
        while not completed:
            if pages>=MAX_PAGES:
                raise RuntimeError(f'{athlete}: MAX_PAGES reached, incomplete raw stream')
            payload={'thirdPartyApiId':API_KEY,'rawDataId':m['raw_id']}
            if cursor is not None:payload['nextPage']=cursor
            body=api('getSessionGpsData',payload)
            rows=unwrap_gps(body)
            nxt=next_cursor(body,cursor)
            if rows:
                frame=normalize_gps(rows)
                if not frame.empty:
                    tns=frame.timestamp.astype('int64').to_numpy()
                    first=min(first,int(tns.min())) if first is not None else int(tns.min())
                    last=max(last,int(tns.max())) if last is not None else int(tns.max())
                    # entire stream, preserving the GPS data's own timestamps
                    agg=batch_to_bins(frame,(int(tns.min()),int(tns.max())+1))
                    with conn:
                        conn.executemany('INSERT INTO bins VALUES (?,?,?,?) ON CONFLICT(bin) DO UPDATE SET n=n+excluded.n,sum_speed=sum_speed+excluded.sum_speed,max_speed=MAX(max_speed,excluded.max_speed)',agg)
                samples+=len(rows)
            pages+=1
            completed=nxt is None or not rows
            with conn:
                conn.executemany('INSERT OR REPLACE INTO info VALUES (?,?)',[
                    ('pages',str(pages)),('samples',str(samples)),('next_page',str(nxt or '')),
                    ('completed','yes' if completed else 'no')])
            cursor=nxt
            if pages%20==0:print(f'  pages {pages} / records {samples:,}')
        data=conn.execute('SELECT bin,n,sum_speed,max_speed FROM bins ORDER BY bin').fetchall()
        conn.close()
        if not data:
            print('  No valid timestamp+speed samples',athlete);continue
        lookup={int(k):(int(n),float(s),float(mx)) for k,n,s,mx in data}
        grid=np.arange(min(lookup),max(lookup)+1,dtype=np.int64)
        trace=pd.DataFrame({'sec':grid*(BIN_MS/1000.),
                'mean':[lookup[k][1]/lookup[k][0] if k in lookup else np.nan for k in grid],
                'rawmax':[lookup[k][2] if k in lookup else np.nan for k in grid],
                'count':[lookup[k][0] if k in lookup else 0 for k in grid]})
        events,sets,blocks,opts=analyze_one(trace,athlete)
        selected,ambiguous=primary_block(blocks)
        if selected is None or selected.get('count',0)!=30 or ambiguous or selected.get('requires_review'):
            print('  No uniquely supported complete 3x10 pattern; no automatic recovered results')
            continue
        df=result_rows(events,selected,athlete,'Assessment 1',trace,day)
        df['Match key']=key
        # Replace all failed tag rows for this athlete with one PROVISIONAL
        # recovery record, to prevent the dashboard counting duplicated tags.
        failed=(review['Athlete'].astype(str).str.casefold()==namekey) & (
            review['Status'].astype(str).str.contains('No GPS samples overlap',case=False,regex=False))
        failedkeys=set(review.loc[failed,'Match key'].astype(str))
        if 'Match key' in reps:
            reps=reps[~reps['Match key'].astype(str).isin(failedkeys | {key})]
        reps=pd.concat([reps,df],ignore_index=True)
        msg=('PROVISIONAL: full-recording 3x10 pattern; '+str(len(options))+
             ' original tag(s) did not overlap GPS. Verify identity and time in Sonra.')
        newrow=rr.to_dict()
        newrow.update({'Match key':key,'Athlete':athlete,'Status':msg,
                       'Matched reps':30,'Candidate peaks':len(events),
                       'Blocks detected':len(blocks)})
        review=review.loc[~failed].copy()
        review=pd.concat([review,pd.DataFrame([newrow])],ignore_index=True)
        pd.DataFrame(events).to_csv(recover_dir/f'candidates_{key}.csv',index=False)
        print('  Recovered 30/30 with original v19 functions; PROVISIONAL',athlete)
    backup=folder/('before_recovery_'+datetime.now().strftime('%Y%m%d_%H%M%S'))
    backup.mkdir(exist_ok=True)
    for k in ('reps','review','summary','pdf','html'):
        if paths[k].exists():shutil.copy2(paths[k],backup/paths[k].name)
    reps.to_csv(paths['reps'],index=False)
    review.to_csv(paths['review'],index=False)
    generate_all_reports(day,paths)
    print('\nFINAL HTML:',paths['html'])
    print('FINAL PDF: ',paths['pdf'])
    print('SUMMARY:   ',paths['summary'])
    print('REPS:      ',paths['raw_reps'])
    print('QUALITY:   ',paths['review'])
    if not a.no_browser:launch_saved_dashboard(day)

if __name__=='__main__':
    run_everyone_day()

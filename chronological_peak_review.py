"""Pure, testable operations for inserting an actual raw GPS peak chronologically.

Never invent speed, change a source recording, silently merge athletes or
assign the same source peak twice. The caller must verify athlete/cache identity.
"""
import pandas as pd
import numpy as np

MPH=2.2369362921
FIELDS=('Peak (m/s)','Peak (mph)','Raw max (mph)','Peak time local',
        'Detection','QC note','Timing error (s)','Samples')

class PeakInsertionError(ValueError):
    pass

def chronological_insert(rep_rows, set_no, selected_sec, selected_raw_ms,
                         selected_smoothed_ms, reason='Chronological insertion of visually verified raw GPS peak'):
    """Return audit operations and preview after inserting a measured GPS peak.

    Input rep numbers need not be chronological (e.g. existing 2..10 when 1 is
    missing). All existing measured peaks are sorted with the new one, and
    assigned sequentially to slots for the chosen set. Never adds an 11th rep.
    """
    if set_no not in (1,2,3):raise PeakInsertionError('Unknown set')
    if not all(np.isfinite(float(x)) for x in (selected_sec,selected_raw_ms,selected_smoothed_ms)):
        raise PeakInsertionError('Selected GPS speed or timestamp is not numeric')
    if selected_raw_ms<=0:raise PeakInsertionError('GPS speed must be positive')
    start=(set_no-1)*10+1
    rows=rep_rows[rep_rows['Rep'].astype(int).between(start,start+9)].copy().sort_values('Rep')
    if len(rows)!=10 or rows['Rep'].duplicated().any():
        raise PeakInsertionError('Expected exactly ten unique rep slots for this set')
    existing=[]
    for _,r in rows.iterrows():
        raw=pd.to_numeric(r.get('Raw max (mph)'),errors='coerce')
        if pd.isna(raw) or raw<=0:continue
        ts=pd.to_datetime(r.get('Peak time local'),errors='coerce',utc=True)
        if pd.isna(ts):raise PeakInsertionError('Existing assigned rep has no valid timestamp; manual audit needed')
        second=float(ts.timestamp())
        if abs(second-selected_sec)<.31:
            raise PeakInsertionError('That GPS peak is already assigned to another repetition. Choose an unassigned green point.')
        existing.append(dict(sec=second, values={k:r.get(k) for k in FIELDS},
                             old_rep=int(r['Rep']),inserted=False))
    if len(existing)>=10:
        raise PeakInsertionError('This set already has 10 assigned peaks. Reject an incorrect peak before inserting another.')
    # Verify that selected peak does not overlap one of the other sets.
    other=rep_rows[~rep_rows['Rep'].astype(int).between(start,start+9)]
    for _,r in other.iterrows():
        raw=pd.to_numeric(r.get('Raw max (mph)'),errors='coerce')
        if pd.isna(raw) or raw<=0:continue
        ts=pd.to_datetime(r.get('Peak time local'),errors='coerce',utc=True)
        if pd.notna(ts) and abs(float(ts.timestamp())-selected_sec)<.31:
            raise PeakInsertionError('Selected GPS peak is assigned in a different set. Review set boundaries first.')
    new_values={'Peak (m/s)':float(selected_smoothed_ms),
        'Peak (mph)':float(selected_smoothed_ms)*MPH,
        'Raw max (mph)':float(selected_raw_ms)*MPH,
        'Peak time local':str(pd.Timestamp(selected_sec,unit='s',tz='UTC').tz_convert('America/New_York')),
        'Detection':'MANUALLY_ADDED',
        'QC note':reason+' | Actual GPS measurement, provisional until quality review',
        'Timing error (s)':None,'Samples':1}
    existing.append(dict(sec=float(selected_sec),values=new_values,old_rep=None,inserted=True))
    ordered=sorted(existing,key=lambda x:x['sec'])
    # A duplicate at a different timestamp near the same effort is not
    # automatically identified as a new sprint. Flag peaks too close in time.
    for a,b in zip(ordered,ordered[1:]):
        if b['sec']-a['sec']<5.0:
            raise PeakInsertionError('Two selected peaks are less than 5 seconds apart. Check that they represent separate sprint efforts.')
    ops=[];preview_rows=[];inserted_rep=None
    before=rows.set_index('Rep')
    for offset in range(10):
        dest=start+offset
        old=before.loc[dest].to_dict()
        if offset<len(ordered):
            item=ordered[offset]
            updated=dict(item['values'])
            if item['inserted']:
                inserted_rep=offset+1
                event='MANUALLY_ADDED'
            else:
                event='MANUALLY_RENUMBERED'
                if item['old_rep']!=dest:
                    updated['Detection']='MANUALLY_RENUMBERED'
                    updated['QC note']=str(updated.get('QC note') or '')+' | Chronological renumbering from '+str(item['old_rep'])
            preview_rows.append({'Rep':offset+1,'Recorded time (local)':updated.get('Peak time local'),
                                 'Raw speed (mph)':round(float(updated.get('Raw max (mph)')),2),
                                 'Source':'NEW CLICK' if item['inserted'] else f'Existing rep {(item["old_rep"]-1)%10+1}'})
        else:
            event='MANUALLY_UNASSIGNED'
            updated={'Peak (m/s)':None,'Peak (mph)':None,'Raw max (mph)':None,
                'Peak time local':'','Detection':'MISSING',
                'QC note':'Unassigned after chronological reordering',
                'Timing error (s)':None,'Samples':0}
            preview_rows.append({'Rep':offset+1,'Recorded time (local)':'—',
                                 'Raw speed (mph)':None,'Source':'Missing'})
        ops.append((event,dest,old,updated,reason))
    return ops,{'inserted_rep':inserted_rep,'rows':preview_rows,'existing':len(ordered)-1,'total':len(ordered)}

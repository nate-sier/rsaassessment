#!/usr/bin/env python3
"""STATSports 3x10 manual GPS peak review. Read-only source, versioned outputs."""
import hashlib
import os
import importlib.util
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from chronological_peak_review import chronological_insert, PeakInsertionError
from export_name_override import normalize_export_name, validate_export_name, apply_export_names, default_trial_names, trial_export_names, partition_saved_trial_docs
from workspace_portability import default_locations

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / 'statsports_backend_v22.py'
MPH = 2.2369362921
BIN_S = .2

st.title('STATSports | Three-set sprint peak review — Cloud v37')
st.caption('3 sets × 10 reps · 17-second starts · Manual changes require review and remain separate from raw data.')

def backend():
    spec = importlib.util.spec_from_file_location('statsports_backend_v22', BACKEND)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

mod = backend()
base_root, default_review_root = default_locations(ROOT)
with st.sidebar:
    st.header('Session')
    day = st.date_input('Assessment date', value=datetime(2026,10,29).date()).isoformat()
    root = Path(st.text_input('v22 source folder', str(base_root),
        disabled=(os.environ.get('STATSPORTS_CLOUD_MODE') == '1'))).expanduser()
    output_root = Path(st.text_input('Reviewed results destination', str(default_review_root),
        disabled=(os.environ.get('STATSPORTS_CLOUD_MODE') == '1'))).expanduser()
    st.caption('Pull the selected date with the bundled API backend. The pull is not automatic on app load.')
    if st.button('Pull / refresh selected date', use_container_width=True):
        cmd=[sys.executable,str(BACKEND),'--date',day,'--no-browser']
        with st.spinner('Running the original v22 STATSports API pull and assessment…'):
            proc=subprocess.run(cmd,cwd=str(ROOT),capture_output=True,text=True,timeout=7200,
                env={**os.environ,'STATSPORTS_DATA_ROOT':str(root)})
        with st.expander('API pull log',expanded=proc.returncode!=0):
            st.code((proc.stdout+'\n'+proc.stderr)[-22000:])
        if proc.returncode:st.error(f'Pull exited with code {proc.returncode}. Check the log.')
        else:st.success('API assessment finished. Select a recording below.')

folder=root/f'statsports_all_assessments_{day}'
reps_path=folder/f'assessment_{day}_all_30_reps.csv'
review_path=folder/f'assessment_{day}_quality_review.csv'
index_path=folder/'manual_review_identity_index.json'
if not reps_path.exists() or not review_path.exists():
    st.info(f'No assessment found for {day} at {folder}. Use **Pull / refresh selected date**, or change the source folder.')
    st.stop()
if not index_path.exists():
    st.warning('Identity index is missing. This folder came from an older script. Run the bundled pull once for this date; the API will link each trace to the proper athlete.')
    st.stop()

source_reps=pd.read_csv(reps_path,low_memory=False)
source_review=pd.read_csv(review_path).fillna('')
index=json.loads(index_path.read_text())
if not index:st.error('No identified tagged assessments.');st.stop()
trial_defaults=default_trial_names(index)

# Build a date-wide review queue from original repetition assignments plus saved edits.
# The review criterion is 10 actual assigned raw GPS peaks PER SET, not simply
# the existence of 30 candidate peaks in the detector's debug output.
queue_review_dir=output_root/f'statsports_all_assessments_{day}'/'corrections'
def count_assigned_peaks(entry):
    key0=str(entry['match_key'])
    if 'Match key' not in source_reps.columns:
        base=pd.DataFrame()
    else:
        base=source_reps[source_reps['Match key'].astype(str)==key0].copy()
    assigned={i:False for i in range(1,31)}
    rep_issues=[]
    if not base.empty:
        ids=pd.to_numeric(base['Rep'],errors='coerce')
        invalid=base[ids.isna()|~ids.between(1,30)]
        if len(invalid):rep_issues.append(f'{len(invalid)} invalid rep IDs')
        valid=base[ids.notna()&ids.between(1,30)].copy()
        valid['Rep']=pd.to_numeric(valid['Rep']).astype(int)
        duplicates=valid[valid['Rep'].duplicated(keep=False)]
        if len(duplicates):rep_issues.append('Duplicate numbered reps in baseline')
        for _,rr in valid.iterrows():
            r=int(rr['Rep'])
            peak=pd.to_numeric(rr.get('Raw max (mph)'),errors='coerce')
            assigned[r]=assigned[r] or bool(pd.notna(peak) and np.isfinite(peak) and peak>0)
    edits_path=queue_review_dir/f'{key0}.json'
    if edits_path.exists():
        try:
            doc=json.loads(edits_path.read_text())
            if str(doc.get('match_key'))!=key0:
                rep_issues.append('Correction key mismatch')
            elif str(doc.get('rawDataId'))!=str(entry.get('raw_id')) or str(doc.get('session_player_id'))!=str(entry.get('session_player_id')):
                rep_issues.append('Correction identity mismatch')
            else:
                for rep,update in doc.get('overrides',{}).items():
                    rr=int(rep)
                    if not 1<=rr<=30:rep_issues.append('Invalid override rep number');continue
                    if not update:continue  # restoring automatic value
                    val=pd.to_numeric(update.get('Raw max (mph)'),errors='coerce')
                    assigned[rr]=bool(pd.notna(val) and np.isfinite(val) and val>0)
        except (ValueError,TypeError,OSError,KeyError) as ex:
            rep_issues.append('Cannot read saved edits: '+str(ex)[:100])
    counts=[sum(assigned[n] for n in range((set_i-1)*10+1,set_i*10+1)) for set_i in (1,2,3)]
    missing={i:[n for n in range((i-1)*10+1,i*10+1) if not assigned[n]] for i in (1,2,3)}
    return counts,missing,rep_issues

choices={f"{trial_defaults[str(x['match_key'])]} · {str(x['start'])[:19]} · {x['match_key'][:8]}":x for x in index}
queue={name:count_assigned_peaks(entry0) for name,entry0 in choices.items()}
flagged=[name for name,(counts,missing,issues) in queue.items() if min(counts)<10 or issues]
complete=[name for name in choices if name not in flagged]
st.markdown('### Incomplete set review queue')
col_a,col_b,col_c=st.columns(3)
col_a.metric('Assessments needing review',len(flagged))
col_b.metric('Assessments with 3 complete sets',len(complete))
col_c.metric('Incomplete sets',sum(c<10 for counts,_,_ in queue.values() for c in counts))
if flagged:
    flagged_table=[]
    for name in flagged:
        counts,miss,issues=queue[name]
        flagged_table.append({'Athlete / tagged assessment':name,
            'Set 1':f'{counts[0]}/10','Set 2':f'{counts[1]}/10','Set 3':f'{counts[2]}/10',
            'Missing rep numbers':', '.join(str(v) for subset in miss.values() for v in subset) or '—',
            'Additional flags':'; '.join(issues)})
    st.dataframe(pd.DataFrame(flagged_table),use_container_width=True,hide_index=True)
else:
    st.success('Every assessment has 10 assigned GPS peaks in each set. This does not replace athlete-linkage and sprint-quality review.')
show_only=st.checkbox('Show only assessments needing review',value=bool(flagged))
visible=[n for n in choices if (n in flagged or not show_only)]
if not visible:
    st.info('There are no assessments matching the filter.');st.stop()
choice=st.selectbox('Athlete / specific tagged assessment',visible,format_func=lambda n:('⚠ ' if n in flagged else '✓ ')+n)
entry=choices[choice]; key=entry['match_key']; athlete=entry['player']
if not entry.get('raw_id') or not entry.get('session_player_id'):
    st.error('Missing STATSports recording or session-player link. Manual peak review disabled.');st.stop()

# Convert exact API identity to the v22 speed-cache key. No time-only linking.
try:
    # Must use the identical padded [start_ns, end_ns] window used by
    # fetch_drill_trace -> open_db; otherwise valid caches fail verification.
    # The padding is 3 seconds on each side in the bundled v22 backend.
    window_start_ns,window_end_ns=mod.drill_window(entry,[],'utc')
except Exception as ex:
    st.error('Invalid drill timestamps: '+str(ex));st.stop()
identity='|'.join(str(entry.get(k) or '') for k in ('raw_id','session_player_id','drill_id','start','end'))
cache_key=hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]
cache_file=folder/'cache'/f'{cache_key}.sqlite'
# The main pull uses UTC and no shift by default; do not guess alternate offsets.
if not cache_file.exists():
    st.error(f'No exact recording cache for this drill: {cache_file.name}. Run the bundled pull or check recording linkage.');st.stop()

@st.cache_data(show_spinner=False)
def read_trace(path,mtime):
    con=sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro',uri=True)
    try:
        md=dict(con.execute('SELECT key,value FROM metadata'))
        arr=con.execute('SELECT bin,n,sum_speed,max_speed FROM speed_bins ORDER BY bin').fetchall()
    finally:con.close()
    if not arr:return pd.DataFrame(),md
    i=np.array([r[0] for r in arr],dtype=np.int64)
    n=np.array([r[1] for r in arr],dtype=int)
    s=np.array([r[2] for r in arr],dtype=float)
    mx=np.array([r[3] for r in arr],dtype=float)
    return pd.DataFrame({'sec':i*.2,'mean':s/n,'rawmax':mx,'count':n}),md

trace,md=read_trace(str(cache_file),cache_file.stat().st_mtime_ns)
expected={'rawid':str(entry['raw_id']),'spid':str(entry['session_player_id']),'start_ns':int(window_start_ns),'end_ns':int(window_end_ns),'bin_ms':mod.BIN_MS}
try:
    stored=json.loads(md['identity'])
except (KeyError,ValueError):stored={}
mismatch={k:(stored.get(k),v) for k,v in expected.items() if stored.get(k)!=v}
if mismatch:
    st.error('Cache identity does not match this specific tagged athlete/recording. Editing blocked.')
    st.caption('Mismatch fields: '+', '.join(mismatch)+'. The app will not modify a cache or bypass the recording identity check.')
    st.stop()
if trace.empty:st.error('GPS speed cache contains no samples inside this tag. No correction is possible from an empty trace.');st.stop()
if md.get('done')!='yes':st.warning('GPS cache is not complete; review results are provisional.')

# Corrections are stored outside baseline folder, append-only history and effective overrides.
review_dir=output_root/f'statsports_all_assessments_{day}'
overrides_dir=review_dir/'corrections';overrides_dir.mkdir(parents=True,exist_ok=True)
corrections_path=overrides_dir/f'{key}.json'

def read_corrections():
    if corrections_path.exists():return json.loads(corrections_path.read_text())
    return {'athlete':athlete,'match_key':key,'rawDataId':str(entry['raw_id']),
            'session_player_id':str(entry['session_player_id']),'history':[],'overrides':{}}

def save_changes_atomically(operations):
    """Apply a group of edits as one transaction; preserve before/after in audit trail."""
    doc=read_corrections()
    if (str(doc.get('match_key'))!=str(key) or
        str(doc.get('rawDataId'))!=str(entry['raw_id']) or
        str(doc.get('session_player_id'))!=str(entry['session_player_id'])):
        raise RuntimeError('Correction identity does not match this exact recording')
    for op,rep_no,old,updated,reason in operations:
        doc['history'].append({'timestamp_utc':datetime.now(timezone.utc).isoformat(),
            'operation':op,'rep':int(rep_no),'before':old,'after':updated,'reason':reason})
        doc['overrides'][str(rep_no)]=updated
    tmp=corrections_path.with_suffix('.tmp')
    tmp.write_text(json.dumps(doc,indent=2,default=str),encoding='utf-8')
    tmp.replace(corrections_path)
    st.cache_data.clear()

def save_change(op,rep_no,old,updated,reason):
    return save_changes_atomically([(op,rep_no,old,updated,reason)])

# The old save function is now replaced by the transaction writer.
def _legacy_save_change_unused(op,rep_no,old,updated,reason):
    doc=read_corrections()
    doc['history'].append({'timestamp_utc':datetime.now(timezone.utc).isoformat(),
                           'operation':op,'rep':int(rep_no),'before':old,'after':updated,'reason':reason})
    doc['overrides'][str(rep_no)]=updated
    tmp=corrections_path.with_suffix('.tmp')
    tmp.write_text(json.dumps(doc,indent=2,default=str),encoding='utf-8')
    tmp.replace(corrections_path)
    st.cache_data.clear()

# A display-name override affects exports ONLY. It is bound to the exact tagged
# assessment and never changes the source identity, rawDataId, or cache key.
st.markdown('#### Export player name')
st.caption('Change the name shown in the regenerated CSV, HTML dashboard and PDF. '
           'The original STATSports athlete and recording identity stay unchanged.')
name_doc=read_corrections()
current_export_name=normalize_export_name(name_doc.get('export_player_name')) or trial_defaults[str(key)]
new_export_name=st.text_input('Name to show in exported assessment',
    value=current_export_name,key=f'export_name_{day}_{key}',max_chars=100)
st.caption(f'Source API athlete: {athlete} · Tagged trial: {str(entry.get("start"))[:19]} · Current export name: {current_export_name}. This label changes only this tagged trial.')
name_c1,name_c2=st.columns([1,1])
with name_c1:
    if st.button('Save export player name',use_container_width=True,key=f'save_export_name_{day}_{key}'):
        try:
            proposed=validate_export_name(new_export_name)
            # Recompute labels for ALL trials. Other trials retain unique defaults
            # or their own saved overrides; only this exact match_key is updated.
            other_docs=[]
            for other in index:
                other_key=str(other['match_key'])
                if other_key==str(key):continue
                other_path=overrides_dir/f'{other_key}.json'
                if other_path.exists():other_docs.append(json.loads(other_path.read_text()))
            other_docs.append({'match_key':str(key), 'rawDataId':str(entry['raw_id']),
                'session_player_id':str(entry['session_player_id']), 'export_player_name':proposed})
            trial_export_names(index,other_docs)
            doc=read_corrections()
            if str(doc.get('match_key'))!=str(key) or str(doc.get('rawDataId'))!=str(entry['raw_id']) or str(doc.get('session_player_id'))!=str(entry['session_player_id']):
                raise RuntimeError('Source-recording identity mismatch. Rename blocked.')
            old_name=normalize_export_name(doc.get('export_player_name')) or trial_defaults[str(key)]
            # Empty override means use the source athlete name, not a blank exported name.
            doc['export_player_name']=None if proposed==trial_defaults[str(key)] else proposed
            if proposed!=old_name:
                doc['history'].append({'timestamp_utc':datetime.now(timezone.utc).isoformat(),
                    'operation':'EXPORT_TRIAL_NAME_CHANGED','rep':None,
                    'before':old_name,'after':proposed,
                    'reason':'User-selected display name for exported assessment; API identity unchanged'})
            tmp=corrections_path.with_suffix('.tmp')
            tmp.write_text(json.dumps(doc,indent=2,default=str),encoding='utf-8')
            tmp.replace(corrections_path)
            st.success(f'Saved export name for this tagged trial only: {proposed}. Regenerate reviewed reports to apply it.')
        except (ValueError,RuntimeError,OSError) as ex:
            st.error(str(ex))
with name_c2:
    if st.button('Reset trial export label',use_container_width=True,key=f'reset_export_name_{day}_{key}'):
        doc=read_corrections()
        if str(doc.get('match_key'))!=str(key) or str(doc.get('rawDataId'))!=str(entry['raw_id']) or str(doc.get('session_player_id'))!=str(entry['session_player_id']):
            st.error('Source-recording identity mismatch. Reset blocked.')
        else:
            old_name=normalize_export_name(doc.get('export_player_name')) or trial_defaults[str(key)]
            doc['export_player_name']=None
            if old_name!=trial_defaults[str(key)]:
                doc['history'].append({'timestamp_utc':datetime.now(timezone.utc).isoformat(),
                    'operation':'EXPORT_TRIAL_NAME_RESET','rep':None,
                    'before':old_name,'after':trial_defaults[str(key)],'reason':'Restored default label for this tagged trial'})
            tmp=corrections_path.with_suffix('.tmp')
            tmp.write_text(json.dumps(doc,indent=2,default=str),encoding='utf-8')
            tmp.replace(corrections_path)
            st.rerun()

rep_rows=source_reps[source_reps['Match key'].astype(str)==str(key)].copy() if 'Match key' in source_reps else pd.DataFrame()
# Always represent all 30 slots. A missing baseline row is not a fatal error;
# it is precisely what this manual review is intended to expose.
if rep_rows.empty:
    rep_rows=pd.DataFrame()
else:
    rep_rows=rep_rows.sort_values('Rep')
    ids=pd.to_numeric(rep_rows['Rep'],errors='coerce')
    if ids.isna().any() or not ids.between(1,30).all() or ids.duplicated().any():
        st.error('Baseline has invalid or duplicate rep numbers. Editing blocked until identity/rep numbering is resolved.');st.stop()
    rep_rows['Rep']=ids.astype(int)
missing=set(range(1,31))-set(rep_rows['Rep'].astype(int)) if not rep_rows.empty else set(range(1,31))
if missing:
    slots=pd.DataFrame([{'Athlete':athlete,'Recording':athlete,'Block':'Assessment 1',
        'Set':(i-1)//10+1,'Set Rep':(i-1)%10+1,'Rep':i,
        'Peak (m/s)':np.nan,'Peak (mph)':np.nan,'Raw max (mph)':np.nan,
        'Peak time local':'','Detection':'Missing','QC note':'No automatic rep assigned',
        'Timing error (s)':np.nan,'Samples':0,'Match key':key} for i in sorted(missing)])
    rep_rows=pd.concat([rep_rows,slots],ignore_index=True)
rep_rows=rep_rows.sort_values('Rep').reset_index(drop=True)
cor=read_corrections()
def effective(df, edits):
    df=df.copy()
    for k,v in edits.items():
        mask=df['Rep'].astype(int)==int(k)
        if mask.sum()!=1:raise RuntimeError('Ambiguous repetition key: '+k)
        for col,val in v.items():df.loc[mask,col]=val
    return df

rep_rows=effective(rep_rows,cor['overrides'])
# Highlight and navigate incomplete sets directly.
peak_values=pd.to_numeric(rep_rows['Raw max (mph)'],errors='coerce')
valid_mask=peak_values.notna() & np.isfinite(peak_values) & (peak_values>0)
set_counts={i:int(valid_mask[rep_rows['Set'].astype(int)==i].sum()) for i in (1,2,3)}
missing_by_set={i:[int(v) for v in rep_rows.loc[(rep_rows['Set'].astype(int)==i)&(~valid_mask),'Rep']] for i in (1,2,3)}
st.markdown('#### Set completion')
set_cols=st.columns(3)
for set_i,col in enumerate(set_cols,1):
    with col:
        n=set_counts[set_i]
        if n<10:
            st.error(f'Set {set_i}: {n}/10 — REVIEW REQUIRED')
            st.caption('Missing: '+', '.join(f'Rep {(r-1)%10+1}' for r in missing_by_set[set_i]))
        else:
            st.success(f'Set {set_i}: 10/10 assigned')
st.markdown('#### Optional: manually shift assignments')
st.caption('Usually unnecessary: click any unlabeled peak below and chronological re-numbering happens automatically. This manual shift is retained only for deliberate corrections.')
shift_set=st.selectbox('Set for one-position shift', [1,2,3],
    format_func=lambda i: f'Set {i}: {set_counts[i]}/10'+(' — missing first rep' if (i-1)*10+1 in missing_by_set[i] else ''),
    key=f'shift_set_{day}_{key}')
start_rep=(shift_set-1)*10+1
shift_rows=rep_rows.set_index('Rep')
shift_ok=(start_rep in missing_by_set[shift_set] and
    all(pd.notna(pd.to_numeric(shift_rows.loc[n,'Raw max (mph)'],errors='coerce')) for n in range(start_rep+1,start_rep+10)))
if st.button(f'Shift Set {shift_set} assignments one position earlier (2→1 … 10→9)',
             disabled=not shift_ok, use_container_width=True, key=f'shift_{day}_{key}_{shift_set}'):
    before={n:shift_rows.loc[n].to_dict() for n in range(start_rep,start_rep+10)}
    cols_to_copy=['Peak (m/s)','Peak (mph)','Raw max (mph)','Peak time local',
                  'Detection','QC note','Timing error (s)','Samples']
    edits=[]
    for n in range(start_rep,start_rep+9):
        vals={c:before[n+1].get(c) for c in cols_to_copy}
        vals['Detection']='MANUALLY_SHIFTED'
        vals['QC note']=str(vals.get('QC note') or '')+' | Shifted from rep '+str(n+1)+' after visual review'
        edits.append(('MANUALLY_SHIFTED',n,before[n],vals,'Offset correction: all peaks shifted one rep earlier'))
    cleared={'Peak (m/s)':None,'Peak (mph)':None,'Raw max (mph)':None,
             'Peak time local':'','Detection':'MANUALLY_UNASSIGNED',
             'QC note':'Awaiting final unlabeled sprint peak after sequence shift','Timing error (s)':None}
    edits.append(('MANUALLY_UNASSIGNED',start_rep+9,before[start_rep+9],cleared,
                  'Released last rep slot after shifting 2–10 to 1–9'))
    save_changes_atomically(edits)
    st.rerun()
if not shift_ok:
    st.caption('Shift is enabled only when the first position is missing and positions 2–10 all have assigned peaks. Other patterns can still be corrected individually.')

initial_set=next((i for i in (1,2,3) if set_counts[i]<10),1)
set_no=st.selectbox('Set to inspect',[1,2,3],index=initial_set-1,
    format_func=lambda i:f'Set {i} — {set_counts[i]}/10'+(' — REVIEW' if set_counts[i]<10 else ''))
options=list(range((set_no-1)*10+1,set_no*10+1))
first_missing=next((i for i in options if i in missing_by_set[set_no]),options[0])
number=st.selectbox('Repetition to review',options,index=options.index(first_missing),
    format_func=lambda r:f'Rep {(r-1)%10+1} (#{r})'+(' — MISSING' if r in missing_by_set[set_no] else ' — assigned'))
current=rep_rows[rep_rows['Rep'].astype(int)==number].iloc[0]
valid_t=pd.to_datetime(rep_rows['Peak time local'],errors='coerce',utc=True)
# The old fixed +/-28 s window could hide a late/unassigned sprint, especially
# when set numbering was shifted. Expand the window before selecting a peak.
subset=rep_rows[rep_rows['Set'].astype(int)==set_no]
peaks=pd.to_datetime(subset['Peak time local'],errors='coerce',utc=True).dropna()
trace_min=float(trace['sec'].min())
trace_max=float(trace['sec'].max())
st.markdown('#### Speed graph time range')
view_mode=st.radio(
    'Time range to display',
    ['Extended set window', 'Entire available GPS recording'],
    horizontal=True,
    help='The extended window includes time before the first and after the last assigned sprint. '
         'Entire recording displays all GPS samples in this drill cache.',
    key=f'trace_view_mode_{day}_{key}_{set_no}',
)
if view_mode=='Extended set window':
    st.caption('Use separate padding for the beginning and the END of the set. A late missing repetition may be well beyond the last assigned peak.')
    col_before,col_after=st.columns(2)
    with col_before:
        before_padding=st.slider(
            'Seconds BEFORE first assigned peak',
            min_value=0,max_value=1200,value=90,step=15,
            key=f'trace_padding_before_{day}_{key}_{set_no}',
        )
    with col_after:
        after_padding=st.slider(
            'Seconds AFTER last assigned peak',
            min_value=0,max_value=1200,value=360,step=15,
            key=f'trace_padding_after_{day}_{key}_{set_no}',
        )
    if len(peaks)>=1:
        desired_left=float(peaks.min().timestamp()-before_padding)
        desired_right=float(peaks.max().timestamp()+after_padding)
    else:
        desired_left,desired_right=trace_min,trace_max
    left=max(desired_left,trace_min)
    right=min(desired_right,trace_max)
    if desired_right>trace_max+0.2:
        st.warning(f'Requested {after_padding} s after the last assigned peak, but the GPS cache ends '
                   f'{max(0,trace_max-float(peaks.max().timestamp())):.1f} s after it. '
                   'No later GPS samples are currently available in this cached drill. '
                   'Increasing the slider cannot load samples beyond the API pull.')
    if desired_left<trace_min-0.2:
        st.caption('The requested earlier range extends before the cached GPS samples.')
else:
    left,right=trace_min,trace_max
plot=trace[(trace.sec>=left)&(trace.sec<=right)].copy()
if view_mode=='Extended set window' and len(peaks)>=1:
    if right>=trace_max-0.3 or left<=trace_min+0.3:
        st.caption('The displayed range reaches the beginning/end of the available GPS cache. '
                   'The chart cannot show samples that were not downloaded by the API pull.')
st.caption(f'Available GPS trace: {trace_max-trace_min:.1f} seconds; '
           f'currently displaying {max(0,right-left):.1f} seconds. '
           'Clicking a point still assigns it to the selected set in chronological order.')
if plot.empty:st.error('Selected set has no GPS samples in the expected window.');st.stop()
origin=float(trace.sec.min())
plot['relative_s']=plot.sec-origin
# streamlit-plotly-events bundles a legacy Plotly frontend. Plotly 6 encodes
# pandas/NumPy numeric arrays as typed-array {bdata,dtype} objects, which that
# frontend may fail to render (rep diamonds, created from Python lists, survive).
# Convert ALL GPS trace arrays to standard Python float lists before drawing.
plot_x=[float(v) for v in plot['relative_s'].to_numpy()]
plot_mean=[float(v)*MPH if np.isfinite(v) else None for v in pd.to_numeric(plot['mean'],errors='coerce').to_numpy()]
plot_raw=[float(v)*MPH if np.isfinite(v) else None for v in pd.to_numeric(plot['rawmax'],errors='coerce').to_numpy()]
plot_sec=[float(v) for v in plot['sec'].to_numpy()]
fig=go.Figure()
fig.add_trace(go.Scatter(
    x=plot_x,
    y=plot_mean,
    mode='lines',
    name='0.2 s mean',
    line=dict(color='#475569',width=2.5),
    hovertemplate='Time from recording start: %{x:.1f} s<br>0.2 s mean %{y:.2f} mph<extra></extra>'
))
fig.add_trace(go.Scatter(
    x=plot_x,
    y=plot_raw,
    mode='lines+markers',
    name='Raw 0.2 s maximum',
    line=dict(color='#0f766e',width=1.2),
    marker=dict(size=6,color='#0f766e',opacity=.55),
    customdata=plot_sec,
    hovertemplate='Time from recording start: %{x:.1f} s<br>Raw max %{y:.2f} mph<extra></extra>'
))
for _,r in subset.iterrows():
    try:
        timestamp=pd.Timestamp(r['Peak time local'])
        pt=float(timestamp.timestamp())
    except (ValueError,TypeError):continue
    if not np.isfinite(pt) or not (left<=pt<=right):continue
    speed=pd.to_numeric(r['Raw max (mph)'],errors='coerce')
    if pd.isna(speed):continue
    selected=int(r['Rep'])==number
    fig.add_trace(go.Scatter(x=[pt-origin],y=[float(speed)],mode='markers+text',
        text=[str(int(r['Set Rep']))],textposition='top center',name=f"Rep {int(r['Rep'])}",
        marker=dict(size=13 if selected else 10,color='#f97316' if selected else '#2563eb',symbol='diamond')))
ymax=max((v for v in plot_raw if v is not None),default=15.0)
fig.update_layout(height=500,margin=dict(l=30,r=20,t=20,b=50),
    xaxis_title='Seconds from beginning of linked recording',
    yaxis_title='Speed (mph)',legend=dict(orientation='h',y=-.25),
    clickmode='event+select',hovermode='closest')
fig.update_xaxes(showgrid=True,gridcolor='#e5e7eb',zeroline=False)
fig.update_yaxes(showgrid=True,gridcolor='#e5e7eb',zeroline=False,range=[0,max(14.5,ymax*1.08)])
saved_key=f'last_saved_{day}_{key}'
if saved_key in st.session_state:
    saved_set,saved_rep,saved_mph,saved_relative=st.session_state.pop(saved_key)
    st.success(f'Saved raw GPS peak as Set {saved_set}, Rep {saved_rep} ({saved_mph:.2f} mph at {saved_relative:.1f} s). All assigned reps were renumbered by timestamp.')
st.markdown('#### Click a raw GPS peak — repetition numbers are assigned automatically')
st.info(f'Set {set_no}: {set_counts[set_no]}/10 assigned. Click the GREEN GPS dot at the top of an unassigned sprint. The app sorts all recorded peaks chronologically and assigns reps 1–10.')
st.caption('Gray = 0.2 s mean speed; green = raw 0.2 s maxima; blue diamonds = currently assigned reps. Click a green GPS point: it saves immediately and renumbers the set chronologically. Every change is audited.')
# Use Streamlit's native Plotly selection API instead of the legacy
# streamlit-plotly-events component. The old component can render Plotly 6
# traces but fail to deliver a point-click event to Python.
fig.update_traces(hoverinfo='skip',selector=dict(name='0.2 s mean'))
if len(plot_x)<2 or not any(v is not None for v in plot_mean) or not any(v is not None for v in plot_raw):
    st.error('The speed trace has no numeric samples for this set.');st.stop()
# Selectable raw max trace is trace index 1. Rep diamonds retain their own
# individual traces, but they must not be interpreted as newly clicked GPS.
chart_revision=len(cor.get('history',[]))
chart_key=f'peak_select_{day}_{key}_{set_no}_{chart_revision}'
selection=st.plotly_chart(fig,use_container_width=True,on_select='rerun',
    selection_mode='points',key=chart_key)
selected_points=(selection.selection.points if selection is not None else [])
raw_choices=[]
for pt in selected_points:
    curve=pt.get('curve_number',pt.get('curveNumber'))
    index=pt.get('point_index',pt.get('pointNumber',pt.get('point_number')))
    if curve==1 and index is not None:
        try:
            index=int(index)
            if 0<=index<len(plot_sec):raw_choices.append(index)
        except (TypeError,ValueError):pass
# Some Streamlit versions expose plot indices only as x coordinates.
if not raw_choices:
    for pt in selected_points:
        if pt.get('curve_number',pt.get('curveNumber'))==1 and 'x' in pt:
            try:
                raw_choices.append(int(np.argmin(np.abs(np.asarray(plot_x)-float(pt['x'])))))
            except (TypeError,ValueError):pass
if len(set(raw_choices))>1:
    st.warning('Multiple GPS points selected. Click just one green point to add a repetition.')
elif raw_choices:
    chosen_idx=raw_choices[0]
    picked_sec=plot_sec[chosen_idx]
    picked_raw=plot_raw[chosen_idx]
    if picked_raw is None or not np.isfinite(picked_raw):
        st.error('The selected GPS point has no valid raw speed. Select another point.')
    else:
        nearest=plot.iloc[chosen_idx]
        peak_raw_ms=float(nearest['rawmax'])
        nearby=trace[(trace.sec>=picked_sec-.4)&(trace.sec<=picked_sec+.4)]['mean']
        smoothed_ms=float(nearby.median()) if len(nearby) else float(nearest['mean'])
        try:
            operations,preview=chronological_insert(rep_rows,set_no,picked_sec,
                peak_raw_ms,smoothed_ms)
            save_changes_atomically(operations)
            st.session_state[f'last_saved_{day}_{key}']=(set_no,preview['inserted_rep'],picked_raw,picked_sec-origin)
            st.rerun()
        except PeakInsertionError as ex:
            st.warning(str(ex))
else:
    st.caption('Click a green raw GPS dot to select it. Native point selection may display a selection toolbar; choose point selection if prompted. The app will preview the proposed rep before saving.')

st.markdown('#### Alternative: insert a raw GPS peak by timestamp')
with st.expander('Use a timestamp instead of clicking (optional)'):
    old_ts=pd.to_datetime(current.get('Peak time local'),errors='coerce',utc=True)
    defpos=float(old_ts.timestamp()) if pd.notna(old_ts) else float((left+right)/2)
    defpos=min(max(defpos,float(plot.sec.min())),float(plot.sec.max()))
    select_rel=st.number_input('Peak position (seconds from recording start)',
        min_value=round(float(plot.sec.min()-origin),1),max_value=round(float(plot.sec.max()-origin),1),
        value=round(defpos-origin,1),step=.2,format='%.1f',
        key=f'fallback_time_{day}_{key}_{set_no}_{number}')
    halfwidth=st.slider('Search radius (seconds)',min_value=.2,max_value=1.0,value=.4,step=.2)
    search=trace[(trace.sec>=origin+select_rel-halfwidth)&(trace.sec<=origin+select_rel+halfwidth)]
    reason=st.text_input('Review note / justification',placeholder='e.g. GPS sprint peak visually verified')
    if not search.empty:
        chosen=search.loc[search['rawmax'].idxmax()]
        peak_sec=float(chosen.sec);peak_raw=float(chosen.rawmax)
        near=trace[(trace.sec>=peak_sec-.4)&(trace.sec<=peak_sec+.4)]['mean']
        smoothed=float(near.median()) if len(near) else float(chosen['mean'])
        st.caption(f'Actual raw measurement: {peak_raw*MPH:.2f} mph at {peak_sec-origin:.1f} seconds')
        if st.button('Insert this measured peak in chronological order',type='secondary'):
            if not reason.strip():st.error('Enter a review justification before saving.')
            else:
                try:
                    operations,preview=chronological_insert(rep_rows,set_no,peak_sec,peak_raw,smoothed,
                        reason=reason.strip())
                    save_changes_atomically(operations)
                    st.success(f'Saved as Set {set_no} Rep {preview["inserted_rep"]}.')
                    st.rerun()
                except PeakInsertionError as ex:st.error(str(ex))
    else:st.warning('No GPS samples at that time.')

col_a,col_b=st.columns(2)
with col_a:
    if st.button('Mark selected rep missing',use_container_width=True):
        new={'Peak (m/s)':None,'Peak (mph)':None,'Raw max (mph)':None,'Peak time local':'',
             'Detection':'MANUALLY_REJECTED','QC note':'Manual rejection; GPS evidence requires review','Timing error (s)':None}
        save_change('MANUALLY_REJECTED',number,current.to_dict(),new,'Manual rejection of selected rep')
        st.rerun()
with col_b:
    if st.button('Restore original automatic rep',use_container_width=True):
        save_change('RESTORED_AUTOMATIC',number,current.to_dict(),{},'Reverted to automatic result')
        st.rerun()

st.markdown('#### Current assessment')
view=rep_rows[['Set','Set Rep','Rep','Peak (mph)','Raw max (mph)','Detection','QC note']].copy()
view['Raw max (mph)']=pd.to_numeric(view['Raw max (mph)'],errors='coerce').round(2)
st.dataframe(view,use_container_width=True,hide_index=True,height=300)
valid=int(pd.to_numeric(rep_rows['Raw max (mph)'],errors='coerce').notna().sum())
st.metric('Repetitions with GPS peaks',f'{valid}/30')
if valid<30:st.warning('INCOMPLETE ASSESSMENT: '+', '.join(f'Set {i}: {set_counts[i]}/10' for i in (1,2,3) if set_counts[i]<10))
st.caption('A complete 30/30 set still requires athlete-recording identity verification and protocol review.')

# The reviewed PDF is prepared from saved edits BEFORE displaying its download
# button, so downloading takes one click and never returns an outdated report.
def _export_fingerprint(selected_keys):
    files=[reps_path,review_path,index_path,BACKEND,ROOT/'export_name_override.py']
    files.extend(sorted(overrides_dir.glob('*.json')))
    digest=hashlib.sha256()
    for source in files:
        digest.update(str(source).encode('utf-8'))
        if source.exists():
            stat=source.stat()
            digest.update(str((stat.st_size,stat.st_mtime_ns)).encode('ascii'))
    for selected in sorted(selected_keys):
        digest.update(selected.encode('utf-8'))
    return digest.hexdigest()


def build_reviewed_reports(selected_keys, target_root):
    """Rebuild exports in the original v22 PDF/HTML format, never raw GPS."""
    # Rebuild the complete dated dataset using untouched baseline + every correction on disk.
    merged=source_reps.copy()
    active_keys={str(e['match_key']) for e in index}
    for file in overrides_dir.glob('*.json'):
        doc=json.loads(file.read_text())
        record_key=str(doc['match_key'])
        if record_key not in active_keys:
            continue  # historical trial; preserved on disk, never applied to current pull
        original=merged[merged['Match key'].astype(str)==record_key].copy() if 'Match key' in merged else pd.DataFrame()
        if original.empty:
            linked=next((x for x in index if x['match_key']==record_key),None)
            if linked is None:raise RuntimeError(f'No validated identity index for {record_key}')
            name=linked['player']
            original=pd.DataFrame([{'Athlete':name,'Recording':name,'Block':'Assessment 1',
                'Set':(i-1)//10+1,'Set Rep':(i-1)%10+1,'Rep':i,
                'Peak (m/s)':np.nan,'Peak (mph)':np.nan,'Raw max (mph)':np.nan,
                'Peak time local':'','Detection':'Missing','QC note':'No automatic 3x10 match',
                'Timing error (s)':np.nan,'Samples':0,'Match key':record_key}
                for i in range(1,31)])
        # Restore missing numbered slots before applying manual overrides.
        present=set(pd.to_numeric(original['Rep'],errors='coerce').dropna().astype(int))
        if len(original)!=len(present):
            raise RuntimeError(f'Duplicate or invalid rep slots for {record_key}')
        absent=set(range(1,31))-present
        if absent:
            supplement=pd.DataFrame([{'Athlete':doc['athlete'],'Recording':doc['athlete'],
                'Block':'Assessment 1','Set':(r-1)//10+1,'Set Rep':(r-1)%10+1,'Rep':r,
                'Peak (m/s)':np.nan,'Peak (mph)':np.nan,'Raw max (mph)':np.nan,
                'Peak time local':'','Detection':'Missing','QC note':'No automatic rep assigned',
                'Timing error (s)':np.nan,'Samples':0,'Match key':record_key} for r in sorted(absent)])
            original=pd.concat([original,supplement],ignore_index=True)
        updated=effective(original,doc['overrides'])
        merged=merged[merged['Match key'].astype(str)!=record_key].copy() if 'Match key' in merged else pd.DataFrame()
        merged=pd.concat([merged,updated],ignore_index=True)
    # Apply export-only names after all peak corrections. This is the only
    # place Athlete/Recording labels change; API identities remain immutable.
    all_docs=[json.loads(file.read_text()) for file in overrides_dir.glob('*.json')]
    current_docs,_=partition_saved_trial_docs(index,all_docs)
    merged,rename_map=apply_export_names(merged,current_docs,index)
    # Filter by immutable tagged-trial keys only AFTER applying approved labels.
    merged=merged[merged['Match key'].astype(str).isin(selected_keys)].copy()
    rename_map=[r for r in rename_map if str(r['match_key']) in selected_keys]
    if merged.empty:raise RuntimeError('No reviewable rep data')
    review=source_review.copy()
    for record_key in [doc['match_key'] for doc in current_docs]:
        mask=review['Match key'].astype(str)==record_key
        if not mask.any():raise RuntimeError(f'Missing review identity entry for {record_key}')
        review.loc[mask,'Status']=review.loc[mask,'Status'].astype(str)+'; MANUAL PEAK CORRECTIONS APPLIED - PROVISIONAL'
    for rename in rename_map:
        mask=review['Match key'].astype(str)==str(rename['match_key'])
        if not mask.any():raise RuntimeError('No quality-review entry for '+str(rename['match_key']))
        review.loc[mask,'Athlete']=rename['export_player_name']
    review=review[review['Match key'].astype(str).isin(selected_keys)].copy()
    if review.empty:raise RuntimeError('No quality-review records for selected trials')
    # Keep all original reports and corrections untouched. This selection gets
    # an isolated destination so report generation can never overwrite them.
    target_root.mkdir(parents=True,exist_ok=True)
    mod.OUTPUT_ROOT=target_root
    target_dir,paths=mod.group_paths(day)
    target_dir.mkdir(parents=True,exist_ok=True)
    import csv
    with (target_dir/'assessment_export_name_audit.csv').open('w',newline='',encoding='utf-8') as fh:
        writer=csv.DictWriter(fh,fieldnames=['match_key','source_api_athlete','export_player_name','rawDataId','session_player_id'])
        writer.writeheader()
        writer.writerows(rename_map)
    merged.to_csv(paths['reps'],index=False)
    review.to_csv(paths['review'],index=False)
    mod.generate_all_reports(day,paths)
    pdf=paths['pdf']
    if not pdf.is_file() or pdf.stat().st_size<100 or pdf.open('rb').read(5)!=b'%PDF-':
        raise RuntimeError('PDF generator completed without creating a valid PDF file')
    return paths

st.markdown('### Export selected assessment trials')
st.caption('Choose the athletes/trials to include in this PDF. Each trial remains independent, '
           'even if two trials originated under the same STATSports player name. '
           'Source results, name overrides and correction history are not changed.')
review_dir.mkdir(parents=True,exist_ok=True)
# Always display SAVED per-trial export names, rather than raw API athlete names.
try:
    all_saved_docs=[json.loads(f.read_text()) for f in sorted(overrides_dir.glob('*.json'))]
    active_saved_docs,orphaned_docs=partition_saved_trial_docs(index,all_saved_docs)
    effective_names=trial_export_names(index,active_saved_docs)
    if orphaned_docs:
        with st.expander(f'{len(orphaned_docs)} saved correction/rename file(s) not in this date’s current trial inventory — safely skipped'):
            st.caption('Historical edits were NOT deleted or reassigned. A fresh pull can change trial keys; check the original athlete, recording and drill identity before reapplying an old rename.')
            st.dataframe(pd.DataFrame([{'Saved trial key':str(d.get('match_key','')),
                'Original athlete':str(d.get('athlete','')),
                'Saved export name':str(d.get('export_player_name') or ''),
                'Saved rawDataId':str(d.get('rawDataId',''))} for d in orphaned_docs]),
                use_container_width=True,hide_index=True)
except (ValueError,KeyError,OSError) as exc:
    st.error('Export name validation failed: '+str(exc))
    st.stop()
key_to_entry={str(e['match_key']):e for e in index}
all_keys=[str(e['match_key']) for e in index]
def export_option_label(k):
    e=key_to_entry[k]
    return f"{effective_names[k]} · {str(e.get('start',''))[:19]} · trial {k[:8]}"
state_key=f'selected_export_trials_{day}'
if state_key not in st.session_state:
    st.session_state[state_key]=all_keys.copy()
else:
    st.session_state[state_key]=[k for k in st.session_state[state_key] if k in key_to_entry]
col_all,col_none=st.columns(2)
with col_all:
    if st.button('Select all trials',key=f'all_export_{day}',use_container_width=True):
        st.session_state[state_key]=all_keys.copy()
        st.rerun()
with col_none:
    if st.button('Clear selection',key=f'clear_export_{day}',use_container_width=True):
        st.session_state[state_key]=[]
        st.rerun()
selected_keys=st.multiselect('Assessment trials to include in export',options=all_keys,
    format_func=export_option_label,key=state_key)
st.caption(f'{len(selected_keys)} of {len(all_keys)} trials selected.')
if not selected_keys:
    st.warning('Select at least one trial to enable PDF, HTML and CSV downloads.')
else:
    selected_keys=set(selected_keys)
    signature=_export_fingerprint(selected_keys)
    scope_hash=hashlib.sha256('|'.join(sorted(selected_keys)).encode('utf-8')).hexdigest()[:12]
    scoped_root=output_root/'selected_exports'/day/scope_hash
    manifest=scoped_root/'reviewed_export_manifest.json'
    mod.OUTPUT_ROOT=scoped_root
    _,published_paths=mod.group_paths(day)
    pdf_path=published_paths['pdf']
    html_path=published_paths['html']
    raw_csv=published_paths['raw_reps']
    try:
        previous=json.loads(manifest.read_text()) if manifest.exists() else {}
    except (OSError,ValueError):previous={}
    ready=(previous.get('signature')==signature and pdf_path.is_file() and
           html_path.is_file() and raw_csv.is_file() and pdf_path.stat().st_size>100)
    if not ready:
        try:
            with st.spinner(f'Preparing selected report ({len(selected_keys)} trial(s))…'):
                built=build_reviewed_reports(selected_keys,scoped_root)
                scoped_root.mkdir(parents=True,exist_ok=True)
                temp=manifest.with_suffix('.tmp')
                temp.write_text(json.dumps({'signature':signature,'date':day,
                    'selected_match_keys':sorted(selected_keys),'pdf':str(built['pdf']),
                    'generated_utc':datetime.now(timezone.utc).isoformat()},indent=2))
                temp.replace(manifest)
            ready=True
        except Exception as exc:
            ready=False
            st.error('Could not generate selected report: '+str(exc))
    if ready:
        label='Selected_'+str(len(selected_keys))+'_Trials_'+day
        st.download_button('Download selected PDF report',data=pdf_path.read_bytes(),
            file_name=f'STATSports_{label}.pdf',mime='application/pdf',
            type='primary',use_container_width=True,key=f'selected_pdf_{day}_{scope_hash}')
        st.download_button('Download selected HTML dashboard',data=html_path.read_bytes(),
            file_name=f'STATSports_{label}.html',mime='text/html',
            use_container_width=True,key=f'selected_html_{day}_{scope_hash}')
        st.download_button('Download selected repetition CSV',data=raw_csv.read_bytes(),
            file_name=f'STATSports_{label}.csv',mime='text/csv',
            use_container_width=True,key=f'selected_csv_{day}_{scope_hash}')
        st.caption('Export saved to '+str(scoped_root))
if corrections_path.exists():
    with st.expander('Full history of saved peak edits'):
        st.json(read_corrections()['history'])


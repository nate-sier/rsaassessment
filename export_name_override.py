"""Per-tagged-trial export names. Never alters API athlete or recording identity."""
import re
from collections import Counter, defaultdict

def normalize_export_name(value):
    return re.sub(r'\s+', ' ', str(value or '').strip())

def validate_export_name(value):
    value=normalize_export_name(value)
    if not value:
        raise ValueError('Export player name cannot be blank.')
    if len(value)>100 or any(ord(c)<32 for c in value):
        raise ValueError('Export name must be 1–100 printable characters.')
    return value

def default_trial_names(identity_index):
    """Stable unique defaults for repeated API names, sorted by tag time/key.

    Trial suffix is a report label, NOT an identity assertion.
    """
    counts=Counter(normalize_export_name(e['player']).casefold() for e in identity_index)
    groups=defaultdict(list)
    for e in identity_index:
        groups[normalize_export_name(e['player']).casefold()].append(e)
    defaults={}
    for group in groups.values():
        for n,e in enumerate(sorted(group,key=lambda x:(str(x.get('start') or ''),str(x['match_key']))),1):
            source=normalize_export_name(e['player'])
            defaults[str(e['match_key'])]=source if counts[source.casefold()]==1 else f'{source} — Trial {n}'
    return defaults

def partition_saved_trial_docs(identity_index, docs):
    """Return (current, orphaned) saved edits without altering their files.

    An orphan is a historical match_key absent from the CURRENT tagged-trial
    inventory. A present key with conflicting raw/session-player IDs is never
    considered an orphan: downstream identity validation must still reject it.
    """
    current_keys={str(e['match_key']) for e in identity_index}
    current=[]
    orphaned=[]
    for doc in docs:
        if str(doc['match_key']) in current_keys:
            current.append(doc)
        else:
            orphaned.append(doc)
    return current,orphaned

def trial_export_names(identity_index, docs):
    """Effective label per assessment. Explicit labels override unique defaults."""
    defaults=default_trial_names(identity_index)
    docs_by_key={str(d['match_key']):d for d in docs}
    valid_keys={str(e['match_key']) for e in identity_index}
    if set(docs_by_key)-valid_keys:
        raise ValueError('Saved rename refers to an unknown tagged trial')
    names={}
    for e in identity_index:
        key=str(e['match_key'])
        d=docs_by_key.get(key,{})
        if d and (str(d.get('rawDataId'))!=str(e.get('raw_id')) or
                  str(d.get('session_player_id'))!=str(e.get('session_player_id'))):
            raise ValueError('Saved export override does not match recording identity: '+key)
        names[key]=validate_export_name(normalize_export_name(d.get('export_player_name')) or defaults[key])
    owners={}
    for key,name in names.items():
        cf=name.casefold()
        if cf in owners:
            raise ValueError(f'Duplicate export name {name!r} across separate trials; rename one before generating reports.')
        owners[cf]=key
    return names

def apply_export_names(reps, docs, identity_index):
    frame=reps.copy()
    mapping=[]
    names=trial_export_names(identity_index,docs)
    keys={str(e['match_key']) for e in identity_index}
    if 'Match key' not in frame:
        raise ValueError('Cannot export without per-trial Match key')
    if set(frame['Match key'].dropna().astype(str))-keys:
        raise ValueError('Repetition rows contain an unknown tagged trial')
    for e in identity_index:
        key=str(e['match_key'])
        label=names[key]
        mapping.append({'match_key':key,'source_api_athlete':str(e['player']),
                        'export_player_name':label,'rawDataId':str(e.get('raw_id')),
                        'session_player_id':str(e.get('session_player_id'))})
        mask=frame['Match key'].astype(str)==key
        frame.loc[mask,'Athlete']=label
        if 'Recording' in frame:frame.loc[mask,'Recording']=label
    return frame,mapping

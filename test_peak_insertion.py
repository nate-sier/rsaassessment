from pathlib import Path
import sys
import numpy as np
import pandas as pd
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chronological_peak_review import chronological_insert, PeakInsertionError


def sample_rows():
    out=[]
    for i in range(1,31):
        # Set 1 has 9 assigned peaks labeled 2–10, chronological order.
        valid=2 <= i <= 10
        sec=1_700_000_000+(i-2)*17 if valid else None
        out.append({'Rep':i, 'Set':(i-1)//10+1,'Set Rep':(i-1)%10+1,
            'Peak time local':str(pd.Timestamp(sec,unit='s',tz='UTC')) if valid else '',
            'Raw max (mph)':12.0 if valid else np.nan,
            'Peak (m/s)':5.0 if valid else np.nan,
            'Peak (mph)':11.18 if valid else np.nan,
            'Detection':'AUTO' if valid else 'MISSING', 'QC note':'',
            'Timing error (s)':0.0,'Samples':1 if valid else 0})
    return pd.DataFrame(out)


def test_append_last_peak_renumbers_all_ten():
    frame=sample_rows()
    sec=1_700_000_000+9*17
    ops,preview=chronological_insert(frame,1,sec,6.5,6.1)
    assert preview['inserted_rep']==10
    assert preview['total']==10
    assert len(ops)==10
    assert ops[9][3]['Detection']=='MANUALLY_ADDED'


def test_reject_duplicate_time():
    with pytest.raises(PeakInsertionError,match='already assigned'):
        chronological_insert(sample_rows(),1,1_700_000_000,6,5)

import pandas as pd
import pytest
from scalping.adaptation.calibration import calibrate, chronological_splits

def test_selection_is_training_only_with_embargo_and_all_attempts(tmp_path):
    d=pd.DataFrame({'value':range(300)},index=pd.date_range('2026-01-01',periods=300,freq='min',tz='UTC'))
    calls=[]
    def evaluate(rows,candidate):
        calls.append((rows.index.min(),rows.index.max(),candidate['a']))
        return {'metric':candidate['a']}
    result=calibrate(d,{'a':[1,2,3]},evaluate,'metric',d.index[99],d.index[199],tmp_path/'calibration',synthetic=True)
    assert len(result['attempts'])==3 and result['config']['a']==3
    assert all(end<=d.index[99] for start,end,a in calls[:3])
    assert calls[3][0]>d.index[129] and calls[4][0]>d.index[229]
    assert result['status']=='engineering_fixture_performance_pending'
    with pytest.raises(ValueError): chronological_splits(d,d.index[99],d.index[199],29)

from datetime import date
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter
import gc
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import pandas as pd
from bomber.framework.dataprep.contracts import BarReadSpec, InputPlan
from bomber.framework.dataprep.catalog import scan_bar_files
from bomber.framework.dataprep.prepared import PreparedSource, PreparedDataset, build_prepared_dataset
from bomber.framework.dataprep.scenarios.option_chain import prepare_option_chain
from bomber.framework.dataprep.session import InputSession

with TemporaryDirectory() as directory:
    root = Path(directory)
    for day in pd.bdate_range('2024-01-02', periods=10):
        folder=root/'raw'/day.strftime('%Y%m%d')
        folder.mkdir(parents=True)
        frame=pd.DataFrame(dict(datetime=pd.date_range(day.strftime('%Y-%m-%d')+' 09:35',periods=48,freq='5min'),
            open=100.,high=102.,low=99.,close=101.,volume=2))
        for number in range(40):
            frame.to_feather(folder/f'MO2406-C-{6000+number*100}_{day:%Y%m%d}.feather')
    spec=BarReadSpec(interval_seconds=300,trading_day_policy='day_session')
    source=PreparedSource('option',root/'raw','MO',spec)
    start,end=date(2024,1,2),date(2024,1,15)
    began=perf_counter()
    manifest=build_prepared_dataset((source,),root/'prepared',start_day=start,end_day=end)
    preparation=perf_counter()-began
    plan=InputPlan(tuple(scan_bar_files(root/'raw','option')),requested=(start,end))
    times={'raw':[],'prepared':[]}
    for trial in range(4):
        signatures={}
        for mode in (('raw','prepared') if trial%2==0 else ('prepared','raw')):
            gc.collect()
            with InputSession():
                began=perf_counter()
                dataset=None if mode=='raw' else PreparedDataset(root/'prepared')
                bundle=prepare_option_chain({'option':root/'raw'},plan,specs={'option':spec},
                    copy_frames=False,prepared_dataset=dataset,products={'option':'MO'})
                elapsed=perf_counter()-began
                if trial:times[mode].append(elapsed)
                signatures[mode]=[(s.result.key,tuple(s.result.frame.event_ns),tuple(s.result.frame.close),
                    tuple(str(t) for t in s.result.frame.dtypes)) for s in bundle.sources]
            del bundle
        assert signatures['raw']==signatures['prepared']
    before,after=median(times['raw']),median(times['prepared'])
    print(json.dumps(dict(source_files=manifest['source_files'],bars=manifest['rows'],partitions=manifest['partitions'],
        initial_preparation_seconds=round(preparation,6),raw_load_seconds=round(before,6),prepared_load_seconds=round(after,6),
        reduction_percent=round((1-after/before)*100,2),equal=True,
        environment=dict(pandas=pd.__version__),
        method='Real synthetic Feather; same source plan, one warmup and three alternating runs; independent InputSessions; OS page cache uncontrolled',
        scope='Input loading only, includes prepared manifest/index validation and source-frame restoration; excludes engine/strategy'),indent=2))

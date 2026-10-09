import unittest
import pandas as pd
import numpy as np
from engine import Config, indicators, signals, backtest

class Tests(unittest.TestCase):
    def data(self,n=1500):
        idx=pd.date_range('2026-01-05',periods=n,freq='min',tz='UTC')
        a=100+np.sin(np.arange(n)/20)
        return pd.DataFrame({'open':a,'high':a+.2,'low':a-.2,'close':a,'volume':100},index=idx)
    def test_no_lookahead(self):
        d=self.data(); c=Config()
        for method in ('trend','regular_ma','far_ma','base','double'):
            full=signals(d,c,method); prefix=signals(d.iloc[:1300],c,method)
            pd.testing.assert_frame_equal(prefix,full.loc[prefix.index])
    def test_stop_before_target(self):
        d=self.data(4); ts=d.index[0]
        d.loc[d.index[1],['open','high','low','close']]=[100,103,98,100]
        s=pd.DataFrame([{'side':1,'limit':100,'stop':99,'target1':101,'target2':102}],index=[ts])
        c=Config(session_start='00:00',session_end='23:59',timezone='UTC',slippage_ticks=0)
        t,e,m=backtest(d,s,c)
        self.assertEqual(t.iloc[0].reason,'stop'); self.assertLess(t.iloc[0].pnl,0)
    def test_risk_sizing_and_costs(self):
        d=self.data(4); ts=d.index[0]
        d.loc[d.index[1],['open','high','low','close']]=[100,100.1,98,100]
        s=pd.DataFrame([{'side':1,'limit':100,'stop':99,'target1':101,'target2':102}],index=[ts])
        c=Config(session_start='00:00',session_end='23:59',timezone='UTC',commission=.01,slippage_ticks=1)
        t,e,m=backtest(d,s,c)
        self.assertLessEqual(-t.iloc[0].pnl,10000*c.risk_fraction+1e-8)
    def test_zero_volume_vwap(self):
        d=self.data(300); d.volume=0
        self.assertTrue(indicators(d,Config()).vwap.isna().all())
if __name__=='__main__': unittest.main()

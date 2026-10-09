"""Event-driven research simulator. Zero latency means the next open event.

OHLC limit-touch fills assume full fill without queue knowledge. This is an
explicit research assumption; ambiguous stop/target bars always choose stop.
"""
from dataclasses import replace
from decimal import Decimal
import json
import pandas as pd
from ..contracts import AccountSnapshot, SignalProposal, PolicySnapshot, OrderType, Costs, freeze, primitive, dumps
from ..instrument import decimal, cash_per_price_unit, round_quantity, round_price
from .sizing import size_order
from .costs import executable_prices, round_trip_price

def restore_proposal(d):
    p=d['policy']; p['distances']=freeze(p['distances']); p['allowed_sides']=tuple(p['allowed_sides']); p['reasons']=tuple(p['reasons'])
    d['policy']=PolicySnapshot(**p)
    for k in ('decision_time','source_available_at','expiry'): d[k]=pd.Timestamp(d[k])
    for k in ('entry','stop','structural_invalidation'): d[k]=decimal(d[k])
    d['targets']=tuple(decimal(x) for x in d['targets']); d['fractions']=tuple(d['fractions'])
    d['order_type']=OrderType(d['order_type']); d['evidence']=freeze(d['evidence']); d['reasons']=tuple(d['reasons'])
    return SignalProposal(**d)

class Simulator:
    def __init__(self,instrument,calendar,equity=10000,buying_power=None,approved_cash_risk=None,research=True,latency_seconds=0):
        if decimal(equity)<=0 or latency_seconds<0: raise ValueError('Invalid equity/latency')
        self.instrument=instrument; self.calendar=calendar
        self.initial=decimal(equity); self.cash=self.initial; self.buying_power=None if buying_power is None else decimal(buying_power)
        self.approved_cash_risk=None if approved_cash_risk is None else decimal(approved_cash_risk)
        self.research=research; self.latency_seconds=latency_seconds
        self.pending=None; self.position=None; self.seen=set(); self.last_event=None; self.last_session=None; self.last_close=None
        self.trades=[]; self.curve=[dict(timestamp=None,equity=float(self.initial))]; self.diagnostics=[]
        self.ambiguities=0; self.exposed_events=0; self.events=0; self.turnover=Decimal(0); self.friction=Decimal(0)
        self.spread_cash=Decimal(0); self.slippage_cash=Decimal(0); self.fills=0

    def to_json(self):
        return dumps({k:v for k,v in self.__dict__.items() if k not in ('instrument','calendar') and k!='seen'} | {'seen':sorted(self.seen)})

    @classmethod
    def from_json(cls,text,instrument,calendar):
        d=json.loads(text); s=cls(instrument,calendar,d['initial'],d['buying_power'],d['approved_cash_risk'],d['research'],d['latency_seconds'])
        for k in ('cash','turnover','friction','spread_cash','slippage_cash'): d[k]=decimal(d[k])
        for k in ('initial','buying_power','approved_cash_risk'): d[k]=decimal(d[k]) if d[k] is not None else None
        d['seen']=set(d['seen'])
        for k in ('last_event','last_close'): d[k]=pd.Timestamp(d[k]) if d[k] else None
        if d['pending']: d['pending']=restore_proposal(d['pending'])
        if d['position']:
            p=d['position']; p['proposal']=restore_proposal(p['proposal'])
            for k in ('entry','stop','qty','initial_qty','pnl','planned_risk','point'): p[k]=decimal(p[k])
            p['targets']=tuple(decimal(x) for x in p['targets']); p['entered']=pd.Timestamp(p['entered'])
        s.__dict__.update(d)
        return s

    def submit(self,proposal):
        if proposal.signal_id in self.seen: return
        self.seen.add(proposal.signal_id)
        if self.pending or self.position:
            self.diagnostics.append(dict(signal_id=proposal.signal_id,reason='BUSY_NO_PYRAMID'))
        else: self.pending=proposal

    def cancel_pending(self,reason):
        if self.pending:
            self.diagnostics.append(dict(signal_id=self.pending.signal_id,reason=reason))
            self.pending=None

    def _uncertain(self,reason,timestamp):
        self.diagnostics.append(dict(reason=reason,timestamp=str(timestamp),signal_id=self.position['proposal'].signal_id if self.position else None))
        if self.position:
            q=self.position
            self.trades.append(dict(signal_id=q['proposal'].signal_id,entry_time=str(q['entered']),exit_time=None,side=q['proposal'].side,
                entry=float(q['entry']),quantity=float(q['initial_qty']),pnl=None,reason=reason,completed=False,planned_risk=float(q['planned_risk']),
                session=self.last_session,regime=q['proposal'].policy.regime,strategy=q['proposal'].strategy_id,instrument=self.instrument.instrument_id))
        self.position=None; self.pending=None

    def process(self,bar,signals,costs,conversion=None,quote=None):
        ts=bar.open_time
        if self.last_event is not None and ts<=self.last_event:
            if ts==self.last_event: return
            raise ValueError('Out-of-order execution event')
        session=self.calendar.session_at(ts)
        if not session: self.pending=None; return
        if self.position and self.last_session!=session.session_id:
            self._uncertain('UNPRICEABLE_SESSION_LIQUIDATION',ts)
        if self.last_session!=session.session_id: self.pending=None
        self.last_session=session.session_id; self.last_event=ts; self.last_close=bar.close_time
        self.events+=1
        for proposal in signals:
            if proposal.source_available_at>ts: raise ValueError('Signal not yet available')
            if proposal.expiry>ts: self.submit(proposal)
        if self.pending and (ts>=self.pending.expiry or costs.spread<0): self.pending=None
        try: point=cash_per_price_unit(self.instrument,conversion,ts)
        except ValueError:
            self.diagnostics.append(dict(reason='UNPRICEABLE_FX_EVENT',timestamp=str(ts)))
            if self.position: self._uncertain('UNPRICEABLE_FX_LIQUIDATION',ts)
            self.pending=None
            return
        if self.pending and not self.position and ts>=self.pending.source_available_at+pd.Timedelta(seconds=self.latency_seconds):
            p=self.pending; side=p.side
            opening,high,low=executable_prices(bar,side,True,costs,quote)
            hit=p.order_type==OrderType.MARKET or (low<=p.entry if side==1 else high>=p.entry) if p.order_type!=OrderType.STOP else (high>=p.entry if side==1 else low<=p.entry)
            if hit:
                if p.order_type==OrderType.LIMIT:
                    raw=min(opening,p.entry) if side==1 else max(opening,p.entry)
                    fill=min(raw+costs.slippage,p.entry) if side==1 else max(raw-costs.slippage,p.entry)
                elif p.order_type==OrderType.STOP:
                    fill=(max(opening,p.entry) if side==1 else min(opening,p.entry))+side*costs.slippage
                else: fill=opening+side*costs.slippage
                fill=round_price(fill,self.instrument.tick_size,'up' if side==1 else 'down')
                targets=p.targets
                if p.order_type==OrderType.MARKET:
                    targets=tuple(round_price(fill+side*decimal(p.policy.distances[k])*self.instrument.tick_size,self.instrument.tick_size,'down' if side==1 else 'up') for k in ('target1','target2'))
                actual=replace(p,entry=fill,targets=targets)
                account=AccountSnapshot(ts,self.cash,self.instrument.account_currency,self.buying_power,self.approved_cash_risk)
                # Stop orders freeze the planned quantity; gap risk is recorded, never wished away.
                sizing=size_order(p if p.order_type==OrderType.STOP else actual,account,self.instrument,costs,conversion,self.research)
                valid_geometry=side*(fill-p.stop)>0 and all(side*(t-fill)>0 for t in targets)
                valid_cost=side*(targets[0]-fill)>decimal(p.evidence.get('min_target_cost_ratio',3))*round_trip_price(costs,self.instrument,conversion)
                if sizing.accepted and ((valid_geometry and valid_cost) or p.order_type==OrderType.STOP):
                    qty=sizing.quantity
                    self.fills+=1
                    self.spread_cash+=costs.spread/2*qty*point
                    self.slippage_cash+=(abs(fill-raw) if p.order_type==OrderType.LIMIT else costs.slippage)*qty*point
                    fee=qty*costs.fee_per_side; self.cash-=fee; self.friction+=fee
                    self.position=dict(proposal=p,entry=fill,stop=p.stop,targets=targets,qty=qty,initial_qty=qty,entered=ts,pnl=-fee,partial=False,
                        planned_risk=sizing.planned_cash_risk,point=point,session=session.session_id)
                    self.turnover+=abs(fill)*qty*point
                    realized_planned=(abs(fill-p.stop)*point+(costs.slippage+costs.spread/2)*point+2*costs.fee_per_side)*qty
                    if realized_planned>sizing.planned_cash_risk or not valid_geometry:
                        self.diagnostics.append(dict(reason='STOP_FILL_RISK_BREACH',signal_id=p.signal_id,actual_risk=float(realized_planned),planned_risk=float(sizing.planned_cash_risk)))
                else:
                    self.diagnostics.append(dict(reason='FILL_REVALIDATION' if sizing.accepted else ','.join(sizing.reasons),signal_id=p.signal_id))
                self.pending=None
        if self.position:
            self.exposed_events+=1
            q=self.position; p=q['proposal']; side=p.side
            opening,high,low=executable_prices(bar,side,False,costs,quote)
            stopped=low<=q['stop'] if side==1 else high>=q['stop']
            target_hit=high>=q['targets'][0] if side==1 else low<=q['targets'][0]
            if stopped and target_hit: self.ambiguities+=1
            reason=None
            if stopped:
                price=(min(opening,q['stop']) if side==1 else max(opening,q['stop']))-side*costs.slippage
                self._exit(price,q['qty'],costs,point); reason='stop'
            else:
                for index,target in enumerate(q['targets']):
                    if index==0 and q['partial']: continue
                    if high>=target if side==1 else low<=target:
                        qty=round_quantity(q['initial_qty']*decimal(p.fractions[0]),self.instrument.quantity_step) if index==0 else q['qty']
                        # Target limit is never paid worse than its limit; no extra slippage.
                        self._exit(target,qty,costs,point); q['partial']=True
                        if q['qty']==0: reason='targets'; break
                if q['qty']>0 and (bar.close_time>=session.end or bar.close_time>=q['entered']+pd.Timedelta(minutes=p.max_hold)):
                    price=decimal(bar.close)-side*(costs.slippage+costs.spread/2)
                    self._exit(price,q['qty'],costs,point); reason='session_close' if bar.close_time>=session.end else 'timeout'
            if reason:
                self.trades.append(dict(signal_id=p.signal_id,entry_time=str(q['entered']),exit_time=str(bar.close_time),side=side,entry=float(q['entry']),
                    quantity=float(q['initial_qty']),pnl=float(q['pnl']),reason=reason,completed=True,planned_risk=float(q['planned_risk']),
                    holding_minutes=(bar.close_time-q['entered']).total_seconds()/60,regime=p.policy.regime,session=session.session_id,strategy=p.strategy_id,instrument=self.instrument.instrument_id))
                self.position=None
        mtm=self.cash if not self.position else self.cash+self.position['proposal'].side*(decimal(bar.close)-self.position['entry'])*self.position['qty']*point
        self.curve.append(dict(timestamp=str(bar.close_time),equity=float(mtm)))
        if bar.close_time>=session.end: self.pending=None

    def _exit(self,price,qty,costs,point):
        q=self.position
        pnl=q['proposal'].side*(price-q['entry'])*qty*point-qty*costs.fee_per_side
        self.cash+=pnl; q['pnl']+=pnl; q['qty']-=qty
        self.friction+=qty*costs.fee_per_side; self.turnover+=abs(price)*qty*point
        if price not in q['targets']:
            self.spread_cash+=costs.spread/2*qty*point
            self.slippage_cash+=costs.slippage*qty*point

    def finish(self):
        self.pending=None
        if self.position: self._uncertain('UNPRICEABLE_FINAL_LIQUIDATION',self.last_close)
        return self.trades,self.curve,self.metrics()

    def metrics(self):
        completed=[t for t in self.trades if t['completed']]
        pnls=[t['pnl'] for t in completed]; wins=sum(p for p in pnls if p>0); losses=-sum(p for p in pnls if p<0)
        high=float(self.initial); dd=0
        for r in self.curve:
            high=max(high,r['equity']); dd=min(dd,r['equity']/high-1)
        return dict(trades=len(completed),net_pnl=sum(pnls),net_expectancy=sum(pnls)/len(pnls) if pnls else None,
            expectancy_R=sum(t['pnl']/t['planned_risk'] for t in completed)/len(completed) if completed else None,
            profit_factor=wins/losses if losses else None,max_drawdown=dd,
            holding_minutes=sum(t['holding_minutes'] for t in completed)/len(completed) if completed else None,
            exposure=self.exposed_events/self.events if self.events else 0,turnover=float(self.turnover),fees_cash=float(self.friction),
            ambiguity_count=self.ambiguities,unpriceable_liquidations=sum(not t['completed'] for t in self.trades),
            no_trade=not completed,limit_fill_model='full_touch_no_queue',synthetic=self.instrument.synthetic,
            cash_balance=float(self.cash),realized_cash_change=float(self.cash-self.initial),
            spread_burden_estimate=float(self.spread_cash),slippage_burden_estimate=float(self.slippage_cash),
            fill_rate=self.fills/len(self.seen) if self.seen else None,proposed_orders=len(self.seen),
            sample_size=len(completed),out_of_sample_degradation=None,performance_validation='pending')

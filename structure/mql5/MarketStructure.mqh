//+------------------------------------------------------------------+
//| MarketStructure.mqh                                               |
//| Market structure reference implementation (MQL5).                 |
//|                                                                  |
//| One of three matching implementations of the owner's market       |
//| structure rules (October 2026):                                   |
//|   structure/python/market_structure.py                            |
//|   structure/mql5/MarketStructure.mqh  (this file)                 |
//|   structure/pine/market_structure.pine                            |
//| Rules and defaults: structure/README.md. Change all three         |
//| together.                                                         |
//|                                                                  |
//| Swing : pivot with `width` bars each side, counted only once      |
//|         price moves react x ATR away; a higher pivot high          |
//|         replaces a high still waiting (lows mirror).               |
//| Trend : last 2 highs AND last 2 lows step the same way, and the   |
//|         close has not broken the last swing low (high).            |
//| POI   : last HL (uptrend) / LH (downtrend) +/- box half-width,     |
//|         widened to swings and key levels within poiAtr x ATR.      |
//| Health: a 4H close beyond the last Daily HL / LH = challenged.     |
//|                                                                  |
//| Read only: nothing here places, changes or closes orders.         |
//+------------------------------------------------------------------+
#ifndef MARKET_STRUCTURE_MQH
#define MARKET_STRUCTURE_MQH

#define MS_UP     1
#define MS_DOWN  -1
#define MS_RANGE  0

struct MsSwing
  {
   int               kind;      // +1 swing high, -1 swing low
   int               index;     // bar of the swing (0 = oldest bar passed in)
   int               confirm;   // bar at which the reaction rule was met
   double            price;
   datetime          time;
   string            label;     // H, HH, LH / L, HL, LL
  };

//+------------------------------------------------------------------+
//| Wilder ATR, same as TradingView ta.atr and Python atr():          |
//| TR (high-low on the first bar), RMA seeded with the mean of the   |
//| first n true ranges. EMPTY_VALUE before bar n-1.                   |
//+------------------------------------------------------------------+
void MsAtr(const double &high[], const double &low[], const double &close[], const int count, const int n, double &out[])
  {
   ArrayResize(out, count);
   ArrayInitialize(out, EMPTY_VALUE);
   if(count < n)
      return;
   double sum = 0.0, prev = 0.0;
   for(int t = 0; t < count; t++)
     {
      double tr = high[t] - low[t];
      if(t > 0)
         tr = MathMax(tr, MathMax(MathAbs(high[t] - close[t - 1]), MathAbs(low[t] - close[t - 1])));
      if(t < n)
        {
         sum += tr;
         if(t == n - 1)
           {
            prev = sum / n;
            out[t] = prev;
           }
        }
      else
        {
         prev = (prev * (n - 1) + tr) / n;
         out[t] = prev;
        }
     }
  }

//+------------------------------------------------------------------+
class CMarketStructure
  {
private:
   int               m_width;
   double            m_react;
   int               m_atrLen;
   int               m_trend;
   double            m_lastAtr;
   MsSwing           m_swings[];
   double            m_hs[];       // confirmed swing high prices, oldest first
   double            m_ls[];       // confirmed swing low prices, oldest first

   bool              IsPivot(const double &x[], const int k, const int count, const bool isHigh)
     {
      if(k - m_width < 0 || k + m_width >= count)
         return false;
      for(int j = k - m_width; j <= k + m_width; j++)
        {
         if(j == k)
            continue;
         if(isHigh && !(x[k] > x[j]))
            return false;
         if(!isHigh && !(x[k] < x[j]))
            return false;
        }
      return true;
     }

   void              Push(double &arr[], const double v)
     {
      int n = ArraySize(arr);
      ArrayResize(arr, n + 1);
      arr[n] = v;
     }

   void              AddSwing(const int kind, const int index, const int confirm, const double price,
                              const datetime tm, const string label)
     {
      int n = ArraySize(m_swings);
      ArrayResize(m_swings, n + 1);
      m_swings[n].kind = kind;
      m_swings[n].index = index;
      m_swings[n].confirm = confirm;
      m_swings[n].price = price;
      m_swings[n].time = tm;
      m_swings[n].label = label;
     }

public:
                     CMarketStructure(void) : m_width(3), m_react(1.5), m_atrLen(14), m_trend(MS_RANGE), m_lastAtr(EMPTY_VALUE) {}

   void              Init(const int width, const double reactAtr, const int atrLen)
     {
      m_width = width;
      m_react = reactAtr;
      m_atrLen = atrLen;
     }

   //--- arrays oldest first (not series); pass completed bars only
   bool              Run(const double &high[], const double &low[], const double &close[], const datetime &time[], const int count)
     {
      ArrayResize(m_swings, 0);
      ArrayResize(m_hs, 0);
      ArrayResize(m_ls, 0);
      m_trend = MS_RANGE;
      m_lastAtr = EMPTY_VALUE;
      if(count < m_atrLen + 2 * m_width + 1)
         return false;
      double a[];
      MsAtr(high, low, close, count, m_atrLen, a);
      bool   hasH = false, hasL = false;
      double cH = 0.0, cL = 0.0;
      int    cHk = -1, cLk = -1;
      for(int t = 0; t < count; t++)
        {
         int k = t - m_width;
         if(k >= 0 && IsPivot(high, k, count, true) && (!hasH || high[k] > cH))
           {
            hasH = true;
            cH = high[k];
            cHk = k;
           }
         if(k >= 0 && IsPivot(low, k, count, false) && (!hasL || low[k] < cL))
           {
            hasL = true;
            cL = low[k];
            cLk = k;
           }
         double loW = low[t], hiW = high[t];
         for(int j = MathMax(0, t - m_width + 1); j <= t; j++)
           {
            loW = MathMin(loW, low[j]);
            hiW = MathMax(hiW, high[j]);
           }
         bool atrOk = (a[t] != EMPTY_VALUE);
         if(hasH && atrOk && loW <= cH - m_react * a[t])
           {
            int nh = ArraySize(m_hs);
            string lab = (nh == 0 ? "H" : (cH > m_hs[nh - 1] ? "HH" : "LH"));
            AddSwing(1, cHk, t, cH, time[cHk], lab);
            Push(m_hs, cH);
            hasH = false;
           }
         if(hasL && atrOk && hiW >= cL + m_react * a[t])
           {
            int nl = ArraySize(m_ls);
            string lab = (nl == 0 ? "L" : (cL > m_ls[nl - 1] ? "HL" : "LL"));
            AddSwing(-1, cLk, t, cL, time[cLk], lab);
            Push(m_ls, cL);
            hasL = false;
           }
         m_trend = MS_RANGE;
         int nh2 = ArraySize(m_hs), nl2 = ArraySize(m_ls);
         if(nh2 >= 2 && nl2 >= 2)
           {
            if(m_hs[nh2 - 1] > m_hs[nh2 - 2] && m_ls[nl2 - 1] > m_ls[nl2 - 2] && close[t] >= m_ls[nl2 - 1])
               m_trend = MS_UP;
            else
               if(m_hs[nh2 - 1] < m_hs[nh2 - 2] && m_ls[nl2 - 1] < m_ls[nl2 - 2] && close[t] <= m_hs[nh2 - 1])
                  m_trend = MS_DOWN;
           }
        }
      m_lastAtr = a[count - 1];
      return true;
     }

   int               Trend(void)       { return m_trend; }
   double            LastAtr(void)     { return m_lastAtr; }
   int               SwingCount(void)  { return ArraySize(m_swings); }
   bool              Swing(const int i, MsSwing &s)
     {
      if(i < 0 || i >= ArraySize(m_swings))
         return false;
      s = m_swings[i];
      return true;
     }
   //--- back = 0 is the latest confirmed swing high / low; EMPTY_VALUE if there is none
   double            LastHigh(const int back = 0)
     {
      int n = ArraySize(m_hs);
      return (back < n ? m_hs[n - 1 - back] : EMPTY_VALUE);
     }
   double            LastLow(const int back = 0)
     {
      int n = ArraySize(m_ls);
      return (back < n ? m_ls[n - 1 - back] : EMPTY_VALUE);
     }
  };

//+------------------------------------------------------------------+
//| Load completed bars of a timeframe (forming bar skipped) and run  |
//+------------------------------------------------------------------+
bool MsLoad(const string sym, const ENUM_TIMEFRAMES tf, const int bars, CMarketStructure &ms)
  {
   double h[], l[], c[];
   datetime tm[];
   int nh = CopyHigh(sym, tf, 1, bars, h);
   int nl = CopyLow(sym, tf, 1, bars, l);
   int nc = CopyClose(sym, tf, 1, bars, c);
   int nt = CopyTime(sym, tf, 1, bars, tm);
   int n = MathMin(MathMin(nh, nl), MathMin(nc, nt));
   if(n <= 0)
      return false;
   return ms.Run(h, l, c, tm, n);
  }

//+------------------------------------------------------------------+
//| Daily POI: last HL (uptrend) / LH (downtrend) +/- boxHalf,        |
//| widened to the last 3 swing highs / lows and key levels           |
//| (multiples of midStep) within poiAtr x Daily ATR.                  |
//+------------------------------------------------------------------+
bool MsPOI(CMarketStructure &daily, const double boxHalf, const double midStep, const double poiAtr,
           double &lo, double &hi, int &count)
  {
   int d = daily.Trend();
   double anchor = (d == MS_UP ? daily.LastLow(0) : d == MS_DOWN ? daily.LastHigh(0) : EMPTY_VALUE);
   double atr = daily.LastAtr();
   if(anchor == EMPTY_VALUE || atr == EMPTY_VALUE)
      return false;
   double radius = poiAtr * atr;
   lo = anchor - boxHalf;
   hi = anchor + boxHalf;
   count = 1;
   for(int b = 0; b < 3; b++)
     {
      double ps[2];
      ps[0] = daily.LastHigh(b);
      ps[1] = daily.LastLow(b);
      for(int i = 0; i < 2; i++)
         if(ps[i] != EMPTY_VALUE && ps[i] != anchor && MathAbs(ps[i] - anchor) <= radius)
           {
            lo = MathMin(lo, ps[i]);
            hi = MathMax(hi, ps[i]);
            count++;
           }
     }
   double base = MathRound(anchor / midStep) * midStep;
   for(int k = -1; k <= 1; k++)
     {
      double kl = base + k * midStep;
      if(MathAbs(kl - anchor) <= radius)
        {
         lo = MathMin(lo, kl - boxHalf);
         hi = MathMax(hi, kl + boxHalf);
         count++;
        }
     }
   return true;
  }

//+------------------------------------------------------------------+
//| 4H health against the Daily trend                                 |
//+------------------------------------------------------------------+
string MsHealth(CMarketStructure &daily, const double close4h)
  {
   int d = daily.Trend();
   if(d == MS_UP)
      return (close4h < daily.LastLow(0) ? "challenged" : "healthy");
   if(d == MS_DOWN)
      return (close4h > daily.LastHigh(0) ? "challenged" : "healthy");
   return "no trend";
  }

string MsTrendText(const int t)
  {
   return (t == MS_UP ? "Uptrend (HH + HL)" : t == MS_DOWN ? "Downtrend (LH + LL)" : "Ranging");
  }

#endif
//+------------------------------------------------------------------+

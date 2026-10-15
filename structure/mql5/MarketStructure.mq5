//+------------------------------------------------------------------+
//| MarketStructure.mq5                                               |
//| Chart indicator for the owner's market structure rules: HH / HL / |
//| LH / LL labels on the chart timeframe, Weekly / Daily / 4H trend, |
//| 4H health and the Daily POI. Logic lives in MarketStructure.mqh.  |
//| Read only: it never places, changes or closes orders.             |
//|                                                                  |
//| Install: copy MarketStructure.mqh and this file into              |
//| MQL5/Indicators/MarketStructure/, open this file in MetaEditor,   |
//| press Compile (F7), then drag it onto a chart.                     |
//+------------------------------------------------------------------+
#property copyright "Algo-Scripts-Broker-Test-1"
#property version   "1.00"
#property indicator_chart_window
#property indicator_plots 0

#include "MarketStructure.mqh"

input int    InpWidth      = 3;     // Swing: bars each side
input double InpReactAtr   = 1.5;   // Swing: min reaction (x ATR)
input int    InpAtrLen     = 14;    // ATR length
input double InpPoiAtr     = 0.5;   // POI clustering radius (x Daily ATR)
input double InpBoxHalf    = 0;     // Key box half-width in price (0 = auto: FX 10 pips, gold $10, S&P 10)
input double InpMidStep    = 0;     // Key level grid step in price (0 = auto: FX 250 pips, gold $50, S&P 50)
input bool   InpShowLabels = true;  // Label swings on this chart (HH / HL / LH / LL)
input int    InpMaxLabels  = 60;    // Max swing labels
input int    InpBars       = 1500;  // Bars analysed per timeframe
input color  InpBullColor  = clrTeal;
input color  InpBearColor  = clrRed;

const string PFX = "MS_";
datetime     g_lastBar = 0;

//--- auto key level settings by symbol name (same presets as the Pine indicator)
void KeyLevelPreset(double &boxHalf, double &midStep)
  {
   string s = _Symbol;
   StringToUpper(s);
   bool gold = (StringFind(s, "XAU") >= 0 || StringFind(s, "GOLD") >= 0);
   bool spx  = (StringFind(s, "SPX") >= 0 || StringFind(s, "US500") >= 0 || StringFind(s, "SP500") >= 0);
   double pip = (StringFind(s, "JPY") >= 0 ? 0.01 : 0.0001);
   boxHalf = InpBoxHalf > 0 ? InpBoxHalf : (gold || spx ? 10.0 : 10 * pip);
   midStep = InpMidStep > 0 ? InpMidStep : (gold || spx ? 50.0 : 250 * pip);
  }

int OnInit()
  {
   g_lastBar = 0;
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   ObjectsDeleteAll(0, PFX);
   Comment("");
  }

int OnCalculate(const int rates_total, const int prev_calculated, const datetime &time[], const double &open[],
                const double &high[], const double &low[], const double &close[], const long &tick_volume[],
                const long &volume[], const int &spread[])
  {
   if(rates_total < 50 || time[rates_total - 1] == g_lastBar)
      return(rates_total);           // recalculate once per new bar
   g_lastBar = time[rates_total - 1];

   //--- chart timeframe: completed bars only (the last bar is still forming)
   int n = MathMin(InpBars, rates_total - 1);
   int off = rates_total - 1 - n;
   double h[], l[], c[];
   datetime tm[];
   ArrayResize(h, n);
   ArrayResize(l, n);
   ArrayResize(c, n);
   ArrayResize(tm, n);
   for(int i = 0; i < n; i++)
     {
      h[i] = high[off + i];
      l[i] = low[off + i];
      c[i] = close[off + i];
      tm[i] = time[off + i];
     }
   CMarketStructure chartMs;
   chartMs.Init(InpWidth, InpReactAtr, InpAtrLen);
   chartMs.Run(h, l, c, tm, n);

   ObjectsDeleteAll(0, PFX);
   if(InpShowLabels)
     {
      int total = chartMs.SwingCount();
      for(int i = MathMax(0, total - InpMaxLabels); i < total; i++)
        {
         MsSwing s;
         if(!chartMs.Swing(i, s))
            continue;
         string name = PFX + "sw" + IntegerToString(i);
         bool bull = (s.label == "HH" || s.label == "HL" || s.label == "H" || s.label == "L");
         ObjectCreate(0, name, OBJ_TEXT, 0, s.time, s.price);
         ObjectSetString(0, name, OBJPROP_TEXT, s.label);
         ObjectSetInteger(0, name, OBJPROP_COLOR, bull ? InpBullColor : InpBearColor);
         ObjectSetInteger(0, name, OBJPROP_ANCHOR, s.kind > 0 ? ANCHOR_LOWER : ANCHOR_UPPER);
         ObjectSetInteger(0, name, OBJPROP_FONTSIZE, 8);
         ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
        }
     }

   //--- top-down read: Weekly (context), Daily (direction + POI), 4H (health)
   CMarketStructure w, d, h4;
   w.Init(InpWidth, InpReactAtr, InpAtrLen);
   d.Init(InpWidth, InpReactAtr, InpAtrLen);
   h4.Init(InpWidth, InpReactAtr, InpAtrLen);
   bool okW = MsLoad(_Symbol, PERIOD_W1, MathMin(InpBars, 400), w);
   bool okD = MsLoad(_Symbol, PERIOD_D1, InpBars, d);
   bool okH = MsLoad(_Symbol, PERIOD_H4, InpBars, h4);
   double close4h = iClose(_Symbol, PERIOD_H4, 1);

   string txt = "Market structure (" + _Symbol + ")\n";
   txt += "Weekly: " + (okW ? MsTrendText(w.Trend()) : "no data") + "\n";
   txt += "Daily:  " + (okD ? MsTrendText(d.Trend()) : "no data") + (okD && d.Trend() == MS_RANGE ? "  -> no trade" : "") + "\n";
   if(okD && d.Trend() != MS_RANGE)
     {
      txt += "Weekly agrees: " + (okW && w.Trend() == d.Trend() ? "yes (+1 confluence)" : "no") + "\n";
      txt += "4H: " + (okH ? MsTrendText(h4.Trend()) : "no data") + " | health: " + MsHealth(d, close4h) + "\n";
      double boxHalf, midStep, lo, hi;
      int cnt;
      KeyLevelPreset(boxHalf, midStep);
      if(MsPOI(d, boxHalf, midStep, InpPoiAtr, lo, hi, cnt))
        {
         txt += "Daily POI: " + DoubleToString(lo, _Digits) + " - " + DoubleToString(hi, _Digits) + " (" + IntegerToString(cnt) + " levels)\n";
         string box = PFX + "poi";
         datetime t1 = iTime(_Symbol, PERIOD_D1, 30);
         datetime t2 = time[rates_total - 1] + PeriodSeconds() * 20;
         ObjectCreate(0, box, OBJ_RECTANGLE, 0, t1, hi, t2, lo);
         ObjectSetInteger(0, box, OBJPROP_COLOR, clrGold);
         ObjectSetInteger(0, box, OBJPROP_STYLE, STYLE_DOT);
         ObjectSetInteger(0, box, OBJPROP_FILL, false);
         ObjectSetInteger(0, box, OBJPROP_BACK, true);
         ObjectSetInteger(0, box, OBJPROP_SELECTABLE, false);
        }
     }
   txt += "Chart TF: " + MsTrendText(chartMs.Trend());
   Comment(txt);
   ChartRedraw();
   return(rates_total);
  }
//+------------------------------------------------------------------+

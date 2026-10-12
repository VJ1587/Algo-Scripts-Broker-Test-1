// Read-only consumer of this strategy's Python output. No calculation approximations or orders.
#property strict
#property script_show_inputs
#property version "1.00"
#property description "ITS_S1_Macro_Trend_Continuation: individual Python signal bridge, read-only"
const string STRATEGY="s1_macro_trend_continuation";
const string EXPECTED_VERSION="1.0.0";
const string EXPECTED_HEADER="schema\tstrategy_id\tstrategy_version\tbroker_symbol\tinstrument_id\ttimeframe\tbar_open_utc\tavailable_utc\tgenerated_utc\texpires_utc\tsynthetic\treplay\tstatus\tsignal\tentry\tstop\ttarget\ttrail_stop\ttime_stop_bars\texit_long\texit_short\tgrade\treason\tpayload_json\tconfig_hash\tdataset_hash";
input string InpFeedFile="s1_macro_trend_continuation.tsv"; // Relative path under MQL5/Files
input bool InpCommonFiles=false; // Optional Terminal/Common/Files
input int InpMaxFeedAgeSeconds=300;
input bool InpAllowDiagnosticData=false; // Explicit display of SYNTHETIC/REPLAY; alerts suppressed
input bool InpAlert=true;
input string InpExpectedConfigHash=""; // Optional pin to exporter config hash
input string InpExpectedInstrument=""; // Optional canonical Python instrument ID
bool Fail(string reason) { Print(STRATEGY," bridge rejected: ",reason); return false; }
bool Integer(string s,long &value)
{
   int n=StringLen(s); if(n==0) return false;
   int start=StringGetCharacter(s,0)=='-' ? 1 : 0;
   if(start==n) return false;
   for(int i=start;i<n;i++) if(StringGetCharacter(s,i)<'0' || StringGetCharacter(s,i)>'9') return false;
   value=StringToInteger(s); return true;
}
bool Number(string s,double &value)
{
   int n=StringLen(s),i=0,digits=0;
   if(n==0) return false;
   ushort c=StringGetCharacter(s,0);
   if(c=='-' || c=='+') i++;
   while(i<n && StringGetCharacter(s,i)>='0' && StringGetCharacter(s,i)<='9') { i++; digits++; }
   if(i<n && StringGetCharacter(s,i)=='.')
   {
      i++;
      while(i<n && StringGetCharacter(s,i)>='0' && StringGetCharacter(s,i)<='9') { i++; digits++; }
   }
   if(digits==0) return false;
   if(i<n && (StringGetCharacter(s,i)=='e' || StringGetCharacter(s,i)=='E'))
   {
      i++;
      if(i<n && (StringGetCharacter(s,i)=='-' || StringGetCharacter(s,i)=='+')) i++;
      int exp_digits=0;
      while(i<n && StringGetCharacter(s,i)>='0' && StringGetCharacter(s,i)<='9') { i++; exp_digits++; }
      if(exp_digits==0) return false;
   }
   if(i!=n) return false;
   value=StringToDouble(s); return MathIsValidNumber(value);
}
int Seconds(string tf)
{
   if(tf=="1m") return 60; if(tf=="5m") return 300; if(tf=="15m") return 900;
   if(tf=="30m") return 1800; if(tf=="1H") return 3600; if(tf=="2H") return 7200;
   if(tf=="4H") return 14400; if(tf=="D") return 86400; if(tf=="W") return 604800;
   return 0;
}
bool Validate(string &row[])
{
   if(ArraySize(row)!=26) return Fail("field count differs from contract");
   if(row[0]!="its-mt5-bridge-1" || row[1]!=STRATEGY || row[2]!=EXPECTED_VERSION) return Fail("schema/strategy/version mismatch");
   if(row[3]!=_Symbol) return Fail("feed broker symbol differs from attached chart "+_Symbol);
   if(row[12]=="ERROR") return Fail("Python exporter error: "+row[22]);
   if(row[12]!="SIGNAL" && row[12]!="FLAT" && row[12]!="SCREEN") return Fail("unknown output status");
   if(InpExpectedInstrument!="" && row[4]!=InpExpectedInstrument) return Fail("canonical instrument mismatch");
   if(InpExpectedConfigHash!="" && row[24]!=InpExpectedConfigHash) return Fail("config hash mismatch");
   if(StringLen(row[24])!=64 || StringLen(row[25])!=64) return Fail("missing config/data provenance hash");
   long opened,available,generated,expires,synthetic,replay,signal,xl,xs;
   if(!Integer(row[6],opened) || !Integer(row[7],available) || !Integer(row[8],generated) || !Integer(row[9],expires) || !Integer(row[10],synthetic) || !Integer(row[11],replay) || !Integer(row[13],signal) || !Integer(row[19],xl) || !Integer(row[20],xs)) return Fail("invalid integer fields");
   if((synthetic!=0 && synthetic!=1) || (replay!=0 && replay!=1) || (xl!=0 && xl!=1) || (xs!=0 && xs!=1) || signal<-1 || signal>1) return Fail("invalid direction/flags");
   bool diagnostic=synthetic!=0 || replay!=0;
   if(diagnostic && !InpAllowDiagnosticData) return Fail("synthetic/replay feed blocked; explicit diagnostic display required");
   int step=Seconds(row[5]);
   if(step==0 || available<opened+step || available>generated || expires<=generated) return Fail("invalid availability/expiry timestamps");
   if(!diagnostic)
   {
      long now=(long)TimeGMT();
      if(generated>now+5 || now>expires || now-generated>InpMaxFeedAgeSeconds) return Fail("future/expired/stale feed; refresh Python export");
   }
   if(STRATEGY=="s7_options_volatility" && (row[12]!="SCREEN" || signal!=0)) return Fail("S7 must remain an options research screen");
   if(STRATEGY!="s7_options_volatility" && row[12]=="SCREEN") return Fail("screen supplied to bar strategy");
   if((row[12]=="SIGNAL")!=(signal!=0)) return Fail("status/direction mismatch");
   double entry,stop;
   if(!Number(row[14],entry)) return Fail("invalid entry/reference price");
   if(signal!=0 && (!Number(row[15],stop) || signal*(entry-stop)<=0)) return Fail("invalid stop reference");
   for(int i=15;i<=18;i++) { double v; if(row[i]!="" && !Number(row[i],v)) return Fail("invalid optional numeric field"); }
   if(StringLen(row[23])<2 || StringSubstr(row[23],0,1)!="{") return Fail("missing JSON decision/screen payload");
   return true;
}
int OnStart()
{
   if(InpMaxFeedAgeSeconds<1) { Fail("invalid max feed age"); return 1; }
   int flags=FILE_READ|FILE_TXT|FILE_ANSI|FILE_SHARE_READ;
   if(InpCommonFiles) flags|=FILE_COMMON;
   int file=FileOpen(InpFeedFile,flags,0,CP_UTF8);
   if(file==INVALID_HANDLE) { Fail("feed not found; run this strategy's Python mt5_export first"); return 2; }
   string header=FileReadString(file),line=FileReadString(file);
   bool extra=false;
   while(!FileIsEnding(file)) if(FileReadString(file)!="") extra=true;
   FileClose(file);
   if(header!=EXPECTED_HEADER || extra) { Fail("invalid header or multiple decisions in latest feed"); return 3; }
   string row[]; StringSplit(line,'\t',row);
   if(!Validate(row)) return 4;
   bool diagnostic=row[10]=="1" || row[11]=="1";
   string label=diagnostic ? "DIAGNOSTIC SYNTHETIC/REPLAY" : "RESEARCH SIGNAL";
   Print(STRATEGY," ",label," ",row[12]," instrument=",row[4]," timeframe=",row[5]," side=",row[13]," entry=",row[14]," stop=",row[15]," target=",row[16]," trail=",row[17]," time_stop_bars=",row[18]," exit_long=",row[19]," exit_short=",row[20]," grade=",row[21]);
   Print("Reason: ",row[22]);
   string safe_symbol=_Symbol; StringReplace(safe_symbol,"/","_"); StringReplace(safe_symbol,"\\","_");
   string report=STRATEGY+"_"+safe_symbol+"_latest.json";
   int out=FileOpen(report,FILE_WRITE|FILE_TXT|FILE_ANSI,0,CP_UTF8);
   if(out==INVALID_HANDLE) { Fail("cannot write full JSON report"); return 5; }
   FileWriteString(out,row[23]); FileClose(out);
   Print("Full decision/options legs in MQL5/Files/",report,"; read-only, no size or risk approval");
   if(InpAlert && !diagnostic && (row[12]=="SIGNAL" || row[12]=="SCREEN" || row[19]=="1" || row[20]=="1")) Alert(STRATEGY+" "+_Symbol+" "+row[12]+": "+row[22]);
   return 0;
}

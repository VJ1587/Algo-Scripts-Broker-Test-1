"""Explicit UTC intervals, including overnight sessions and scheduled breaks."""
from dataclasses import dataclass
from pathlib import Path
import pandas as pd
import yaml
from zoneinfo import ZoneInfo

def utc(value):
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        raise ValueError("Session timestamps must have explicit offsets")
    return ts.tz_convert('UTC')

@dataclass(frozen=True)
class Session:
    session_id: str
    start: pd.Timestamp
    end: pd.Timestamp
    breaks: tuple = ()

    def active(self, ts):
        return self.start <= ts < self.end and not any(a <= ts < b for a,b in self.breaks)

class SessionCalendar:
    def __init__(self, sessions, timezone='UTC'):
        ZoneInfo(timezone)
        self.timezone = timezone
        self.sessions = tuple(sorted(sessions, key=lambda s: s.start))
        if len({s.session_id for s in self.sessions}) != len(self.sessions):
            raise ValueError("Duplicate session IDs")
        for i,s in enumerate(self.sessions):
            if s.end <= s.start or (i and s.start < self.sessions[i-1].end):
                raise ValueError("Invalid/overlapping session intervals")
            previous = s.start
            for a,b in s.breaks:
                if not s.start <= a < b <= s.end or a < previous:
                    raise ValueError("Invalid/overlapping break")
                previous = b

    @classmethod
    def load(cls, path):
        raw = yaml.safe_load(Path(path).read_text())
        return cls([Session(str(s['id']), utc(s['start']), utc(s['end']),
                    tuple((utc(b[0]),utc(b[1])) for b in s.get('breaks',())))
                    for s in raw['sessions']], raw['timezone'])

    def session_at(self, ts, include_break=False):
        ts = utc(ts)
        return next((s for s in self.sessions if (s.start <= ts < s.end if include_break else s.active(ts))), None)

    def expected_bar_opens(self, start, end):
        return pd.DatetimeIndex([t for s in self.sessions
            for t in pd.date_range(max(utc(start),s.start),min(utc(end),s.end),freq='min',inclusive='left')
            if s.active(t)])

    def next_close(self, ts):
        s = self.session_at(ts, include_break=True)
        return s.end if s else None

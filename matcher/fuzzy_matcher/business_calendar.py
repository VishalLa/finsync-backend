from __future__ import annotations

from datetime import date, timedelta
from typing import List, Optional, Tuple, Iterable

class BusinessCalendar:
    """
    Business-day arithmetic for reconciliation timing windows.

    lag(a, b)  = signed number of *open* days between two dates:
                > 0  b is later than a   (open days in (a, b])
                < 0  b is earlier than a (minus the open days in (b, a])
                = 0  same day, or only closed days in between

        Fri -> Mon (weekend closed)  : lag  1
        Mon -> Fri                   : lag -1
        Fri -> Sat (1st/3rd Saturday): lag  1

    Defaults follow the RBI rule for Indian banks: every Sunday plus the 2nd and 4th
    Saturday of the month are closed. State / public holidays differ by state, so they
    are supplied by the caller. If your bank ever moves to a five-day week, pass
    closed_saturdays=(1, 2, 3, 4, 5) or use BusinessCalendar.mon_fri().

    UPI / IMPS (and today NEFT / RTGS) settle on holidays, so for items that arrive over
    those rails you may prefer BusinessCalendar.calendar_days(), where lag == calendar days.
    """
    _PAD_DAYS = 366

    def __init__(
        self,
        holidays: Iterable[date] = (),
        closed_weekdays: Iterable[int] = (6,),          # Mon=0 ... Sun=6
        closed_saturdays: Iterable[int] = (2, 4),       # which Saturdays of the month are closed
    ) -> None:
        self._holidays = frozenset(holidays)
        self._closed_weekdays = frozenset(closed_weekdays)
        self._closed_saturdays = frozenset(closed_saturdays)
        self._lo: Optional[int] = None                  # first ordinal covered by the prefix table
        self._cum: List[int] = []                       # _cum[i] = open days in [lo, lo+i]


    # ready-made calendars
    @classmethod
    def india_banks(cls, holidays: Iterable[date] = ()) -> "BusinessCalendar":
        return cls(holidays, closed_weekdays=(6,), closed_saturdays=(2, 4))

    @classmethod
    def mon_fri(cls, holidays: Iterable[date] = ()) -> "BusinessCalendar":
        return cls(holidays, closed_weekdays=(5, 6), closed_saturdays=())

    @classmethod
    def calendar_days(cls) -> "BusinessCalendar":
        return cls((), closed_weekdays=(), closed_saturdays=())


    # basics
    def is_open(self, d: date) -> bool:
        if d in self._holidays:
            return False
        
        wd = d.weekday()
        if wd in self._closed_weekdays:
            return False
        
        if wd == 5 and ((d.day - 1) // 7 + 1) in self._closed_saturdays:
            return False
        
        return True


    def prepare(self, lo: date, hi: date) -> None:
        """Build the prefix table once for the date range you are about to query."""
        self._build(lo.toordinal() - self._PAD_DAYS, hi.toordinal() + self._PAD_DAYS)


    def _build(self, lo_ord: int, hi_ord: int) -> None:
        cum, running = [], 0
        for o in range(lo_ord, hi_ord + 1):
            if self.is_open(date.fromordinal(o)):
                running += 1
            cum.append(running)
        self._lo, self._cum = lo_ord, cum


    def _cum_at(self, d: date) -> int:
        o = d.toordinal()
        
        if self._lo is None:
            self._build(o - self._PAD_DAYS, o + self._PAD_DAYS)
        elif o < self._lo or o >= self._lo + len(self._cum):
            self._build(
                lo_ord=min(o, self._lo) - self._PAD_DAYS,
                hi_ord=max(o, self._lo + len(self._cum) - 1) + self._PAD_DAYS
            )
            
        return self._cum[o - self._lo]


    def lag(self, a: date, b: date) -> int:
        """Signed open-day lag from a to b (see module docstring)."""
        lo, cum = self._lo, self._cum
        if lo is not None:                                  # fast path: both dates inside the prepared range
            ia, ib = a.toordinal() - lo, b.toordinal() - lo
            if 0 <= ia < len(cum) and 0 <= ib < len(cum):
                return cum[ib] - cum[ia]
            
        return self._cum_at(b) - self._cum_at(a)


    # windows -> calendar bounds (for index pre-filtering)
    def shift(self, a: date, n: int) -> date:
        """The n-th open day after a (n >= 0). lag(a, shift(a, n)) == n for n >= 1."""
        d, count = a, 0
        while count < n:
            d += timedelta(days=1)
            if self.is_open(d):
                count += 1
        return d
    

    def _walk_back(self, a: date, k: int) -> date:
        d, count = a, 0
        while count < k:
            d -= timedelta(days=1)
            if self.is_open(d):
                count += 1
        return d


    def date_bounds(self, a: date, min_lag: int, max_lag: int) -> Tuple[date, date]:
        """(lo, hi) such that every date b with min_lag <= lag(a, b) <= max_lag lies in [lo, hi].
        Requires min_lag <= 0 <= max_lag. May include a few extra dates; callers re-check lag."""
        hi = self.shift(a, max_lag + 1)
        early = -min_lag
        lo = self._walk_back(a, early if self.is_open(a) else early + 1)
        return lo, hi


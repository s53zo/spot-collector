"""
RBN-style de-duplication and dwell/limbo handling inspired by DXSpider's RBN logic.

This module keeps a short-lived cache of spots keyed by call + ~0.1 kHz frequency,
waits for multiple skimmers (or dwell-time expiry), scores skimmers by QRG agreement,
and emits a single consolidated spot with a quality tag (Q:n[*][+]).
"""

from __future__ import annotations

import calendar
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Iterable, List, Optional, Tuple


@dataclass
class SpotRecord:
    origin: str
    freq: float
    call: str
    mode: str
    strength: int
    utz: int
    respot: bool = False
    raw_line: str = ""
    qra: str = ""


@dataclass
class SkimmerScore:
    score: int = 1  # bias towards trust on first sight
    good: int = 1
    bad: int = 0
    last_in: float = 0.0
    deviants: Deque[float] = field(default_factory=lambda: deque(maxlen=5))


class RbnAggregator:
    DX_RE = re.compile(
        r"^DX\s+de\s+(?P<origin>[A-Z0-9\-/#]+)\s*:?\s+"
        r"(?P<freq>\d+\.\d{1,3})\s+"
        r"(?P<call>[A-Z0-9\/\-]+)\s+"
        r"(?P<mode>[A-Z0-9]+)\s+"
        r"(?P<snr>\d+)\s*dB"
        r"(?:\s+(?P<rest>.*))?$",
        re.IGNORECASE,
    )

    def __init__(
        self,
        dwell_time: int = 10,
        limbo_time: int = 300,
        respot_time: int = 180,
        cache_time: int = 3600,
        min_quality: int = 2,
        max_quality: int = 9,
        search_khz: int = 5,
        max_deviants: int = 5,
        trace=None,
        inrush_delay: int = 15,
    ) -> None:
        self.dwell_time = dwell_time
        self.limbo_time = limbo_time
        self.respot_time = respot_time
        self.cache_time = cache_time
        self.min_quality = min_quality
        self.max_quality = max_quality
        self.search_khz = search_khz
        self.max_deviants = max_deviants
        self.inrush_delay = inrush_delay

        self.spots: Dict[str, Dict[str, object]] = {}
        self.queue: Dict[str, int] = {}
        self.skimmers: Dict[str, SkimmerScore] = {}
        self._trace_fn = trace
        self._start_time = self._now()

    @staticmethod
    def _now() -> float:
        return time.time()

    def _trace(self, msg: str) -> None:
        if self._trace_fn:
            try:
                self._trace_fn(msg)
            except Exception:
                pass

    @staticmethod
    def _norm_callsign(call: str) -> str:
        s = re.sub(r"[^A-Z0-9/-]", "", call.upper())
        # Trim trailing -# or -7-# while keeping the numeric ssid (e.g., TK0C-7-# -> TK0C-7)
        s = re.sub(r"-(\d+)-#$", r"-\1", s)
        s = re.sub(r"-#$", "", s)
        return s

    @staticmethod
    def _parse_time(rest: str, now: float) -> int:
        """Parse HHMMZ-ish tokens to unix seconds; fallback to now."""
        if not rest:
            return int(now)
        m = re.search(r"\b(\d{4})Z\b", rest.upper())
        if not m:
            return int(now)
        hhmm = m.group(1)
        hh = int(hhmm[:2])
        mm = int(hhmm[2:])
        today = time.gmtime(now)
        utz = int(calendar.timegm((today.tm_year, today.tm_mon, today.tm_mday, hh, mm, 0, 0, 0, 0)))
        if utz > now + 3600:
            utz -= 86400
        return utz

    def _find_existing_key(self, call: str, nqrg: int) -> Tuple[str, Optional[Dict[str, object]]]:
        sp = f"{call}|{nqrg}"
        cand = self.spots.get(sp)
        if cand:
            return sp, cand
        for delta in range(1, self.search_khz + 1):
            up_key = f"{call}|{nqrg + delta}"
            if up_key in self.spots:
                return up_key, self.spots[up_key]
            down_key = f"{call}|{nqrg - delta}"
            if down_key in self.spots:
                return down_key, self.spots[down_key]
        return sp, None

    def _parse_line(self, line: str, now: float) -> Optional[SpotRecord]:
        m = re.match(
            r"^DX\s+de\s+(?P<origin>[A-Z0-9\-/#]+)\s*:?\s+(?P<freq>\d+\.\d{1,3})\s+(?P<call>[A-Z0-9\/\-]+)\s+(?P<tail>.+)$",
            line.strip(),
            re.IGNORECASE,
        )
        if not m:
            self._trace(f"pass non-rbn '{line.strip()}'")
            return None
        origin = self._norm_callsign(m.group("origin"))
        call = self._norm_callsign(m.group("call"))
        try:
            freq = float(m.group("freq"))
        except (TypeError, ValueError):
            return None
        tail = m.group("tail").strip()
        tokens = tail.split()
        if not tokens:
            return None
        modes = {"CW", "RTTY", "RTT", "PSK", "FT8", "FT4", "FT", "RTTY", "RTTY"}
        mode = "CW"
        first = tokens[0].upper()
        if first in modes:
            mode = first
            tokens = tokens[1:]
        if not tokens:
            return None
        snr_token = tokens.pop(0)
        snr_str = re.sub(r"[^0-9]", "", snr_token)
        if not snr_str and tokens:
            snr_token = tokens.pop(0)
            snr_str = re.sub(r"[^0-9]", "", snr_token)
        if not snr_str:
            return None
        strength = int(snr_str)
        if tokens and tokens[0].lower().startswith("db"):
            tokens = tokens[1:]
        rest = " ".join(tokens)
        utz = self._parse_time(rest, now)
        return SpotRecord(origin=origin, freq=freq, call=call, mode=mode, strength=strength, utz=utz, raw_line=line)

    def ingest_line(self, line: str, now: Optional[float] = None) -> List[str]:
        """Feed one textual line; returns zero or more output lines to forward."""
        now = now or self._now()
        record = self._parse_line(line, now)
        if not record:
            return [line]  # pass-through for non-RBN lines

        # Warm-up period to avoid initial inrush flood
        if self.inrush_delay and now - self._start_time < self.inrush_delay:
            return []

        nqrg = int(round(record.freq * 10))
        sp, cand = self._find_existing_key(record.call, nqrg)

        if cand and not cand.get("records"):
            # Cached recently-sent spot; suppress respots inside respot window.
            if now - cand["ctime"] < self.respot_time:
                return []

        respot = False
        if cand:
            if not cand.get("records"):
                respot = True
                cand["ctime"] = now
            records: List[SpotRecord] = cand.get("records", [])  # type: ignore
        else:
            records = []
            cand = {"ctime": now, "cqual": 0, "records": records}
            self.spots[sp] = cand
        record.respot = respot
        records.append(record)
        self.queue[sp] = self.queue.get(sp, 0) + 1

        return self.process_queue(now)

    def _skimmer_key(self, origin: str, freq: float) -> str:
        band = int(freq // 1000)
        return f"{origin}|{band}"

    def process_queue(self, now: Optional[float] = None) -> List[str]:
        now = now or self._now()
        outputs: List[str] = []
        for sp in list(self.queue.keys()):
            cand = self.spots.get(sp)
            if not cand or "ctime" not in cand:
                self.queue.pop(sp, None)
                continue

            ctime = cand["ctime"]
            records: List[SpotRecord] = cand.get("records", [])  # type: ignore
            quality = len(records)
            dwellsecs = now - ctime
            if quality < self.max_quality and dwellsecs < self.dwell_time and dwellsecs < self.limbo_time:
                continue

            if not records:
                self.queue.pop(sp, None)
                self.spots.pop(sp, None)
                continue

            # Collapse duplicate skimmers when enough time/quality is present.
            if quality >= self.min_quality and dwellsecs > self.dwell_time + 1:
                uniq: Dict[str, SpotRecord] = {}
                for rec in records:
                    uniq.setdefault(rec.origin, rec)
                quality = len(uniq)
                records = list(uniq.values())
                cand["records"] = records

            if dwellsecs > self.limbo_time and quality < self.min_quality:
                self.queue.pop(sp, None)
                self.spots.pop(sp, None)
                self._trace(f"drop limbo {sp} q={quality}")
                continue

            if quality < self.min_quality:
                continue

            quality = min(quality, self.max_quality)
            cand["cqual"] = max(cand.get("cqual", 0), quality)

            votes: Dict[float, float] = defaultdict(float)
            for rec in records:
                sk_key = self._skimmer_key(rec.origin, rec.freq)
                score = self.skimmers.setdefault(sk_key, SkimmerScore())
                votes[rec.freq] += max(score.score, 0.1)

            if not votes:
                self.queue.pop(sp, None)
                self.spots.pop(sp, None)
                continue

            consensus_freq, max_vote = 0.0, -1.0
            for freq, vote in votes.items():
                if vote > max_vote:
                    consensus_freq, max_vote = freq, vote

            if consensus_freq <= 0:
                self.queue.pop(sp, None)
                self.spots.pop(sp, None)
                continue

            deviants: List[str] = []
            origins_seen = set()
            for rec in records:
                diff = round(rec.freq - consensus_freq, 1) if len(votes) > 1 else 0.0
                sk_key = self._skimmer_key(rec.origin, rec.freq)
                score = self.skimmers.setdefault(sk_key, SkimmerScore())
                if diff:
                    score.bad = min(score.bad + 1, self.max_deviants)
                    score.good = max(score.good - 1, 0)
                    score.deviants.append(diff)
                    deviants.append(f"{rec.origin}:{diff:+.1f}")
                else:
                    score.good = min(score.good + 1, self.max_deviants)
                    score.bad = max(score.bad - 1, 0)
                    if score.deviants:
                        score.deviants.popleft()
                score.score = score.good - score.bad
                score.last_in = now
                rec.freq = consensus_freq
                origins_seen.add(rec.origin)

            records_sorted = sorted(records, key=lambda r: r.strength)
            best = records_sorted[0]
            quality_tag = f"Q:{cand['cqual']}"
            if len(origins_seen) > 1:
                quality_tag += "*"
            if any(r.respot for r in records):
                quality_tag += "+"

            freq_str = f"{consensus_freq:9.1f}"
            call_str = f"{best.call:<12.12}"
            mode_str = f"{best.mode:<3.3}"
            snr_str = f"{best.strength:>2d}dB"
            origin_out = best.origin.rstrip('-')
            zone_tokens = ["15"]  # local zone contribution
            for o in sorted(origins_seen):
                base = o.rstrip('-#')
                m = re.search(r"-(\d+)$", base)
                if m:
                    token = m.group(1)
                else:
                    # drop non-numeric tokens to avoid polluting Z: with callsigns
                    continue
                if token not in zone_tokens:
                    zone_tokens.append(token)
            zones = ",".join(zone_tokens) if zone_tokens else ""
            time_str = time.strftime("%H%MZ", time.gmtime(best.utz or now))
            origin_fmt = f"{origin_out}-#:"
            # Build aligned fields similar to classic DXSpider formatting
            base = f"DX de {origin_fmt:<10} {freq_str}  {call_str} {mode_str} {snr_str} {quality_tag:<4} Z:{zones}"
            out_line = f"{base:<70}{time_str}"
            outputs.append(out_line)
            trace_needed = bool(deviants)
            if trace_needed:
                # Show only the most deviant contributors (sorted by absolute diff desc, limit 5)
                self._trace(f"emit {out_line} deviants={','.join(deviants)}")
                # Build diffs per record for sorting
                scored_records = []
                for r in records:
                    diff = round(r.freq - consensus_freq, 1)
                    scored_records.append((abs(diff), diff, r))
                scored_records.sort(reverse=True, key=lambda x: x[0])
                for _, diff, r in scored_records[:5]:
                    if r.raw_line:
                        self._trace(f"  src diff={diff:+.1f} {r.raw_line}")
                    else:
                        self._trace(f"  src diff={diff:+.1f} {r.origin} {r.call} {r.freq:.1f} {r.mode} {r.strength}dB")

            # Cache for respot suppression at the consensus key.
            new_key = f"{best.call}|{int(round(consensus_freq * 10))}"
            self.spots[new_key] = {"ctime": now, "cqual": cand["cqual"], "records": []}
            self.queue.pop(sp, None)
            if new_key != sp:
                self.spots.pop(sp, None)

        # Drop stale cache entries.
        for k in list(self.spots.keys()):
            cand = self.spots[k]
            if now - cand.get("ctime", now) > self.cache_time:
                self.spots.pop(k, None)
                self.queue.pop(k, None)
        for k in list(self.skimmers.keys()):
            score = self.skimmers[k]
            if score.last_in and now - score.last_in > self.cache_time:
                self.skimmers.pop(k, None)

        return outputs

    def flush_all(self) -> List[str]:
        """Force a flush without waiting for dwell timers (useful on shutdown)."""
        return self.process_queue(self._now())

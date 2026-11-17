"""Bounded station-local spot selection, without rewriting source comments.

GoCluster is an operational frequency reference, not an independent RF standard.
Only original local measurements enter offset estimation. A report seen through
another route is not another receiver, and opaque upstream support is never
converted into votes.
"""
import calendar
from collections import Counter, OrderedDict
from dataclasses import dataclass, replace
import heapq
import hashlib
import math
import re
from statistics import median
import time


DEFAULTS = {
    'enabled': True, 'repeat_seconds': 180, 'match_tolerance_hz': 500,
    'move_tolerance_hz': 500, 'max_changes': 2, 'max_age_seconds': 300,
    'future_tolerance_seconds': 120, 'reference_ttl_seconds': 120,
    'pair_window_seconds': 30, 'calibration_window_seconds': 1800,
    'calibration_min_calls': 5, 'calibration_min_span_hz': 10000,
    'calibration_min_span_seconds': 30,
    'calibration_max_offset_hz': 1000, 'calibration_max_scatter_hz': 30,
    'calibration_max_slope_ppm': 5, 'calibration_uncertainty_hz': 50,
    'corroboration_seconds': 10, 'corroboration_tolerance_hz': 100,
    'calibration_match_tolerance_hz': 1000, 'calibration_retry_batch': 16,
    'local_call_hold_seconds': 3, 'local_call_tolerance_hz': 200,
    'max_entries': 4096,
}
_HEADER = re.compile(
    rb'^DX\s+de\s+(?P<origin>[A-Z0-9/#@-]+):\s*'
    rb'(?P<freq>\d+(?:\.\d{1,6})?)\s+'
    rb'(?P<call>[A-Z0-9/-]+)(?P<gap> +)(?P<tail>.*)$', re.I)
_STAMP = re.compile(rb'(?:^|\s)([0-2][0-9])([0-5][0-9])Z(?:\s+[A-Ra-r]{2}[0-9]{2}(?:[A-Xa-x]{2}(?:[0-9]{2})?)?)?\s*$')
_REPORT = re.compile(rb'^([+-]?\d+)\s*dB(?:\s|$)', re.I)
_MODES = {'CW', 'RTTY', 'RTT', 'FT8', 'FT4', 'FT2', 'PSK', 'USB', 'LSB', 'SSB'}
_BANDS = ((1800, 2000, '160M'), (3500, 4000, '80M'), (5330, 5410, '60M'),
          (7000, 7300, '40M'), (10100, 10150, '30M'), (14000, 14350, '20M'),
          (18068, 18168, '17M'), (21000, 21450, '15M'), (24890, 24990, '12M'),
          (28000, 29700, '10M'), (50000, 54000, '6M'))


def receiver_identity(value):
    """Strip transport markers only; numeric SSIDs identify distinct receivers."""
    return re.sub(r'-(?:#|@)$', '', value.upper())


@dataclass(frozen=True)
class ParsedSpot:
    raw: bytes
    origin: str
    call: str
    frequency_hz: float
    band: str
    mode: str
    report: object
    observed_at: object
    confidence: str
    freq_span: tuple
    call_span: tuple
    receiver_base: str = ''

    def __post_init__(self):
        # A property of this immutable record, not a process-global identity cache.
        object.__setattr__(self, 'receiver_base', re.sub(r'-\d+$', '', receiver_identity(self.origin)))

    def render(self, frequency_hz=None, call=None):
        """Replace header fields only. The complete original suffix is opaque."""
        changes = []
        if frequency_hz is not None and abs(frequency_hz - self.frequency_hz) >= 0.005:
            frequency = ('%.2f' % (frequency_hz / 1000)).encode('ascii')
            changes.append((self.freq_span, frequency))
        if call is not None and call != self.call:
            changes.append((self.call_span, call.encode('ascii')))
        result = self.raw
        for (start, end), replacement in sorted(changes, reverse=True):
            # For padded layouts preserve the suffix column when a new call
            # fits in the old field. Never truncate either call or suffix.
            if (start, end) == self.call_span:
                gap_end = end
                while gap_end < len(result) and result[gap_end:gap_end + 1] == b' ':
                    gap_end += 1
                width = gap_end - start
                if len(replacement) < width:
                    replacement += b' ' * (width - len(replacement))
                    end = gap_end
            result = result[:start] + replacement + result[end:]
        return result


def parse_spot(line, now, role='cluster'):
    """Parse known ASCII headers; unknown lines remain byte-preserving traffic."""
    match = _HEADER.match(line)
    if not match:
        return None
    frequency = float(match.group('freq'))
    if not math.isfinite(frequency) or frequency <= 0:
        return None
    band = next((name for lo, hi, name in _BANDS if lo <= frequency <= hi), None)
    if band is None:
        return None
    call = match.group('call').decode('ascii').upper()
    if not re.search('[A-Z]', call) or not re.search('[0-9]', call):
        return None
    tail = match.group('tail')
    parts = tail.split(None, 1)
    token = parts[0].decode('ascii', errors='replace').upper() if parts else ''
    mode = token if token in _MODES else 'UNKNOWN'
    mode = 'RTTY' if mode == 'RTT' else mode
    report = None
    if mode != 'UNKNOWN' and len(parts) == 2:
        report_match = _REPORT.match(parts[1])
        if report_match:
            value = int(report_match.group(1))
            if -200 <= value <= 200:
                report = value
    observed = None
    stamp = _STAMP.search(tail)
    if stamp and int(stamp.group(1)) < 24:
        date = time.gmtime(now)
        base = calendar.timegm((date.tm_year, date.tm_mon, date.tm_mday,
                                int(stamp.group(1)), int(stamp.group(2)), 0))
        observed = min((base - 86400, base, base + 86400), key=lambda x: abs(x - now))
    confidence = ''
    # GC's mode begins at column 40; confidence/time are anchored to the end,
    # independently of the configured line length. Never interpret a prose C.
    if role == 'gocluster' and len(line) >= 65 and match.start('tail') == 39:
        conf = re.search(rb' ([?SPVCB]) ([0-2][0-9][0-5][0-9]Z)$', line)
        if conf:
            confidence = conf.group(1).decode('ascii')
    return ParsedSpot(line, receiver_identity(match.group('origin').decode('ascii')),
                      call, frequency * 1000, band, mode, report, observed,
                      confidence, match.span('freq'), match.span('call'))


@dataclass
class Observation:
    spot: ParsedSpot
    source: str
    at: float
    role: str = 'cluster'


@dataclass
class Repeat:
    at: float
    observed: object
    frequency: float
    changes: int = 0
    generation: float = -1


@dataclass
class LocalCallHold:
    observation: Observation
    deadline: float
    force_original: bool = False


class _IndexedStore(OrderedDict):
    """Entry-owned indexes and bounded expiry metadata for one retained store.

    Index memberships are removed on overwrite, expiry, eviction, reset and
    explicit clear/pop. Lazy heap records never retain values; periodic rebuilds
    cap stale records even when a single hot key is overwritten indefinitely.
    """
    def __init__(self, limit, ttl, timestamp, groups=None, inclusive=False):
        super().__init__()
        self.limit = limit
        self.ttl = ttl
        self.timestamp = timestamp
        self.groupers = groups or {}
        self.indexes = {name: {} for name in self.groupers}
        self.expiry = []
        self.versions = {}
        self.serial = 0
        self.inclusive = inclusive

    def _memberships(self, key, value):
        for name, grouper in self.groupers.items():
            yield name, grouper(key, value)

    def _unindex(self, key, value):
        for name, group in self._memberships(key, value):
            members = self.indexes[name][group]
            del members[key]
            if not members:
                del self.indexes[name][group]

    def __setitem__(self, key, value):
        if key in self:
            self._unindex(key, self[key])
        super().__setitem__(key, value)
        for name, group in self._memberships(key, value):
            self.indexes[name].setdefault(group, {})[key] = None
        self.serial += 1
        self.versions[key] = self.serial
        heapq.heappush(self.expiry, (self.timestamp(value) + self.ttl, self.serial, key))
        if len(self.expiry) > 2 * self.limit + 64:
            self.expiry = [(self.timestamp(value) + self.ttl, self.versions[key], key)
                           for key, value in self.items()]
            heapq.heapify(self.expiry)

    def __delitem__(self, key):
        self._unindex(key, self[key])
        del self.versions[key]
        super().__delitem__(key)

    def pop(self, key, *default):
        if key not in self:
            if default:
                return default[0]
            raise KeyError(key)
        value = self[key]
        del self[key]
        return value

    def popitem(self, last=True):
        key = next(reversed(self) if last else iter(self))
        return key, self.pop(key)

    def clear(self):
        super().clear()
        for index in self.indexes.values():
            index.clear()
        self.versions.clear()
        self.expiry.clear()

    def move_to_end(self, key, last=True):
        super().move_to_end(key, last)
        for name, group in self._memberships(key, self[key]):
            members = self.indexes[name][group]
            del members[key]
            if last:
                members[key] = None
            else:
                # This path is not used by ordinary admission, but preserves
                # dictionary-like ordering for callers using OrderedDict's API.
                self.indexes[name][group] = dict([(key, None)] + list(members.items()))

    def keys_for(self, name, group):
        return self.indexes[name].get(group, {})

    def values_for(self, name, group):
        return (self[key] for key in self.keys_for(name, group))

    def prune(self, now, backwards=False):
        if backwards:
            for key, value in list(self.items()):
                if self.timestamp(value) > now:
                    del self[key]
        while self.expiry:
            deadline, version, key = self.expiry[0]
            if deadline > now or (deadline == now and not self.inclusive):
                break
            heapq.heappop(self.expiry)
            if self.versions.get(key) == version:
                del self[key]


class SpotProcessor:
    """Single-event-loop owner. All retained collections have hard entry caps."""
    def __init__(self, config=None, clock=time.time):
        self.config = dict(DEFAULTS)
        if config is None:
            config = {}
        if not isinstance(config, dict):
            raise ValueError('spot_processing must be an object')
        unknown = set(config) - set(DEFAULTS)
        if unknown:
            raise ValueError('Unknown spot_processing setting: ' + ', '.join(sorted(unknown)))
        self.config.update(config)
        for key, value in self.config.items():
            if key == 'enabled':
                if not isinstance(value, bool):
                    raise ValueError('spot_processing.enabled must be boolean')
            elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError('spot_processing.%s must be finite numeric' % key)
            elif value < 0 or (value == 0 and key not in ('max_changes', 'future_tolerance_seconds')):
                raise ValueError('spot_processing.%s must be positive' % key)
        for key in ('max_entries', 'max_changes', 'calibration_min_calls', 'calibration_retry_batch'):
            if not isinstance(self.config[key], int):
                raise ValueError('spot_processing.%s must be integer' % key)
        if self.config['calibration_min_calls'] < 3:
            raise ValueError('calibration_min_calls must be at least three')
        self.clock = clock
        self.counts = Counter()
        self.last_evaluation = None
        limit = self.config['max_entries']
        self._frequency_bin_hz = self.config['match_tolerance_hz']
        observation_groups = {
            'owner': lambda key, obs: (obs.source, obs.spot.origin, obs.spot.band),
            'identity': lambda key, obs: (obs.spot.call, obs.spot.band, obs.spot.mode),
            'source': lambda key, obs: obs.source,
            'frequency': lambda key, obs: (obs.spot.band, obs.spot.mode,
                                           int(obs.spot.frequency_hz // self._frequency_bin_hz)),
        }
        self.references = _IndexedStore(limit, self.config['reference_ttl_seconds'],
                                        lambda obs: obs.at, observation_groups)
        self.locals = _IndexedStore(limit, self.config['pair_window_seconds'],
                                   lambda obs: obs.at, observation_groups)
        self.samples = _IndexedStore(limit, self.config['calibration_window_seconds'],
                                     lambda sample: sample[0], {
            'owner': lambda key, sample: key[:3],
            'source': lambda key, sample: key[0],
            'reference': lambda key, sample: sample[4],
        })
        observation_groups['owner'] = lambda key, obs: (obs.source, obs.spot.origin, obs.spot.band)
        self.measurements = _IndexedStore(limit, self.config['pair_window_seconds'],
                                         lambda obs: obs.at, observation_groups)
        self.votes = _IndexedStore(limit, self.config['corroboration_seconds'],
                                  lambda obs: obs.at, observation_groups, inclusive=True)
        self.pending = _IndexedStore(limit, self.config['corroboration_seconds'],
                                    lambda obs: obs.at, observation_groups, inclusive=True)
        self.calibration_retry = _IndexedStore(limit, self.config['pair_window_seconds'], lambda value: value[0])
        self.rejection = _IndexedStore(limit, self.config['reference_ttl_seconds'],
                                      lambda value: value[0],
                                      {'source': lambda key, value: key[0]})
        presentation_groups = {'episode': lambda key, value: (key[0], key[2])}
        self.repeats = _IndexedStore(limit, self.config['repeat_seconds'],
                                     lambda repeat: repeat.at, presentation_groups, inclusive=True)
        self.local_origins = _IndexedStore(limit, self.config['calibration_window_seconds'],
                                           lambda at: at,
                                           {'base': lambda key, value: re.sub(r'-\d+$', '', key)})
        latest_ttl = self.config['max_age_seconds'] + self.config['future_tolerance_seconds'] + 60
        self.latest = _IndexedStore(limit, latest_ttl, lambda obs: obs.at, presentation_groups)
        self._last_prune = None
        self.local_call_holds = OrderedDict()
        self._completed_local_holds = []

    def _put(self, store, key, value):
        store[key] = value
        store.move_to_end(key)
        while len(store) > self.config['max_entries']:
            store.popitem(last=False)
            self.counts['state_evictions'] += 1

    def _prune(self, now):
        backwards = self._last_prune is not None and now < self._last_prune
        for store in (self.references, self.locals, self.samples, self.repeats,
                      self.local_origins, self.latest, self.measurements, self.votes, self.rejection, self.calibration_retry):
            store.prune(now, backwards)
        before = len(self.pending)
        self.pending.prune(now, backwards)
        self.counts['expired_moves'] += before - len(self.pending)
        if backwards:
            for key, sample in list(self.samples.items()):
                if sample[6] > now:
                    del self.samples[key]
            for hold in self.local_call_holds.values():
                hold.force_original = True
                hold.deadline = now
        self._last_prune = now

    def reset_source(self, source_id):
        """Connection epochs invalidate their measurements and dependent alignment."""
        for store in (self.references, self.locals, self.measurements, self.votes,
                      self.pending, self.rejection):
            for key in list(store.keys_for('source', source_id)):
                del store[key]
        for key, sample in list(self.samples.items()):
            if key[0] == source_id or any(item[1] == source_id for item in sample[5]):
                del self.samples[key]
        self.calibration_retry.clear()
        for hold in self.local_call_holds.values():
            if hold.observation.source == source_id:
                hold.force_original = True
        self.counts['source_resets'] += 1

    def snapshot(self):
        self._prune(self.clock())
        result = dict(self.counts)
        result.update(reference_entries=len(self.references), local_entries=len(self.locals),
                      calibration_samples=len(self.samples), repeat_entries=len(self.repeats),
                      local_receivers=len(self.local_origins), latest_entries=len(self.latest),
                      current_pending_moves=len(self.pending), receiver_observations=len(self.measurements),
                      current_local_call_holds=len(self.local_call_holds))
        return result

    def alignment_snapshot(self, source_ids=None):
        """Per receiver, band and input route; never pool one public RBN endpoint."""
        now = self.clock()
        self._prune(now)
        wanted = set(source_ids) if source_ids is not None else None
        owners = set(self.measurements.indexes['owner']) | set(self.samples.indexes['owner'])
        if wanted is not None:
            owners = {owner for owner in owners if owner[0] in wanted}
        rows = []
        band_order = {name: index for index, (_, _, name) in enumerate(_BANDS)}
        for source, receiver, band in sorted(owners, key=lambda owner:(owner[0],owner[1],band_order[owner[2]])):
            owner = (source, receiver, band)
            retained = list(self.samples.values_for('owner', owner))
            usable = [sample for sample in retained if now - sample[0] <= self.config['reference_ttl_seconds']]
            offset = self._offset(source, band, now, receiver)
            if offset is not None:
                state = 'Ready'
            elif retained and not usable:
                state = 'Expired'
            elif len(usable) < self.config['calibration_min_calls']:
                state = 'Collecting'
            elif (max(x[0] for x in usable) - min(x[0] for x in usable) < self.config['calibration_min_span_seconds'] or
                  max(x[1] for x in usable) - min(x[1] for x in usable) < self.config['calibration_min_span_hz']):
                state = 'Checking'
            else:
                state = 'Unsettled'
            rejected = self.rejection.get(owner)
            rows.append(dict(source=source, receiver=receiver, band=band, state=state,
                             samples=len(usable), retained_samples=len(retained), excluded_samples=len(retained)-len(usable),
                             required=self.config['calibration_min_calls'],
                             offset_hz=-offset if offset is not None else None,
                             last_match_age_seconds=now-max(x[0] for x in retained) if retained else None,
                             rejection_reason=('' if offset is not None else 'insufficient-time/frequency-span' if state == 'Checking'
                                               else 'scatter-or-scale' if state == 'Unsettled' else 'reference-expired' if state == 'Expired'
                                               else rejected[1] if rejected else 'insufficient-calls')))
        if wanted is not None:
            present = {row['source'] for row in rows}
            for source in sorted(wanted-present):
                rows.append(dict(source=source,receiver='--',band='--',state='No data',samples=0,
                                 retained_samples=0,excluded_samples=0,required=self.config['calibration_min_calls'],
                                 offset_hz=None,last_match_age_seconds=None,rejection_reason='no-observations'))
        return rows

    def _nearby(self, store, spot, tolerance_hz=None):
        """Capture only potentially comparable raw-frequency buckets.

        Preserve admission order when equal timestamp candidates tie, matching
        the original ordered population without traversing unrelated signals.
        """
        bucket = int(spot.frequency_hz // self._frequency_bin_hz)
        tolerance = self.config['match_tolerance_hz'] if tolerance_hz is None else tolerance_hz
        radius = int(math.ceil(tolerance / self._frequency_bin_hz))
        keys = []
        for index in range(bucket - radius, bucket + radius + 1):
            keys.extend(store.keys_for('frequency', (spot.band, spot.mode, index)))
        keys.sort(key=store.versions.__getitem__)
        return (store[key] for key in keys)

    def _local_reference(self, origin):
        # Negative eligibility may conservatively pool aliases: GoCluster often
        # strips numeric SSIDs. Never use this base identity as a positive vote.
        base = re.sub(r'-\d+$', '', origin)
        return bool(self.local_origins.keys_for('base', base))

    @staticmethod
    def _receiver_base(origin):
        # Conservative mirror exclusion; different SSIDs are not positive votes.
        return re.sub(r'-\d+$', '', receiver_identity(origin))

    def _eligible_reference(self, local, reference):
        a, b = local.spot, reference.spot
        return (a.receiver_base != b.receiver_base and
                a.call == b.call and a.band == b.band and a.mode == b.mode and a.mode != 'UNKNOWN' and
                a.observed_at is not None and b.observed_at is not None and
                abs(a.observed_at-b.observed_at) <= self.config['pair_window_seconds'] and
                abs(local.at-reference.at) <= self.config['pair_window_seconds'] and
                abs(a.frequency_hz-b.frequency_hz) <= self.config['calibration_match_tolerance_hz'])

    def _groups(self, observations, now, subject=None, adjusted=False, window_filter=None):
        """Max-spread groups with one latest original observation per receiver."""
        latest = {}
        for obs in observations:
            spot = obs.spot
            if spot.mode == 'UNKNOWN' or spot.observed_at is None:
                continue
            base = spot.receiver_base
            if subject is not None and not self._eligible_reference(subject, obs):
                continue
            previous = latest.get(base)
            if previous is None or (spot.observed_at, obs.at, obs.role == 'gocluster') > (previous.spot.observed_at, previous.at, previous.role == 'gocluster'):
                latest[base] = obs
        values = []
        for base, obs in latest.items():
            frequency = obs.spot.frequency_hz
            if adjusted and obs.role in ('local', 'rbn', 'gocluster'):
                offset = self._offset(obs.source, obs.spot.band, now, obs.spot.origin)
                if offset is not None:
                    frequency -= offset
            values.append((frequency, base, obs))
        values.sort(key=lambda value: (value[0], value[1]))
        tolerance = self.config['corroboration_tolerance_hz']
        groups = []
        seen = set()
        for stamp in sorted({value[2].spot.observed_at for value in values}):
            eligible = [value for value in values if abs(value[2].spot.observed_at-stamp) <= self.config['pair_window_seconds']]
            right, prior_right = 0, -1
            gc_heap, time_heap = [], []
            summaries = {}
            for left, first in enumerate(eligible):
                right = max(right, left)
                while right < len(eligible) and eligible[right][0]-first[0] <= tolerance:
                    value = eligible[right]
                    heapq.heappush(time_heap, (-value[2].spot.observed_at, right))
                    if value[2].role == 'gocluster':
                        heapq.heappush(gc_heap, (-value[2].spot.observed_at, -value[2].at, right))
                    right += 1
                while gc_heap and gc_heap[0][2] < left:
                    heapq.heappop(gc_heap)
                while time_heap and time_heap[0][1] < left:
                    heapq.heappop(time_heap)
                if right == prior_right or right-left < 2:
                    continue
                prior_right = right
                has_gc = bool(gc_heap)
                count = right-left
                if has_gc:
                    frequency = eligible[gc_heap[0][2]][0]
                else:
                    middle = left+count//2
                    frequency = eligible[middle][0] if count%2 else (eligible[middle-1][0]+eligible[middle][0])/2
                descriptor = (left,right,frequency,-time_heap[0][0])
                if window_filter is not None and not window_filter(frequency,descriptor[3]):
                    continue
                bucket = summaries.setdefault(has_gc,{})
                if 'minimum' not in bucket or frequency < bucket['minimum'][2]:
                    bucket['minimum'] = descriptor
                if 'maximum' not in bucket or frequency > bucket['maximum'][2]:
                    bucket['maximum'] = descriptor
                if 'best' not in bucket or (count,descriptor[3]) > (bucket['best'][1]-bucket['best'][0],bucket['best'][3]):
                    bucket['best'] = descriptor
            # Selection needs only each priority class's frequency extremes
            # (ambiguity) and best support/newest window. Materializing every
            # overlapping window would retain quadratic receiver memberships.
            descriptors = sorted({descriptor for bucket in summaries.values() for descriptor in bucket.values()})
            for left,right,frequency,observed in descriptors:
                members=eligible[left:right]
                identities=frozenset(value[1] for value in members)
                if identities in seen:
                    continue
                seen.add(identities)
                groups.append(dict(ids=identities,members=members,
                                   gc=any(value[2].role=='gocluster' for value in members),
                                   frequency=frequency,observed=observed))
        return groups

    def _choose_group(self, groups):
        if not groups:
            return None
        preferred = [group for group in groups if group['gc']] or groups
        frequencies = [group['frequency'] for group in preferred]
        if max(frequencies)-min(frequencies) > self.config['corroboration_tolerance_hz']:
            return None
        return max(preferred, key=lambda group: (len(group['ids']), group['observed']))

    def _learn_population(self, observations, now, current=None):
        # Original, not adjusted, records supply all training references.
        # One identity population is bounded by max_entries; no global scan.
        base_groups = self._groups(observations, now)
        # Repeated packets with unchanged reference authority need only update
        # their own receiver. When groups change, revisit waiting receivers in
        # bounded rotating batches rather than rebuilding every subject's
        # reference population on every input.
        episode = (current.spot.call, current.spot.band, current.spot.mode)
        digest = hashlib.sha256()
        for group in base_groups:
            digest.update(repr((group['frequency'],group['gc'],min(len(group['ids']),4),
                                tuple(sorted(group['ids'])) if len(group['ids']) <= 3 else (),
                                group['observed'])).encode('ascii'))
        signature = digest.digest()
        prior = self.calibration_retry.get(episode)
        cursor = prior[2] if prior and prior[1] == signature else None
        active = bool(base_groups)
        waiting = [obs for obs in observations if obs is not current and obs.role in ('local','rbn','gocluster') and
                   (obs.source,obs.spot.origin,obs.spot.band,obs.spot.call) not in self.samples]
        waiting.sort(key=lambda obs:(obs.source,obs.spot.origin))
        selected = []
        if active and waiting:
            # Resume after a stable receiver identity, not a shrinking list's
            # numeric position: successful learners disappear from waiting.
            tail = [obs for obs in waiting if cursor is None or (obs.source,obs.spot.origin) > cursor]
            if not tail:
                tail = waiting
            selected = tail[:self.config['calibration_retry_batch']]
            cursor = (selected[-1].source, selected[-1].spot.origin)
        else:
            active = False
        self._put(self.calibration_retry, episode, (now,signature,cursor,active))
        subjects = ([current] if current is not None else []) + selected
        for subject in subjects:
            if subject.role not in ('local', 'rbn', 'gocluster'):
                continue
            groups = self._groups(observations, now, subject=subject)
            group = self._choose_group(groups)
            owner = (subject.source, subject.spot.origin, subject.spot.band)
            if group is None:
                reason = 'ambiguous-reference' if groups else 'missing-reference'
                if not groups:
                    others = [obs for obs in observations if obs.spot.receiver_base != subject.spot.receiver_base]
                    if not others and len(observations) > 1:
                        reason = 'self'
                    elif len(others) >= 2:
                        timely = [obs for obs in others if obs.spot.observed_at is not None and subject.spot.observed_at is not None and
                                  abs(obs.spot.observed_at-subject.spot.observed_at) <= self.config['pair_window_seconds']]
                        if len(timely) < 2:
                            reason = 'time'
                        elif sum(abs(obs.spot.frequency_hz-subject.spot.frequency_hz) <= self.config['calibration_match_tolerance_hz'] for obs in timely) < 2:
                            reason = 'frequency'
                self._put(self.rejection, owner, (now, reason))
                self.counts['alignment_rejected_' + reason.replace('-', '_')] += 1
                continue
            residual = subject.spot.frequency_hz-group['frequency']
            if abs(residual) > self.config['calibration_max_offset_hz']:
                self._put(self.rejection, owner, (now, 'frequency'))
                self.counts['alignment_rejected_frequency'] += 1
                continue
            refs = tuple(sorted((value[1], value[2].source) for value in group['members']))
            key = owner + (subject.spot.call,)
            authority_at = max([subject.at] + [value[2].at for value in group['members']])
            self._put(self.samples, key, (subject.at, subject.spot.frequency_hz, residual,
                                         ','.join(value[0] for value in refs), refs[0][1], refs, authority_at))

    def _offset(self, source, band, now, receiver=None, before=None):
        if receiver is None:
            owners = [owner for owner in self.samples.indexes['owner'] if owner[0] == source and owner[2] == band]
            if len(owners) != 1:
                return None
            receiver = owners[0][1]
        samples = [sample for sample in self.samples.values_for('owner', (source, receiver, band))
                   if now-sample[0] <= self.config['reference_ttl_seconds'] and
                   (before is None or sample[0] < before and sample[6] < before)]
        if len(samples) < self.config['calibration_min_calls']:
            return None
        if max(x[0] for x in samples)-min(x[0] for x in samples) < self.config['calibration_min_span_seconds']:
            return None
        frequencies = [sample[1] for sample in samples]
        if max(frequencies)-min(frequencies) < self.config['calibration_min_span_hz']:
            return None
        center = median(sample[2] for sample in samples)
        uncertainty = self.config['calibration_uncertainty_hz']
        if max(abs(sample[2]-center) for sample in samples) > self.config['calibration_max_scatter_hz']+uncertainty:
            return None
        low, high = min(samples,key=lambda x:x[1]), max(samples,key=lambda x:x[1])
        slope = max(0,abs(high[2]-low[2])-2*uncertainty)/(high[1]-low[1])*1e6
        if slope > self.config['calibration_max_slope_ppm']:
            return None
        return center

    def _movement(self, obs, call, frequency, previous, now):
        identity = (call, obs.spot.band, obs.spot.mode)
        votes = [self.votes[key] for key in self.votes.keys_for('identity', identity)
                 if self.votes.versions[key] > previous.generation]
        groups = self._groups(votes, now, adjusted=True,
                              window_filter=lambda frequency,observed:
                              abs(frequency-previous.frequency) > self.config['move_tolerance_hz'] and
                              observed >= previous.observed)
        group = self._choose_group(groups)
        if groups and group is None:
            self.counts['move_conflicts'] += 1
            return None, 'corroboration-conflict', 0, None
        if group is None:
            pending_key = (call, obs.spot.band, obs.spot.mode,
                           int(round(frequency/self.config['corroboration_tolerance_hz'])))
            if pending_key not in self.pending:
                self._put(self.pending, pending_key, obs)
                self.counts['pending_moves'] += 1
            support = 1 if obs.spot.mode != 'UNKNOWN' and obs.spot.observed_at is not None else 0
            return None, 'pending-corroboration', support, None
        if abs(group['frequency']-frequency) > self.config['corroboration_tolerance_hz']:
            # Do not render a different proposal using an unrelated triggering line.
            self.counts['move_conflicts'] += 1
            return None, 'corroboration-conflict', len(group['ids']), 'gocluster' if group['gc'] else 'raw'
        return group['frequency'], 'frequency-move', len(group['ids']), 'gocluster' if group['gc'] else 'raw'

    def _local_call_decision(self, observation, frequency, now, before=None):
        """Return unique corroborated identity, or a reason for safe non-mutation.

        Receiver mirrors and direct local observations cannot validate a local
        decoder. Portable forms are deliberately opaque in this first policy.
        """
        local = observation.spot
        if (local.mode != 'CW' or len(local.call) <= 3 or '/' in local.call or
                local.observed_at is None):
            return None, 'local-call-ineligible', 0, None
        query = replace(local, frequency_hz=frequency)
        tolerance = self.config['local_call_tolerance_hz']
        by_call = {}
        nearby = list(self._nearby(self.measurements, query, tolerance))
        nearby.extend(self._nearby(self.measurements, replace(query, mode='UNKNOWN'), tolerance))
        for ref in nearby:
            other = ref.spot
            if (ref.role not in ('cluster', 'rbn', 'gocluster') or
                    other.receiver_base == local.receiver_base or self._local_reference(other.origin) or
                    other.observed_at is None or (before is not None and ref.at >= before) or now-ref.at < 0 or
                    now-ref.at > self.config['pair_window_seconds'] or
                    abs(other.observed_at-local.observed_at) > self.config['pair_window_seconds'] or
                    abs(other.frequency_hz-frequency) > tolerance):
                continue
            if other.call == local.call:
                return None, 'local-call-original-supported', 1, other.call
            if other.mode == 'CW' and len(other.call) > 3 and '/' not in other.call and not (
                    ref.role == 'gocluster' and len(other.call) >= 10) and _edit_one(local.call, other.call):
                by_call.setdefault(other.call, []).append(ref)
        confirmed = []
        strongest = 0
        for call, refs in by_call.items():
            group = self._choose_group(self._groups(refs, now))
            aliases = {ref.spot.receiver_base for ref in refs}
            strongest = max(strongest, len(aliases))
            if group is not None and abs(group['frequency']-frequency) <= tolerance:
                confirmed.append((call, group))
        if len(confirmed) == 1:
            call, group = confirmed[0]
            return group, 'local-call-corrected', len(group['ids']), call
        if len(confirmed) > 1:
            return None, 'pending-local-call-conflict', strongest, None
        if by_call:
            return None, 'pending-local-call', strongest, next(iter(by_call)) if len(by_call) == 1 else None
        return None, 'local-call-no-candidate', 0, None

    @staticmethod
    def _local_hold_key(observation):
        spot = observation.spot
        return observation.source, spot.receiver_base, spot.call, spot.band, spot.mode

    def _release_local_hold(self, hold, now):
        obs, spot = hold.observation, hold.observation.spot
        due = now >= hold.deadline
        cutoff = hold.deadline if due else None
        before = dict(self.counts)
        self.counts['local_call_released'] += 1
        if spot.observed_at is not None and (
                now-spot.observed_at > self.config['max_age_seconds'] or
                spot.observed_at-now > self.config['future_tolerance_seconds']):
            self.counts['stale_spots'] += 1
            if due:
                self.counts['local_call_expired'] += 1
            self.last_evaluation = dict(call=spot.call, frequency_hz=spot.frequency_hz,
                                       frequency_basis='input', reason='local-call-stale', held=False)
            outputs = []
        else:
            frequency = spot.frequency_hz
            offset = self._offset(obs.source, spot.band, now, spot.origin, before=cutoff)
            if offset is not None:
                frequency -= offset
            group, reason, _, _ = self._local_call_decision(obs, frequency, now, before=cutoff)
            force_original = hold.force_original or due and group is None
            if due and force_original:
                self.counts['local_call_expired'] += 1
            outputs = self._select_output(obs, now, allow_hold=False,
                                          force_original=force_original, reference_before=cutoff)
            if self.last_evaluation.get('call') == spot.call:
                self.last_evaluation['reason'] = ('local-call-reset' if hold.force_original
                                                   else 'local-call-timeout' if due else reason)
        delta = {counter: value-before.get(counter, 0) for counter, value in self.counts.items()}
        return dict(source=obs.source, input=spot.raw, outputs=outputs,
                    evaluation=dict(self.last_evaluation), delta=delta)

    def drain_local_holds(self, force=False):
        """Release qualified/due work without admitting its observation again."""
        now = self.clock()
        self._prune(now)
        events, self._completed_local_holds = self._completed_local_holds, []
        for key, hold in list(self.local_call_holds.items()):
            obs = hold.observation
            frequency = obs.spot.frequency_hz
            offset = self._offset(obs.source, obs.spot.band, now, obs.spot.origin)
            if offset is not None:
                frequency -= offset
            _, reason, _, _ = self._local_call_decision(obs, frequency, now)
            if not (force or hold.force_original or now >= hold.deadline or
                    not reason.startswith('pending-local-call')):
                continue
            del self.local_call_holds[key]
            events.append(self._release_local_hold(hold, now))
        return events

    def process(self, source_id, role, line, history=False):
        self.last_evaluation = None
        if not isinstance(line, bytes):
            raise TypeError('spot lines must be bytes')
        if role not in ('local', 'rbn', 'gocluster', 'cluster'):
            raise ValueError('unknown source role: ' + str(role))
        self.counts['received_lines'] += 1
        if not self.config['enabled'] or history:
            return [line]
        now = self.clock()
        self._prune(now)
        spot = parse_spot(line, now, role)
        if spot is None:
            return [line]
        self.counts['parsed_spots'] += 1
        if spot.observed_at is not None and (
                now - spot.observed_at > self.config['max_age_seconds'] or
                spot.observed_at - now > self.config['future_tolerance_seconds']):
            self.counts['stale_spots'] += 1
            return []
        obs = Observation(spot, source_id, now, role)
        allow_hold = True
        if role == 'local':
            key = self._local_hold_key(obs)
            old = self.local_call_holds.get(key)
            if old is not None and (old.force_original or now >= old.deadline):
                if len(self._completed_local_holds) < self.config['max_entries']:
                    del self.local_call_holds[key]
                    self._completed_local_holds.append(self._release_local_hold(old, now))
                    self.last_evaluation = None
                else:
                    # The relay drains before each input. Direct API users
                    # who defer draining get fail-open new work, never an
                    # unbounded release queue or silent eviction of old work.
                    self.counts['local_call_overflow'] += 1
                    allow_hold = False
        identity = (spot.band, spot.mode, spot.call)
        latest = self.latest.get(identity)
        known = [key for key in self.latest.keys_for('episode', (spot.band, spot.call))
                 if key[1] != 'UNKNOWN']
        compatible = None
        if spot.mode == 'UNKNOWN' and len(known) == 1:
            compatible = self.latest[known[0]]
        elif spot.mode != 'UNKNOWN' and (not known or known == [identity]):
            compatible = self.latest.get((spot.band, 'UNKNOWN', spot.call))
        # This is ordering protection for a sole presentation episode, not
        # mode evidence. The original mode still owns all learning below.
        if (compatible is not None and compatible.spot.observed_at is not None and
                (latest is None or latest.spot.observed_at is None or
                 compatible.spot.observed_at > latest.spot.observed_at)):
            latest = compatible
        if (spot.observed_at is None and latest is not None and
                latest.spot.observed_at is not None and
                abs(spot.frequency_hz - latest.spot.frequency_hz) >= 0.005):
            # Undated observations cannot establish a move away from retained
            # dated evidence. A first undated spot still passes normally.
            self.counts['undated_frequency_conflicts'] += 1
            return []
        if (spot.observed_at is not None and latest is not None and
                latest.spot.observed_at is not None and
                spot.observed_at < latest.spot.observed_at):
            self.counts['out_of_order_spots'] += 1
            return []
        if spot.observed_at is not None:
            self._put(self.latest, identity, obs)
        key = (source_id, spot.origin, spot.call, spot.band, spot.mode)
        self._put(self.measurements, key, obs)
        self._put(self.votes, key, obs)
        if role in ('local', 'rbn'):
            if role == 'local':
                self._put(self.local_origins, spot.origin, now)
            self._put(self.locals, key, obs)
        elif role == 'gocluster':
            self._put(self.references, key, obs)
        population = list(self.measurements.values_for('identity', (spot.call, spot.band, spot.mode)))
        self._learn_population(population, now, obs)
        return self._select_output(obs, now, allow_hold=allow_hold)

    def _select_output(self, obs, now, allow_hold=True, force_original=False, reference_before=None):
        spot, source_id, role = obs.spot, obs.source, obs.role
        population = [item for item in self.measurements.values_for('identity', (spot.call, spot.band, spot.mode))
                      if reference_before is None or item.at < reference_before]
        call, frequency = spot.call, spot.frequency_hz
        call_reason, call_support, candidate = 'local-call-reset' if force_original else 'local-call-no-candidate', 0, None
        frequency_basis = 'input'
        if role in ('local', 'rbn', 'gocluster') and not force_original:
            current_group = self._choose_group(self._groups(population, now, subject=obs))
            offset = self._offset(source_id, spot.band, now, spot.origin, before=reference_before)
            if current_group is not None and abs(current_group['frequency']-frequency) <= self.config['match_tolerance_hz']:
                frequency = current_group['frequency']
                frequency_basis = 'gocluster' if current_group['gc'] else 'corroborated'
            elif offset is not None:
                frequency -= offset
                frequency_basis = 'alignment'
            else:
                self.counts['correction_unavailable'] += 1
            if role == 'local':
                group, call_reason, call_support, candidate = self._local_call_decision(obs, frequency, now, before=reference_before)
                hold_key = self._local_hold_key(obs)
                if group is not None:
                    call, frequency = candidate, group['frequency']
                    frequency_basis = 'gocluster' if group['gc'] else 'corroborated'
                    self.counts['callsign_corrections'] += 1
                    self.counts['local_call_corrected'] += 1
                elif call_reason.startswith('pending-local-call') and allow_hold:
                    hold = self.local_call_holds.get(hold_key)
                    if hold is not None or len(self.local_call_holds) < self.config['max_entries']:
                        if hold is None:
                            hold = LocalCallHold(obs, now+self.config['local_call_hold_seconds'])
                            self.local_call_holds[hold_key] = hold
                            self.counts['local_call_held'] += 1
                        else:
                            hold.observation = obs  # A real repeat may replace payload, not deadline.
                        self.last_evaluation = dict(call=spot.call, frequency_hz=frequency,
                                                    frequency_basis=frequency_basis, receiver=spot.origin,
                                                    reason=call_reason, held=True, call_support=call_support,
                                                    call_candidate=candidate, call_required=2)
                        return []
                    self.counts['local_call_overflow'] += 1
                if allow_hold and hold_key in self.local_call_holds:
                    del self.local_call_holds[hold_key]
                    self.counts['local_call_released'] += 1
            if frequency != spot.frequency_hz:
                self.counts['frequency_corrections'] += 1
        output = spot.render(frequency, call)
        self.last_evaluation = {'call': call, 'frequency_hz': frequency,
                                'frequency_basis': frequency_basis, 'receiver': spot.origin,
                                'required': 2, 'support': 0, 'anchor_hz': None,
                                'candidate_hz': frequency, 'reference_basis': None, 'held': False}
        if role == 'local':
            self.last_evaluation.update(call_candidate=candidate,
                                        call_support=call_support, call_required=2,
                                        call_reason=call_reason)
        repeat_key = (spot.band, spot.mode, call)
        previous = self.repeats.get(repeat_key)
        if previous is None:
            # Missing human mode is not new receiver/signal evidence. Reuse an
            # unambiguous presentation episode only; never feed this inference
            # back into mode, reference matching or calibration.
            known = [key for key in self.repeats.keys_for('episode', (spot.band, call))
                     if key[1] != 'UNKNOWN']
            unknown = (spot.band, 'UNKNOWN', call)
            if spot.mode == 'UNKNOWN' and len(known) == 1:
                repeat_key = known[0]
                previous = self.repeats[repeat_key]
            elif spot.mode != 'UNKNOWN' and not known and unknown in self.repeats:
                previous = self.repeats.pop(unknown)
                self._put(self.repeats, repeat_key, previous)
        if previous is not None:
            moved = abs(frequency - previous.frequency) > self.config['move_tolerance_hz']
            ordered = (spot.observed_at is not None and previous.observed is not None and
                       spot.observed_at >= previous.observed)
            self.last_evaluation['anchor_hz'] = previous.frequency
            if not moved or not ordered or previous.changes >= self.config['max_changes']:
                self.last_evaluation['reason'] = 'move-budget-exhausted' if moved and previous.changes >= self.config['max_changes'] else 'repeat'
                self.counts['suppressed_repeats'] += 1
                return []
            selected, reason, support, basis = self._movement(obs, call, frequency, previous, now)
            self.last_evaluation.update(reason=reason,support=support,reference_basis=basis)
            if selected is None:
                self.counts['suppressed_repeats'] += 1
                return []
            frequency = selected
            output = spot.render(frequency, call)
            self.last_evaluation['frequency_hz'] = frequency
            self.last_evaluation['frequency_basis'] = basis
            previous.frequency = frequency
            previous.observed = spot.observed_at
            previous.changes += 1
            previous.generation = self.votes.serial
            self.counts['frequency_change_updates'] += 1
            self.counts['confirmed_moves'] += 1
            for pending_key in list(self.pending.keys_for('identity', (spot.call, spot.band, spot.mode))):
                del self.pending[pending_key]
        else:
            self._put(self.repeats, repeat_key, Repeat(now, spot.observed_at, frequency))
            self.last_evaluation['reason'] = 'new-or-window-expired'
        self.counts['delivered_spots'] += 1
        return [output]


def _edit_one(a, b):
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    if len(a) > len(b):
        a, b = b, a
    index = 0
    while index < len(a) and a[index] == b[index]:
        index += 1
    return a[index:] == b[index + 1:]

"""Plain-text operational status for the spot collector.

This module only formats snapshots. It does not read clocks, inspect relay
connections, or change processing state.
"""

import math
import textwrap


_DETAIL_WIDTH = 104


def _plain(value, missing='-'):
    if value is None or value == '':
        return missing
    return ''.join(character if 32 <= ord(character) <= 126
                   else character.encode('unicode_escape').decode('ascii')
                   for character in str(value))


def _number(value, suffix=''):
    if value is None:
        return 'n/a'
    return '%s%s' % (_plain(format(value, 'g')), suffix)


def _count(value):
    return format(value if value is not None else 0, ',d')


def _adjustment(value):
    if value is None or not math.isfinite(value):
        return 'n/a'
    # Normalise negative zero without hiding a real negative adjustment.
    return '%+.1f Hz' % (value if value != 0 else 0.0)


def _age(value):
    if value is None or not math.isfinite(value):
        return 'never'
    return _number(value, 's')


def _table(headers, rows, right_columns=()):
    widths = [max(len(header), max((len(row[index]) for row in rows), default=0))
              for index, header in enumerate(headers)]

    def render(row):
        cells = [cell.rjust(widths[index]) if index in right_columns
                 else cell.ljust(widths[index])
                 for index, cell in enumerate(row)]
        return '  ' + '  '.join(cells).rstrip()

    lines = [render(headers), render(['-' * width for width in widths])]
    lines.extend(render(row) for row in rows)
    return lines


def _detail(value):
    return textwrap.wrap(value, width=_DETAIL_WIDTH, initial_indent='    ',
                         subsequent_indent='      ', break_on_hyphens=False)


def format_status(*, uptime, listen_port, primary, client_count, client_drops,
                  processing_enabled, counters, sources, alignments, policy):
    """Format supplied status snapshots as newline-terminated ASCII text.

    ``uptime``, source ``up`` and ``last_spot`` values are preformatted text.
    Sparse processing counters represent zero. Alignment ``None`` values mean
    unavailable evidence; zero adjustments and zero ages remain visible.
    ``offset_hz`` is the signed adjustment added to this receiver/route frequency.
    """
    def count(key):
        return _count(counters.get(key, 0))

    rejected = sum(counters.get(key, 0) for key in
                   ('stale_spots', 'out_of_order_spots',
                    'undated_frequency_conflicts'))
    processing = 'enabled' if processing_enabled else 'disabled (bypass)'
    lines = [
        'Spot Collector Status',
        '=====================',
        'Uptime: %s | Listen: %s | Primary: %s' %
        (_plain(uptime), _plain(listen_port), _plain(primary)),
        'Clients: %s | Queue-full disconnects: %s | Processing: %s' %
        (_count(client_count), _count(client_drops), processing),
        '',
        'Spot flow (process lifetime)',
        '  Received lines: %s (all lines) | Parsed spots: %s' %
        (count('received_lines'), count('parsed_spots')),
        '  Selected: %s | Repeats suppressed: %s | Rejected: %s' %
        (count('delivered_spots'), count('suppressed_repeats'), _count(rejected)),
        '  Rejected details: stale/future %s, out of order %s, undated move %s' %
        (count('stale_spots'), count('out_of_order_spots'),
         count('undated_frequency_conflicts')),
        '  Selected = admitted for client fanout; client receipt is not measured.',
        '',
        'Spot changes (evaluated before repeat suppression)',
        '  Frequency corrections: %s | Callsign corrections: %s' %
        (count('frequency_corrections'), count('callsign_corrections')),
        '  Correction unavailable: %s | Selected frequency moves: %s' %
        (count('correction_unavailable'), count('frequency_change_updates')),
        '  Correction counts can include spots subsequently suppressed as repeats.',
        '  Local call checks: held now %s | Held %s | Corrected %s | Released %s' %
        (count('current_local_call_holds'), count('local_call_held'),
         count('local_call_corrected'), count('local_call_released')),
        '  Local hold deadlines: %s | Hold overflows: %s | Delay: %s' %
        (count('local_call_expired'), count('local_call_overflow'),
         _number(policy.get('local_call_hold_seconds'), ' s')),
        '  Movement proposals: pending now %s | Confirmed %s | Expired %s | Conflicts %s' %
        (count('current_pending_moves'), count('confirmed_moves'), count('expired_moves'),
         count('move_conflicts')),
        '  Corroboration: 2 distinct receivers | Agreement: %s | Window: %s' %
        (_number(policy.get('corroboration_tolerance_hz'), ' Hz'),
         _number(policy.get('corroboration_seconds'), ' s')),
        '  Repeat window: %s | Move tolerance: %s | Max updates: %s' %
        (_number(policy.get('repeat_seconds'), ' s'),
         _number(policy.get('move_tolerance_hz'), ' Hz'),
         _number(policy.get('max_changes'))),
        '',
        'Receiver frequency alignment',
    ]
    if alignments:
        # Prioritize usable evidence while bounding a public RBN report's size.
        ranked = sorted(alignments, key=lambda row: (
            row.get('state') != 'Ready', -(row.get('samples') or 0),
            row.get('last_match_age_seconds') if row.get('last_match_age_seconds') is not None else float('inf'),
            str(row.get('source', '')), str(row.get('receiver', '')), str(row.get('band', ''))))
        visible = ranked[:40]
        rows = []
        for alignment in visible:
            progress = '%s/%s' % (_number(alignment.get('samples')),
                                 _number(alignment.get('required')))
            rows.append([
                _plain(alignment.get('source')),
                _plain(alignment.get('receiver')),
                _plain(alignment.get('band')),
                _plain(alignment.get('state')),
                progress,
                _number(alignment.get('retained_samples', alignment.get('samples'))),
                _adjustment(alignment.get('offset_hz')),
                _age(alignment.get('last_match_age_seconds')),
            ])
        table = _table(('Source', 'Receiver', 'Band', 'State', 'Usable', 'Retained',
                        'Adjustment', 'Last match'), rows, (4, 5, 6, 7))
        lines.extend(table[:2])
        for row, alignment in zip(table[2:], visible):
            lines.append(row)
            if alignment.get('rejection_reason') or alignment.get('excluded_samples'):
                lines.extend(_detail('Evidence: %s | Excluded samples: %s' %
                                     (_plain(alignment.get('rejection_reason'), 'qualified'),
                                      _count(alignment.get('excluded_samples', 0)))))
        if len(ranked) > len(visible):
            lines.append('  Showing %d of %d receiver/band rows; %d omitted.' %
                         (len(visible), len(ranked), len(ranked) - len(visible)))
    else:
        lines.append('  No receiver alignment observations yet.')
    lines.append('  Relative to qualified references; corroborated GoCluster data has priority.')
    lines.append('  Usable = fresh distinct calls / required; retained may include stale or excluded samples.')
    lines.append('  Adjustment is added to this receiver/representation only; n/a is not zero.')
    lines.append('  Alignment match rejections (evaluations): self/mirror %s | No reference %s | Ambiguous %s' %
                 (count('alignment_rejected_self'), count('alignment_rejected_missing_reference'),
                  count('alignment_rejected_ambiguous_reference')))
    lines.append('  Match rejections: time %s | Frequency %s' %
                 (count('alignment_rejected_time'), count('alignment_rejected_frequency')))
    lines.extend(['', 'Feeds (process lifetime)'])
    if sources:
        rows = [[
            _plain(source.get('name')),
            _plain(source.get('role')),
            _plain(source.get('state')),
            _count(source.get('parsed', 0)),
            _count(source.get('forwarded', 0)),
            _count(source.get('repeats', 0)),
            _count(source.get('rejected', 0)),
            _plain(source.get('last_spot'), 'never'),
        ] for source in sources]
        table = _table(('Source', 'Role', 'State', 'Parsed', 'Selected',
                        'Repeats', 'Rejected', 'Last spot'), rows, (3, 4, 5, 6))
        lines.extend(table[:2])
        for row, source in zip(table[2:], sources):
            lines.append(row)
            lines.extend(_detail('Address: %s | Direction: %s | Up: %s' %
                                 (_plain(source.get('address')),
                                  _plain(source.get('direction')),
                                  _plain(source.get('up')))))
    else:
        lines.append('  No feeds configured.')
    lines.extend([
        '',
        'Evidence: references %s | Raw observations %s | Receiver reports %s | Alignment samples %s' %
        (count('reference_entries'), count('local_entries'), count('receiver_observations'), count('calibration_samples')),
        'Retained repeat keys: %s | Capacity evictions: %s | Source resets: %s' %
        (count('repeat_entries'), count('state_evictions'), count('source_resets')),
        '',
        'Spot outcome and feed processing counts exclude history and processing bypass.',
        'Last spot reports source activity, including activity while processing is bypassed.',
    ])
    return '\n'.join(lines) + '\n'


def format_short_status(*, uptime, listen_port, primary, client_count, client_drops,
                        processing_enabled, counters, sources, alignments, policy):
    """Compact periodic/client overview; detailed diagnostics use longstatus."""
    enabled = [source for source in sources if source.get('state') != 'Disabled']
    up = sum(source.get('state') in ('TCP up', 'Login sent') for source in enabled)
    down = ', '.join(_plain(source.get('name')) for source in enabled
                     if source.get('state') == 'Down')
    if len(down) > 80:
        down = down[:77] + '...'
    def count(key):
        return _count(counters.get(key, 0))
    return '\n'.join([
        'Spot Collector | Up %s | Port %s | Clients %s | %s' %
        (_plain(uptime), _plain(listen_port), _count(client_count),
         'Processing ON' if processing_enabled else 'Processing OFF'),
        'Feeds TCP up: %d/%d%s' % (up, len(enabled), ' | Down: ' + down if down else ''),
        'Spots: parsed %s | selected %s | repeats %s' %
        (count('parsed_spots'), count('delivered_spots'), count('suppressed_repeats')),
        'Changes evaluated: freq %s | calls %s | holds now %s | queue drops %s' %
        (count('frequency_corrections'), count('callsign_corrections'),
         count('current_local_call_holds'), _count(client_drops)),
        'Totals since startup; selected is not receipt. Type longstatus for details.',
        '',
    ])

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib/trace_measurement.py — Trace Duration Reconstruction (Track A).

Builds the complete pipeline from FinalTimingRow → Trace → duration_ms:

    FinalTimingRow (ready)
        → Trace Signal Resolver (timing label → trace variable)
        → TraceBooleanEvaluator (boolean expression over trace samples)
        → Event Extraction (false→true entry, true→false exit)
        → Cycle/Occurrence Pairing (start entry → end entry)
        → Duration Statistics (count/median/mean/min/max/p95/std)

Key principles:
    - NO assumptions about trace format — adapter pattern
    - NO project-specific branching in the engine
    - Composite row = single timing interval (not sum of parts)
    - Duration is a statistical distribution, not a single value
    - Fallback/not_ready rows are NEVER measured (G25)
    - Trace cannot silently modify static labels (G26)

Schema:
    TraceRecord     — one sample point
    TraceSignal     — one signal channel
    TraceDataSet    — collection of signals + samples
    SignalResolution— result of mapping a timing label to a trace signal
    TraceEvent      — entry or exit event
    TraceRowMeasurement — duration measurement for one FinalTimingRow
"""
from __future__ import annotations

import json
import re
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from collections import defaultdict


# ============================================================
# Measurement Status
# ============================================================

MS_MEASURED = 'measured'
MS_AMBIGUOUS_PAIRING = 'ambiguous_pairing'
MS_MISSING_START = 'missing_start'
MS_MISSING_END = 'missing_end'
MS_INCOMPLETE_TRACE = 'incomplete_trace'
MS_INVALID_ORDER = 'invalid_order'
MS_EXPRESSION_UNOBSERVABLE = 'expression_unobservable'
MS_SIGNAL_NOT_FOUND = 'signal_not_found'
MS_NO_OCCURRENCES = 'no_occurrences'

VALID_MEASUREMENT_STATUSES = {
    MS_MEASURED, MS_AMBIGUOUS_PAIRING, MS_MISSING_START,
    MS_MISSING_END, MS_INCOMPLETE_TRACE, MS_INVALID_ORDER,
    MS_EXPRESSION_UNOBSERVABLE, MS_SIGNAL_NOT_FOUND, MS_NO_OCCURRENCES,
}


# ============================================================
# Event Type
# ============================================================

EVT_ENTRY = 'entry'  # false → true
EVT_EXIT = 'exit'    # true → false


# ============================================================
# Trace Record / Signal / Dataset
# ============================================================

@dataclass
class TraceRecord:
    """One sample point in the trace.

    Fields:
        timestamp_ms  — Timestamp in milliseconds from trace start
        signal_values — Dict of {signal_name: value}
        index         — Sample index
    """
    timestamp_ms: float
    signal_values: dict
    index: int = 0


@dataclass
class TraceSignal:
    """One signal channel in the trace.

    Fields:
        canonical_name  — Normalized PLC symbol (e.g., 'Process_steps[2]')
        source_name     — Name as it appears in trace (e.g., 'EM01V.Process_steps[2]')
        datatype        — Data type (INT, BOOL, etc.)
        sample_count    — Number of samples
        values          — List of values
        timestamps      — List of timestamps (ms)
        station         — Station prefix (e.g., 'EM01')
        state_variable  — State variable name (e.g., 'Process_steps')
        array_index     — Array index (e.g., 2)
    """
    canonical_name: str = ''
    source_name: str = ''
    datatype: str = ''
    sample_count: int = 0
    values: list = field(default_factory=list)
    timestamps: list = field(default_factory=list)
    station: str = ''
    state_variable: str = ''
    array_index: int = -1


@dataclass
class TraceDataSet:
    """Collection of trace signals + metadata.

    Fields:
        signals         — Dict of {source_name: TraceSignal}
        start_time_tick — Start time tick from trace
        duration_ms     — Total trace duration in ms
        source_file     — Source file path
        format          — Trace format ('codesys' or 'csv')
    """
    signals: dict = field(default_factory=dict)
    start_time_tick: int = 0
    duration_ms: int = 0
    source_file: str = ''
    format: str = ''

    def get_signal(self, source_name: str) -> TraceSignal:
        return self.signals.get(source_name)

    def find_signals_by_pattern(self, state_var: str,
                                array_index: int) -> list:
        """Find all signals matching a state variable + array index."""
        results = []
        for sig in self.signals.values():
            if (sig.state_variable == state_var
                    and sig.array_index == array_index):
                results.append(sig)
        return results


# ============================================================
# Signal Resolution
# ============================================================

@dataclass
class SignalResolution:
    """Result of mapping a timing label to a trace signal.

    Fields:
        label           — Original timing label
        state_variable  — Parsed state variable
        array_index     — Parsed array index
        expected_value  — Parsed expected value
        station         — Resolved station
        trace_signal    — Resolved TraceSignal (or None)
        match_type      — exact_match / normalized_match / ambiguous / missing
        alternatives    — Other candidate signals
    """
    label: str = ''
    state_variable: str = ''
    array_index: int = -1
    expected_value: str = ''
    station: str = ''
    trace_signal: TraceSignal = None
    match_type: str = 'missing'
    alternatives: list = field(default_factory=list)


def parse_timing_label(label: str):
    """Parse a timing label into (state_var, array_index, value).

    Handles:
        Process_steps[2]=120
        Process_steps[2]=INT#120
        m_nNextAutoStep[29]=INT#110
        NOT(Process_steps[2]=120)
    """
    if not label:
        return None, None, None

    label = label.strip()
    # Remove NOT() wrapper for parsing
    label_clean = label.replace('NOT(', '').replace(')', '')
    label_clean = label_clean.replace('INT#', '')

    match = re.match(r'([\w]+)\[(\d+)\]=(\d+)', label_clean)
    if match:
        return match.group(1), int(match.group(2)), match.group(3)
    return None, None, None


def is_not_expression(label: str) -> bool:
    """Check if a label is a NOT() expression."""
    if not label:
        return False
    return label.strip().upper().startswith('NOT')


def resolve_signal(label: str, station: str,
                   trace_data: TraceDataSet) -> SignalResolution:
    """Resolve a timing label to a trace signal.

    Path:
        timing label → parse → (state_var, array_idx, value)
        → construct source_name: {station}V.{state_var}[{idx}]
        → look up in trace_data.signals

    Returns:
        SignalResolution with match_type and trace_signal
    """
    state_var, array_idx, value = parse_timing_label(label)
    if state_var is None:
        return SignalResolution(
            label=label, match_type='missing',
            alternatives=[])

    # Construct expected trace variable name
    if station:
        source_name = '{}V.{}[{}]'.format(station, state_var, array_idx)
        sig = trace_data.get_signal(source_name)
        if sig:
            return SignalResolution(
                label=label,
                state_variable=state_var,
                array_index=array_idx,
                expected_value=value,
                station=station,
                trace_signal=sig,
                match_type='exact_match')

    # If no exact match, search by pattern
    candidates = trace_data.find_signals_by_pattern(state_var, array_idx)
    if len(candidates) == 1:
        return SignalResolution(
            label=label,
            state_variable=state_var,
            array_index=array_idx,
            expected_value=value,
            station=candidates[0].station,
            trace_signal=candidates[0],
            match_type='normalized_match')
    if len(candidates) > 1:
        return SignalResolution(
            label=label,
            state_variable=state_var,
            array_index=array_idx,
            expected_value=value,
            station=station,
            match_type='ambiguous',
            alternatives=candidates)

    return SignalResolution(
        label=label,
        state_variable=state_var,
        array_index=array_idx,
        expected_value=value,
        station=station,
        match_type='missing')


# ============================================================
# Boolean Trace Evaluator
# ============================================================

def evaluate_expression_over_time(label: str,
                                  signal: TraceSignal) -> list:
    """Evaluate a boolean expression over trace samples.

    Returns a list of (timestamp_ms, bool_value) pairs.

    Supports:
        - Simple EQ: Process_steps[2]=120 → value == 120
        - NOT: NOT(Process_steps[2]=120) → value != 120
        - AND/OR: expr1 AND expr2 (requires multiple signals — not yet)

    For simple EQ/NOT, uses the signal's values directly.
    """
    if not signal or not signal.values:
        return []

    _, _, expected_value = parse_timing_label(label)
    if expected_value is None:
        return []

    is_not = is_not_expression(label)

    results = []
    for i, val in enumerate(signal.values):
        ts = signal.timestamps[i] if i < len(signal.timestamps) else 0
        # Compare as string (handles '120' == 120)
        match = (str(val).strip() == str(expected_value).strip())
        if is_not:
            match = not match
        results.append((ts, match))

    return results


def extract_events(label: str, signal: TraceSignal) -> list:
    """Extract entry/exit events from a boolean expression over trace.

    Entry event: false → true (the condition becomes true)
    Exit event: true → false (the condition becomes false)

    Returns list of TraceEvent dicts:
        {type: 'entry'/'exit', timestamp_ms: int, value: bool}
    """
    eval_results = evaluate_expression_over_time(label, signal)
    if not eval_results:
        return []

    events = []
    prev_val = eval_results[0][1]

    # If starts true, that's an entry at timestamp 0
    # (or residual from before trace start)
    if prev_val:
        events.append({
            'type': EVT_ENTRY,
            'timestamp_ms': eval_results[0][0],
            'residual': True,
        })

    for i in range(1, len(eval_results)):
        ts, cur_val = eval_results[i]
        prev_ts, prev_bool = eval_results[i - 1]

        if not prev_bool and cur_val:
            # false → true: entry
            events.append({
                'type': EVT_ENTRY,
                'timestamp_ms': ts,
                'residual': False,
            })
        elif prev_bool and not cur_val:
            # true → false: exit
            events.append({
                'type': EVT_EXIT,
                'timestamp_ms': ts,
                'residual': False,
            })

    return events


# ============================================================
# Event Pairing (Cycle/Occurrence)
# ============================================================

@dataclass
class OccurrencePair:
    """One paired (start_event, end_event) occurrence.

    Fields:
        cycle_id          — Occurrence index for this row
        start_timestamp   — Timestamp of start entry event
        end_timestamp     — Timestamp of end entry event
                           (for next_step_entry end labels)
                           OR start exit event
                           (for step_exit / NOT() end labels)
        duration_ms       — end_timestamp - start_timestamp
        start_residual    — Whether start was residual
        end_incomplete    — Whether end was incomplete (no exit found)
    """
    cycle_id: int = 0
    start_timestamp: float = 0
    end_timestamp: float = 0
    duration_ms: float = 0
    start_residual: bool = False
    end_incomplete: bool = False


def pair_events(start_events: list, end_events: list,
                end_is_entry: bool = True) -> list:
    """Pair start events with end events chronologically.

    For next_step_entry end labels (end_is_entry=True):
        Start = start entry event (false→true on start label)
        End = end entry event (false→true on end label)
        Duration = end_entry_ts - start_entry_ts

    For step_exit / NOT() end labels (end_is_entry=False):
        Start = start entry event
        End = start exit event (true→false on start label)
        Duration = start_exit_ts - start_entry_ts

    Rules (T1-T6):
        T1: start must occur before end
        T2: one start occurrence → one end (no duplicate pairing)
        T3: one end cannot be reused
        T4: if next start appears before end, mark ambiguous
        T5: missing signal → no measurement
        T6: fallback rows excluded (checked by caller)

    Returns:
        List of OccurrencePair
    """
    if not start_events:
        return []

    pairs = []

    if end_is_entry:
        # Pair start entries with end entries
        # Each start entry finds the NEXT end entry after it
        end_idx = 0
        for start_ev in start_events:
            if start_ev['type'] != EVT_ENTRY:
                continue

            start_ts = start_ev['timestamp_ms']

            # Find next end entry after this start
            found_end = False
            while end_idx < len(end_events):
                end_ev = end_events[end_idx]
                if end_ev['type'] != EVT_ENTRY:
                    end_idx += 1
                    continue

                end_ts = end_ev['timestamp_ms']

                if end_ts > start_ts:
                    # Valid pair (T1: start before end)
                    pair = OccurrencePair(
                        cycle_id=len(pairs) + 1,
                        start_timestamp=start_ts,
                        end_timestamp=end_ts,
                        duration_ms=end_ts - start_ts,
                        start_residual=start_ev.get('residual', False),
                        end_incomplete=False,
                    )
                    pairs.append(pair)
                    end_idx += 1
                    found_end = True
                    break
                else:
                    # end before start — skip this end
                    end_idx += 1

            if not found_end:
                # No end found — incomplete (T4)
                pair = OccurrencePair(
                    cycle_id=len(pairs) + 1,
                    start_timestamp=start_ts,
                    end_timestamp=0,
                    duration_ms=0,
                    start_residual=start_ev.get('residual', False),
                    end_incomplete=True,
                )
                pairs.append(pair)
    else:
        # For step_exit end labels: pair start entry with start exit
        # The "end" is when the start condition becomes false
        i = 0
        while i < len(start_events):
            ev = start_events[i]
            if ev['type'] == EVT_ENTRY:
                start_ts = ev['timestamp_ms']
                # Find next exit after this entry
                found_exit = False
                for j in range(i + 1, len(start_events)):
                    if start_events[j]['type'] == EVT_EXIT:
                        end_ts = start_events[j]['timestamp_ms']
                        pair = OccurrencePair(
                            cycle_id=len(pairs) + 1,
                            start_timestamp=start_ts,
                            end_timestamp=end_ts,
                            duration_ms=end_ts - start_ts,
                            start_residual=ev.get('residual', False),
                            end_incomplete=False,
                        )
                        pairs.append(pair)
                        found_exit = True
                        i = j + 1
                        break
                if not found_exit:
                    # No exit found — incomplete
                    pair = OccurrencePair(
                        cycle_id=len(pairs) + 1,
                        start_timestamp=start_ts,
                        end_timestamp=0,
                        duration_ms=0,
                        start_residual=ev.get('residual', False),
                        end_incomplete=True,
                    )
                    pairs.append(pair)
                    i += 1
            else:
                i += 1

    return pairs


# ============================================================
# Duration Statistics
# ============================================================

@dataclass
class DurationStatistics:
    """Statistical distribution of durations for one FinalTimingRow.

    Fields:
        count      — Number of measured occurrences
        median_ms  — Median duration
        mean_ms    — Mean duration
        min_ms     — Minimum duration
        max_ms     — Maximum duration
        p95_ms     — 95th percentile
        std_ms     — Standard deviation
        durations  — List of all measured durations
    """
    count: int = 0
    median_ms: float = 0
    mean_ms: float = 0
    min_ms: float = 0
    max_ms: float = 0
    p95_ms: float = 0
    std_ms: float = 0
    durations: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'count': self.count,
            'median_ms': round(self.median_ms, 2),
            'mean_ms': round(self.mean_ms, 2),
            'min_ms': self.min_ms,
            'max_ms': self.max_ms,
            'p95_ms': round(self.p95_ms, 2),
            'std_ms': round(self.std_ms, 2),
            'durations': self.durations,
        }


def compute_statistics(durations: list) -> DurationStatistics:
    """Compute duration statistics from a list of durations."""
    if not durations:
        return DurationStatistics()

    sorted_d = sorted(durations)
    n = len(sorted_d)

    median = sorted_d[n // 2] if n % 2 == 1 else (sorted_d[n // 2 - 1] + sorted_d[n // 2]) / 2
    mean = sum(durations) / n
    min_val = sorted_d[0]
    max_val = sorted_d[-1]

    # P95
    p95_idx = int(math.ceil(0.95 * n)) - 1
    p95 = sorted_d[max(0, p95_idx)]

    # Std
    if n > 1:
        variance = sum((d - mean) ** 2 for d in durations) / (n - 1)
        std = math.sqrt(variance)
    else:
        std = 0.0

    return DurationStatistics(
        count=n,
        median_ms=median,
        mean_ms=mean,
        min_ms=min_val,
        max_ms=max_val,
        p95_ms=p95,
        std_ms=std,
        durations=sorted_d,
    )


# ============================================================
# Trace Row Measurement
# ============================================================

@dataclass
class TraceRowMeasurement:
    """Duration measurement for one FinalTimingRow.

    Fields:
        final_row_id        — Reference to FinalTimingRow
        start_label         — Start label used
        end_label           — End label used
        start_resolution    — Signal resolution for start
        end_resolution      — Signal resolution for end
        occurrences         — List of OccurrencePair
        statistics          — DurationStatistics
        measurement_status  — measured / ambiguous / missing / etc.
        required_signals    — List of required signal names
        trace_coverage      — Whether all signals were found
        evidence            — Supporting evidence
        notes               — Additional notes
    """
    final_row_id: str = ''
    start_label: str = ''
    end_label: str = ''
    start_resolution: SignalResolution = None
    end_resolution: SignalResolution = None
    occurrences: list = field(default_factory=list)
    statistics: DurationStatistics = None
    measurement_status: str = MS_NO_OCCURRENCES
    required_signals: list = field(default_factory=list)
    trace_coverage: str = 'unknown'
    evidence: list = field(default_factory=list)
    notes: str = ''

    def to_dict(self) -> dict:
        return {
            'final_row_id': self.final_row_id,
            'start_label': self.start_label,
            'end_label': self.end_label,
            'start_signal': (
                self.start_resolution.trace_signal.source_name
                if self.start_resolution and self.start_resolution.trace_signal
                else ''),
            'end_signal': (
                self.end_resolution.trace_signal.source_name
                if self.end_resolution and self.end_resolution.trace_signal
                else ''),
            'start_match_type': (
                self.start_resolution.match_type
                if self.start_resolution else 'missing'),
            'end_match_type': (
                self.end_resolution.match_type
                if self.end_resolution else 'missing'),
            'occurrence_count': len(self.occurrences),
            'measured_count': sum(1 for o in self.occurrences
                                  if not o.end_incomplete),
            'incomplete_count': sum(1 for o in self.occurrences
                                    if o.end_incomplete),
            'statistics': (self.statistics.to_dict()
                           if self.statistics else None),
            'measurement_status': self.measurement_status,
            'required_signals': self.required_signals,
            'trace_coverage': self.trace_coverage,
            'evidence': self.evidence,
            'notes': self.notes,
            'occurrences': [
                {
                    'cycle_id': o.cycle_id,
                    'start_ts': o.start_timestamp,
                    'end_ts': o.end_timestamp,
                    'duration_ms': o.duration_ms,
                    'start_residual': o.start_residual,
                    'end_incomplete': o.end_incomplete,
                }
                for o in self.occurrences
            ],
        }


def measure_row(row, station: str, trace_data: TraceDataSet) -> TraceRowMeasurement:
    """Measure duration for a single FinalTimingRow.

    Args:
        row: FinalTimingRowFact (must be trace_readiness=ready)
        station: Station prefix for signal resolution
        trace_data: TraceDataSet with loaded signals

    Returns:
        TraceRowMeasurement
    """
    start_label = row.timing_start.value if row.timing_start else ''
    end_label = row.timing_end.value if row.timing_end else ''

    measurement = TraceRowMeasurement(
        final_row_id=row.row_id,
        start_label=start_label,
        end_label=end_label,
    )

    # Resolve signals
    start_res = resolve_signal(start_label, station, trace_data)
    end_res = resolve_signal(end_label, station, trace_data)

    measurement.start_resolution = start_res
    measurement.end_resolution = end_res

    # Check signal availability
    if not start_res.trace_signal:
        measurement.measurement_status = MS_MISSING_START
        measurement.trace_coverage = 'start_missing'
        return measurement

    if not end_res.trace_signal:
        # Check if end label is a different state variable
        # (e.g., Init_Steps vs Process_steps)
        end_state_var, _, _ = parse_timing_label(end_label)
        if end_state_var and end_state_var != start_res.state_variable:
            measurement.measurement_status = MS_SIGNAL_NOT_FOUND
            measurement.trace_coverage = 'end_state_var_not_in_trace'
            measurement.notes = 'End label uses {} but trace only has {}'.format(
                end_state_var, start_res.state_variable)
        else:
            measurement.measurement_status = MS_MISSING_END
            measurement.trace_coverage = 'end_missing'
        return measurement

    measurement.trace_coverage = 'full'
    measurement.required_signals = [
        start_res.trace_signal.source_name,
        end_res.trace_signal.source_name,
    ]

    # Extract events
    start_events = extract_events(start_label, start_res.trace_signal)

    # Determine end event type
    # For next_step_entry end labels: use entry events of end label
    # For NOT() / step_exit end labels: use exit events of start label
    end_is_entry = not is_not_expression(end_label)

    if end_is_entry:
        end_events = extract_events(end_label, end_res.trace_signal)
    else:
        # For NOT() end labels, we need the exit of the NOT condition
        # which is the entry of the positive condition
        # Actually, NOT(Process_steps[2]=120) becoming true means
        # Process_steps[2] != 120
        # So NOT() entry = original exit, NOT() exit = original entry
        # For timing: if end is NOT(start_label), the end is when start exits
        end_events = extract_events(end_label, end_res.trace_signal)

    # Pair events
    pairs = pair_events(start_events, end_events, end_is_entry)
    measurement.occurrences = pairs

    # Compute statistics
    complete_durations = [p.duration_ms for p in pairs
                          if not p.end_incomplete and p.duration_ms > 0]

    if complete_durations:
        measurement.statistics = compute_statistics(complete_durations)
        if all(not p.end_incomplete for p in pairs):
            measurement.measurement_status = MS_MEASURED
        else:
            measurement.measurement_status = MS_MEASURED
            measurement.notes = '{} of {} occurrences incomplete'.format(
                sum(1 for p in pairs if p.end_incomplete), len(pairs))
    elif pairs:
        # All pairs incomplete
        measurement.measurement_status = MS_INCOMPLETE_TRACE
    else:
        measurement.measurement_status = MS_NO_OCCURRENCES
        measurement.notes = 'No start events found in trace'

    # Evidence
    measurement.evidence = [
        'start_signal: {}'.format(start_res.trace_signal.source_name),
        'end_signal: {}'.format(end_res.trace_signal.source_name),
        'start_events: {}'.format(len(start_events)),
        'end_events: {}'.format(len(end_events)),
        'occurrences: {}'.format(len(pairs)),
    ]

    return measurement


# ============================================================
# Track A V2 — Runtime Measurement Quality
# A3-A7: Cycle Segmentation, Censoring, Interruption, Pairing
# A8:   Timing Uncertainty
# A9-A10: Valid Measurement Definition
# A1-A2: Coverage Matrix
# ============================================================

# ---- Thresholds (discovered, not preset) ----
DEFAULT_QUIET_PERIOD_MULTIPLIER = 10.0
DEFAULT_QUIET_PERIOD_MIN_MS = 5000
DEFAULT_INTERRUPTION_MULTIPLIER = 5.0
DEFAULT_GAP_DETECTION_MULTIPLIER = 3.0

# ---- Measurement Validity (A9) ----
MV_VALID = 'valid'
MV_INTERRUPTED = 'interrupted'
MV_CENSORED = 'censored'
MV_AMBIGUOUS = 'ambiguous'
MV_INCOMPLETE = 'incomplete'
MV_NOT_EXERCISED = 'not_exercised'
MV_SIGNAL_MISSING = 'signal_missing'

VALID_MEASUREMENT_VALIDITIES = {
    MV_VALID, MV_INTERRUPTED, MV_CENSORED,
    MV_AMBIGUOUS, MV_INCOMPLETE, MV_NOT_EXERCISED, MV_SIGNAL_MISSING,
}

# ---- Interruption Types (A6) ----
INT_NORMAL = 'normal_complete'
INT_MACHINE_IDLE = 'machine_idle'
INT_PRODUCTION_PAUSE = 'production_pause'
INT_INTERRUPTED_CYCLE = 'interrupted_cycle'
INT_TRACE_GAP = 'trace_gap'
INT_AMBIGUOUS = 'ambiguous'

# ---- Censoring Types (A5) ----
CENSOR_NONE = 'none'
CENSOR_LEFT = 'left_censored'
CENSOR_RIGHT = 'right_censored'
CENSOR_BOTH = 'both_censored'

# ---- Coverage Failure Reasons (A1) ----
COV_SIGNAL_NOT_RECORDED = 'trace_signal_not_recorded'
COV_STEP_NOT_EXERCISED = 'trace_signal_recorded_but_step_not_exercised'
COV_TRACE_WINDOW_INCOMPLETE = 'trace_window_incomplete'
COV_EXPRESSION_NOT_OBSERVABLE = 'expression_not_observable'
COV_STATIC_BOUNDARY_CONFLICT = 'static_boundary_conflict'
COV_MEASUREMENT_PAIRING_FAILURE = 'measurement_pairing_failure'
COV_MEASURABLE = 'measurable'

# Local copy of TRACE_READY (defined in action_row_fact.py as 'ready')
# Kept here to avoid circular dependency
_TRACE_READY = 'ready'


# ============================================================
# Sampling Metadata (A8)
# ============================================================

@dataclass
class SamplingMetadata:
    """Sampling characteristics of a TraceDataSet.

    Fields:
        nominal_sampling_interval_ms  — Nominal (expected) interval
        observed_median_interval_ms   — Median of observed intervals
        observed_mean_interval_ms     — Mean of observed intervals
        observed_min_interval_ms      — Minimum interval
        observed_max_interval_ms      — Maximum interval
        observed_std_interval_ms      — Std dev of intervals
        timestamp_resolution_ms       — Smallest detectable time difference
        total_samples                 — Total samples in trace
        dropped_or_gapped_samples     — Count of gaps (intervals > 2x median)
        interval_p95_ms               — 95th percentile interval
        interval_distribution         — Histogram buckets
    """
    nominal_sampling_interval_ms: float = 0
    observed_median_interval_ms: float = 0
    observed_mean_interval_ms: float = 0
    observed_min_interval_ms: float = 0
    observed_max_interval_ms: float = 0
    observed_std_interval_ms: float = 0
    timestamp_resolution_ms: float = 0
    total_samples: int = 0
    dropped_or_gapped_samples: int = 0
    interval_p95_ms: float = 0
    interval_distribution: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'nominal_sampling_interval_ms': round(self.nominal_sampling_interval_ms, 3),
            'observed_median_interval_ms': round(self.observed_median_interval_ms, 3),
            'observed_mean_interval_ms': round(self.observed_mean_interval_ms, 3),
            'observed_min_interval_ms': self.observed_min_interval_ms,
            'observed_max_interval_ms': self.observed_max_interval_ms,
            'observed_std_interval_ms': round(self.observed_std_interval_ms, 3),
            'timestamp_resolution_ms': self.timestamp_resolution_ms,
            'total_samples': self.total_samples,
            'dropped_or_gapped_samples': self.dropped_or_gapped_samples,
            'interval_p95_ms': round(self.interval_p95_ms, 3),
            'interval_distribution': self.interval_distribution,
        }

    @property
    def timing_uncertainty_ms(self) -> float:
        """Timing uncertainty is approximately the sampling interval."""
        return max(self.observed_median_interval_ms, self.timestamp_resolution_ms)


def analyze_sampling_metadata(trace_data: TraceDataSet) -> SamplingMetadata:
    """Analyze sampling characteristics of a TraceDataSet.

    Computes interval distribution from the first signal's timestamps
    and detects gaps (intervals significantly larger than median).
    """
    if not trace_data.signals:
        return SamplingMetadata()

    first_sig = next(iter(trace_data.signals.values()))
    if not first_sig.timestamps or len(first_sig.timestamps) < 2:
        return SamplingMetadata(
            total_samples=first_sig.sample_count,
            timestamp_resolution_ms=1,
        )

    timestamps = first_sig.timestamps
    intervals = []
    for i in range(1, len(timestamps)):
        dt = timestamps[i] - timestamps[i - 1]
        if dt > 0:
            intervals.append(dt)

    if not intervals:
        return SamplingMetadata(
            total_samples=len(timestamps),
            timestamp_resolution_ms=1,
        )

    sorted_intervals = sorted(intervals)
    n = len(sorted_intervals)
    median = sorted_intervals[n // 2] if n % 2 == 1 else (
        sorted_intervals[n // 2 - 1] + sorted_intervals[n // 2]) / 2
    mean = sum(intervals) / n
    min_val = sorted_intervals[0]
    max_val = sorted_intervals[-1]
    p95_idx = max(0, int(math.ceil(0.95 * n)) - 1)
    p95 = sorted_intervals[p95_idx]

    if n > 1:
        variance = sum((d - mean) ** 2 for d in intervals) / (n - 1)
        std = math.sqrt(variance)
    else:
        std = 0.0

    gap_count = sum(1 for d in intervals
                    if d > DEFAULT_GAP_DETECTION_MULTIPLIER * median)

    resolution = 1
    for d in sorted_intervals:
        if d > 0:
            resolution = min(resolution, d)
            break

    bucket_size = max(1, int(median / 2)) if median > 0 else 1
    buckets = {}
    for d in intervals:
        bucket = int(d / bucket_size) * bucket_size
        buckets[bucket] = buckets.get(bucket, 0) + 1
    distribution = [
        {'range_start': k, 'range_end': k + bucket_size, 'count': v}
        for k, v in sorted(buckets.items())
    ]

    return SamplingMetadata(
        observed_median_interval_ms=median,
        observed_mean_interval_ms=mean,
        observed_min_interval_ms=min_val,
        observed_max_interval_ms=max_val,
        observed_std_interval_ms=std,
        timestamp_resolution_ms=resolution,
        total_samples=len(timestamps),
        dropped_or_gapped_samples=gap_count,
        interval_p95_ms=p95,
        interval_distribution=distribution,
    )


# ============================================================
# Trace Cycle (A3-A4)
# ============================================================

@dataclass
class TraceCycle:
    """One production cycle identified in the trace.

    A cycle is a period of active state-machine operation bounded by
    quiet periods (gaps with no state changes) or state resets.

    Fields:
        cycle_id              — Sequential cycle index
        source_trace          — Source trace file path
        state_domain          — State variable name (e.g., 'Process_steps')
        thread                — Array index (e.g., 3)
        start_timestamp       — Cycle start timestamp (ms)
        end_timestamp         — Cycle end timestamp (ms)
        cycle_start_evidence  — How cycle start was identified
        cycle_end_evidence    — How cycle end was identified
        complete              — Whether the cycle appears complete
        interruption_detected — Whether an interruption was found within
        interruption_reason   — Description of interruption if any
        confidence            — Confidence in cycle boundary (0-1)
        state_changes         — Number of state changes in this cycle
        values_seen           — Set of values observed during this cycle
    """
    cycle_id: int = 0
    source_trace: str = ''
    state_domain: str = ''
    thread: int = -1
    start_timestamp: float = 0
    end_timestamp: float = 0
    cycle_start_evidence: str = ''
    cycle_end_evidence: str = ''
    complete: bool = False
    interruption_detected: bool = False
    interruption_reason: str = ''
    confidence: float = 0.0
    state_changes: int = 0
    values_seen: list = field(default_factory=list)

    def contains(self, timestamp: float) -> bool:
        """Check if a timestamp falls within this cycle."""
        return self.start_timestamp <= timestamp <= self.end_timestamp

    def to_dict(self) -> dict:
        return {
            'cycle_id': self.cycle_id,
            'state_domain': self.state_domain,
            'thread': self.thread,
            'start_timestamp': self.start_timestamp,
            'end_timestamp': self.end_timestamp,
            'duration_ms': self.end_timestamp - self.start_timestamp,
            'cycle_start_evidence': self.cycle_start_evidence,
            'cycle_end_evidence': self.cycle_end_evidence,
            'complete': self.complete,
            'interruption_detected': self.interruption_detected,
            'interruption_reason': self.interruption_reason,
            'confidence': round(self.confidence, 3),
            'state_changes': self.state_changes,
            'values_seen': self.values_seen,
        }


@dataclass
class TraceSession:
    """Collection of cycles from one trace recording.

    Fields:
        source_file        — Trace file path
        format             — Trace format ('codesys' or 'csv')
        total_duration_ms  — Total trace duration
        sampling_metadata  — Sampling characteristics
        cycles             — List of TraceCycle objects (per signal)
    """
    source_file: str = ''
    format: str = ''
    total_duration_ms: float = 0
    sampling_metadata: SamplingMetadata = None
    cycles: list = field(default_factory=list)

    def get_cycles_for_signal(self, state_domain: str,
                              thread: int) -> list:
        """Get cycles for a specific (state_domain, thread) pair."""
        return [c for c in self.cycles
                if c.state_domain == state_domain and c.thread == thread]

    def to_dict(self) -> dict:
        return {
            'source_file': self.source_file,
            'format': self.format,
            'total_duration_ms': self.total_duration_ms,
            'sampling_metadata': (
                self.sampling_metadata.to_dict()
                if self.sampling_metadata else None),
            'cycles': [c.to_dict() for c in self.cycles],
        }


def segment_trace_cycles(trace_data: TraceDataSet,
                         state_domain: str,
                         thread: int) -> list:
    """Identify production cycles from a trace signal.

    Algorithm (discovery-based, no preset rules):
    1. Get the signal for this (state_domain, thread)
    2. Find all state change points (value transitions)
    3. Compute intervals between consecutive changes
    4. Find quiet periods (intervals > threshold)
       threshold = max(multiplier * median, min_ms)
    5. Also identify state=0 events (potential resets)
    6. Segment into cycles between quiet periods
    7. For each cycle, check completeness and interruptions

    Returns:
        List of TraceCycle objects
    """
    sig = trace_data.find_signals_by_pattern(state_domain, thread)
    if not sig:
        return []
    signal = sig[0]
    if not signal.values or len(signal.values) < 2:
        return []

    timestamps = signal.timestamps
    values = signal.values

    state_changes = []
    for i in range(1, len(values)):
        if str(values[i]).strip() != str(values[i - 1]).strip():
            state_changes.append({
                'timestamp': timestamps[i],
                'from_value': str(values[i - 1]).strip(),
                'to_value': str(values[i]).strip(),
                'index': i,
            })

    if not state_changes:
        return []

    if len(state_changes) == 1:
        return [TraceCycle(
            cycle_id=1,
            source_trace=trace_data.source_file,
            state_domain=state_domain,
            thread=thread,
            start_timestamp=timestamps[0],
            end_timestamp=timestamps[-1],
            cycle_start_evidence='single_state_change',
            cycle_end_evidence='trace_end',
            complete=False,
            confidence=0.3,
            state_changes=1,
            values_seen=list(set(str(v).strip() for v in values)),
        )]

    intervals = []
    for i in range(1, len(state_changes)):
        dt = state_changes[i]['timestamp'] - state_changes[i - 1]['timestamp']
        intervals.append(dt)

    sorted_intervals = sorted(intervals)
    n = len(sorted_intervals)
    median_interval = sorted_intervals[n // 2] if n % 2 == 1 else (
        sorted_intervals[n // 2 - 1] + sorted_intervals[n // 2]) / 2

    quiet_threshold = max(
        DEFAULT_QUIET_PERIOD_MULTIPLIER * median_interval,
        DEFAULT_QUIET_PERIOD_MIN_MS,
    )

    boundaries = []
    for i in range(1, len(state_changes)):
        dt = state_changes[i]['timestamp'] - state_changes[i - 1]['timestamp']
        if dt > quiet_threshold:
            mid_ts = (state_changes[i - 1]['timestamp'] +
                      state_changes[i]['timestamp']) / 2
            boundaries.append({
                'timestamp': mid_ts,
                'evidence': 'quiet_period_{}ms'.format(int(dt)),
                'gap_duration': dt,
            })

    for sc_idx, sc in enumerate(state_changes):
        if sc['to_value'] == '0':
            if sc_idx + 1 < len(state_changes):
                next_sc = state_changes[sc_idx + 1]
                persistence = next_sc['timestamp'] - sc['timestamp']
            else:
                persistence = timestamps[-1] - sc['timestamp']
            if persistence > quiet_threshold:
                boundaries.append({
                    'timestamp': sc['timestamp'],
                    'evidence': 'state_reset_to_0_persisted_{}ms'.format(
                        int(persistence)),
                    'gap_duration': persistence,
                })

    boundaries.sort(key=lambda b: b['timestamp'])

    unique_boundaries = []
    for b in boundaries:
        if (unique_boundaries and
                abs(b['timestamp'] - unique_boundaries[-1]['timestamp']) < median_interval):
            if b['gap_duration'] > unique_boundaries[-1]['gap_duration']:
                unique_boundaries[-1] = b
        else:
            unique_boundaries.append(b)
    boundaries = unique_boundaries

    if not boundaries:
        return [TraceCycle(
            cycle_id=1,
            source_trace=trace_data.source_file,
            state_domain=state_domain,
            thread=thread,
            start_timestamp=timestamps[0],
            end_timestamp=timestamps[-1],
            cycle_start_evidence='trace_start',
            cycle_end_evidence='trace_end',
            complete=True,
            confidence=0.5,
            state_changes=len(state_changes),
            values_seen=list(set(str(v).strip() for v in values)),
        )]

    cycles = []
    cycle_start_ts = timestamps[0]
    cycle_start_evidence = 'trace_start'

    for b in boundaries:
        changes_in_cycle = [sc for sc in state_changes
                            if cycle_start_ts <= sc['timestamp'] <= b['timestamp']]
        values_in_cycle = list(set(
            str(values[sc['index']]).strip() for sc in changes_in_cycle
        ))
        values_in_cycle.insert(0, str(values[0]).strip())

        has_interruption = False
        if len(changes_in_cycle) > 1:
            for i in range(1, len(changes_in_cycle)):
                dt = (changes_in_cycle[i]['timestamp'] -
                      changes_in_cycle[i - 1]['timestamp'])
                if dt > quiet_threshold:
                    has_interruption = True
                    break

        is_complete = len(changes_in_cycle) > 0

        cycles.append(TraceCycle(
            cycle_id=len(cycles) + 1,
            source_trace=trace_data.source_file,
            state_domain=state_domain,
            thread=thread,
            start_timestamp=cycle_start_ts,
            end_timestamp=b['timestamp'],
            cycle_start_evidence=cycle_start_evidence,
            cycle_end_evidence=b['evidence'],
            complete=is_complete,
            interruption_detected=has_interruption,
            interruption_reason='quiet_period_within_cycle' if has_interruption else '',
            confidence=0.8 if is_complete and not has_interruption else 0.5,
            state_changes=len(changes_in_cycle),
            values_seen=values_in_cycle,
        ))
        cycle_start_ts = b['timestamp']
        cycle_start_evidence = b['evidence'] + '_end'

    changes_in_last = [sc for sc in state_changes
                       if sc['timestamp'] > cycle_start_ts]
    if changes_in_last or cycle_start_ts < timestamps[-1]:
        values_in_last = list(set(
            str(values[sc['index']]).strip() for sc in changes_in_last
        ))
        if changes_in_last:
            first_idx = changes_in_last[0]['index']
            if first_idx > 0:
                values_in_last.insert(0, str(values[first_idx - 1]).strip())

        cycles.append(TraceCycle(
            cycle_id=len(cycles) + 1,
            source_trace=trace_data.source_file,
            state_domain=state_domain,
            thread=thread,
            start_timestamp=cycle_start_ts,
            end_timestamp=timestamps[-1],
            cycle_start_evidence=cycle_start_evidence,
            cycle_end_evidence='trace_end',
            complete=False,
            confidence=0.3,
            state_changes=len(changes_in_last),
            values_seen=values_in_last,
        ))

    return cycles


# ============================================================
# Censoring Detection (A5)
# ============================================================

def detect_censoring(eval_results_start: list,
                     eval_results_end: list,
                     start_ts: float,
                     end_ts: float,
                     trace_start_ts: float,
                     trace_end_ts: float) -> str:
    """Detect left/right censoring for an occurrence.

    Left censored: start expression is already TRUE at trace start.
    Right censored: end event not found before trace end.

    Returns:
        Censoring type string (CENSOR_NONE/LEFT/RIGHT/BOTH)
    """
    left_censored = False
    right_censored = False

    if eval_results_start and eval_results_start[0][1]:
        if start_ts <= trace_start_ts:
            left_censored = True

    if end_ts == 0 or end_ts == 0.0:
        right_censored = True

    if left_censored and right_censored:
        return CENSOR_BOTH
    if left_censored:
        return CENSOR_LEFT
    if right_censored:
        return CENSOR_RIGHT
    return CENSOR_NONE


# ============================================================
# Runtime Interruption Detection (A6)
# ============================================================

@dataclass
class RuntimeInterruption:
    """Hypothesis about a runtime interruption within an occurrence.

    Fields:
        interruption_type — normal_complete/machine_idle/production_pause/
                            interrupted_cycle/trace_gap/ambiguous
        start_timestamp   — When interruption started
        end_timestamp     — When interruption ended
        duration_ms       — Interruption duration
        evidence          — Supporting evidence list
        confidence        — Confidence (0-1)
    """
    interruption_type: str = INT_AMBIGUOUS
    start_timestamp: float = 0
    end_timestamp: float = 0
    duration_ms: float = 0
    evidence: list = field(default_factory=list)
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return {
            'interruption_type': self.interruption_type,
            'start_timestamp': self.start_timestamp,
            'end_timestamp': self.end_timestamp,
            'duration_ms': self.duration_ms,
            'evidence': self.evidence,
            'confidence': round(self.confidence, 3),
        }


def detect_interruption(signal: TraceSignal,
                        start_ts: float,
                        end_ts: float,
                        normal_duration_ms: float) -> RuntimeInterruption:
    """Detect runtime interruptions within an occurrence.

    Checks:
    1. State stays at same value for > multiplier * normal duration
    2. State goes to 0 (reset) during the interval
    3. Trace gaps (intervals > multiplier * median sampling interval)
    4. State goes backwards (unexpected jump)

    Returns:
        RuntimeInterruption hypothesis
    """
    if end_ts <= start_ts or not signal.values:
        return RuntimeInterruption(interruption_type=INT_NORMAL)

    timestamps = signal.timestamps
    values = signal.values

    start_idx = 0
    end_idx = len(timestamps) - 1
    for i in range(len(timestamps)):
        if timestamps[i] >= start_ts:
            start_idx = i
            break
    for i in range(len(timestamps) - 1, -1, -1):
        if timestamps[i] <= end_ts:
            end_idx = i
            break

    if start_idx >= end_idx:
        return RuntimeInterruption(interruption_type=INT_NORMAL)

    interval_values = values[start_idx:end_idx + 1]
    interval_timestamps = timestamps[start_idx:end_idx + 1]

    if len(interval_timestamps) < 2:
        return RuntimeInterruption(interruption_type=INT_NORMAL)

    intervals = []
    for i in range(1, len(interval_timestamps)):
        dt = interval_timestamps[i] - interval_timestamps[i - 1]
        intervals.append((dt, i,
                          str(interval_values[i - 1]).strip(),
                          str(interval_values[i]).strip()))

    if not intervals:
        return RuntimeInterruption(interruption_type=INT_NORMAL)

    sorted_intervals = sorted(intervals, key=lambda x: x[0], reverse=True)
    longest_gap = sorted_intervals[0]
    gap_duration = longest_gap[0]
    gap_value = longest_gap[2]

    if normal_duration_ms > 0:
        interruption_threshold = DEFAULT_INTERRUPTION_MULTIPLIER * normal_duration_ms
    else:
        all_intervals_sorted = sorted([x[0] for x in intervals])
        median_interval = all_intervals_sorted[len(all_intervals_sorted) // 2]
        interruption_threshold = max(
            DEFAULT_INTERRUPTION_MULTIPLIER * median_interval,
            DEFAULT_QUIET_PERIOD_MIN_MS,
        )

    has_reset = any(iv[3] == '0' for iv in intervals)

    if has_reset:
        reset_iv = next(iv for iv in intervals if iv[3] == '0')
        return RuntimeInterruption(
            interruption_type=INT_INTERRUPTED_CYCLE,
            start_timestamp=interval_timestamps[reset_iv[1] - 1],
            end_timestamp=interval_timestamps[reset_iv[1]],
            duration_ms=reset_iv[0],
            evidence=[
                'state_reset_to_0_during_occurrence',
                'reset_at_ts={}'.format(interval_timestamps[reset_iv[1]]),
                'value_before={}_value_after=0'.format(reset_iv[2]),
            ],
            confidence=0.9,
        )

    if gap_duration > interruption_threshold:
        if gap_duration > DEFAULT_QUIET_PERIOD_MIN_MS * 10:
            int_type = INT_MACHINE_IDLE
        elif gap_duration > DEFAULT_QUIET_PERIOD_MIN_MS:
            int_type = INT_PRODUCTION_PAUSE
        else:
            int_type = INT_TRACE_GAP

        return RuntimeInterruption(
            interruption_type=int_type,
            start_timestamp=interval_timestamps[longest_gap[1] - 1],
            end_timestamp=interval_timestamps[longest_gap[1]],
            duration_ms=gap_duration,
            evidence=[
                'long_static_period_{}ms'.format(int(gap_duration)),
                'value_held={}'.format(gap_value),
                'threshold={}ms'.format(int(interruption_threshold)),
                'normal_duration={}ms'.format(int(normal_duration_ms)),
            ],
            confidence=0.8,
        )

    return RuntimeInterruption(interruption_type=INT_NORMAL)


# ============================================================
# Upgraded Event Pairing (A7)
# ============================================================

@dataclass
class OccurrencePairV2:
    """Upgraded occurrence pair with cycle/censoring/interruption info.

    Fields:
        cycle_id              — References TraceCycle.cycle_id
        trace_cycle           — The TraceCycle this occurrence belongs to
        start_timestamp       — Start event timestamp
        end_timestamp         — End event timestamp (0 if incomplete)
        duration_ms           — end - start
        start_residual        — Whether start was residual (TRUE at trace start)
        end_incomplete        — Whether end was not found
        left_censored         — Whether start is left-censored
        right_censored        — Whether end is right-censored
        censoring_type        — Censoring classification
        interruption          — RuntimeInterruption hypothesis (or None)
        measurement_validity  — valid/interrupted/censored/ambiguous/etc.
        pairing_reason        — Why this pairing was made
        pairing_confidence    — Confidence in pairing (0-1)
        intervening_events    — Events between start and end
        timing_uncertainty_ms — Measurement uncertainty
    """
    cycle_id: int = 0
    trace_cycle: TraceCycle = None
    start_timestamp: float = 0
    end_timestamp: float = 0
    duration_ms: float = 0
    start_residual: bool = False
    end_incomplete: bool = False
    left_censored: bool = False
    right_censored: bool = False
    censoring_type: str = CENSOR_NONE
    interruption: RuntimeInterruption = None
    measurement_validity: str = MV_VALID
    pairing_reason: str = ''
    pairing_confidence: float = 0.0
    intervening_events: list = field(default_factory=list)
    timing_uncertainty_ms: float = 0

    def to_dict(self) -> dict:
        return {
            'cycle_id': self.cycle_id,
            'start_ts': self.start_timestamp,
            'end_ts': self.end_timestamp,
            'duration_ms': self.duration_ms,
            'start_residual': self.start_residual,
            'end_incomplete': self.end_incomplete,
            'left_censored': self.left_censored,
            'right_censored': self.right_censored,
            'censoring_type': self.censoring_type,
            'interruption': (
                self.interruption.to_dict()
                if self.interruption else None),
            'measurement_validity': self.measurement_validity,
            'pairing_reason': self.pairing_reason,
            'pairing_confidence': round(self.pairing_confidence, 3),
            'intervening_events': self.intervening_events,
            'timing_uncertainty_ms': self.timing_uncertainty_ms,
        }


def find_cycle_for_timestamp(cycles: list, timestamp: float) -> TraceCycle:
    """Find which TraceCycle a timestamp belongs to."""
    for c in cycles:
        if c.contains(timestamp):
            return c
    if cycles:
        if timestamp < cycles[0].start_timestamp:
            return cycles[0]
        return cycles[-1]
    return None


def classify_measurement_validity(end_incomplete: bool,
                                  left_censored: bool,
                                  right_censored: bool,
                                  interruption: RuntimeInterruption,
                                  conflicting_restart: bool) -> str:
    """Classify an occurrence's measurement validity (A9).

    A valid measurement requires ALL of:
    - Complete start (not left-censored)
    - Complete end (not incomplete, not right-censored)
    - No interruption (interruption_type == normal_complete)
    - No conflicting restart
    """
    if left_censored and right_censored:
        return MV_CENSORED
    if left_censored or right_censored:
        return MV_CENSORED
    if end_incomplete:
        return MV_INCOMPLETE
    if interruption and interruption.interruption_type != INT_NORMAL:
        return MV_INTERRUPTED
    if conflicting_restart:
        return MV_AMBIGUOUS
    return MV_VALID


def pair_events_with_cycles(start_events: list,
                            end_events: list,
                            end_is_entry: bool,
                            cycles: list,
                            signal: TraceSignal,
                            trace_start_ts: float,
                            trace_end_ts: float,
                            eval_results_start: list,
                            eval_results_end: list,
                            timing_uncertainty_ms: float,
                            normal_duration_ms: float = 0) -> list:
    """Pair start/end events with cycle constraints (A7).

    Upgraded from pair_events() with:
    - Cycle assignment: each occurrence is assigned to a TraceCycle
    - Censoring detection: left/right censored occurrences flagged
    - Interruption detection: long/static periods within occurrences flagged
    - Validity classification: each occurrence gets a measurement_validity
    - No cross-cycle pairing: start and end must be in the same cycle

    Returns:
        List of OccurrencePairV2
    """
    if not start_events:
        return []

    pairs = []
    end_idx = 0
    start_entries = [e for e in start_events if e['type'] == EVT_ENTRY]

    for start_ev in start_entries:
        start_ts = start_ev['timestamp_ms']
        cycle = find_cycle_for_timestamp(cycles, start_ts)

        if end_is_entry:
            found_end = False
            while end_idx < len(end_events):
                end_ev = end_events[end_idx]
                if end_ev['type'] != EVT_ENTRY:
                    end_idx += 1
                    continue

                end_ts = end_ev['timestamp_ms']

                if end_ts <= start_ts:
                    end_idx += 1
                    continue

                if cycle and not cycle.contains(end_ts):
                    if end_ts > cycle.end_timestamp:
                        break
                    else:
                        end_idx += 1
                        continue

                conflicting_restart = False
                for next_start in start_entries:
                    if (next_start['timestamp_ms'] > start_ts
                            and next_start['timestamp_ms'] < end_ts):
                        if cycle and cycle.contains(next_start['timestamp_ms']):
                            conflicting_restart = True
                            break

                interruption = detect_interruption(
                    signal, start_ts, end_ts, normal_duration_ms)

                censoring = detect_censoring(
                    eval_results_start, eval_results_end,
                    start_ts, end_ts,
                    trace_start_ts, trace_end_ts)

                left_censored = censoring in (CENSOR_LEFT, CENSOR_BOTH)
                right_censored = censoring in (CENSOR_RIGHT, CENSOR_BOTH)

                validity = classify_measurement_validity(
                    end_incomplete=False,
                    left_censored=left_censored,
                    right_censored=right_censored,
                    interruption=interruption,
                    conflicting_restart=conflicting_restart,
                )

                pairing_reason = 'start_entry_to_end_entry'
                if conflicting_restart:
                    pairing_reason += '_with_conflicting_restart'
                if cycle:
                    pairing_reason += '_in_cycle_{}'.format(cycle.cycle_id)

                confidence = 0.9
                if conflicting_restart:
                    confidence = 0.4
                if left_censored:
                    confidence *= 0.7
                if interruption.interruption_type != INT_NORMAL:
                    confidence *= 0.5

                pairs.append(OccurrencePairV2(
                    cycle_id=cycle.cycle_id if cycle else 0,
                    trace_cycle=cycle,
                    start_timestamp=start_ts,
                    end_timestamp=end_ts,
                    duration_ms=end_ts - start_ts,
                    start_residual=start_ev.get('residual', False),
                    end_incomplete=False,
                    left_censored=left_censored,
                    right_censored=right_censored,
                    censoring_type=censoring,
                    interruption=interruption,
                    measurement_validity=validity,
                    pairing_reason=pairing_reason,
                    pairing_confidence=confidence,
                    timing_uncertainty_ms=timing_uncertainty_ms,
                ))
                end_idx += 1
                found_end = True
                break

            if not found_end:
                pairs.append(OccurrencePairV2(
                    cycle_id=cycle.cycle_id if cycle else 0,
                    trace_cycle=cycle,
                    start_timestamp=start_ts,
                    end_timestamp=0,
                    duration_ms=0,
                    start_residual=start_ev.get('residual', False),
                    end_incomplete=True,
                    right_censored=True,
                    censoring_type=CENSOR_RIGHT,
                    measurement_validity=MV_INCOMPLETE,
                    pairing_reason='no_end_found_in_cycle',
                    pairing_confidence=0.2,
                    timing_uncertainty_ms=timing_uncertainty_ms,
                ))
        else:
            found_exit = False
            start_ev_idx = start_events.index(start_ev)
            for j in range(start_ev_idx + 1, len(start_events)):
                if start_events[j]['type'] == EVT_EXIT:
                    end_ts = start_events[j]['timestamp_ms']

                    if cycle and not cycle.contains(end_ts):
                        continue

                    interruption = detect_interruption(
                        signal, start_ts, end_ts, normal_duration_ms)

                    censoring = detect_censoring(
                        eval_results_start, eval_results_end,
                        start_ts, end_ts,
                        trace_start_ts, trace_end_ts)

                    left_censored = censoring in (CENSOR_LEFT, CENSOR_BOTH)
                    right_censored = censoring in (CENSOR_RIGHT, CENSOR_BOTH)

                    validity = classify_measurement_validity(
                        end_incomplete=False,
                        left_censored=left_censored,
                        right_censored=right_censored,
                        interruption=interruption,
                        conflicting_restart=False,
                    )

                    pairs.append(OccurrencePairV2(
                        cycle_id=cycle.cycle_id if cycle else 0,
                        trace_cycle=cycle,
                        start_timestamp=start_ts,
                        end_timestamp=end_ts,
                        duration_ms=end_ts - start_ts,
                        start_residual=start_ev.get('residual', False),
                        end_incomplete=False,
                        left_censored=left_censored,
                        right_censored=right_censored,
                        censoring_type=censoring,
                        interruption=interruption,
                        measurement_validity=validity,
                        pairing_reason='start_entry_to_start_exit',
                        pairing_confidence=0.85,
                        timing_uncertainty_ms=timing_uncertainty_ms,
                    ))
                    found_exit = True
                    break

            if not found_exit:
                pairs.append(OccurrencePairV2(
                    cycle_id=cycle.cycle_id if cycle else 0,
                    trace_cycle=cycle,
                    start_timestamp=start_ts,
                    end_timestamp=0,
                    duration_ms=0,
                    start_residual=start_ev.get('residual', False),
                    end_incomplete=True,
                    right_censored=True,
                    censoring_type=CENSOR_RIGHT,
                    measurement_validity=MV_INCOMPLETE,
                    pairing_reason='no_exit_found',
                    pairing_confidence=0.2,
                    timing_uncertainty_ms=timing_uncertainty_ms,
                ))

    return pairs


# ============================================================
# Duration Statistics V2 (A10)
# ============================================================

@dataclass
class DurationStatisticsV2:
    """Statistics using only valid occurrences.

    Fields:
        valid_count         — Number of valid occurrences
        excluded_count      — Number of excluded occurrences
        interruption_count  — Occurrences excluded due to interruption
        censored_count      — Occurrences excluded due to censoring
        ambiguous_count     — Occurrences excluded due to ambiguity
        incomplete_count    — Occurrences excluded due to incompleteness
        median_ms           — Median of valid durations
        mean_ms             — Mean of valid durations
        min_ms              — Minimum valid duration
        max_ms              — Maximum valid duration
        p95_ms              — 95th percentile of valid durations
        std_ms              — Std dev of valid durations
        valid_durations     — List of valid duration values
        all_durations       — List of all duration values (for reference)
        timing_uncertainty_ms — Measurement timing uncertainty
        excluded_details    — List of exclusion details
    """
    valid_count: int = 0
    excluded_count: int = 0
    interruption_count: int = 0
    censored_count: int = 0
    ambiguous_count: int = 0
    incomplete_count: int = 0
    median_ms: float = 0
    mean_ms: float = 0
    min_ms: float = 0
    max_ms: float = 0
    p95_ms: float = 0
    std_ms: float = 0
    valid_durations: list = field(default_factory=list)
    all_durations: list = field(default_factory=list)
    timing_uncertainty_ms: float = 0
    excluded_details: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'valid_count': self.valid_count,
            'excluded_count': self.excluded_count,
            'interruption_count': self.interruption_count,
            'censored_count': self.censored_count,
            'ambiguous_count': self.ambiguous_count,
            'incomplete_count': self.incomplete_count,
            'median_ms': round(self.median_ms, 2),
            'mean_ms': round(self.mean_ms, 2),
            'min_ms': self.min_ms,
            'max_ms': self.max_ms,
            'p95_ms': round(self.p95_ms, 2),
            'std_ms': round(self.std_ms, 2),
            'valid_durations': self.valid_durations,
            'all_durations': self.all_durations,
            'timing_uncertainty_ms': round(self.timing_uncertainty_ms, 3),
            'excluded_details': self.excluded_details,
        }


def compute_valid_statistics(occurrences: list,
                             timing_uncertainty_ms: float = 0) -> DurationStatisticsV2:
    """Compute duration statistics using only valid occurrences (A10).

    Excluded occurrences are counted by reason but their durations
    are NOT included in median/mean/min/max/p95/std.
    """
    valid_durations = []
    all_durations = []
    excluded_details = []

    interruption_count = 0
    censored_count = 0
    ambiguous_count = 0
    incomplete_count = 0

    for occ in occurrences:
        if occ.duration_ms > 0:
            all_durations.append(occ.duration_ms)

        if occ.measurement_validity == MV_VALID:
            valid_durations.append(occ.duration_ms)
        else:
            excluded_details.append({
                'cycle_id': occ.cycle_id,
                'duration_ms': occ.duration_ms,
                'validity': occ.measurement_validity,
                'reason': occ.pairing_reason,
                'interruption_type': (
                    occ.interruption.interruption_type
                    if occ.interruption else None),
            })

            if occ.measurement_validity == MV_INTERRUPTED:
                interruption_count += 1
            elif occ.measurement_validity == MV_CENSORED:
                censored_count += 1
            elif occ.measurement_validity == MV_AMBIGUOUS:
                ambiguous_count += 1
            elif occ.measurement_validity == MV_INCOMPLETE:
                incomplete_count += 1

    stats = DurationStatisticsV2(
        valid_count=len(valid_durations),
        excluded_count=len(excluded_details),
        interruption_count=interruption_count,
        censored_count=censored_count,
        ambiguous_count=ambiguous_count,
        incomplete_count=incomplete_count,
        all_durations=sorted(all_durations),
        timing_uncertainty_ms=timing_uncertainty_ms,
        excluded_details=excluded_details,
    )

    if valid_durations:
        sorted_vd = sorted(valid_durations)
        n = len(sorted_vd)
        stats.median_ms = sorted_vd[n // 2] if n % 2 == 1 else (
            sorted_vd[n // 2 - 1] + sorted_vd[n // 2]) / 2
        stats.mean_ms = sum(valid_durations) / n
        stats.min_ms = sorted_vd[0]
        stats.max_ms = sorted_vd[-1]
        p95_idx = max(0, int(math.ceil(0.95 * n)) - 1)
        stats.p95_ms = sorted_vd[p95_idx]
        if n > 1:
            variance = sum(
                (d - stats.mean_ms) ** 2 for d in valid_durations) / (n - 1)
            stats.std_ms = math.sqrt(variance)
        stats.valid_durations = sorted_vd

    return stats


# ============================================================
# Trace Row Measurement V2 (A9-A10)
# ============================================================

@dataclass
class TraceRowMeasurementV2:
    """Upgraded duration measurement for one FinalTimingRow.

    Fields:
        final_row_id        — Reference to FinalTimingRow
        start_label         — Start label used
        end_label           — End label used
        start_resolution    — Signal resolution for start
        end_resolution      — Signal resolution for end
        occurrences         — List of OccurrencePairV2
        valid_statistics    — DurationStatisticsV2 (valid only)
        measurement_status  — Overall measurement status
        measurement_quality — Quality summary string
        required_signals    — List of required signal names
        trace_coverage      — Coverage description
        cycles_used         — List of TraceCycle objects used
        sampling_metadata   — SamplingMetadata reference
        evidence            — Supporting evidence
        notes               — Additional notes
    """
    final_row_id: str = ''
    start_label: str = ''
    end_label: str = ''
    start_resolution: SignalResolution = None
    end_resolution: SignalResolution = None
    occurrences: list = field(default_factory=list)
    valid_statistics: DurationStatisticsV2 = None
    measurement_status: str = MS_NO_OCCURRENCES
    measurement_quality: str = ''
    required_signals: list = field(default_factory=list)
    trace_coverage: str = 'unknown'
    cycles_used: list = field(default_factory=list)
    sampling_metadata: SamplingMetadata = None
    evidence: list = field(default_factory=list)
    notes: str = ''

    def to_dict(self) -> dict:
        return {
            'final_row_id': self.final_row_id,
            'start_label': self.start_label,
            'end_label': self.end_label,
            'start_signal': (
                self.start_resolution.trace_signal.source_name
                if self.start_resolution and self.start_resolution.trace_signal
                else ''),
            'end_signal': (
                self.end_resolution.trace_signal.source_name
                if self.end_resolution and self.end_resolution.trace_signal
                else ''),
            'start_match_type': (
                self.start_resolution.match_type
                if self.start_resolution else 'missing'),
            'end_match_type': (
                self.end_resolution.match_type
                if self.end_resolution else 'missing'),
            'occurrence_count': len(self.occurrences),
            'valid_count': sum(1 for o in self.occurrences
                               if o.measurement_validity == MV_VALID),
            'interrupted_count': sum(1 for o in self.occurrences
                                     if o.measurement_validity == MV_INTERRUPTED),
            'censored_count': sum(1 for o in self.occurrences
                                  if o.measurement_validity == MV_CENSORED),
            'ambiguous_count': sum(1 for o in self.occurrences
                                   if o.measurement_validity == MV_AMBIGUOUS),
            'incomplete_count': sum(1 for o in self.occurrences
                                    if o.measurement_validity == MV_INCOMPLETE),
            'valid_statistics': (
                self.valid_statistics.to_dict()
                if self.valid_statistics else None),
            'measurement_status': self.measurement_status,
            'measurement_quality': self.measurement_quality,
            'required_signals': self.required_signals,
            'trace_coverage': self.trace_coverage,
            'cycles_used': [c.to_dict() for c in self.cycles_used],
            'sampling_metadata': (
                self.sampling_metadata.to_dict()
                if self.sampling_metadata else None),
            'evidence': self.evidence,
            'notes': self.notes,
            'occurrences': [o.to_dict() for o in self.occurrences],
        }


def measure_row_v2(row, station: str, trace_data: TraceDataSet,
                   session: TraceSession = None) -> TraceRowMeasurementV2:
    """Measure duration for a single FinalTimingRow (V2).

    Upgraded from measure_row() with:
    - Cycle segmentation (A3-A4)
    - Censoring detection (A5)
    - Interruption detection (A6)
    - Cycle-aware event pairing (A7)
    - Timing uncertainty (A8)
    - Valid measurement classification (A9)
    - Valid-only statistics (A10)
    """
    start_label = row.timing_start.value if row.timing_start else ''
    end_label = row.timing_end.value if row.timing_end else ''

    measurement = TraceRowMeasurementV2(
        final_row_id=row.row_id,
        start_label=start_label,
        end_label=end_label,
    )

    start_res = resolve_signal(start_label, station, trace_data)
    end_res = resolve_signal(end_label, station, trace_data)

    measurement.start_resolution = start_res
    measurement.end_resolution = end_res

    if not start_res.trace_signal:
        measurement.measurement_status = MS_MISSING_START
        measurement.trace_coverage = COV_SIGNAL_NOT_RECORDED
        measurement.measurement_quality = 'start_signal_not_in_trace'
        return measurement

    if not end_res.trace_signal:
        end_state_var, _, _ = parse_timing_label(end_label)
        if end_state_var and end_state_var != start_res.state_variable:
            measurement.measurement_status = MS_SIGNAL_NOT_FOUND
            measurement.trace_coverage = COV_EXPRESSION_NOT_OBSERVABLE
            measurement.measurement_quality = 'end_state_var_not_in_trace'
            measurement.notes = 'End label uses {} but trace only has {}'.format(
                end_state_var, start_res.state_variable)
        else:
            measurement.measurement_status = MS_MISSING_END
            measurement.trace_coverage = COV_SIGNAL_NOT_RECORDED
            measurement.measurement_quality = 'end_signal_not_in_trace'
        return measurement

    measurement.trace_coverage = COV_MEASURABLE
    measurement.required_signals = [
        start_res.trace_signal.source_name,
        end_res.trace_signal.source_name,
    ]

    start_signal = start_res.trace_signal
    end_signal = end_res.trace_signal

    timing_signal = (start_signal
                     if start_signal.source_name == end_signal.source_name
                     else start_signal)

    cycles = []
    if session:
        cycles = session.get_cycles_for_signal(
            start_res.state_variable, start_res.array_index)
    if not cycles:
        cycles = segment_trace_cycles(
            trace_data, start_res.state_variable, start_res.array_index)
    measurement.cycles_used = cycles

    sampling_meta = None
    if session and session.sampling_metadata:
        sampling_meta = session.sampling_metadata
    else:
        sampling_meta = analyze_sampling_metadata(trace_data)
    measurement.sampling_metadata = sampling_meta

    timing_uncertainty = sampling_meta.timing_uncertainty_ms if sampling_meta else 0

    start_events = extract_events(start_label, start_signal)
    end_is_entry = not is_not_expression(end_label)
    end_events = extract_events(end_label, end_signal)

    if not start_events:
        has_value = any(
            str(v).strip() == start_res.expected_value
            for v in start_signal.values
        )
        if not has_value:
            measurement.measurement_status = MS_NO_OCCURRENCES
            measurement.trace_coverage = COV_STEP_NOT_EXERCISED
            measurement.measurement_quality = 'step_value_not_in_trace'
            measurement.notes = 'Step value {} never appears in trace'.format(
                start_res.expected_value)
        else:
            measurement.measurement_status = MS_NO_OCCURRENCES
            measurement.measurement_quality = 'no_state_transitions'
            measurement.notes = 'Signal exists and value present but no transitions'
        return measurement

    trace_start_ts = start_signal.timestamps[0] if start_signal.timestamps else 0
    trace_end_ts = start_signal.timestamps[-1] if start_signal.timestamps else 0

    eval_results_start = evaluate_expression_over_time(start_label, start_signal)
    eval_results_end = evaluate_expression_over_time(end_label, end_signal)

    pairs = pair_events_with_cycles(
        start_events, end_events, end_is_entry,
        cycles, timing_signal,
        trace_start_ts, trace_end_ts,
        eval_results_start, eval_results_end,
        timing_uncertainty,
        normal_duration_ms=0,
    )
    measurement.occurrences = pairs

    if not pairs:
        measurement.measurement_status = MS_NO_OCCURRENCES
        measurement.measurement_quality = 'no_pairs_formed'
        return measurement

    valid_durations_initial = [p.duration_ms for p in pairs
                               if p.measurement_validity == MV_VALID
                               and p.duration_ms > 0]
    normal_duration = 0
    if valid_durations_initial:
        sorted_vd = sorted(valid_durations_initial)
        n = len(sorted_vd)
        normal_duration = sorted_vd[n // 2] if n % 2 == 1 else (
            sorted_vd[n // 2 - 1] + sorted_vd[n // 2]) / 2

    if normal_duration > 0:
        for p in pairs:
            if p.measurement_validity == MV_VALID and p.duration_ms > 0:
                interruption = detect_interruption(
                    timing_signal, p.start_timestamp, p.end_timestamp,
                    normal_duration)
                if interruption.interruption_type != INT_NORMAL:
                    p.interruption = interruption
                    p.measurement_validity = classify_measurement_validity(
                        end_incomplete=p.end_incomplete,
                        left_censored=p.left_censored,
                        right_censored=p.right_censored,
                        interruption=interruption,
                        conflicting_restart=False,
                    )
                    p.pairing_confidence *= 0.5

    stats = compute_valid_statistics(pairs, timing_uncertainty)
    measurement.valid_statistics = stats

    valid_count = stats.valid_count
    total_count = len(pairs)

    if valid_count > 0:
        measurement.measurement_status = MS_MEASURED
        if stats.excluded_count > 0:
            measurement.measurement_quality = 'measured_with_exclusions'
            measurement.notes = '{} of {} occurrences excluded ({} interrupted, {} censored, {} ambiguous, {} incomplete)'.format(
                stats.excluded_count, total_count,
                stats.interruption_count, stats.censored_count,
                stats.ambiguous_count, stats.incomplete_count)
        else:
            measurement.measurement_quality = 'all_valid'
    elif total_count > 0:
        if stats.interruption_count > 0:
            measurement.measurement_status = MS_MEASURED
            measurement.measurement_quality = 'all_interrupted'
        elif stats.censored_count > 0:
            measurement.measurement_status = MS_INCOMPLETE_TRACE
            measurement.measurement_quality = 'all_censored'
        elif stats.incomplete_count > 0:
            measurement.measurement_status = MS_INCOMPLETE_TRACE
            measurement.measurement_quality = 'all_incomplete'
        else:
            measurement.measurement_status = MS_AMBIGUOUS_PAIRING
            measurement.measurement_quality = 'all_ambiguous'
    else:
        measurement.measurement_status = MS_NO_OCCURRENCES

    measurement.evidence = [
        'start_signal: {}'.format(start_signal.source_name),
        'end_signal: {}'.format(end_signal.source_name),
        'start_events: {}'.format(len(start_events)),
        'end_events: {}'.format(len(end_events)),
        'occurrences: {}'.format(len(pairs)),
        'valid_occurrences: {}'.format(valid_count),
        'cycles_identified: {}'.format(len(cycles)),
        'timing_uncertainty_ms: {}'.format(timing_uncertainty),
    ]

    return measurement


# ============================================================
# Coverage Matrix (A1-A2)
# ============================================================

@dataclass
class CoverageEntry:
    """Coverage analysis for one FinalTimingRow.

    Fields:
        final_row_id        — Row ID
        start_label         — Start timing label
        end_label           — End timing label
        required_signals    — List of required signal names
        available_signals   — List of available signals in trace
        missing_signals     — List of missing signals
        start_observable    — Can start label be resolved to a trace signal?
        end_observable      — Can end label be resolved to a trace signal?
        exercised_in_trace  — Does the step value appear in trace?
        measurement_possible — Can we measure this row?
        failure_reason      — Why measurement is not possible (or 'measurable')
        details             — Additional details
    """
    final_row_id: str = ''
    start_label: str = ''
    end_label: str = ''
    required_signals: list = field(default_factory=list)
    available_signals: list = field(default_factory=list)
    missing_signals: list = field(default_factory=list)
    start_observable: bool = False
    end_observable: bool = False
    exercised_in_trace: bool = False
    measurement_possible: bool = False
    failure_reason: str = ''
    details: str = ''

    def to_dict(self) -> dict:
        return {
            'final_row_id': self.final_row_id,
            'start_label': self.start_label,
            'end_label': self.end_label,
            'required_signals': self.required_signals,
            'available_signals': self.available_signals,
            'missing_signals': self.missing_signals,
            'start_observable': self.start_observable,
            'end_observable': self.end_observable,
            'exercised_in_trace': self.exercised_in_trace,
            'measurement_possible': self.measurement_possible,
            'failure_reason': self.failure_reason,
            'details': self.details,
        }


def build_trace_coverage_matrix(rows: list, trace_data: TraceDataSet,
                                station_map: dict = None) -> list:
    """Build coverage matrix for all FinalTimingRows (A1-A2).

    For each row, determines WHY it can or cannot be measured,
    using the 6 distinct failure categories.

    Categories:
    - trace_signal_not_recorded: signal not in trace at all
    - trace_signal_recorded_but_step_not_exercised: signal exists but value never appears
    - trace_window_incomplete: trace doesn't cover the action period
    - expression_not_observable: label uses a state variable not in trace
    - static_boundary_conflict: row is fallback/not ready
    - measurement_pairing_failure: signals exist, values appear, but pairing failed
    - measurable: all conditions met
    """
    entries = []

    for row in rows:
        entry = CoverageEntry(
            final_row_id=row.row_id,
            start_label=row.timing_start.value if row.timing_start else '',
            end_label=row.timing_end.value if row.timing_end else '',
        )

        if row.trace_readiness != _TRACE_READY:
            entry.failure_reason = COV_STATIC_BOUNDARY_CONFLICT
            entry.details = 'trace_readiness={}'.format(row.trace_readiness)
            entries.append(entry)
            continue

        station = ''
        if station_map:
            stations = set()
            for step_id in getattr(row, 'source_steps', []):
                m = re.search(r'thread=(\d+)/step=(\d+)', step_id)
                if m:
                    t = int(m.group(1))
                    s = int(m.group(2))
                    s_name = station_map.get((t, s), '')
                    if s_name:
                        stations.add(s_name)
            if len(stations) == 1:
                station = stations.pop()

        start_res = resolve_signal(entry.start_label, station, trace_data)
        end_res = resolve_signal(entry.end_label, station, trace_data)

        entry.start_observable = start_res.trace_signal is not None
        entry.end_observable = end_res.trace_signal is not None

        required = []
        available = []
        missing = []

        if start_res.trace_signal:
            required.append(start_res.trace_signal.source_name)
            available.append(start_res.trace_signal.source_name)
        else:
            sn = '{}.{}[{}]'.format(station, start_res.state_variable,
                                     start_res.array_index) if station else \
                 '{}[{}]'.format(start_res.state_variable, start_res.array_index)
            required.append(sn)
            missing.append(sn)

        if end_res.trace_signal:
            if end_res.trace_signal.source_name not in available:
                required.append(end_res.trace_signal.source_name)
                available.append(end_res.trace_signal.source_name)
        else:
            end_state_var, _, _ = parse_timing_label(entry.end_label)
            if end_state_var:
                sn = '{}.{}[{}]'.format(station, end_state_var,
                                         end_res.array_index) if station else \
                     '{}[{}]'.format(end_state_var, end_res.array_index)
                required.append(sn)
                missing.append(sn)

        entry.required_signals = required
        entry.available_signals = available
        entry.missing_signals = missing

        if not entry.start_observable and not entry.end_observable:
            end_state_var, _, _ = parse_timing_label(entry.end_label)
            if (end_state_var and start_res.state_variable
                    and end_state_var != start_res.state_variable):
                entry.failure_reason = COV_EXPRESSION_NOT_OBSERVABLE
                entry.details = 'End uses {} but trace has {}'.format(
                    end_state_var, start_res.state_variable)
            else:
                entry.failure_reason = COV_SIGNAL_NOT_RECORDED
                entry.details = 'Neither start nor end signal in trace'
            entries.append(entry)
            continue

        if not entry.start_observable:
            entry.failure_reason = COV_SIGNAL_NOT_RECORDED
            entry.details = 'Start signal not in trace'
            entries.append(entry)
            continue

        if not entry.end_observable:
            end_state_var, _, _ = parse_timing_label(entry.end_label)
            if (end_state_var and start_res.state_variable
                    and end_state_var != start_res.state_variable):
                entry.failure_reason = COV_EXPRESSION_NOT_OBSERVABLE
                entry.details = 'End label uses {} but trace only has {}'.format(
                    end_state_var, start_res.state_variable)
            else:
                entry.failure_reason = COV_SIGNAL_NOT_RECORDED
                entry.details = 'End signal not in trace'
            entries.append(entry)
            continue

        if start_res.trace_signal:
            has_start_value = any(
                str(v).strip() == start_res.expected_value
                for v in start_res.trace_signal.values
            )
            if not has_start_value:
                entry.exercised_in_trace = False
                entry.failure_reason = COV_STEP_NOT_EXERCISED
                entry.details = 'Step value {} never appears in trace signal {}'.format(
                    start_res.expected_value, start_res.trace_signal.source_name)
                entries.append(entry)
                continue

        entry.exercised_in_trace = True
        entry.measurement_possible = True
        entry.failure_reason = COV_MEASURABLE
        entry.details = 'All conditions met for measurement'

        entries.append(entry)

    return entries

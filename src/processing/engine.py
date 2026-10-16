"""Section 3 signal processing engine.

Produces deterministic 1-second window results:
  - missing_1, missing_2
  - complete
  - mean, min, max per channel (null if no samples)
  - magnitude: mean of sqrt(ch1^2 + ch2^2) over available sensor 2 samples
  - heart_rate: beats per minute from sensor 1 channel 2 using exact 4-condition peak detection
    and 5-second lookback interval.
"""
import math
from typing import Any, Dict, List, Optional, Set, Tuple

from src.common.models import WindowResult

SAMPLES_PER_SECOND = 400
WINDOW_SIZE = 400


class SignalProcessingEngine:
    """Stateful or stateless signal processing engine adhering strictly to Section 3."""

    def __init__(self):
        # Peak cache per session: session_id -> list of detected peak indices
        self._peak_cache: Dict[str, List[int]] = {}
        # Highest sample index evaluated per session
        self._highest_evaluated: Dict[str, int] = {}

    def reset_session(self, session_id: str):
        """Clear peak cache when a session ends or restarts."""
        if session_id in self._peak_cache:
            del self._peak_cache[session_id]
        if session_id in self._highest_evaluated:
            del self._highest_evaluated[session_id]

    def detect_peaks_for_range(
        self,
        session_id: str,
        start_index: int,
        end_index: int,
        sensor1_ch2_samples: Dict[int, int]
    ) -> List[int]:
        """Detect all peaks in sample range [start_index, end_index] for sensor 1 channel 2.
        
        Peak conditions: sample i is a peak when all four hold:
          1. The 800 samples before it (i-800 to i-1), and the sample after it (i+1), are available;
          2. It is larger than both neighbours: val[i] > val[i-1] and val[i] > val[i+1];
          3. It is larger than half of the maximum of the 800 samples before it:
             val[i] > 0.5 * max(val[i-800 : i-1]);
          4. It is at least 100 samples after the previous peak:
             i - last_peak >= 100.
        """
        if session_id not in self._peak_cache:
            self._peak_cache[session_id] = []
            self._highest_evaluated[session_id] = -1

        cached_peaks = self._peak_cache[session_id]

        # Evaluate sequentially from the next un-evaluated sample index
        eval_start = max(0, self._highest_evaluated[session_id] + 1)

        for i in range(eval_start, end_index + 1):
            if i not in sensor1_ch2_samples:
                self._highest_evaluated[session_id] = i
                continue
            val_i = sensor1_ch2_samples[i]

            # Condition 1: sample i+1 must be available
            if (i + 1) not in sensor1_ch2_samples:
                # Cannot determine peak at i without right neighbour; stop evaluation here
                break
            val_next = sensor1_ch2_samples[i + 1]

            # Condition 1: 800 samples before i (i-800 to i-1) must be available
            all_800_available = True
            max_prev_800 = -float("inf")
            for prev_idx in range(i - 800, i):
                if prev_idx not in sensor1_ch2_samples:
                    all_800_available = False
                    break
                p_val = sensor1_ch2_samples[prev_idx]
                if p_val > max_prev_800:
                    max_prev_800 = p_val

            self._highest_evaluated[session_id] = i

            if not all_800_available:
                continue

            val_prev = sensor1_ch2_samples[i - 1]

            # Condition 2: larger than both neighbours
            if not (val_i > val_prev and val_i > val_next):
                continue

            # Condition 3: larger than half of maximum of 800 samples before it
            if not (val_i > 0.5 * max_prev_800):
                continue

            # Condition 4: at least 100 samples after previous peak
            current_last_peak = cached_peaks[-1] if cached_peaks else -1000
            if (i - current_last_peak) < 100:
                continue

            # All 4 conditions satisfied -> sample i is a peak!
            cached_peaks.append(i)

        return cached_peaks

    def process_window(
        self,
        session_id: str,
        window: int,
        mode: str,
        sensor1_samples: Dict[int, List[int]],
        sensor2_samples: Dict[int, List[int]],
        all_sensor1_history: Optional[Dict[int, List[int]]] = None
    ) -> WindowResult:
        """Process a single 1-second window w (samples 400*w to 400*w + 399)."""
        w_start = WINDOW_SIZE * window
        w_end = w_start + WINDOW_SIZE - 1

        # 1. Missing samples count
        s1_in_win = [s for s in range(w_start, w_end + 1) if s in sensor1_samples]
        s2_in_win = [s for s in range(w_start, w_end + 1) if s in sensor2_samples]

        missing_1 = WINDOW_SIZE - len(s1_in_win)
        missing_2 = WINDOW_SIZE - len(s2_in_win)
        complete = (missing_1 == 0 and missing_2 == 0)

        # 2. Per-channel statistics: mean, min, max (null if no samples)
        # Sensor 1: 3 channels. Sensor 2: 2 channels. Total = 5 channels.
        means: List[Optional[float]] = []
        mins: List[Optional[float]] = []
        maxs: List[Optional[float]] = []

        # Sensor 1 channels (0, 1, 2)
        if s1_in_win:
            for ch in range(3):
                vals = [sensor1_samples[s][ch] for s in s1_in_win]
                means.append(round(sum(vals) / len(vals), 1))
                mins.append(float(min(vals)))
                maxs.append(float(max(vals)))
        else:
            means.extend([None, None, None])
            mins.extend([None, None, None])
            maxs.extend([None, None, None])

        # Sensor 2 channels (0, 1)
        if s2_in_win:
            for ch in range(2):
                vals = [sensor2_samples[s][ch] for s in s2_in_win]
                means.append(round(sum(vals) / len(vals), 1))
                mins.append(float(min(vals)))
                maxs.append(float(max(vals)))
        else:
            means.extend([None, None])
            mins.extend([None, None])
            maxs.extend([None, None])

        # 3. Magnitude: Mean of sqrt(ch1^2 + ch2^2) over available sensor 2 samples
        if s2_in_win:
            mags = [
                math.sqrt(sensor2_samples[s][0] ** 2 + sensor2_samples[s][1] ** 2)
                for s in s2_in_win
            ]
            magnitude: Optional[float] = round(sum(mags) / len(mags), 1)
        else:
            magnitude = None

        # 4. Heart-rate calculation:
        # Rate: for window w, take the peaks in the last 5 seconds (samples 400(w-4) to 400(w+1)-1).
        # No rate: heart_rate is null if w < 4, if any sample in those 5 seconds is missing, or if fewer than 2 peaks.
        heart_rate: Optional[float] = None

        if window >= 4:
            five_sec_start = SAMPLES_PER_SECOND * (window - 4)
            five_sec_end = SAMPLES_PER_SECOND * (window + 1) - 1

            # Prepare sensor 1 channel 2 signal pool
            history = all_sensor1_history if all_sensor1_history is not None else sensor1_samples
            s1_ch2_pool: Dict[int, int] = {idx: history[idx][1] for idx in history if len(history[idx]) > 1}

            # Check if ANY sample in the 5-second range is missing
            any_missing_in_5s = False
            for s in range(five_sec_start, five_sec_end + 1):
                if s not in s1_ch2_pool:
                    any_missing_in_5s = True
                    break

            if not any_missing_in_5s:
                # Detect peaks up to five_sec_end
                # Note: evaluating peaks requires lookahead i+1, so sample five_sec_end + 1 is needed
                # to detect if five_sec_end is a peak
                all_peaks = self.detect_peaks_for_range(
                    session_id=session_id,
                    start_index=max(0, five_sec_start - 800),
                    end_index=five_sec_end,
                    sensor1_ch2_samples=s1_ch2_pool
                )

                # Filter peaks that fall inside the 5-second interval [five_sec_start, five_sec_end]
                qualifying_peaks = [p for p in all_peaks if five_sec_start <= p <= five_sec_end]

                if len(qualifying_peaks) >= 2:
                    # Calculate gaps between consecutive peaks in seconds
                    gaps_samples = [
                        qualifying_peaks[k] - qualifying_peaks[k - 1]
                        for k in range(1, len(qualifying_peaks))
                    ]
                    mean_gap_sec = (sum(gaps_samples) / SAMPLES_PER_SECOND) / len(gaps_samples)
                    if mean_gap_sec > 0:
                        calculated_bpm = 60.0 / mean_gap_sec
                        heart_rate = round(calculated_bpm, 1)

        return WindowResult(
            session_id=session_id,
            window=window,
            missing_1=missing_1,
            missing_2=missing_2,
            complete=complete,
            mean=means,
            min=mins,
            max=maxs,
            magnitude=magnitude,
            heart_rate=heart_rate,
            mode=mode
        )

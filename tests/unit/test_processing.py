"""Unit tests for Section 3 signal processing rules and edge cases."""
import math
import unittest

from src.processing.engine import SignalProcessingEngine, WINDOW_SIZE, SAMPLES_PER_SECOND
from src.mock.biosignal_mock import SignalGenerator


class TestSignalProcessingEngine(unittest.TestCase):

    def setUp(self):
        self.engine = SignalProcessingEngine()

    def test_window_0_to_3_null_heart_rate(self):
        """Rule 3: heart_rate is null if w < 4."""
        gen = SignalGenerator(mode="signal", seed=7)
        s1_samples = {}
        s2_samples = {}
        for i in range(1600):
            s1_samples[i] = gen.get_sample(1, i)
            s2_samples[i] = gen.get_sample(2, i)

        for w in range(4):
            res = self.engine.process_window(
                session_id="test_sess",
                window=w,
                mode="offline",
                sensor1_samples=s1_samples,
                sensor2_samples=s2_samples,
                all_sensor1_history=s1_samples
            )
            self.assertIsNone(res.heart_rate, f"Window {w} heart_rate must be null")

    def test_complete_flag_and_missing_counts(self):
        """Verify complete=true when both missing are 0, and missing counts match."""
        s1_samples = {i: [100, 200, 300] for i in range(400)}
        s2_samples = {i: [1000, 2000] for i in range(350)}  # 50 missing from sensor 2

        res = self.engine.process_window(
            session_id="test_sess",
            window=0,
            mode="offline",
            sensor1_samples=s1_samples,
            sensor2_samples=s2_samples
        )
        self.assertEqual(res.missing_1, 0)
        self.assertEqual(res.missing_2, 50)
        self.assertFalse(res.complete)

    def test_magnitude_calculation(self):
        """Magnitude = mean of sqrt(ch1^2 + ch2^2) over available sensor 2 samples."""
        # 3, 4 -> sqrt(9 + 16) = 5
        s2_samples = {0: [3, 4], 1: [30, 40]}  # sqrt(900+1600) = 50. Mean = (5 + 50)/2 = 27.5
        s1_samples = {0: [1, 2, 3], 1: [1, 2, 3]}

        res = self.engine.process_window(
            session_id="test_sess",
            window=0,
            mode="offline",
            sensor1_samples=s1_samples,
            sensor2_samples=s2_samples
        )
        self.assertEqual(res.magnitude, 27.5)

    def test_ecg_heart_rate_detection(self):
        """At minute 0-2 of signal mode, heart rate is 60 bpm (1 peak every 400 samples)."""
        gen = SignalGenerator(mode="signal", seed=7)
        # Generate 6 seconds (windows 0 to 5) = 2400 samples
        s1_samples = {}
        s2_samples = {}
        for i in range(2800):
            s1_samples[i] = gen.get_sample(1, i)
            s2_samples[i] = gen.get_sample(2, i)

        # Window 4: samples 0 to 1999 (5 seconds)
        res4 = self.engine.process_window(
            session_id="test_ecg",
            window=4,
            mode="online",
            sensor1_samples=s1_samples,
            sensor2_samples=s2_samples,
            all_sensor1_history=s1_samples
        )
        self.assertIsNotNone(res4.heart_rate)
        # 60 bpm expected, allow slight discrete rounding +/- 0.5 bpm
        self.assertAlmostEqual(res4.heart_rate, 60.0, delta=0.5)

    def test_missing_sample_in_5s_invalidates_heart_rate(self):
        """Rule 3: heart_rate is null if ANY sample in those 5 seconds is missing."""
        gen = SignalGenerator(mode="signal", seed=7)
        s1_samples = {}
        s2_samples = {}
        for i in range(2500):
            s1_samples[i] = gen.get_sample(1, i)
            s2_samples[i] = gen.get_sample(2, i)

        # Delete exactly one sample at index 500 (inside window 4's 5s span: 0..1999)
        del s1_samples[500]

        res = self.engine.process_window(
            session_id="test_missing_hr",
            window=4,
            mode="online",
            sensor1_samples=s1_samples,
            sensor2_samples=s2_samples,
            all_sensor1_history=s1_samples
        )
        self.assertIsNone(res.heart_rate)

    def test_peak_conditions_strictness(self):
        """Test peak conditions:
        - must be strictly greater than neighbors (not equal)
        - must be > 0.5 * max(800 preceding)
        - must be >= 100 samples after previous peak
        """
        # Baseline = 100
        pool = {i: 100 for i in range(1200)}

        # Flat plateau: samples 900 and 901 are equal (200, 200). Neither is strictly greater than both neighbours!
        pool[900] = 200
        pool[901] = 200
        peaks = self.engine.detect_peaks_for_range("test_plateau", 801, 1000, pool)
        self.assertNotIn(900, peaks)
        self.assertNotIn(901, peaks)

        # Peak at 910 with 150: max of preceding 800 is 200.
        # Condition 3: 150 > 0.5 * 200 = 100.
        # Condition 2: 150 > 100 and 150 > 100.
        pool[910] = 150
        peaks2 = self.engine.detect_peaks_for_range("test_p2", 801, 1000, pool)
        self.assertIn(910, peaks2)

        # Second peak at 950 (only 40 samples later, < 100) -> rejected by Condition 4!
        pool[950] = 160
        peaks3 = self.engine.detect_peaks_for_range("test_p2", 911, 1000, pool)
        self.assertNotIn(950, peaks3)


if __name__ == "__main__":
    unittest.main()

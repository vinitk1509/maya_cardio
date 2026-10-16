"""Unit tests verifying SignalGenerator and GroundTruthChecker for both signal and ramp modes."""
import json
import os
import tempfile
import unittest

from src.mock.biosignal_mock import SignalGenerator, RATE
from src.mock.check_ground_truth import GroundTruthChecker


class TestMockAndGroundTruth(unittest.TestCase):

    def test_ramp_mode_values(self):
        """Verify ramp formula:
        Sensor 1 channel c at sample i = (3i + c) mod 8,388,608 for c in [1, 2, 3]
        Sensor 2 channel c at sample i = (2i + c) mod 8,388,608 for c in [1, 2]
        """
        gen = SignalGenerator(mode="ramp", seed=42)
        s1_0 = gen.get_sample(1, 0)
        self.assertEqual(s1_0, [1, 2, 3])

        s2_0 = gen.get_sample(2, 0)
        self.assertEqual(s2_0, [1, 2])

        s1_10 = gen.get_sample(1, 10)
        self.assertEqual(s1_10, [31, 32, 33])

        s2_10 = gen.get_sample(2, 10)
        self.assertEqual(s2_10, [21, 22])

    def test_signal_mode_channel_ratios(self):
        """Verify signal mode channel relationships:
        Sensor 1: channel 1 and 3 are channel 2 * 0.6 and * 1.3
        """
        gen = SignalGenerator(mode="signal", seed=7)
        for i in range(100):
            s1 = gen.get_sample(1, i)
            ch1, ch2, ch3 = s1
            self.assertEqual(ch1, int(round(ch2 * 0.6)))
            self.assertEqual(ch3, int(round(ch2 * 1.3)))

    def test_ground_truth_checker_pass_and_fail(self):
        """Verify ground truth checker accepts valid export and catches discrepancies/silent loss."""
        checker = GroundTruthChecker(mode="ramp", seed=10)
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".jsonl") as f:
            temp_path = f.name
            # Write 10 valid samples for sensor 1 and 2
            for i in range(10):
                f.write(json.dumps({"sensor": 1, "index": i, "values": checker.generator.get_sample(1, i)}) + "\n")
                f.write(json.dumps({"sensor": 2, "index": i, "values": checker.generator.get_sample(2, i)}) + "\n")

        try:
            res = checker.verify_export_file(temp_path, expected_max_samples=10)
            self.assertTrue(res["passed"])
            self.assertEqual(res["duplicates"], 0)
            self.assertEqual(res["value_mismatches"], 0)
            self.assertEqual(res["silent_losses"], 0)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

        # Test silent loss detection
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".jsonl") as f:
            temp_path = f.name
            # Write samples 0..3 and 5..9 (sample 4 is silently missing)
            for i in [0, 1, 2, 3, 5, 6, 7, 8, 9]:
                f.write(json.dumps({"sensor": 1, "index": i, "values": checker.generator.get_sample(1, i)}) + "\n")
                f.write(json.dumps({"sensor": 2, "index": i, "values": checker.generator.get_sample(2, i)}) + "\n")

        try:
            res = checker.verify_export_file(temp_path, expected_max_samples=10)
            self.assertFalse(res["passed"])
            self.assertGreater(res["silent_losses"], 0)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)


if __name__ == "__main__":
    unittest.main()

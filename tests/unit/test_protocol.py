"""Unit tests for wire protocol parsing and raw data CRC-32 extraction."""
import json
import zlib
import unittest
from src.common.protocol import (
    parse_mock_reply,
    extract_data_array_slice,
    compute_crc32_hex,
    format_read_request,
    format_mock_reply,
    CRCValidationError,
    MalformedMessageError
)


class TestProtocol(unittest.TestCase):

    def test_official_example_crc32(self):
        """Verify exact example from MayaCardio Assessment Section 6.2."""
        raw_wire = (
            b'{"session_id": "5f0c2a9e", "sensor": 1, "first_index": 120000, "count": 3, "lost": [], '
            b'"session_end": false, "crc32": "f9a5bf62", "data": [[720, 1200, 1560], [708, 1180, 1534], [726, 1210, 1573]]}\n'
        )
        raw_slice, start, end = extract_data_array_slice(raw_wire)
        self.assertEqual(raw_slice, b'[[720, 1200, 1560], [708, 1180, 1534], [726, 1210, 1573]]')
        self.assertEqual(compute_crc32_hex(raw_slice), "f9a5bf62")

        parsed = parse_mock_reply(raw_wire, verify_crc=True)
        self.assertEqual(parsed["session_id"], "5f0c2a9e")
        self.assertEqual(parsed["sensor"], 1)
        self.assertEqual(parsed["first_index"], 120000)
        self.assertEqual(parsed["count"], 3)
        self.assertEqual(parsed["crc32"], "f9a5bf62")
        self.assertEqual(len(parsed["data"]), 3)

    def test_corrupted_crc32_rejection(self):
        """Verify that a reply with mismatched CRC32 raises CRCValidationError."""
        corrupted_wire = (
            b'{"session_id": "5f0c2a9e", "sensor": 1, "first_index": 120000, "count": 3, "lost": [], '
            b'"session_end": false, "crc32": "00000000", "data": [[720, 1200, 1560], [708, 1180, 1534], [726, 1210, 1573]]}\n'
        )
        with self.assertRaises(CRCValidationError) as cm:
            parse_mock_reply(corrupted_wire, verify_crc=True)
        self.assertIn("CRC32 mismatch", str(cm.exception))
        self.assertEqual(cm.exception.expected, "00000000")
        self.assertEqual(cm.exception.actual, "f9a5bf62")

    def test_format_and_parse_roundtrip(self):
        """Verify helper serialization produces valid parseable wire line."""
        samples = [[100, 200, 300], [105, 205, 305]]
        wire = format_mock_reply(
            session_id="test_sess_01",
            sensor=1,
            first_index=400,
            data=samples,
            lost=[]
        )
        parsed = parse_mock_reply(wire, verify_crc=True)
        self.assertEqual(parsed["session_id"], "test_sess_01")
        self.assertEqual(parsed["sensor"], 1)
        self.assertEqual(parsed["first_index"], 400)
        self.assertEqual(parsed["data"], samples)

    def test_read_request_formatting(self):
        """Verify cmd read request format."""
        req1 = format_read_request(1)
        self.assertEqual(json.loads(req1.decode()), {"cmd": "read", "sensor": 1})
        self.assertTrue(req1.endswith(b"\n"))

        req2 = format_read_request(2, from_index=120000)
        self.assertEqual(json.loads(req2.decode()), {"cmd": "read", "sensor": 2, "from_index": 120000})
        self.assertTrue(req2.endswith(b"\n"))


if __name__ == "__main__":
    unittest.main()

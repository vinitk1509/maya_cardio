"""Protocol parsing, serialization, and CRC32 verification for the MayaCardio biosignal-mock TCP stream."""
import json
import zlib
from typing import Any, Dict, List, Optional, Tuple


class ProtocolError(Exception):
    """Base protocol exception."""
    pass


class CRCValidationError(ProtocolError):
    """Raised when CRC32 checksum does not match transmitted data text."""
    def __init__(self, expected: str, actual: str):
        super().__init__(f"CRC32 mismatch: expected {expected}, calculated {actual}")
        self.expected = expected
        self.actual = actual


class MalformedMessageError(ProtocolError):
    """Raised when the message cannot be framed or parsed."""
    pass


def compute_crc32_hex(raw_data_bytes: bytes) -> str:
    """Compute zlib CRC-32 over raw data text and return 8-char lowercase hex string."""
    val = zlib.crc32(raw_data_bytes) & 0xFFFFFFFF
    return f"{val:08x}"


def extract_data_array_slice(raw_line_bytes: bytes) -> Tuple[bytes, int, int]:
    """Find the exact slice of the 'data' array from the opening '[' to matching ']'.
    
    This preserves the exact transmitted whitespace and formatting as required by
    MayaCardio specification Section 6.2 / 6.3.
    """
    data_key_idx = raw_line_bytes.find(b'"data"')
    if data_key_idx == -1:
        raise MalformedMessageError("Field 'data' not found in raw payload")

    # Find colon after "data"
    colon_idx = raw_line_bytes.find(b":", data_key_idx + 6)
    if colon_idx == -1:
        raise MalformedMessageError("Malformed JSON: missing colon after 'data'")

    # Find first '[' after colon
    start_bracket = raw_line_bytes.find(b"[", colon_idx)
    if start_bracket == -1:
        raise MalformedMessageError("Malformed JSON: missing opening '[' for 'data'")

    # Walk forward to find matching closing bracket
    depth = 0
    in_string = False
    escape = False

    for idx in range(start_bracket, len(raw_line_bytes)):
        byte = raw_line_bytes[idx:idx+1]
        if in_string:
            if escape:
                escape = False
            elif byte == b"\\":
                escape = True
            elif byte == b'"':
                in_string = False
        else:
            if byte == b'"':
                in_string = True
            elif byte == b"[":
                depth += 1
            elif byte == b"]":
                depth -= 1
                if depth == 0:
                    return raw_line_bytes[start_bracket:idx+1], start_bracket, idx+1

    raise MalformedMessageError("Unterminated array in 'data' field")


def parse_mock_reply(raw_line_bytes: bytes, verify_crc: bool = True) -> Dict[str, Any]:
    """Parse a single newline-delimited JSON reply line from the mock service.
    
    Validates required keys and verifies CRC-32 against the exact data text slice.
    """
    trimmed = raw_line_bytes.strip()
    if not trimmed:
        raise MalformedMessageError("Empty line received")

    # JSON parse entire line
    try:
        parsed = json.loads(trimmed.decode("utf-8"))
    except Exception as e:
        raise MalformedMessageError(f"JSON decode failed: {e}") from e

    # Required fields verification
    required_fields = ["session_id", "sensor", "first_index", "count", "lost", "session_end", "crc32", "data"]
    for field in required_fields:
        if field not in parsed:
            raise MalformedMessageError(f"Missing required field: {field}")

    if verify_crc:
        raw_slice, _, _ = extract_data_array_slice(trimmed)
        calculated_crc = compute_crc32_hex(raw_slice)
        expected_crc = str(parsed["crc32"]).strip().lower()
        if calculated_crc != expected_crc:
            raise CRCValidationError(expected=expected_crc, actual=calculated_crc)

    return parsed


def format_read_request(sensor: int, from_index: Optional[int] = None) -> bytes:
    """Format request to biosignal-mock: {"cmd": "read", "sensor": 1[, "from_index": N]}."""
    req: Dict[str, Any] = {"cmd": "read", "sensor": sensor}
    if from_index is not None:
        req["from_index"] = from_index
    return json.dumps(req).encode("utf-8") + b"\n"


def format_mock_reply(
    session_id: str,
    sensor: int,
    first_index: int,
    data: List[List[int]],
    lost: Optional[List[Dict[str, Any]]] = None,
    session_end: bool = False,
    corrupt_crc: bool = False
) -> bytes:
    """Helper to produce an exact formatted mock reply wire line, with exact CRC32."""
    if lost is None:
        lost = []

    # Serialize data array first
    data_str = json.dumps(data)
    crc_hex = compute_crc32_hex(data_str.encode("utf-8"))

    if corrupt_crc:
        # Invert or alter hex
        crc_hex = "00000000" if crc_hex != "00000000" else "ffffffff"

    reply_dict = {
        "session_id": session_id,
        "sensor": sensor,
        "first_index": first_index,
        "count": len(data),
        "lost": lost,
        "session_end": session_end,
        "crc32": crc_hex,
        "data": data
    }

    # Format cleanly with data exactly matching data_str
    # Using json.dumps ensures valid JSON
    # To ensure exact match with data_str:
    # json.dumps without extra spaces around data
    wire = json.dumps(reply_dict, separators=(', ', ': ')) + "\n"
    
    # Verify that the slice in wire matches data_str exactly
    raw_slice, _, _ = extract_data_array_slice(wire.encode("utf-8"))
    actual_slice_crc = compute_crc32_hex(raw_slice)
    if not corrupt_crc and actual_slice_crc != crc_hex:
        # If python json.dumps formatted data with different spacing, rebuild explicitly
        wire = (
            f'{{"session_id": "{session_id}", "sensor": {sensor}, '
            f'"first_index": {first_index}, "count": {len(data)}, '
            f'"lost": {json.dumps(lost)}, "session_end": {"true" if session_end else "false"}, '
            f'"crc32": "{crc_hex}", "data": {data_str}}}\n'
        )
    return wire.encode("utf-8")

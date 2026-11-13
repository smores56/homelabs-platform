"""Minimal RFC 6455 client framing.

Only what the enqueue needs: a masked text frame out, text frames in, ping/pong,
a close handshake. The acceptance example is from RFC 6455 §1.3.
"""

from __future__ import annotations

import unittest

from webhook.ws import accept_for, build_frame, parse_frame


class HandshakeTests(unittest.TestCase):
    def test_accept_key_matches_the_rfc_example(self) -> None:
        self.assertEqual(accept_for("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")


class FrameTests(unittest.TestCase):
    def test_a_masked_client_text_frame_round_trips(self) -> None:
        frame = build_frame(0x1, b'{"hello":1}', mask=b"\x01\x02\x03\x04")
        self.assertEqual(frame[1] & 0x80, 0x80, "client frames are masked")
        parsed = parse_frame(frame)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        fin, opcode, payload, consumed = parsed
        self.assertTrue(fin)
        self.assertEqual(opcode, 0x1)
        self.assertEqual(payload, b'{"hello":1}')
        self.assertEqual(consumed, len(frame))

    def test_an_unmasked_server_frame_round_trips(self) -> None:
        frame = build_frame(0x1, b"ready", mask=None)
        parsed = parse_frame(frame)
        assert parsed is not None
        self.assertEqual(parsed[2], b"ready")

    def test_a_frame_needing_the_16_bit_length_round_trips(self) -> None:
        payload = b"x" * 200
        frame = build_frame(0x1, payload, mask=b"mask")
        parsed = parse_frame(frame)
        assert parsed is not None
        self.assertEqual(parsed[2], payload)

    def test_a_frame_needing_the_64_bit_length_round_trips(self) -> None:
        payload = b"y" * 70000
        frame = build_frame(0x1, payload, mask=b"mask")
        parsed = parse_frame(frame)
        assert parsed is not None
        self.assertEqual(parsed[2], payload)

    def test_an_incomplete_frame_yields_nothing(self) -> None:
        frame = build_frame(0x1, b"partial", mask=None)
        self.assertIsNone(parse_frame(frame[:-2]))

    def test_an_empty_payload_round_trips(self) -> None:
        parsed = parse_frame(build_frame(0x1, b"", mask=b"mask"))
        assert parsed is not None
        self.assertEqual(parsed[2], b"")


if __name__ == "__main__":
    unittest.main()

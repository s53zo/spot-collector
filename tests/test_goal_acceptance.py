"""Independent consumer-level contracts, derived from the agreed feed behavior."""
import calendar
import re
import unittest

from spot_processing import SpotProcessor


class GoalAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.now = calendar.timegm((2026, 11, 16, 12, 0, 0))

    def gc_line(self, call="JA1ABC", frequency="14020.00", confidence="V", stamp="1200Z"):
        prefix = "DX de DL1AAA-#:"
        header = prefix + " " * (25 - len(prefix) - len(frequency)) + frequency
        header += "  " + call
        body = header.ljust(39) + "CW 21 dB original GC text"
        return (body.ljust(62) + "   PM95 " + confidence + " " + stamp).encode()

    def test_singleton_and_returned_copy_do_not_create_another_alert(self):
        engine = SpotProcessor(clock=lambda: self.now)
        local = b"DX de TK0C-1: 14020.120 JA1ABC CW 12 dB original local 1200Z"
        self.assertEqual(engine.process("20m", "local", local), [local])
        self.assertEqual(engine.process("primary", "cluster", local), [])
        self.assertEqual(engine.process("gc", "gocluster", self.gc_line()), [])

    def test_frequency_preference_changes_header_without_rebuilding_payload(self):
        engine = SpotProcessor({"repeat_seconds": 1}, clock=lambda: self.now)
        reference = self.gc_line()
        self.assertEqual(engine.process("gc", "gocluster", reference), [reference])
        other = reference.replace(b"DL1AAA", b"F1BBBB")
        self.assertEqual(engine.process("gc", "gocluster", other), [])
        self.now += 2
        local = b"DX de TK0C-1: 14020.120 JA1ABC CW 12 dB Q:3 odd  spacing \xff 1200Z"
        result = engine.process("20m", "local", local)
        self.assertEqual(len(result), 1)
        match = re.match(rb"DX de \S+:\s+(\d+\.\d+)\s+(\S+)(.*)", result[0])
        original = re.match(rb"DX de \S+:\s+(\d+\.\d+)\s+(\S+)(.*)", local)
        self.assertIsNotNone(match)
        self.assertEqual(float(match.group(1)), 14020.0)
        self.assertEqual(match.group(2), b"JA1ABC")
        self.assertEqual(match.group(3), original.group(3))


    def test_equal_minute_move_requires_two_distinct_receivers(self):
        engine = SpotProcessor(clock=lambda: self.now)
        first = b"DX de TK0C-1: 14020.000 JA1ABC CW 12 dB CQ 1200Z"
        self.assertEqual(engine.process('local', 'local', first), [first])
        self.now += 1
        self.assertEqual(engine.process('local', 'local', first.replace(b'14020.000', b'14021.000')), [])
        selected = engine.process('gc', 'gocluster', self.gc_line(frequency='14021.00'))
        self.assertEqual(len(selected), 1)
        self.assertEqual(engine.snapshot()['frequency_change_updates'], 1)

    def test_original_external_support_blocks_local_call_replacement(self):
        # The local-quality policy now gives independently supported originals a veto.
        engine = SpotProcessor(clock=lambda: self.now)
        self.now += 10
        engine.process("primary", "cluster", b"DX de F1AAA: 14020.00 JA1ABC CW 12 dB CQ 1200Z")
        engine.process("primary", "cluster", b"DX de F1AAA: 14020.40 JA1ABD CW 12 dB CQ 1200Z")
        self.now += 170
        engine.process("primary", "cluster", b"DX de F1AAA: 14020.00 JA1ABC CW 12 dB CQ 1203Z")
        engine.process("gc", "gocluster", self.gc_line("JA1ABD", "14020.40", "C", "1203Z"))
        self.now += 11
        raw = b"DX de TK0C-1: 14020.00 JA1ABC CW 12 dB original 1203Z"
        result = engine.process("local", "local", raw)
        self.assertEqual(len(result), 1)
        self.assertIn(b"14020.00 JA1ABC", result[0])
        # Even a C candidate cannot overwrite a supported original identity.
        self.assertEqual(result[0].split(b"CW", 1)[1], raw.split(b"CW", 1)[1])


if __name__ == "__main__":
    unittest.main()

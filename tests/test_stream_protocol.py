import unittest
from stream_protocol import LineFramer, TelnetDecoder, ProtocolError, login_prompt, encode_telnet


class StreamTests(unittest.TestCase):
    def test_all_split_positions_preserve_application_lines(self):
        wire = b'one\r\ntwo\r\x00three\rfour\nfive\xff\xfb\x01!\r\n'
        expected = [b'one', b'two', b'three', b'four', b'five!']
        for split in range(len(wire) + 1):
            decoder, framer = TelnetDecoder(), LineFramer()
            actual = []
            for part in (wire[:split], wire[split:]):
                actual.extend(framer.feed(decoder.feed(part)))
            self.assertEqual(actual, expected, split)

    def test_telnet_subnegotiation_and_escaped_iac_bytewise(self):
        decoder = TelnetDecoder()
        wire = b'a\xff\xfa\x18hidden\r\n\xff\xf0b\xff\xffc'
        self.assertEqual(b''.join(decoder.feed(bytes([x])) for x in wire), b'ab\xffc')

    def test_complete_prompt_only(self):
        self.assertTrue(login_prompt(b'Please enter your callsign: '))
        self.assertFalse(login_prompt(b'Welcome, all callsigns are accepted.'))
        framer = LineFramer()
        framer.feed(b'lo')
        self.assertIsNone(framer.take_prompt())
        framer.feed(b'gin: ')
        self.assertEqual(framer.take_prompt(), b'login: ')
        self.assertEqual(framer.feed(b'DX de A: spot\r\n'), [b'DX de A: spot'])

    def test_envelopes_fail_closed_without_fragment_leak(self):
        with self.assertRaises(ProtocolError):
            LineFramer(3).feed(b'1234')
        with self.assertRaises(ProtocolError):
            TelnetDecoder(3).feed(b'\xff\xfa1234')

    def test_cluster_prompt_boundaries_all_split_positions(self):
        prompt=b'TK0C de NODE 16-Nov-2026 12:00 UTC> '
        spot=b'DX de DL1AAA: 14020 JA1ABC CW comment > intact 1200Z'
        wire=prompt+spot+b'\r\n'
        expected=[prompt.rstrip(),spot]
        for split in range(len(wire)+1):
            framer=LineFramer(cluster_prompts=True)
            self.assertEqual(framer.feed(wire[:split])+framer.feed(wire[split:]),expected,split)

    def test_telnet_application_encode_roundtrip(self):
        original=b'plain \xffX \xff\xff end'
        wire=encode_telnet(original)
        for split in range(len(wire)+1):
            decoder=TelnetDecoder()
            self.assertEqual(decoder.feed(wire[:split])+decoder.feed(wire[split:]),original)

    def test_login_prompt_continuation_separator_does_not_prefix_data(self):
        framer=LineFramer(cluster_prompts=True)
        self.assertEqual(framer.feed(b'login:'),[])
        self.assertEqual(framer.take_prompt(),b'login:')
        self.assertEqual(framer.feed(b' TK0C de NODE> DX de A: unchanged > text\r\n'),
                         [b'TK0C de NODE>',b'DX de A: unchanged > text'])

"""Incremental Telnet decoding and bounded application-line framing."""
import re


class ProtocolError(ValueError):
    """The peer exceeded the bounded protocol envelope."""


class TelnetDecoder:
    def __init__(self, max_control_bytes=65536, client_interrupts=False):
        self.client_interrupts = client_interrupts
        self.state = 'data'
        self.control_bytes = 0
        self.max_control_bytes = max_control_bytes

    def feed(self, data):
        out = bytearray()
        for byte in data:
            if self.state != 'data':
                self.control_bytes += 1
                if self.control_bytes > self.max_control_bytes:
                    raise ProtocolError('Telnet control sequence too long')
            if self.state == 'data':
                if byte == 255:
                    self.state = 'iac'
                    self.control_bytes = 0
                else:
                    out.append(byte)
            elif self.state == 'iac':
                if byte == 255:
                    out.append(255)
                    self.state = 'data'
                elif byte in (251, 252, 253, 254):
                    self.state = 'option'
                elif byte == 250:
                    self.state = 'subnegotiation'
                elif byte == 244 and self.client_interrupts:
                    # Telnet Interrupt Process is Ctrl+C for a client session.
                    # Preserve its position relative to preceding commands.
                    out.append(3)
                    self.state = 'data'
                else:
                    self.state = 'data'
            elif self.state == 'option':
                self.state = 'data'
            elif self.state == 'subnegotiation':
                if byte == 255:
                    self.state = 'subnegotiation_iac'
            elif self.state == 'subnegotiation_iac':
                self.state = 'data' if byte == 240 else 'subnegotiation'
        return bytes(out)


class LineFramer:
    def __init__(self, max_line_bytes=65536, cluster_prompts=False):
        self.pending = bytearray()
        self.after_cr = False
        self.max_line_bytes = max_line_bytes
        self.cluster_prompts = cluster_prompts
        self.after_prompt = False

    def feed(self, data):
        lines = []
        for byte in data:
            if self.after_prompt:
                if byte in (9, 32):
                    continue
                self.after_prompt = False
                if byte in (10, 13):
                    self.after_cr = byte == 13
                    continue
            if self.after_cr:
                self.after_cr = False
                if byte in (0, 10):
                    continue
            if byte in (10, 13):
                lines.append(bytes(self.pending))
                self.pending.clear()
                self.after_cr = byte == 13
            else:
                self.pending.append(byte)
                if len(self.pending) > self.max_line_bytes:
                    raise ProtocolError('application line too long')
                if self.cluster_prompts and byte == 62 and cluster_prompt(bytes(self.pending)):
                    # A structured command prompt is a protocol boundary even
                    # without CRLF. Never search inside an ordinary spot line.
                    lines.append(bytes(self.pending))
                    self.pending.clear()
                    self.after_prompt = True
        return lines

    def take_prompt(self):
        """Consume only a recognized, complete unterminated login prompt."""
        value = bytes(self.pending)
        if login_prompt(value):
            self.pending.clear()
            # The prompt can end at a chunk boundary before its trailing
            # space/CRLF arrives. Consume that continuation as a separator,
            # rather than attaching it to the next application line.
            self.after_prompt = True
            return value
        return None


_LOGIN = re.compile(rb'\s*(?:(?:please\s+)?(?:enter|type)(?:\s+your)?\s+)?(?:call(?:sign)?|login|sign)\s*:\s*', re.I)


def login_prompt(value):
    return bool(_LOGIN.fullmatch(value))


_CLUSTER_PROMPT = re.compile(
    rb'[A-Z0-9/-]+\s+de\s+[A-Z0-9/-]+'
    rb'(?:\s+\d{1,2}-[A-Za-z]{3}-\d{4}\s+\d{2}:\d{2}(?::\d{2})?\s+UTC)?\s*>',
    re.I)


def cluster_prompt(value):
    return bool(_CLUSTER_PROMPT.fullmatch(value))


def encode_telnet(data):
    """Encode application bytes only; raw negotiation commands bypass this."""
    return data.replace(b'\xff', b'\xff\xff')

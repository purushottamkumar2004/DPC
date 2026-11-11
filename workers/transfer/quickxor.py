"""quickXorHash: the file hash OneDrive for work or school and SharePoint report for every file
(driveItem.file.hashes.quickXorHash). We compute it locally to prove that what Graph stored is
byte-for-byte what we sent (upload) or what we received is what Graph stored (download).

The algorithm (Microsoft's published reference): a 160-bit value; byte number k of the file is
XORed in at bit offset (11 * k) mod 160, wrapping around; at the end the file length (8 bytes,
little-endian) is XORed into the last 8 of the 20 result bytes; the result is sent as base64.

Because 11 and 160 have no common factor, the offset depends only on k mod 160. So we first XOR
together all bytes with the same k mod 160 (160 running columns, fast with numpy), and place
the 160 column values at their offsets only once, at the end.

    h = QuickXorHash(); h.update(data); ...; h.base64()
    quickxor_of_file(path) -> base64 text
"""

import base64

try:
    import numpy as np
except ImportError:                       # pure-Python fallback: correct, but slow on big files
    np = None

WIDTH_BITS = 160
SHIFT = 11
_MASK = (1 << WIDTH_BITS) - 1
_READ_BYTES = 8 * 1024 * 1024


class QuickXorHash:
    def __init__(self):
        self.columns = bytearray(WIDTH_BITS)      # XOR of every byte whose position % 160 == column
        self.length = 0

    def update(self, data: bytes) -> None:
        if not data:
            return
        start = self.length % WIDTH_BITS
        if np is not None:
            self._update_numpy(data, start)
        else:
            for i, byte in enumerate(data):
                self.columns[(start + i) % WIDTH_BITS] ^= byte
        self.length += len(data)

    def _update_numpy(self, data: bytes, start: int) -> None:
        # Pad the front so the first byte lands in its column, pad the end to whole rows of 160.
        tail = (-(start + len(data))) % WIDTH_BITS
        padded = np.zeros(start + len(data) + tail, dtype=np.uint8)
        padded[start:start + len(data)] = np.frombuffer(data, dtype=np.uint8)
        folded = np.bitwise_xor.reduce(padded.reshape(-1, WIDTH_BITS), axis=0)
        for column in np.nonzero(folded)[0]:
            self.columns[int(column)] ^= int(folded[column])

    def digest(self) -> bytes:
        value = 0
        for column, byte in enumerate(self.columns):
            if byte:
                placed = byte << ((SHIFT * column) % WIDTH_BITS)
                value ^= (placed & _MASK) | (placed >> WIDTH_BITS)      # rotate within 160 bits
        result = bytearray(value.to_bytes(WIDTH_BITS // 8, "little"))
        for i, byte in enumerate(self.length.to_bytes(8, "little")):
            result[WIDTH_BITS // 8 - 8 + i] ^= byte
        return bytes(result)

    def base64(self) -> str:
        return base64.b64encode(self.digest()).decode("ascii")


def quickxor_of_file(path: str) -> str:
    h = QuickXorHash()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(_READ_BYTES), b""):
            h.update(block)
    return h.base64()

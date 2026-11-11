"""quickXorHash against vectors from an independent reference implementation (the
`quickxorhash` C extension, run once when these tests were written)."""
import os
import unittest

from workers.transfer.tests import helpers  # noqa: F401  (sets sys.path)
import quickxor

VECTORS = [
    (b"", "AAAAAAAAAAAAAAAAAAAAAAAAAAA="),
    (b"a", "YQAAAAAAAAAAAAAAAQAAAAAAAAA="),
    (b"hello world" * 1000, "pvxopSpn6jVf+X9i62ZgQYuxSUw="),
    (bytes(range(256)) * 3, "rxAOGe1RimTF/e+k/m0O5nnSZT8="),
]


class QuickXor(unittest.TestCase):
    def test_reference_vectors(self):
        for data, expected in VECTORS:
            h = quickxor.QuickXorHash()
            h.update(data)
            self.assertEqual(h.base64(), expected, f"{len(data)} bytes")

    def test_any_split_gives_the_same_hash(self):
        data = bytes(range(256)) * 3
        for split in (1, 7, 159, 160, 161, 500):
            h = quickxor.QuickXorHash()
            for i in range(0, len(data), split):
                h.update(data[i:i + split])
            self.assertEqual(h.base64(), VECTORS[3][1], f"split {split}")

    def test_pure_python_fallback_matches(self):
        saved = quickxor.np
        quickxor.np = None
        try:
            for data, expected in VECTORS:
                h = quickxor.QuickXorHash()
                h.update(data)
                self.assertEqual(h.base64(), expected)
        finally:
            quickxor.np = saved

    def test_file(self):
        root = helpers.temp_root(self)
        path = os.path.join(root, "f.bin")
        with open(path, "wb") as f:
            f.write(b"hello world" * 1000)
        self.assertEqual(quickxor.quickxor_of_file(path), VECTORS[2][1])


if __name__ == "__main__":
    unittest.main()

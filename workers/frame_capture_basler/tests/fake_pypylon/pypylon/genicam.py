"""Fake pypylon.genicam for tests: only what FrameCapture_Basler uses."""


def IsWritable(node):
    return getattr(node, "writable", True)

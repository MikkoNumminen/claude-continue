"""Shared test helpers. Imported first by every test module to put src/ on the path."""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture(name):
    import json

    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


class FakeClock:
    """A controllable UTC clock whose ``sleep`` advances time."""

    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += timedelta(seconds=seconds)


def utc(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)

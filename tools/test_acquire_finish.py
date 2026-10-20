"""Offline provider retry timing tests: no network traffic."""
import unittest
from datetime import datetime,timezone,timedelta
from acquire_finish import retry_time

class RetryTests(unittest.TestCase):
    def test_retry_after_seconds_and_http_date(self):
        now=datetime(2026,10,12,tzinfo=timezone.utc)
        self.assertEqual(retry_time('900',now,0)[0],now+timedelta(seconds=900))
        self.assertEqual(retry_time('Mon, 12 Oct 2026 01:00:00 GMT',now,0)[0],now+timedelta(hours=1))
    def test_past_date_and_malformed_fallback(self):
        now=datetime(2026,10,12,tzinfo=timezone.utc)
        self.assertEqual(retry_time('Sun, 11 Oct 2026 01:00:00 GMT',now,0)[0],now)
        self.assertEqual(retry_time('bad',now,1)[0],now+timedelta(seconds=907))

if __name__=='__main__':unittest.main()

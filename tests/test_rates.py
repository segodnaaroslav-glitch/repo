import tempfile
import unittest
from pathlib import Path

from supreme import fetcher, rates


class RateTests(unittest.TestCase):
    def test_parse_variants(self):
        self.assertEqual(rates.parse_rapira({"data": [{"symbol": "BTC/USDT", "close": 1},
                                                      {"symbol": "USDT/RUB", "close": "81.5"}]}), 81.5)
        self.assertEqual(rates.parse_rapira([{"symbol": "usdt_rub", "askPrice": 82, "bidPrice": 80}]), 81.0)
        self.assertIsNone(rates.parse_rapira({"data": [{"symbol": "BTC/USDT", "close": 1}]}))

    def test_watcher_keeps_last_rate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "values.json"
            answers = [81.2, fetcher.FetchError("нет сети")]

            def fetch():
                answer = answers.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return answer

            watcher = rates.RateWatcher(path, fetch=fetch)
            self.assertTrue(watcher.update())
            self.assertFalse(watcher.update())
            status = watcher.status()
            self.assertEqual((status["usd_rub"], status["error"]), (81.2, "нет сети"))
            again = rates.RateWatcher(path, fetch=lambda: 1 / 0)  # перезапуск: курс с прошлого раза
            self.assertEqual(again.status()["usd_rub"], 81.2)


if __name__ == "__main__":
    unittest.main()

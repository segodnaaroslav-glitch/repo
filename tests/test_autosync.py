import tempfile
import time
import unittest
from pathlib import Path

from supreme import autosync, liquidity, parser, store


def wait_until(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class FakeJob:
    def __init__(self):
        self.running = False
        self.started = 0

    def start(self):
        self.started += 1
        return True


class AutoSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "values.json"
        self.job = FakeJob()
        self.site_date = "October 5th, 2026 at 12:49 PM"

    def tearDown(self):
        self.tmp.cleanup()

    def sync(self):
        return autosync.AutoSync(self.job, self.path, check_site=lambda: self.site_date, first_after=0)

    def save(self, date, fetched_at=None):
        data = store.empty_data()
        store.replace_category(data, "godlies", [parser.make_item("Alpha", "godlies", {"value": "5"})], "site")
        data["site_last_updated"] = date
        data["fetched_at"] = fetched_at or store.now_iso()
        store.save(data, self.path)

    def test_no_data_triggers_update(self):
        sync = self.sync()
        sync._tick()
        self.assertEqual(self.job.started, 1)
        self.assertIn("цен ещё нет", sync.status()["last_result"])

    def test_same_date_does_nothing(self):
        self.save(self.site_date)
        sync = self.sync()
        sync._tick()
        self.assertEqual(self.job.started, 0)
        self.assertEqual(sync.status()["last_result"], "на сайте без изменений")

    def test_new_date_on_site_triggers_update(self):
        self.save("October 4th, 2026 at 1:00 PM")
        sync = self.sync()
        sync._tick()
        self.assertEqual(self.job.started, 1)
        self.assertIn("новые цены", sync.status()["last_result"])

    def test_old_data_triggers_update(self):
        self.save(self.site_date, fetched_at="2026-01-01T00:00:00+00:00")
        sync = self.sync()
        sync._tick()
        self.assertEqual(self.job.started, 1)

    def test_site_error_is_reported(self):
        self.save(self.site_date)
        sync = autosync.AutoSync(self.job, self.path, check_site=lambda: (_ for _ in ()).throw(OSError("нет сети")))
        sync._tick()
        self.assertEqual(self.job.started, 0)
        self.assertIn("нет сети", sync.status()["last_result"])

    def test_running_job_is_not_restarted(self):
        self.job.running = True
        sync = self.sync()
        sync._tick()
        self.assertEqual((self.job.started, sync.status()["last_result"]), (0, "идёт обновление"))

    def test_loop_runs_and_check_now_wakes_it(self):
        self.save(self.site_date)
        sync = autosync.AutoSync(self.job, self.path, check_site=lambda: self.site_date, first_after=3600)
        sync.start()
        try:
            self.assertIsNone(sync.status()["last_check_at"])
            sync.check_now()
            self.assertTrue(wait_until(lambda: sync.status()["last_check_at"] is not None))
            self.site_date = "October 9th, 2026 at 9:00 AM"
            sync.check_now()
            self.assertTrue(wait_until(lambda: self.job.started == 1))
        finally:
            sync.stop()


class JobWithStatus(FakeJob):
    def __init__(self):
        super().__init__()
        self.error = None
        self.finished_at = None

    def status(self):
        return {"running": self.running, "error": self.error, "finished_at": self.finished_at}


class AutoSyncReviewTests(AutoSyncTests):
    def test_failed_categories_are_retried_later(self):
        self.save(self.site_date, fetched_at="2026-01-01T00:00:00")  # без часового пояса — не падать
        sync = self.sync()
        sync.max_age = 10 ** 9
        data = store.load(self.path)
        data["errors"] = [{"category": "godlies", "title": "Godlies", "message": "нет связи"}]
        store.save(data, self.path)
        sync._tick()
        self.assertEqual(self.job.started, 1)
        self.assertIn("не все категории", sync.status()["last_result"])

    def test_failures_back_off(self):
        self.job = JobWithStatus()
        sync = self.sync()
        sync._tick()  # цен нет — первое обновление
        self.assertEqual(self.job.started, 1)
        self.job.error, self.job.finished_at = "сайт недоступен", "t1"
        sync._tick()  # обновление не удалось — пауза, без повтора каждые 5 минут
        self.assertEqual(self.job.started, 1)
        self.assertIn("не удалось 1 раз", sync.status()["last_result"])
        sync.pause_until = autosync._now()  # пауза прошла
        sync._tick()
        self.assertEqual(self.job.started, 2)
        self.job.error, self.job.finished_at = None, "t2"
        sync._tick()
        self.assertEqual((sync.failures, sync.pause_until), (0, None))

    def test_locked_file_is_not_corruption(self):
        from unittest import mock
        self.save(self.site_date)
        sync = self.sync()
        with mock.patch.object(store, "load", side_effect=PermissionError("занят")):
            sync._tick()
        self.assertEqual(self.job.started, 0)
        self.assertIn("занят", sync.status()["last_result"])

    def test_unexpected_error_does_not_kill_the_loop(self):
        sync = autosync.AutoSync(self.job, self.path, check_site=lambda: 1 / 0, first_after=0)
        self.save(self.site_date)
        sync._decide = lambda: (_ for _ in ()).throw(RuntimeError("сбой"))
        sync._tick()
        self.assertIn("ошибка проверки: сбой", sync.status()["last_result"])


class SecretTests(unittest.TestCase):
    def test_placeholder_million_is_secret(self):
        item = parser.make_item("Batwing", "godlies", {"value": "1,000,000", "demand": "10", "rarity": "10"})
        self.assertTrue(item["secret"])
        self.assertEqual(liquidity.assess(item)["level"], liquidity.SECRET)
        self.assertFalse(parser.make_item("Evergun", "godlies", {"value": "3,450"})["secret"])

    def test_old_file_gets_secret_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v.json"
            path.write_text('{"items": [{"name": "Black Luger", "category": "godlies", "value": 1000000}]}', encoding="utf-8")
            self.assertTrue(store.load(path)["items"][0]["secret"])


if __name__ == "__main__":
    unittest.main()

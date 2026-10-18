"""Доступ с другого компьютера по паролю (remote.py и проверки сервера)."""

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from supreme import remote, server, store


class RemoteSettingsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)

    def test_password_is_stored_as_a_hash_and_sessions_follow_it(self):
        self.assertFalse(remote.load(self.folder)["enabled"])
        with self.assertRaises(ValueError):
            remote.set_enabled(self.folder, True)  # без пароля не включается
        with self.assertRaises(ValueError):
            remote.set_password(self.folder, "12345")  # короткий
        settings = remote.set_password(self.folder, "секрет123")
        self.assertNotIn("секрет123", (self.folder / remote.SETTINGS).read_text(encoding="utf-8"))
        self.assertTrue(remote.check_password(settings, "секрет123"))
        self.assertFalse(remote.check_password(settings, "секрет124"))
        self.assertFalse(remote.check_password(settings, None))
        remote.set_enabled(self.folder, True)
        self.assertTrue(remote.load(self.folder)["enabled"])
        token = remote.make_session(settings, now=1_000_000)
        self.assertTrue(remote.check_session(settings, token, now=1_000_000 + 3600))
        self.assertFalse(remote.check_session(settings, token, now=1_000_000 + 31 * 86400))  # устарел
        self.assertFalse(remote.check_session(settings, token[:-1] + ("0" if token[-1] != "0" else "1"), now=1_000_000))
        self.assertFalse(remote.check_session(settings, "v1.x.y"))
        changed = remote.set_password(self.folder, "другой-пароль")
        self.assertFalse(remote.check_session(changed, token, now=1_000_000))  # смена пароля — вход заново
        self.assertTrue(remote.load(self.folder)["enabled"])

    def test_guessing_is_slowed_down(self):
        clock = [1000.0]
        guard = remote.LoginGuard(clock=lambda: clock[0])
        for _ in range(guard.FREE - 1):
            guard.failed("1.2.3.4")
            self.assertEqual(guard.wait("1.2.3.4"), 0)
        guard.failed("1.2.3.4")
        self.assertEqual(guard.wait("1.2.3.4"), guard.FIRST_PAUSE)
        self.assertEqual(guard.wait("5.6.7.8"), 0)  # другой адрес — свой счёт
        clock[0] += guard.FIRST_PAUSE
        self.assertEqual(guard.wait("1.2.3.4"), 0)
        guard.failed("1.2.3.4")
        self.assertEqual(guard.wait("1.2.3.4"), guard.FIRST_PAUSE * 2)  # пауза растёт
        guard.passed("1.2.3.4")
        self.assertEqual(guard.wait("1.2.3.4"), 0)

    def test_links_are_network_addresses(self):
        for link in remote.links(8765):
            self.assertRegex(link, r"^http://\d+\.\d+\.\d+\.\d+:8765/$")
            self.assertNotIn("127.0.0.1", link)


class _ServerMixin:
    def start(self, remote_on):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        store.save(store.empty_data(), self.folder / "values.json")
        httpd = server.make_server(port=0, data_path=self.folder / "values.json", update=lambda log: None,
                                   remote_on=remote_on)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.port = httpd.server_address[1]
        # Проверка соединяется с этого же компьютера; «другой компьютер» — подменой проверки адреса.
        self.from_other = False
        httpd.RequestHandlerClass._loopback = lambda handler: not self.from_other
        return httpd

    def request(self, method, path, body=None, headers=None, other=False, cookie=None):
        self.from_other = other
        headers = dict(headers or {})
        if body is not None:
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, dict(response.getheaders()), data

    def login(self, password):
        status, headers, data = self.request("POST", "/api/login", {"password": password}, other=True)
        cookie = headers.get("Set-Cookie", "").split(";", 1)[0] if status == 200 else None
        return status, cookie, data


class RemoteServerTests(_ServerMixin, unittest.TestCase):
    def test_off_by_default_other_computers_are_refused(self):
        self.start(remote_on=False)
        self.assertEqual(self.request("GET", "/", other=True)[0], 403)
        self.assertEqual(self.request("POST", "/api/login", {"password": "x"}, other=True)[0], 403)
        status, _, data = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"MM2 Values", data)

    def test_password_login_from_another_computer(self):
        self.start(remote_on=True)
        status, _, data = self.request("GET", "/", other=True)
        self.assertEqual(status, 200)
        self.assertIn("Введите пароль".encode("utf-8"), data)  # страница входа, а не программа
        self.assertNotIn(b"app.js", data)
        self.assertEqual(self.request("GET", "/api/data", other=True)[0], 401)
        self.assertEqual(self.request("GET", "/app.js", other=True)[0], 401)
        self.assertEqual(self.login("secret-pass")[0], 401)  # пароль ещё не задан
        # Пароль задают на этом компьютере (с другого — нельзя).
        self.assertEqual(self.request("POST", "/api/remote", {"password": "123"})[0], 400)
        status, _, data = self.request("POST", "/api/remote", {"password": "secret-pass", "enabled": True})
        self.assertEqual(status, 200)
        state = json.loads(data)
        self.assertEqual((state["enabled"], state["active"], state["password"], state["local"]), (True, True, True, True))
        self.assertEqual(self.request("POST", "/api/remote", {"enabled": False}, other=True)[0], 401)
        self.assertEqual(self.login("wrong-pass")[0], 401)
        status, cookie, _ = self.login("secret-pass")
        self.assertEqual(status, 200)
        self.assertTrue(cookie.startswith(remote.COOKIE + "="))
        status, _, data = self.request("GET", "/", other=True, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn(b"app.js", data)  # сама программа
        self.assertEqual(self.request("GET", "/api/data", other=True, cookie=cookie)[0], 200)
        state = json.loads(self.request("GET", "/api/remote", other=True, cookie=cookie)[2])
        self.assertEqual((state["local"], state["links"]), (False, []))
        same = {"Origin": f"http://127.0.0.1:{self.port}"}
        self.assertEqual(self.request("POST", "/api/calc", {"text": "x"}, headers=same, other=True, cookie=cookie)[0], 200)
        evil = {"Origin": "http://evil.example"}
        self.assertEqual(self.request("POST", "/api/calc", {"text": "x"}, headers=evil, other=True, cookie=cookie)[0], 403)
        self.assertEqual(self.request("POST", "/api/remote", {"enabled": False}, other=True, cookie=cookie)[0], 403)
        # Смена пароля на этом компьютере — другой компьютер входит заново.
        self.request("POST", "/api/remote", {"password": "new-secret"})
        self.assertEqual(self.request("GET", "/api/data", other=True, cookie=cookie)[0], 401)
        status, headers, _ = self.request("POST", "/api/logout", {}, other=True, cookie=self.login("new-secret")[1])
        self.assertIn("Max-Age=0", headers.get("Set-Cookie", ""))

    def test_browser_that_hides_the_page_address(self):
        # Браузер с запретом передачи адреса страницы: Origin: null при входе и в запросах программы.
        self.start(remote_on=True)
        self.request("POST", "/api/remote", {"password": "secret-pass", "enabled": True})
        hidden = {"Origin": "null"}
        status, headers, _ = self.request("POST", "/api/login", {"password": "secret-pass"}, headers=hidden, other=True)
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.request("POST", "/api/calc", {"text": "x"}, headers=hidden, other=True, cookie=cookie)[0], 200)
        # Вход со страницы, открытой по другому адресу этого же сервера (имя вместо IP), — тоже.
        named = {"Origin": "http://my-server.example:8765", "Host": "my-server.example:8765"}
        self.assertEqual(self.request("POST", "/api/login", {"password": "secret-pass"}, headers=named, other=True)[0], 200)
        # Чужой сайт с cookie (старый браузер без SameSite) — нет, и видно, что пришло.
        status, _, data = self.request("POST", "/api/calc", {"text": "x"}, headers={"Origin": "http://evil.example"},
                                       other=True, cookie=cookie)
        self.assertEqual(status, 403)
        self.assertIn("evil.example", json.loads(data)["error"])
        # Без cookie — ни с каким Origin.
        self.assertEqual(self.request("POST", "/api/calc", {"text": "x"}, headers=hidden, other=True)[0], 401)

    def test_guessing_the_password_is_paused(self):
        self.start(remote_on=True)
        self.request("POST", "/api/remote", {"password": "secret-pass", "enabled": True})
        for _ in range(remote.LoginGuard.FREE):
            self.assertEqual(self.login("wrong")[0], 401)
        status, cookie, data = self.login("secret-pass")  # даже верный — после паузы
        self.assertEqual(status, 429)
        self.assertIn("подождите", json.loads(data)["error"])

    def test_this_computer_needs_no_password_but_foreign_host_is_refused(self):
        self.start(remote_on=True)
        self.assertEqual(self.request("GET", "/api/data")[0], 200)
        self.assertEqual(self.request("GET", "/api/data", headers={"Host": "evil.example:8765"})[0], 403)
        self.assertEqual(self.request("POST", "/api/login", {"password": "x"})[0], 404)


if __name__ == "__main__":
    unittest.main()


def _test_browser():
    import os
    from supreme import fetcher
    path = os.environ.get("MM2_TEST_BROWSER") or fetcher.find_system_browser()
    if not path and os.path.exists("/opt/pw-browsers/chromium"):
        path = "/opt/pw-browsers/chromium"
    return path


@unittest.skipUnless(_test_browser(), "нет Edge/Chrome для проверки страницы входа")
class RemoteLoginBrowserTests(_ServerMixin, unittest.TestCase):
    """Вход в настоящем браузере: страница пароля, затем сама программа и её запросы."""

    def test_login_page_in_a_browser(self):
        import time
        from supreme import cdp
        self.start(remote_on=True)
        self.request("POST", "/api/remote", {"password": "secret-pass", "enabled": True})
        self.from_other = True
        browser = cdp.Browser(_test_browser())
        self.addCleanup(browser.close)

        def wait(expression, timeout=20):
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    if browser.evaluate(f"Boolean({expression})"):
                        return
                except cdp.BrowserError:
                    pass
                time.sleep(0.1)
            self.fail(f"не дождались: {expression}")

        browser.open(f"http://127.0.0.1:{self.port}/", settle=0.3)
        wait("document.getElementById('p')")
        browser.evaluate("document.getElementById('p').value = 'wrong-pass'; document.getElementById('f').requestSubmit()")
        wait("document.getElementById('msg').textContent.includes('Неверный пароль')")
        browser.evaluate("document.getElementById('p').value = 'secret-pass'; document.getElementById('f').requestSubmit()")
        wait("typeof state !== 'undefined' && state.dataVersion !== null")  # открылась сама программа
        wait("$('remoteBox').textContent.includes('с другого компьютера')")
        result = browser.evaluate("(async () => { const r = await postJson('/api/calc', {text: 'x'}); return Boolean(r); })()")
        self.assertTrue(result)  # запросы программы проходят

"""Tests fuer rookhub/lumbra-fetch (S2-007): Fehlerseiten ohne JSON, HTTP-Fehler beim Download,
Deckel fuer Rechenaufgabe und Dateigroesse. Ohne Netz (urlopen gemockt).

Aufruf: python3 -I tests/rookhub/test_lumbra_fetch.py   (auch im Image rookhub-explorer)
"""
import email.message
import importlib.machinery
import importlib.util
import io
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, '..', '..', 'rookhub', 'lumbra-fetch')


def load():
    loader = importlib.machinery.SourceFileLoader('lumbra_fetch', SCRIPT)
    spec = importlib.util.spec_from_loader('lumbra_fetch', loader)
    mod = importlib.util.module_from_spec(spec)
    sys.dont_write_bytecode = True
    loader.exec_module(mod)
    return mod


lf = load()


def http_error(code, body=b'<html>Too Many Requests</html>'):
    return urllib.error.HTTPError('https://lumbrasgigabase.com/x', code, 'Too Many Requests',
                                  email.message.Message(), io.BytesIO(body))


class Resp(io.BytesIO):
    def __init__(self, body, headers=None, status=200, url='https://lumbrasgigabase.com/lgb-file/1/'):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}
        self._url = url

    def geturl(self):
        return self._url


class ActionTests(unittest.TestCase):
    def test_html_error_page_is_clean_exit_1(self):
        with mock.patch.object(lf.urllib.request, 'urlopen', side_effect=http_error(429)):
            with self.assertRaises(SystemExit) as cm:
                lf.action(operation='challenge', download_id='1')
        self.assertIsInstance(cm.exception.code, str)     # Meldung, Exit 1 — kein JSONDecodeError
        self.assertIn('HTTP 429', cm.exception.code)
        self.assertIn('ohne JSON', cm.exception.code)

    def test_200_without_json_is_clean_exit_1(self):
        with mock.patch.object(lf.urllib.request, 'urlopen', return_value=Resp(b'<html>Wartung</html>')):
            with self.assertRaises(SystemExit) as cm:
                lf.action(operation='begin', download_id='1')
        self.assertIn('ohne JSON', cm.exception.code)

    def test_file_locked_still_raises_locked(self):
        body = b'{"success": false, "data": {"code": "file_locked", "retry_after": 60}}'
        with mock.patch.object(lf.urllib.request, 'urlopen', side_effect=http_error(423, body)):
            with self.assertRaises(lf.Locked):
                lf.action(operation='begin', download_id='1')

    def test_main_maps_locked_to_75_and_http_error_to_message(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(sys, 'argv', ['lumbra-fetch', 'OTB partial 2026', d]), \
                mock.patch.object(lf, 'find_download', return_value=('7', '2026-12-01')), \
                mock.patch.object(lf, 'begin', side_effect=lf.Locked('gesperrt')):
            with self.assertRaises(SystemExit) as cm:
                lf.main()
            self.assertEqual(cm.exception.code, 75)
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(sys, 'argv', ['lumbra-fetch', 'OTB partial 2026', d]), \
                mock.patch.object(lf, 'find_download', side_effect=http_error(503)):
            with self.assertRaises(SystemExit) as cm:
                lf.main()
            self.assertIsInstance(cm.exception.code, str)
            self.assertIn('HTTP 503', cm.exception.code)


class SolveTests(unittest.TestCase):
    def test_maxnumber_over_cap_is_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            lf.solve({'salt': 's', 'challenge': 'x', 'maxnumber': str(10 ** 12)})
        self.assertIn('Deckel', cm.exception.code)

    def test_small_challenge_is_solved(self):
        import hashlib
        target = hashlib.sha256(b's42').hexdigest()
        self.assertEqual(lf.solve({'salt': 's', 'challenge': target, 'maxnumber': '100'}), 42)


class DownloadTests(unittest.TestCase):
    def test_http_error_on_download_is_clean(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(lf, 'get', side_effect=http_error(403)):
            with self.assertRaises(SystemExit) as cm:
                lf.download('1', 'tok', d)
            self.assertIn('Download HTTP 403', cm.exception.code)

    def test_content_length_over_cap(self):
        resp = Resp(lf.SEVEN_ZIP_MAGIC + b'x', {'Content-Length': str(10 ** 15),
                                               'Content-Disposition': 'attachment; filename="a_OTB.7z"'})
        with tempfile.TemporaryDirectory() as d, mock.patch.object(lf, 'get', return_value=resp):
            with self.assertRaises(SystemExit) as cm:
                lf.download('1', 'tok', d)
            self.assertIn('Deckel', cm.exception.code)
            self.assertEqual(os.listdir(d), [])

    def test_stream_over_cap_removes_part_file(self):
        resp = Resp(lf.SEVEN_ZIP_MAGIC + b'y' * 5000, {'Content-Disposition': 'attachment; filename="a_OTB.7z"'})
        with tempfile.TemporaryDirectory() as d, mock.patch.object(lf, 'get', return_value=resp), \
                mock.patch.object(lf, 'MAX_BYTES', 1000):
            with self.assertRaises(SystemExit) as cm:
                lf.download('1', 'tok', d)
            self.assertIn('Deckel', cm.exception.code)
            self.assertEqual(os.listdir(d), [])

    def test_normal_download_writes_file(self):
        resp = Resp(lf.SEVEN_ZIP_MAGIC + b'z' * 10, {'Content-Disposition': 'attachment; filename="a_OTB.7z"'})
        with tempfile.TemporaryDirectory() as d, mock.patch.object(lf, 'get', return_value=resp):
            path = lf.download('1', 'tok', d)
            self.assertEqual(os.path.basename(path), 'a_OTB.7z')
            self.assertEqual(os.path.getsize(path), 16)


if __name__ == '__main__':
    unittest.main(verbosity=1)

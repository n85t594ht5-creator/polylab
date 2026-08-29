"""Общее для тестов: подмена сети и учёт результатов."""
import os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RESULTS = []


def ck(name, cond, info=""):
    RESULTS.append((name, bool(cond)))
    print(("  OK  " if cond else " FAIL ") + name + (("  " + str(info)) if info else ""))


def summary(title):
    bad = [n for n, o in RESULTS if not o]
    print(f"\n{'=' * 52}\n{title}: ИТОГО {len(RESULTS) - len(bad)}/{len(RESULTS)}")
    if bad:
        print("ПРОВАЛЕНО:", *bad, sep="\n  ")
    return 1 if bad else 0


class FakeResponse:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        if isinstance(self._p, Exception):
            raise self._p
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Отдаёт заранее заданные ответы по подстроке URL. Считает вызовы и время."""
    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        import time
        self.calls.append((url, dict(params or {}), time.time()))
        for frag, val in self.routes.items():
            if frag in url:
                if isinstance(val, Exception):
                    raise val
                if callable(val):
                    return FakeResponse(val(params or {}))
                if isinstance(val, tuple):      # (payload, status)
                    return FakeResponse(val[0], val[1])
                return FakeResponse(val)
        return FakeResponse({}, 404)


def workdir():
    d = tempfile.mkdtemp()
    os.chdir(d)
    return d

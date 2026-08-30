"""Тесты дашборда PHASE 6: контракт данных, честные пустые состояния, разметка."""
import os, re, sys, json, tempfile, subprocess
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

R = []
def ck(n, c, i=""):
    R.append((n, bool(c))); print(("  OK  " if c else " FAIL ") + n + ("  " + i if i else ""))

H = open(os.path.join(ROOT, "docs/index.html"), encoding="utf-8").read()
JS = "\n".join(re.findall(r"<script>(.*?)</script>", H, re.S))

# ── разметка ──
print("\nразметка")
ck("теги div сбалансированы", H.count("<div") == H.count("</div>"), f"{H.count('<div')}/{H.count('</div>')}")
ids = re.findall(r'id="([\w-]+)"', H)
ck("нет дублей id", len(ids) == len(set(ids)), str([i for i in set(ids) if ids.count(i) > 1]))
refs = set(re.findall(r"\$\('([\w-]+)'\)", H))
ck("JS не обращается к несуществующим id", not (refs - set(ids)), str(sorted(refs - set(ids))))
views = re.findall(r'id="v_(\w+)"', H)
ck("нет дублей страниц", len(views) == len(set(views)), str(views))
# берём именно массив PAGES, а не любой литерал вида ['x','y'] (словарь статусов тоже подходил)
pages_block = re.search(r"const PAGES=\[(.*?)\];", JS, re.S)
pages = re.findall(r"\['(\w+)',", pages_block.group(1)) if pages_block else []
ck("каждая страница меню имеет блок", all(p in views for p in set(pages)),
   str([p for p in set(pages) if p not in views]))
ck("каждый блок доступен из меню", all(v in pages for v in views),
   str([v for v in views if v not in pages]))

# ── честные статусы ──
print("\nчестные статусы вместо выдуманных данных")
for k in ("NO_DATA", "INSUFFICIENT", "INCONCLUSIVE", "UNAVAILABLE", "NOT_OBSERVED", "STALE"):
    ck(f"статус {k} определён", f"{k}:" in JS)
ck("есть функция пустого состояния", "function blank" in JS or "const blank" in JS)
for page in ("signals", "arena", "perf", "corr", "moves"):
    ck(f"страница {page} умеет показать пустое состояние", f"drawers.{page}" in JS and
       re.search(rf"drawers\.{page}\s*=.*?blank\(", JS, re.S) is not None)

# ── разделение hypothetical / realized ──
print("\nразделение hypothetical и realized")
ck("оба поля выводятся раздельно", "hypothetical_pnl" in JS and "realized_pnl" in JS)
ck("не суммируются в один P&L", not re.search(r"hypothetical_pnl\s*\+\s*.*realized_pnl", JS))
ck("есть предупреждение о несмешивании", "никогда не складываются" in H or "не смешив" in H.lower())
# ищем правило раскраски, а не близость подстрок
ck("hypothetical помечен цветом предупреждения",
   re.search(r"hypothetical_pnl'\s*\?\s*'warn'", JS) is not None,
   "правило c==='hypothetical_pnl'?'warn' не найдено")

# ── версии стратегии ──
print("\nверсии стратегии")
ck("strategy_version выводится", "strategy_version" in JS)
ck("арена показывает версию", "strategy_version" in JS[JS.find("drawers.arena"):JS.find("drawers.perf")])

# ── абстракция площадки ──
print("\nнезависимость от площадки")
ck("используются venue/instrument", "venue" in JS and "instrument" in JS)
ck("Polymarket не зашит в логику страниц",
   JS.count("polymarket") <= 2, f"{JS.count('polymarket')} упоминаний")

# ── концентрация и малая выборка ──
print("\nзащита от самообмана")
ck("порог недостаточной выборки задан", "INSUFFICIENT" in JS and re.search(r"length\s*<\s*\d+", JS) is not None)
ck("считается доля лучшего дня", "Доля лучшего дня" in JS or "share" in JS)
ck("есть предупреждение о концентрации", "CONCENTRATED" in JS)
ck("причинность задержки отрицается", "CAUSALITY NOT ESTABLISHED" in JS or "не доказывает" in JS)
ck("нет утверждений о спуфинге", "SPOOFING" not in H.upper())

# ── контракт с данными ──
print("\nконтракт с публикацией данных")
sys.path.insert(0, ROOT)
work = tempfile.mkdtemp(); os.chdir(work)
os.makedirs("data/agg", exist_ok=True)
json.dump({"days": []}, open("data/agg/index.json", "w"))
r = subprocess.run([sys.executable, os.path.join(ROOT, "polylab/data/publish.py")],
                   capture_output=True, text=True)
ck("publish отрабатывает на пустых данных", r.returncode == 0, r.stderr[-120:])
man = json.load(open("docs/data/manifest.json"))
ck("манифест содержит статусы источников", set(man["sources"]) == {"dna", "moves", "signals", "latency"},
   str(set(man["sources"])))
ck("пустые источники помечены честно",
   all(v["status"] in ("NO_DATA", "NO_LOCAL_RAW", "OK") for v in man["sources"].values()),
   str({k: v["status"] for k, v in man["sources"].items()}))
ck("режим SHADOW, live выключен", man["mode"] == "SHADOW" and man["live"] is False)
ck("площадки перечислены абстрактно", isinstance(man.get("venues"), list) and man["venues"])
for f in ("dna", "moves", "signals", "latency"):
    d = json.load(open(f"docs/data/{f}.json"))
    ck(f"{f}.json не подставляет нули", d.get("rows") == [] and "note" in d)

# ── нет выдуманных значений ──
print("\nотсутствие выдуманных значений")
ck("нет Math.random в панели", "Math.random" not in JS)
ck("нет захардкоженных примеров P&L", not re.search(r"pnl\s*[:=]\s*-?\d+\.\d+", JS))
# реальный инвариант: отсутствующее значение рисуется прочерком, а не нулём
ck("отсутствующее значение рисуется прочерком",
   JS.count("'—'") >= 3 and "?'—'" in JS.replace(" ", ""), f"{JS.count(chr(39)+chr(8212)+chr(39))} вхождений")
ck("fmt возвращает прочерк для null/NaN",
   re.search(r"fmt=\(n,d=0\)=>\(n==null\|\|isNaN\(n\)\)\?'—'", JS) is not None)
ck("нулями пропуски не подменяются", "?? 0" not in JS)

# ── мобильная вёрстка ──
print("\nмобильная вёрстка")
mq = re.findall(r"@media\(max-width:(\d+)px\)", H)
ck("есть брейкпоинты", len(mq) >= 2, str(mq))
ck("минимальный брейкпоинт ≤ 620px", min(int(x) for x in mq) <= 620, str(mq))
ck("сетки сжимаемы (minmax(0)", "minmax(0" in H)
ck("таблицы в горизонтальном скролле", ".tw{overflow:auto" in H.replace(" ", ""))
ck("нет фиксированной ширины страницы", "width:1200px" not in H and "min-width:1" not in H)
ck("графики адаптивные (viewBox)", "viewBox" in JS and "preserveAspectRatio" in JS)
ck("вкладки со скроллом", "nav{" in H.replace(" ", "") and "overflow-x:auto" in H.replace(" ", ""))

bad = [n for n, o in R if not o]
print(f"\n{'='*52}\nDASHBOARD: {len(R)-len(bad)}/{len(R)}")
if bad: print("ПРОВАЛЕНО:", *bad, sep="\n  ")
sys.exit(1 if bad else 0)

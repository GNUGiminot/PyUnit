# -*- coding: utf-8 -*-
"""Собирает автономную витрину интерфейса в один файл.

Витрина = та же разметка/стили/логика, но:
  - всё инлайном, ноль внешних запросов;
  - без service worker и манифеста (в артефакте им негде работать);
  - демонстрационный режим включён принудительно, симулятор стартует
    в рабочем состоянии, чтобы прибор было видно в деле.
"""
import io, os, re, sys

html = io.open('index.html', encoding='utf-8').read()
css  = io.open('app.css', encoding='utf-8').read()
js   = io.open('app.js', encoding='utf-8').read()

body = html.split('<body>', 1)[1].split('</body>', 1)[0]
# Внешний <script src="app.js"> обязательно убрать: иначе приложение
# стартует дважды и два симулятора пишут в одни и те же элементы.
body, n = re.subn(r'<script[^>]*src=["\']app\.js["\'][^>]*>\s*</script>', '', body)
assert n == 1, 'ожидался ровно один <script src="app.js">, найдено %d' % n

js = js.replace("""    if ('serviceWorker' in navigator && location.protocol.indexOf('http') === 0) {
      navigator.serviceWorker.register('sw.js').catch(function () { /* не критично */ });
    }

""", '')
assert 'serviceWorker.register' not in js

js = js.replace("var forced = /[?&]demo=1/.test(location.search);",
                "var forced = true;  // витрина: устройство отсюда недоступно")
js = js.replace("      status: 'pending', presetId: null, temp: 24.0, rh: 46.0,",
                "      status: 'drying', presetId: 2, temp: 64.2, rh: 10.4,", 1)
js = js.replace("    selectedPreset: null,", "    selectedPreset: 2,", 1)
for probe in ("var forced = true;", "status: 'drying', presetId: 2", "selectedPreset: 2,"):
    assert probe in js, 'не применилось: ' + probe

out = ('<meta charset="utf-8">\n'
       '<title>Сушилка филамента</title>\n'
       '<style>\n' + css + '\n</style>\n'
       + body.strip() + '\n'
       '<script>\n' + js + '\n</script>')
assert out.count('function init(') == 1, 'приложение попало в сборку дважды'

io.open('_preview.html', 'w', encoding='utf-8', newline='').write(out)
dst = sys.argv[1] if len(sys.argv) > 1 else None
if dst:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    io.open(dst, 'w', encoding='utf-8', newline='').write(out)
print('собрано %d байт' % len(out))

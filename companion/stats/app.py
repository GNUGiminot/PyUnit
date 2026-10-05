# -*- coding: utf-8 -*-
"""Счётчик сушек для PyUnit.

Почему отдельная база: PyUnit при КАЖДОМ запуске стирает таблицу dryer_logs
(CLEAR_LOGS_ON_STARTUP=True), так что история сушек там не живёт. Этот
сервис раз в несколько секунд читает свежие строки телеметрии (только
чтение), восстанавливает по ним сессии сушки и хранит их у себя.

Сессия = от первого статуса не-pending до одного из событий:
  * переход в хранение (humidity_storage / temperature_storage) -> completed
  * переход в pending при остатке времени                        -> aborted
  * переход в pending, когда таймер уже дошёл до нуля            -> completed
Хранение сушкой не считается.
"""
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

SRC = os.environ.get('DB_PATH', '/data/pyunit.db')
OWN = os.environ.get('OWN_DB', '/state/stats.db')
POLL = float(os.environ.get('POLL_SECONDS', '5'))
GAP = 120.0            # разрыв телеметрии больше этого не считается временем работы
DRYING = ('drying', 'timer_drying')
STORAGE = ('humidity_storage', 'temperature_storage')

lock = threading.Lock()


def own():
    c = sqlite3.connect(OWN, timeout=10)
    c.execute('PRAGMA journal_mode=WAL')
    c.executescript('''
      CREATE TABLE IF NOT EXISTS sessions(
        id INTEGER PRIMARY KEY, started_at TEXT, material TEXT, preset_id INTEGER,
        seconds REAL, heater_seconds REAL, outcome TEXT);
      CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
    ''')
    return c


def kv_get(c, k, default=None):
    r = c.execute('SELECT v FROM kv WHERE k=?', (k,)).fetchone()
    return json.loads(r[0]) if r else default


def kv_put(c, k, v):
    c.execute('INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v',
              (k, json.dumps(v)))


def ts(s):
    return datetime.fromisoformat(s.replace('Z', '')).timestamp()


def preset_names(src):
    try:
        return {r[0]: r[1] for r in src.execute('SELECT id, name FROM presets')}
    except sqlite3.Error:
        return {}


def finish(c, act, outcome):
    if act['seconds'] < 60:     # случайный клик «старт — стоп» не считаем
        return
    c.execute('INSERT INTO sessions(started_at,material,preset_id,seconds,heater_seconds,outcome) '
              'VALUES(?,?,?,?,?,?)',
              (act['started_at'], act['material'], act['preset_id'],
               act['seconds'], act['heater'], outcome))


def step(c, act, row, names):
    """Один шаг автомата. row = (timestamp, status, preset_id, heater_is_on, time_left)."""
    t, status, preset_id, heater, left = row
    now = ts(t)
    if act:
        dt = now - act['last']
        if 0 < dt <= GAP:
            act['seconds'] += dt
            if heater:
                act['heater'] += dt
        act['last'] = now
        if left is not None:
            act['left'] = left
            act['timer'] = True

    if status in DRYING:
        if not act:
            act = {'started_at': t, 'preset_id': preset_id,
                   'material': names.get(preset_id, 'Без пресета'),
                   'seconds': 0.0, 'heater': 0.0, 'last': now, 'left': None, 'timer': False}
        return act
    if status in STORAGE:
        if act:
            finish(c, act, 'completed')
        return None
    # pending и всё незнакомое
    if act:
        done = act['timer'] and act['left'] is not None and act['left'] <= 120
        finish(c, act, 'completed' if done else 'aborted')
    return None


def ingest():
    if not os.path.exists(SRC):
        return
    c = own()
    try:
        src = sqlite3.connect('file:%s?mode=ro' % SRC, uri=True, timeout=10)
    except sqlite3.Error:
        c.close()
        return
    try:
        last = kv_get(c, 'last_ts', '')
        act = kv_get(c, 'active', None)
        # Часы на устройстве без RTC при загрузке врут и потом скачут назад:
        # если самая свежая строка старше сохранённой метки — база была очищена.
        top = src.execute('SELECT MAX(timestamp) FROM dryer_logs').fetchone()[0]
        if top and last and top < last:
            last = ''
        names = preset_names(src)
        rows = src.execute(
            'SELECT timestamp,status,current_preset_id,heater_is_on,time_left_drying '
            'FROM dryer_logs WHERE timestamp > ? ORDER BY timestamp LIMIT 20000', (last,)).fetchall()
        for row in rows:
            act = step(c, act, row, names)
            last = row[0]
        kv_put(c, 'last_ts', last)
        kv_put(c, 'active', act)
        c.commit()
    finally:
        src.close()
        c.close()


def summary(period):
    c = own()
    try:
        where, args = '', ()
        if period in ('30', '365'):
            cut = (datetime.utcnow() - timedelta(days=int(period))).isoformat()
            where, args = 'WHERE started_at >= ?', (cut,)
        rows = c.execute('SELECT started_at,material,seconds,heater_seconds,outcome '
                         'FROM sessions %s ORDER BY started_at DESC' % where, args).fetchall()
        totals = {'runs': 0, 'seconds': 0.0, 'heater_seconds': 0.0, 'aborted': 0}
        mats = {}
        for started, mat, sec, hs, out in rows:
            totals['seconds'] += sec
            totals['heater_seconds'] += hs
            if out == 'completed':
                totals['runs'] += 1
                m = mats.setdefault(mat, {'name': mat, 'runs': 0, 'seconds': 0.0})
                m['runs'] += 1
                m['seconds'] += sec
            else:
                totals['aborted'] += 1
        for m in mats.values():
            m['avg_seconds'] = m['seconds'] / m['runs']
        act = kv_get(c, 'active', None)
        return {
            'totals': totals,
            'materials': sorted(mats.values(), key=lambda m: -m['seconds']),
            'sessions': [{'started_at': r[0], 'material': r[1], 'seconds': r[2], 'outcome': r[4]}
                         for r in rows[:50]],
            'active': bool(act),
        }
    finally:
        c.close()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path.rstrip('/')
        if path in ('/stats/summary', '/summary'):
            q = parse_qs(u.query).get('period', ['all'])[0]
            body = json.dumps(summary(q), ensure_ascii=False).encode('utf-8')
            code = 200
        elif path in ('/stats/health', '/health'):
            body, code = b'{"ok":true}', 200
        else:
            body, code = b'{"error":"not found"}', 404
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)


def loop():
    while True:
        try:
            with lock:
                ingest()
        except Exception as e:      # не даём потоку умереть
            print('ingest error:', e, flush=True)
        time.sleep(POLL)


if __name__ == '__main__':
    os.makedirs(os.path.dirname(OWN), exist_ok=True)
    own().close()
    threading.Thread(target=loop, daemon=True).start()
    print('stats service on :8000, source=%s' % SRC, flush=True)
    ThreadingHTTPServer(('0.0.0.0', 8000), H).serve_forever()

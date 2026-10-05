/* ==========================================================================
   Сушилка PyUnit — клиент
   Говорит с PyUnit напрямую (nginx проксирует /api на :5000, один origin),
   счётчик берёт из сервиса-компаньона /stats.
   Если устройство недоступно — переходит в демонстрационный режим
   со встроенным симулятором, чтобы интерфейс можно было смотреть офлайн.
   ========================================================================== */
(function () {
  'use strict';

  // --- Геометрия шкалы ----------------------------------------------------
  // Окружность r=78 -> 490.088. Развёртка 270°, старт в 135° (левый низ).
  var CX = 106, CY = 106, R = 78;
  var CIRC = 2 * Math.PI * R;
  var SWEEP = CIRC * 0.75;
  var RANGE = { temp: [0, 120], hum: [0, 100] };
  // Оцифровка шкалы: без неё непонятен диапазон прибора
  var SCALE = { temp: [0, 30, 60, 90, 120], hum: [0, 25, 50, 75, 100] };
  // Угол на шкале для доли f, по часовой от 3 часов
  function angleAt(f) { return (135 + f * 270) * Math.PI / 180; }
  function polar(f, r) {
    var a = angleAt(f);
    return [CX + r * Math.cos(a), CY + r * Math.sin(a)];
  }

  // Цвет кольца вычисляется из самого значения: это шкала, а не украшение.
  // Холодная камера — синяя, прогретая уходит в янтарь и красное;
  // сухой воздух бирюзовый, сырой — густо-синий.
  var HEAT_STOPS  = [[0, 0x4a9eff], [0.30, 0xffc94d], [0.62, 0xff7a2f], [1, 0xff3b30]];
  var MOIST_STOPS = [[0, 0x35e0c4], [0.28, 0x3bb8e8], [1, 0x2a5cc8]];

  function rampColor(stops, f) {
    f = clamp01(f);
    for (var i = 1; i < stops.length; i++) {
      if (f <= stops[i][0] || i === stops.length - 1) {
        var a = stops[i - 1], b = stops[i];
        var k = b[0] === a[0] ? 0 : (f - a[0]) / (b[0] - a[0]);
        if (k < 0) k = 0; if (k > 1) k = 1;
        var out = 0;
        for (var sh = 16; sh >= 0; sh -= 8) {
          var ca = (a[1] >> sh) & 255, cb = (b[1] >> sh) & 255;
          out |= Math.round(ca + (cb - ca) * k) << sh;
        }
        return '#' + ('000000' + out.toString(16)).slice(-6);
      }
    }
    return '#ffffff';
  }

  var $ = function (id) { return document.getElementById(id); };
  var clamp01 = function (v) { return v < 0 ? 0 : v > 1 ? 1 : v; };
  var frac = function (v, metric) {
    var r = RANGE[metric];
    return clamp01((v - r[0]) / (r[1] - r[0]));
  };

  // --- Состояние ----------------------------------------------------------
  var state = {
    demo: false,
    unit: null,          // {id, name}
    presets: [],         // пресеты, привязанные к блоку
    selectedPreset: null,
    last: null,          // последняя телеметрия
    history: [],         // [{t, temp, rh}]
    rangeSec: 7200,
    ws: null,
    wsTimer: null,
    screen: 'run'
  };

  var STATUS = {
    pending:             { text: 'ожидание',   lamp: 'idle',  phase: '' },
    drying:              { text: 'прогрев',    lamp: 'run',   phase: 'выход на влажность' },
    timer_drying:        { text: 'сушка',      lamp: 'run',   phase: 'выдержка' },
    humidity_storage:    { text: 'хранение',   lamp: 'store', phase: 'хранение по влажности' },
    temperature_storage: { text: 'хранение',   lamp: 'store', phase: 'хранение по температуре' }
  };

  // ======================================================================
  //  Шкалы
  // ======================================================================
  function buildTicks() {
    ['temp', 'hum'].forEach(function (metric) {
      var g = document.querySelector('[data-ticks="' + metric + '"]');
      var sc = document.querySelector('[data-scale="' + metric + '"]');
      var df = document.querySelector('[data-defs="' + metric + '"]');
      if (!g) return;

      // 41 деление: каждые 2.5 % шкалы, крупные — каждые 25 %
      var out = '';
      for (var i = 0; i <= 40; i++) {
        var f = i / 40;
        var major = i % 10 === 0;
        var p1 = polar(f, major ? 84 : 86.5);
        var p2 = polar(f, major ? 94 : 91);
        out += '<line class="' + (major ? 'major' : '') + '" x1="' + p1[0].toFixed(2) + '" y1="' + p1[1].toFixed(2) +
               '" x2="' + p2[0].toFixed(2) + '" y2="' + p2[1].toFixed(2) + '"/>';
      }
      g.innerHTML = out;

      // Цифры у крупных делений
      if (sc) {
        var labels = SCALE[metric], txt = '';
        for (var k = 0; k < labels.length; k++) {
          var fk = k / (labels.length - 1);
          var pt = polar(fk, 101);
          txt += '<text x="' + pt[0].toFixed(2) + '" y="' + pt[1].toFixed(2) + '">' + labels[k] + '</text>';
        }
        sc.innerHTML = txt;
      }

      // Градиент вдоль развёртки: даёт объём дуге, стопы обновляются по значению
      if (df) {
        df.innerHTML =
          '<linearGradient id="arc-' + metric + '" gradientUnits="userSpaceOnUse" ' +
          'x1="40" y1="172" x2="172" y2="40">' +
          '<stop offset="0" stop-color="#888"/><stop offset="1" stop-color="#ccc"/></linearGradient>';
      }
    });
  }

  function setArc(dialId, metric, value) {
    var dial = $(dialId);
    if (!dial) return;
    var arc = dial.querySelector('.dial__arc');
    var track = dial.querySelector('.dial__track');
    var head = dial.querySelector('.dial__head');
    var defs = dial.querySelector('[data-defs]');
    track.setAttribute('stroke-dasharray', SWEEP.toFixed(2) + ' ' + CIRC.toFixed(2));

    var known = !(value === null || value === undefined || isNaN(value));
    var f = known ? frac(value, metric) : 0;
    var stops = metric === 'temp' ? HEAT_STOPS : MOIST_STOPS;
    var color = rampColor(stops, f);

    // При нуле дугу убираем совсем, иначе круглый торец оставляет точку
    var dash = (f < 0.004 ? 0 : f * SWEEP).toFixed(2) + ' ' + CIRC.toFixed(2);
    arc.setAttribute('stroke-dasharray', dash);
    var bloom = dial.querySelector('.dial__bloom');
    if (bloom) bloom.setAttribute('stroke-dasharray', dash);

    // Дуга заливается градиентом от цвета начала прогрева к текущему:
    // видно не только где прибор сейчас, но и откуда пришёл
    if (defs) {
      var st = defs.querySelectorAll('stop');
      if (st.length === 2) {
        st[0].setAttribute('stop-color', rampColor(stops, f * 0.45));
        st[1].setAttribute('stop-color', color);
      }
      arc.style.stroke = 'url(#arc-' + metric + ')';
    }
    dial.style.setProperty('--arc', color);
    document.documentElement.style.setProperty(metric === 'temp' ? '--heat-now' : '--moist-now', color);

    if (head) {
      if (!known || f < 0.004) { head.setAttribute('opacity', '0'); }
      else {
        head.removeAttribute('opacity');
        var pt = polar(f, R);
        head.setAttribute('cx', pt[0].toFixed(2));
        head.setAttribute('cy', pt[1].toFixed(2));
      }
    }
    dial.classList.toggle('is-live', known);
  }

  function setTarget(dialId, metric, value) {
    var dial = $(dialId);
    if (!dial) return;
    var g = dial.querySelector('.dial__target');
    if (value === null || value === undefined || isNaN(value)) { g.hidden = true; return; }
    g.hidden = false;
    g.setAttribute('transform', 'rotate(' + (225 + frac(value, metric) * 270).toFixed(2) + ' ' + CX + ' ' + CY + ')');
  }

  // Плавный счётчик цифр
  var tweens = {};
  var tweenRaf = {};
  var tweenFix = {};
  function setNumber(elId, value, digits) {
    var el = $(elId);
    if (!el) return;
    // Предыдущую анимацию обязательно гасим: иначе на каждом тике телеметрии
    // запускается ещё одна, и несколько rAF-циклов перетирают цифры друг другу.
    if (tweenRaf[elId]) { cancelAnimationFrame(tweenRaf[elId]); tweenRaf[elId] = 0; }
    if (value === null || value === undefined || isNaN(value)) { el.textContent = '--.-'; tweens[elId] = null; return; }
    var from = tweens[elId];
    if (from === null || from === undefined) { tweens[elId] = value; el.textContent = value.toFixed(digits); return; }
    if (Math.abs(value - from) < Math.pow(10, -digits) / 2) { tweens[elId] = value; el.textContent = value.toFixed(digits); return; }
    // В скрытой вкладке (экран телефона погас, приложение свёрнуто) браузер
    // не вызывает requestAnimationFrame вообще. Анимация — украшение, а не
    // способ доставки значения: если кадров не будет, число всё равно верное.
    if (document.visibilityState !== 'visible') {
      tweens[elId] = value; el.textContent = value.toFixed(digits); return;
    }
    var start = performance.now(), dur = 500, a = from, b = value;
    function step(now) {
      var k = Math.min(1, (now - start) / dur);
      var cur = a + (b - a) * (1 - Math.pow(1 - k, 3));
      el.textContent = cur.toFixed(digits);
      tweens[elId] = cur;
      if (k < 1) { tweenRaf[elId] = requestAnimationFrame(step); }
      else { tweens[elId] = b; tweenRaf[elId] = 0; }
    }
    tweenRaf[elId] = requestAnimationFrame(step);
    // Страховка: если кадры так и не придут, доводим значение по таймеру.
    clearTimeout(tweenFix[elId]);
    tweenFix[elId] = setTimeout(function () {
      if (tweenRaf[elId]) { cancelAnimationFrame(tweenRaf[elId]); tweenRaf[elId] = 0; }
      if (tweens[elId] !== b) { tweens[elId] = b; el.textContent = b.toFixed(digits); }
    }, dur + 150);
  }

  // ======================================================================
  //  Отрисовка экрана сушки
  // ======================================================================
  function render() {
    var d = state.last;
    var lamp = $('statusLamp'), txt = $('statusText');

    if (!d) {
      lamp.dataset.state = 'offline';
      txt.textContent = state.demo ? 'демо-режим' : 'нет связи';
      return;
    }

    var st = STATUS[d.status] || STATUS.pending;
    lamp.dataset.state = st.lamp;
    txt.textContent = state.demo ? st.text + ' · демо' : st.text;

    $('unitName').textContent = state.unit ? state.unit.name : '—';

    var preset = presetById(d.current_preset_id);
    var mat = $('material');
    if (preset) { mat.hidden = false; mat.textContent = preset.name; } else { mat.hidden = true; }

    // Шкалы
    setNumber('tempValue', d.temperature, 1);
    setNumber('humValue', d.relative_humidity, 1);
    setArc('dialTemp', 'temp', d.temperature);
    setArc('dialHum', 'hum', d.relative_humidity);
    setTarget('dialTemp', 'temp', preset ? preset.temperature : null);
    setTarget('dialHum', 'hum', preset ? preset.humidity : null);
    $('dialTemp').classList.toggle('is-heating', !!d.heater_is_on);

    // Уставки
    $('spTemp').textContent = preset ? preset.temperature + ' °C' : '—';
    $('spHum').textContent = preset ? preset.humidity + ' %' : '—';
    $('spHeater').textContent = (d.heater_temperature === null || d.heater_temperature === undefined)
      ? '—' : d.heater_temperature.toFixed(1) + ' °C';

    // Ход
    var prog = $('progress');
    if (d.status === 'pending' || !preset) {
      prog.hidden = true;
    } else {
      prog.hidden = false;
      $('phaseText').textContent = st.phase;
      var hasLeft = d.time_left_drying !== null && d.time_left_drying !== undefined;
      if (!hasLeft) {
        // Прогрев: обратного отсчёта ещё нет — таймер в PyUnit стартует
        // только когда влажность дойдёт до цели. Показываем время с запуска.
        var el = elapsedSinceStart();
        $('timeLeft').textContent = el.sec === null ? '—' : (el.capped ? '≥ ' : '') + fmtDur(el.sec);
        $('progressFill').style.width = '0%';
        $('elapsedText').textContent = 'идёт прогрев';
        $('etaText').textContent = 'отсчёт с ' + preset.humidity + ' %';
      } else {
        var left = Math.max(0, d.time_left_drying);
        var total = (d.status === 'timer_drying')
          ? preset.dry_time * 60
          : preset.humidity_storage_dry_time * 60;
        $('timeLeft').textContent = fmtDur(left);
        $('progressFill').style.width = (total > 0 ? clamp01(1 - left / total) * 100 : 0).toFixed(1) + '%';
        $('elapsedText').textContent = total > 0 ? 'прошло ' + fmtDur(total - left) : '';
        $('etaText').textContent = 'готово в ' + fmtClock(Date.now() + left * 1000);
      }
    }

    // Лампы
    lampState('stHeater', d.heater_is_on, 'вкл', 'выкл');
    lampState('stLid', d.servo_is_open, 'открыта', 'закрыта');
    lampState('stFan', d.heater_fan_is_run, 'вкл', 'выкл');

    // Кнопки
    $('btnStart').disabled = !state.selectedPreset || d.status !== 'pending';
    $('btnStop').disabled = d.status === 'pending';
  }

  function lampState(id, on, yes, no) {
    var el = $(id);
    el.dataset.on = on ? 'true' : 'false';
    el.querySelector('.state__val').textContent = on ? yes : no;
  }

  function presetById(id) {
    if (id === null || id === undefined) return null;
    for (var i = 0; i < state.presets.length; i++) if (state.presets[i].id === id) return state.presets[i];
    return null;
  }

  function fmtDur(sec) {
    sec = Math.max(0, Math.round(sec));
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    if (h > 0) return h + ':' + String(m).padStart(2, '0');
    return m + ':' + String(s).padStart(2, '0');
  }
  function fmtClock(ms) {
    var d = new Date(ms);
    return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  }

  // Сколько идёт текущая сушка. Считается по истории: отматываем назад,
  // пока статус не станет 'pending'. Если упёрлись в начало буфера —
  // начало сушки в него не попало, и время помечается как «не меньше».
  function elapsedSinceStart() {
    var h = state.history;
    if (!h.length) return { sec: null, capped: false };
    var i = h.length - 1;
    while (i >= 0 && h[i].st && h[i].st !== 'pending') i--;
    var startIdx = i + 1;
    if (startIdx >= h.length) return { sec: null, capped: false };
    return { sec: (Date.now() - h[startIdx].t) / 1000, capped: i < 0 };
  }

  function fmtHours(sec) {
    var h = sec / 3600;
    return h >= 100 ? Math.round(h).toString() : h.toFixed(1);
  }

  // ======================================================================
  //  Самописец
  // ======================================================================
  function drawTrace() {
    var cv = $('traceCanvas');
    if (!cv || state.screen !== 'run') return;
    var css = getComputedStyle(document.documentElement);
    var colHeat = css.getPropertyValue('--heat-now').trim() || '#ff8a3d';
    var colMoist = css.getPropertyValue('--moist-now').trim() || '#3bb8e8';
    var colRule = css.getPropertyValue('--rule').trim();
    var colMute = css.getPropertyValue('--ink-mute').trim();

    var dpr = window.devicePixelRatio || 1;
    var w = cv.clientWidth, h = cv.clientHeight;
    if (!w || !h) return;
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(h * dpr);
    var g = cv.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);

    var padL = 26, padR = 26, padT = 8, padB = 16;
    var iw = w - padL - padR, ih = h - padT - padB;

    // сетка
    g.strokeStyle = colRule; g.lineWidth = 1;
    g.beginPath();
    for (var i = 0; i <= 4; i++) {
      var y = padT + (ih * i) / 4 + 0.5;
      g.moveTo(padL, y); g.lineTo(padL + iw, y);
    }
    g.stroke();

    var now = Date.now();
    var from = now - state.rangeSec * 1000;
    var pts = state.history.filter(function (p) { return p.t >= from; });

    g.fillStyle = colMute;
    g.font = '10px ui-monospace, monospace';
    g.textBaseline = 'middle';
    for (var k = 0; k <= 4; k++) {
      var ty = padT + (ih * k) / 4;
      g.textAlign = 'right'; g.fillText(String(120 - k * 30), padL - 4, ty);
      g.textAlign = 'left'; g.fillText(String(100 - k * 25), padL + iw + 4, ty);
    }

    if (pts.length < 2) {
      g.fillStyle = colMute; g.textAlign = 'center'; g.textBaseline = 'middle';
      g.font = '12px system-ui, sans-serif';
      g.fillText('нет данных за период', w / 2, h / 2);
      return;
    }

    var x = function (t) { return padL + ((t - from) / (now - from)) * iw; };
    var yT = function (v) { return padT + ih - (clamp01(v / 120)) * ih; };
    var yH = function (v) { return padT + ih - (clamp01(v / 100)) * ih; };

    function line(getY, key, color) {
      g.strokeStyle = color; g.lineWidth = 2; g.lineJoin = 'round'; g.lineCap = 'round';
      g.beginPath();
      var started = false;
      for (var j = 0; j < pts.length; j++) {
        var p = pts[j];
        if (p[key] === null || p[key] === undefined || isNaN(p[key])) continue;
        var px = x(p.t), py = getY(p[key]);
        if (!started) { g.moveTo(px, py); started = true; } else { g.lineTo(px, py); }
      }
      g.stroke();
    }
    line(yH, 'rh', colMoist);
    line(yT, 'temp', colHeat);
  }

  function pushHistory(d) {
    var t = parseTs(d.timestamp);
    if (!t) return;
    var h = state.history;
    if (h.length && t <= h[h.length - 1].t) return;
    h.push({ t: t, temp: d.temperature, rh: d.relative_humidity, st: d.status });
    // держим сутки максимум
    var cut = Date.now() - 86400 * 1000;
    while (h.length && h[0].t < cut) h.shift();
  }

  // Бэкенд хранит наивный UTC — дописываем Z, иначе браузер прочтёт как локальное
  function parseTs(s) {
    if (!s) return 0;
    var iso = /[zZ]|[+\-]\d{2}:?\d{2}$/.test(s) ? s : s + 'Z';
    var t = Date.parse(iso);
    return isNaN(t) ? 0 : t;
  }

  // ======================================================================
  //  Связь с PyUnit
  // ======================================================================
  function api(path, opts) {
    return fetch(path, opts || {}).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.status === 204 ? null : r.json();
    });
  }

  function connectWs() {
    if (state.demo || !state.unit) return;
    try { if (state.ws) state.ws.close(); } catch (e) { /* уже закрыт */ }

    var since = new Date(Date.now() - state.rangeSec * 1000)
      .toISOString().replace('Z', '').split('.')[0];
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var url = proto + '//' + location.host + '/api/dashboard/dryer/' + state.unit.id +
              '?start_time=' + encodeURIComponent(since) + '&limit=4000';

    var ws = new WebSocket(url);
    state.ws = ws;

    ws.onmessage = function (ev) {
      var msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      var rows = Array.isArray(msg) ? msg : (msg && msg.history) ? msg.history : null;
      if (!rows) return;
      if (!Array.isArray(msg)) {
        state.history = [];
        rows.forEach(pushHistory);
      } else {
        rows.forEach(pushHistory);
      }
      if (rows.length) state.last = rows[rows.length - 1];
      render();
      drawTrace();
    };
    ws.onclose = function () {
      clearTimeout(state.wsTimer);
      state.wsTimer = setTimeout(connectWs, 3000);
    };
    ws.onerror = function () { try { ws.close(); } catch (e) { /* и так закрывается */ } };
  }

  function loadStatic() {
    return api('/api/common/units').then(function (units) {
      if (!units || !units.length) throw new Error('нет блоков');
      state.unit = units[0];
      return api('/api/common/presets');
    }).then(function (presets) {
      state.presets = (presets || []).filter(function (p) {
        return (p.dryers || []).some(function (d) { return d.id === state.unit.id; });
      });
      renderPresets();
    });
  }

  function renderPresets() {
    var box = $('presets');
    box.innerHTML = '';
    state.presets.forEach(function (p) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'preset' + (state.selectedPreset === p.id ? ' is-on' : '');
      b.textContent = p.name;
      b.title = p.temperature + '°C · ' + p.humidity + '% · ' + p.dry_time + ' мин';
      b.addEventListener('click', function () {
        state.selectedPreset = (state.selectedPreset === p.id) ? null : p.id;
        renderPresets();
        render();
      });
      box.appendChild(b);
    });
  }

  function startDrying() {
    if (!state.selectedPreset || !state.unit) return;
    if (state.demo) { sim.start(state.selectedPreset); toast('Демо: запуск'); return; }
    api('/api/dashboard/control/set-preset/' + state.unit.id + '?preset_id=' + state.selectedPreset, { method: 'POST' })
      .then(function (r) { toast((r && r.message) || 'Запущено'); })
      .catch(function (e) { toast('Не удалось запустить: ' + e.message); });
  }

  function stopDrying() {
    if (!state.unit) return;
    if (state.demo) { sim.stop(); toast('Демо: остановлено'); return; }
    api('/api/dashboard/control/set-preset/' + state.unit.id, { method: 'POST' })
      .then(function (r) { toast((r && r.message) || 'Остановлено'); })
      .catch(function (e) { toast('Не удалось остановить: ' + e.message); });
  }

  // ======================================================================
  //  Счётчик сушек
  // ======================================================================
  var statsPeriod = 'all';

  function loadStats() {
    var p = state.demo ? Promise.resolve(sim.stats()) : api('/stats/summary?period=' + statsPeriod);
    p.then(renderStats).catch(function () {
      $('totRuns').textContent = '—';
      $('matList').innerHTML = '<li class="empty">Счётчик недоступен</li>';
      $('sessionList').innerHTML = '';
    });
  }

  function renderStats(s) {
    $('totRuns').textContent = s.totals.runs;
    $('totHours').textContent = fmtHours(s.totals.seconds);
    $('totHeat').textContent = fmtHours(s.totals.heater_seconds);

    var max = 0;
    s.materials.forEach(function (m) { if (m.seconds > max) max = m.seconds; });
    var ml = $('matList');
    ml.innerHTML = '';
    if (!s.materials.length) ml.innerHTML = '<li class="empty">Пока ни одной сушки</li>';
    s.materials.forEach(function (m) {
      var li = document.createElement('li');
      li.className = 'mat';
      li.innerHTML =
        '<span class="mat__name"></span>' +
        '<span class="mat__num"></span>' +
        '<span class="mat__bar"><i class="mat__fill"></i></span>';
      li.querySelector('.mat__name').textContent = m.name;
      li.querySelector('.mat__num').textContent = m.runs + ' × · ' + fmtHours(m.seconds) + ' ч';
      li.querySelector('.mat__fill').style.width = (max ? (m.seconds / max) * 100 : 0).toFixed(1) + '%';
      ml.appendChild(li);
    });

    var sl = $('sessionList');
    sl.innerHTML = '';
    if (!s.sessions.length) sl.innerHTML = '<li class="empty">История пуста</li>';
    s.sessions.slice(0, 20).forEach(function (x) {
      var li = document.createElement('li');
      li.className = 'sess';
      li.dataset.outcome = x.outcome;
      li.innerHTML = '<span class="sess__date"></span><span class="sess__mat"></span><span class="sess__dur"></span>';
      var dt = new Date(parseTs(x.started_at));
      li.querySelector('.sess__date').textContent =
        String(dt.getDate()).padStart(2, '0') + '.' + String(dt.getMonth() + 1).padStart(2, '0');
      li.querySelector('.sess__mat').textContent = x.material;
      li.querySelector('.sess__dur').textContent = fmtDur(x.seconds);
      sl.appendChild(li);
    });
  }

  // ======================================================================
  //  Демонстрационный симулятор
  // ======================================================================
  var sim = (function () {
    var s = {
      status: 'pending', presetId: null, temp: 24.0, rh: 46.0,
      heater: false, fan: false, lid: false, left: null, t0: 0, tick: 0
    };
    var PRESETS = [
      { id: 1, name: 'PLA',  temperature: 50, humidity: 10, dry_time: 180, max_temperature_delta: 20, humidity_storage_dry_time: 10, storage_type: 'humidity', dryers: [{ id: 1 }] },
      { id: 2, name: 'PETG', temperature: 65, humidity: 10, dry_time: 240, max_temperature_delta: 15, humidity_storage_dry_time: 10, storage_type: 'humidity', dryers: [{ id: 1 }] },
      { id: 3, name: 'TPU',  temperature: 60, humidity: 10, dry_time: 300, max_temperature_delta: 20, humidity_storage_dry_time: 10, storage_type: 'humidity', dryers: [{ id: 1 }] },
      { id: 4, name: 'ABS',  temperature: 80, humidity: 10, dry_time: 300, max_temperature_delta: 20, humidity_storage_dry_time: 10, storage_type: 'humidity', dryers: [{ id: 1 }] },
      { id: 5, name: 'PA',   temperature: 90, humidity: 10, dry_time: 60,  max_temperature_delta: 20, humidity_storage_dry_time: 0,  storage_type: 'temperature', dryers: [{ id: 1 }] },
      { id: 6, name: 'PC',   temperature: 85, humidity: 10, dry_time: 60,  max_temperature_delta: 10, humidity_storage_dry_time: 0,  storage_type: 'none', dryers: [{ id: 1 }] }
    ];

    function preset() { for (var i = 0; i < PRESETS.length; i++) if (PRESETS[i].id === s.presetId) return PRESETS[i]; return null; }

    function step() {
      s.tick++;
      var p = preset();
      if (s.status === 'pending') {
        s.temp += (24 - s.temp) * 0.02;
        s.rh += (46 - s.rh) * 0.02;
        s.heater = false; s.fan = false; s.left = null;
      } else if (p) {
        var target = p.temperature;
        s.heater = s.temp < target - 0.3;
        s.fan = s.temp > 50;
        s.temp += (target - s.temp) * 0.035 + (Math.random() - 0.5) * 0.06;
        // влага падает, пока греем; слегка колеблется у цели
        var floor = p.humidity - 1.5;
        s.rh += (floor - s.rh) * 0.012 + (Math.random() - 0.5) * 0.12;
        if (s.rh < 2) s.rh = 2;
        if (s.status === 'drying' && s.rh <= p.humidity) {
          s.status = 'timer_drying'; s.t0 = Date.now();
        }
        if (s.status === 'timer_drying') {
          var total = p.dry_time * 60;
          var el = (Date.now() - s.t0) / 1000;
          // демо идёт в 120 раз быстрее реального времени
          s.left = Math.max(0, total - el * 120);
          if (s.left <= 0) { s.status = p.storage_type === 'none' ? 'pending' : 'humidity_storage'; s.left = null; }
        }
        s.lid = s.status !== 'pending' && (Math.floor(s.tick / 45) % 2 === 1);
      }
      return {
        dryer_id: 1,
        status: s.status,
        current_preset_id: s.status === 'pending' ? null : s.presetId,
        timestamp: new Date().toISOString().replace('Z', ''),
        temperature: Math.round(s.temp * 10) / 10,
        relative_humidity: Math.round(s.rh * 10) / 10,
        heater_temperature: Math.round((s.temp + (s.heater ? 6 : 1)) * 10) / 10,
        heater_is_on: s.heater,
        heater_fan_is_run: s.fan,
        servo_is_open: s.lid,
        time_left_drying: s.left === null ? null : Math.round(s.left)
      };
    }

    return {
      presets: PRESETS,
      start: function (id) { s.presetId = id; s.status = 'drying'; s.t0 = Date.now(); },
      stop: function () { s.status = 'pending'; s.presetId = null; s.left = null; },
      step: step,
      seed: function () {
        // предыстория на 2 часа, чтобы самописец не был пустым
        var out = [], now = Date.now();
        for (var i = 7200; i > 0; i -= 10) {
          var k = 1 - i / 7200;
          out.push({
            t: now - i * 1000,
            temp: 24 + 41 * Math.min(1, k * 3) + Math.sin(i / 90) * 0.4,
            rh: 46 * Math.exp(-k * 2.6) + 8.5 + Math.sin(i / 60) * 0.25
          });
        }
        return out;
      },
      stats: function () {
        var mats = [
          { name: 'PETG', runs: 23, seconds: 23 * 4.2 * 3600 },
          { name: 'PLA',  runs: 17, seconds: 17 * 3.1 * 3600 },
          { name: 'ABS',  runs: 6,  seconds: 6 * 5.4 * 3600 },
          { name: 'TPU',  runs: 4,  seconds: 4 * 5.1 * 3600 },
          { name: 'PA',   runs: 2,  seconds: 2 * 1.2 * 3600 }
        ];
        var runs = 0, sec = 0;
        mats.forEach(function (m) { runs += m.runs; sec += m.seconds; m.avg_seconds = m.seconds / m.runs; });
        var sess = [], names = ['PETG', 'PLA', 'ABS', 'TPU', 'PA'];
        for (var i = 0; i < 12; i++) {
          sess.push({
            material: names[i % names.length],
            started_at: new Date(Date.now() - (i * 2.4 + 1) * 86400000).toISOString().replace('Z', ''),
            seconds: (2.5 + (i % 4)) * 3600,
            outcome: i === 3 || i === 9 ? 'aborted' : 'completed'
          });
        }
        return { totals: { runs: runs, seconds: sec, heater_seconds: sec * 0.62, aborted: 2 }, materials: mats, sessions: sess };
      }
    };
  })();

  function startDemo(autoRun) {
    state.demo = true;
    state.unit = { id: 1, name: 'U1' };
    state.presets = sim.presets;
    state.history = sim.seed();
    renderPresets();
    if (autoRun) {
      // Витринный режим: показываем прибор в работе, а не в простое
      state.selectedPreset = 2;
      renderPresets();
      sim.start(2);
    }
    setInterval(function () {
      var d = sim.step();
      state.last = d;
      pushHistory(d);
      render();
      if (state.screen === 'run') drawTrace();
    }, 1000);
  }

  // ======================================================================
  //  Оболочка: экраны, тема, уведомления
  // ======================================================================
  var toastTimer = null;
  function toast(msg) {
    var el = $('toast');
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.hidden = true; }, 2600);
  }

  function goto(name) {
    state.screen = name;
    $('screen-run').hidden = name !== 'run';
    $('screen-stats').hidden = name !== 'stats';
    $('screen-more').hidden = name !== 'more';
    document.querySelectorAll('.tab[data-goto]').forEach(function (b) {
      var on = b.dataset.goto === name;
      b.classList.toggle('is-on', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    if (name === 'stats') loadStats();
    if (name === 'more') setupLinks();
    if (name === 'run') requestAnimationFrame(drawTrace);
  }

  // Полный интерфейс PyUnit живёт на :5000 того же хоста. Адрес строится от
  // текущего, поэтому работает и по IP, и по имени, и с телефона.
  var FULL_PORT = 5000;
  function fullUrl(path) {
    return location.protocol + '//' + location.hostname + ':' + FULL_PORT + '/' + path;
  }
  function setupLinks() {
    $('moreHost').textContent = state.demo ? 'демо' : location.hostname + ':' + FULL_PORT;
    document.querySelectorAll('.link[data-path]').forEach(function (a) {
      if (state.demo) {
        a.setAttribute('href', '#');
        a.onclick = function (e) { e.preventDefault(); toast('В демо-режиме ссылки не работают'); };
      } else {
        a.setAttribute('href', fullUrl(a.dataset.path));
        a.onclick = null;
      }
    });
  }

  function initTheme() {
    var saved = null;
    try { saved = localStorage.getItem('dryer-theme'); } catch (e) { /* приватный режим */ }
    // Если тему уже проставил кто-то снаружи (просмотрщик, киоск-обёртка) —
    // уважаем её; наш выбор применяем только поверх 'auto'.
    var pre = document.documentElement.dataset.theme;
    if (pre !== 'light' && pre !== 'dark') document.documentElement.dataset.theme = saved || 'auto';
    $('themeBtn').addEventListener('click', function () {
      var order = ['auto', 'light', 'dark'];
      var cur = document.documentElement.dataset.theme || 'auto';
      var next = order[(order.indexOf(cur) + 1) % order.length];
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem('dryer-theme', next); } catch (e) { /* не сохранится — не критично */ }
      $('themeBtn').textContent = next === 'light' ? '☀' : next === 'dark' ? '☾' : '◐';
      toast('Тема: ' + (next === 'auto' ? 'как в системе' : next === 'light' ? 'светлая' : 'тёмная'));
      requestAnimationFrame(drawTrace);
    });
  }

  function init() {
    buildTicks();
    initTheme();

    document.querySelectorAll('.tab[data-goto]').forEach(function (b) {
      b.addEventListener('click', function () { goto(b.dataset.goto); });
    });
    document.querySelectorAll('[data-range]').forEach(function (b) {
      b.addEventListener('click', function () {
        document.querySelectorAll('[data-range]').forEach(function (x) { x.classList.remove('is-on'); });
        b.classList.add('is-on');
        state.rangeSec = parseInt(b.dataset.range, 10);
        drawTrace();
        if (!state.demo) connectWs();
      });
    });
    document.querySelectorAll('[data-period]').forEach(function (b) {
      b.addEventListener('click', function () {
        document.querySelectorAll('[data-period]').forEach(function (x) { x.classList.remove('is-on'); });
        b.classList.add('is-on');
        statsPeriod = b.dataset.period;
        loadStats();
      });
    });
    $('btnStart').addEventListener('click', startDrying);
    $('btnStop').addEventListener('click', stopDrying);
    window.addEventListener('resize', drawTrace);
    if (window.matchMedia) {
      var mq = window.matchMedia('(prefers-color-scheme: dark)');
      if (mq.addEventListener) mq.addEventListener('change', function () { requestAnimationFrame(drawTrace); });
    }

    // PWA: ставится на главный экран. Регистрируем только по http(s),
    // чтобы открытие файла с диска не сыпало ошибками в консоль.
    if ('serviceWorker' in navigator && location.protocol.indexOf('http') === 0) {
      navigator.serviceWorker.register('sw.js').catch(function () { /* не критично */ });
    }

    var forced = /[?&]demo=1/.test(location.search);
    if (forced) { startDemo(true); render(); drawTrace(); return; }

    loadStatic().then(function () {
      connectWs();
      render();
    }).catch(function () {
      startDemo();
      render();
      drawTrace();
      toast('Сушилка недоступна — демонстрационный режим');
    });

    render();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();

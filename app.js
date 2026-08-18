(() => {
  const CATALOG = Array.isArray(window.MUNICIPALITIES) ? window.MUNICIPALITIES : [];
  const $ = (id) => document.getElementById(id);
  const screens = { home: $('home-screen'), game: $('game-screen'), round: $('round-screen'), results: $('results-screen') };
  const DAILY_ROUNDS = 8, DAILY_SECONDS = 30;
  const state = { rounds: 8, seconds: 30, round: 0, pool: [], current: null, guess: null, guessMarker: null, actualMarker: null, line: null, gameMap: null, resultMap: null, timerId: null, remaining: 0, results: [], dailyDate: null };

  $('catalog-count').textContent = CATALOG.length.toLocaleString();
  const show = (name) => Object.entries(screens).forEach(([key, screen]) => screen.classList.toggle('active', key === name));
  const setMode = (text) => $('mode-label').textContent = text;
  function formatRadarCoordinate(value, positive, negative) {
    return `${Math.abs(value).toFixed(2)}° ${value >= 0 ? positive : negative}`;
  }
  function setRadarLocation() {
    if (!CATALOG.length) return;
    const item = CATALOG[Math.floor(Math.random() * CATALOG.length)];
    $('radar-location').textContent = `${item.name} / ${item.country}`.toUpperCase();
    $('radar-coordinates').innerHTML = `LAT ${formatRadarCoordinate(item.lat, 'N', 'S')}<br>LON ${formatRadarCoordinate(item.lon, 'E', 'W')}`;
  }
  setRadarLocation();

  function updateSetting(kind, value) {
    const input = $(kind); const min = Number(input.min); const max = Number(input.max);
    input.value = Math.max(min, Math.min(max, value));
    const progress = ((Number(input.value) - min) / (max - min)) * 100;
    input.style.setProperty('--range-progress', `${progress}%`);
    if (kind === 'round-count') { state.rounds = Number(input.value); $('round-count-value').textContent = input.value; }
    else { state.seconds = Number(input.value); $('round-time-value').textContent = `${input.value}s`; }
  }
  updateSetting('round-count', $('round-count').value);
  updateSetting('round-time', $('round-time').value);
  $('round-count').addEventListener('input', (e) => updateSetting('round-count', e.target.value));
  $('round-time').addEventListener('input', (e) => updateSetting('round-time', e.target.value));
  document.querySelectorAll('[data-step]').forEach((button) => button.addEventListener('click', () => {
    const input = $(button.dataset.step); const step = Number(input.step || 1); updateSetting(input.id, Number(input.value) + Number(button.dataset.change) * step);
  }));

  function makeFallbackFlag(item) {
    const palettes = [['#c73c3c','#f6d365','#253b70'],['#ee334e','#fff','#00a651'],['#13294b','#fff','#e84a5f'],['#182b49','#ffca3a','#f8f7f2'],['#582c83','#f2c94c','#f2994a'],['#1b4332','#d8f3dc','#95d5b2']];
    const colors = palettes[Math.abs([...item.id].reduce((a, c) => a + c.charCodeAt(0), 0)) % palettes.length];
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 400"><rect width="600" height="400" fill="${colors[0]}"/><rect x="0" y="145" width="600" height="110" fill="${colors[1]}"/><rect x="235" width="130" height="400" fill="${colors[2]}"/><circle cx="300" cy="200" r="55" fill="${colors[1]}" opacity=".9"/><text x="300" y="218" text-anchor="middle" font-family="sans-serif" font-size="42" font-weight="bold" fill="${colors[0]}">${item.name.slice(0, 1)}</text></svg>`;
    return `data:image/svg+xml;charset=UTF-8,${encodeURIComponent(svg)}`;
  }

  function createMap(element) {
    const map = L.map(element, { worldCopyJump: true, minZoom: 2, maxZoom: 19, zoomControl: true }).setView([24, 8], 2);
    L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png', { subdomains: 'abcd', maxZoom: 20, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>' }).addTo(map);
    return map;
  }
  function resetMap(map) { if (map) map.eachLayer((layer) => { if (layer instanceof L.Marker || layer instanceof L.Polyline || layer instanceof L.CircleMarker) map.removeLayer(layer); }); }
  function kmBetween(a, b) {
    const rad = Math.PI / 180, dLat = (b.lat - a.lat) * rad, dLon = (b.lon - a.lon) * rad;
    const x = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) ** 2;
    return 6371.0088 * 2 * Math.atan2(Math.sqrt(x), Math.sqrt(1 - x));
  }
  function scoreFor(distance) { if (distance === null) return 0; if (distance <= 50) return 5000; return Math.max(0, Math.round(5000 * Math.pow(0.1, (Math.min(distance, 25000) - 50) / 5000))); }
  function gradeFor(total, maximum) {
    const percentage = maximum ? total / maximum : 0;
    if (percentage >= 1) return 'S';
    if (percentage >= .9) return 'A+';
    if (percentage >= .8) return 'A';
    if (percentage >= .7) return 'B+';
    if (percentage >= .6) return 'B';
    if (percentage >= .5) return 'C+';
    if (percentage >= .4) return 'C';
    if (percentage >= .3) return 'D+';
    if (percentage >= .2) return 'D';
    return 'F';
  }
  function renderGrade(grade) {
    const mark = $('grade-mark');
    mark.textContent = grade[0];
    if (grade.endsWith('+')) { const plus = document.createElement('sup'); plus.textContent = '+'; mark.append(plus); }
  }
  function formatDistance(distance) { return distance === null ? 'No guess' : distance < 10 ? `${distance.toFixed(1)} km` : `${Math.round(distance).toLocaleString()} km`; }
  function formatCoord(point) { return point ? `${point.lat.toFixed(3)}°, ${point.lon.toFixed(3)}°` : 'No guess'; }

  function localDateKey(date = new Date()) {
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    return `${date.getFullYear()}-${month}-${day}`;
  }
  function seedFromString(value) {
    let seed = 2166136261;
    for (const character of value) { seed ^= character.charCodeAt(0); seed = Math.imul(seed, 16777619); }
    return seed >>> 0;
  }
  function seededRandom(seed) {
    let value = seed >>> 0;
    return () => { value += 0x6D2B79F5; let result = value; result = Math.imul(result ^ (result >>> 15), result | 1); result ^= result + Math.imul(result ^ (result >>> 7), result | 61); return ((result ^ (result >>> 14)) >>> 0) / 4294967296; };
  }
  function pickPool(seed = null) {
    const shuffled = [...CATALOG]; const random = seed === null ? Math.random : seededRandom(seed);
    for (let index = shuffled.length - 1; index > 0; index -= 1) {
      const swapIndex = Math.floor(random() * (index + 1));
      [shuffled[index], shuffled[swapIndex]] = [shuffled[swapIndex], shuffled[index]];
    }
    state.pool = shuffled.slice(0, Math.min(state.rounds, shuffled.length));
  }
  function startGame(daily = false) {
    if (!CATALOG.length) return;
    state.dailyDate = daily ? localDateKey() : null;
    if (daily) { updateSetting('round-count', DAILY_ROUNDS); updateSetting('round-time', DAILY_SECONDS); }
    pickPool(state.dailyDate === null ? null : seedFromString(`cityflagguessr-daily-v1:${state.dailyDate}`));
    state.round = 0; state.results = []; setMode(daily ? `DAILY CHALLENGE · ${state.dailyDate}` : 'EXPEDITION IN PROGRESS'); show('game'); startRound();
  }
  function startRound() {
    state.current = state.pool[state.round]; state.guess = null; state.remaining = state.seconds;
    $('round-number').textContent = String(state.round + 1).padStart(2, '0'); $('round-total').textContent = String(state.pool.length).padStart(2, '0'); $('flag-index').textContent = String(state.round + 1).padStart(2, '0'); $('timer-value').textContent = state.remaining; $('timer').classList.remove('warn');
    const img = $('flag-image'); img.alt = `Flag of ${state.current.name}, ${state.current.country}`; img.src = state.current.flag; img.onerror = () => { img.onerror = null; img.src = makeFallbackFlag(state.current); };
    if (!state.gameMap) state.gameMap = createMap($('guess-map')); else { resetMap(state.gameMap); state.gameMap.setView([24, 8], 2); setTimeout(() => state.gameMap.invalidateSize(), 50); }
    $('accept-guess').disabled = true;
    state.gameMap.off('click').on('click', (event) => { if (state.guessMarker) state.gameMap.removeLayer(state.guessMarker); state.guess = { lat: event.latlng.lat, lon: event.latlng.lng }; state.guessMarker = L.marker(event.latlng).addTo(state.gameMap).bindPopup('Your guess').openPopup(); $('accept-guess').disabled = false; });
    clearInterval(state.timerId); state.timerId = setInterval(() => { state.remaining -= 1; $('timer-value').textContent = state.remaining; if (state.remaining <= 10) $('timer').classList.add('warn'); if (state.remaining <= 0) { clearInterval(state.timerId); resolveRound(true); } }, 1000);
  }
  function resolveRound(timeout = false) {
    if (!state.current) return; clearInterval(state.timerId); state.gameMap.off('click'); $('accept-guess').disabled = true;
    const actual = { lat: state.current.lat, lon: state.current.lon }; const distance = state.guess ? kmBetween(state.guess, actual) : null; const score = scoreFor(distance);
    state.results.push({ item: state.current, guess: state.guess, distance, score });
    $('result-eyebrow').textContent = timeout ? 'TIME EXPIRED' : 'ROUND COMPLETE'; $('result-subtitle').textContent = timeout ? (state.guess ? 'The last pin on the map counted as your final guess.' : 'No location was locked in before the clock ran out.') : 'Your location compared with the real municipality.';
    $('round-score').textContent = score.toLocaleString(); $('round-distance').textContent = formatDistance(distance); $('real-location').textContent = `${state.current.name}, ${state.current.country}`; $('picked-location').textContent = formatCoord(state.guess); $('flag-source').href = state.current.source; $('flag-source').textContent = state.current.sourceLabel + ' ↗'; $('continue-game').textContent = state.round === state.pool.length - 1 ? 'SEE RESULTS  →' : 'NEXT ROUND  →';
    show('round'); if (!state.resultMap) state.resultMap = createMap($('result-map')); else { resetMap(state.resultMap); setTimeout(() => state.resultMap.invalidateSize(), 50); }
    const actualLatLng = [actual.lat, actual.lon]; L.circleMarker(actualLatLng, { radius: 9, color: '#ff8b4d', fillColor: '#ff8b4d', fillOpacity: 1 }).addTo(state.resultMap).bindPopup(`Real location: ${state.current.name}`).openPopup();
    const bounds = [actualLatLng]; if (state.guess) { const guessLatLng = [state.guess.lat, state.guess.lon]; L.marker(guessLatLng).addTo(state.resultMap).bindPopup('Your guess'); L.polyline([guessLatLng, actualLatLng], { color: '#53e0d0', weight: 3, dashArray: '5 10' }).addTo(state.resultMap); bounds.push(guessLatLng); }
    state.resultMap.fitBounds(bounds, { padding: [70, 70], maxZoom: state.guess ? 8 : 8 });
  }
  function showResults() {
    setMode('FIELD REPORT'); show('results'); const total = state.results.reduce((sum, item) => sum + item.score, 0); const maximum = state.results.length * 5000; $('results-title').textContent = state.dailyDate ? 'Daily Challenge complete.' : 'World tour complete.'; $('total-score').textContent = total.toLocaleString(); $('max-score').textContent = maximum.toLocaleString(); renderGrade(gradeFor(total, maximum)); $('results-summary').textContent = `${state.results.length} ROUNDS`;
    $('results-body').innerHTML = state.results.map((result, index) => `<tr><td class="round-pill">${String(index + 1).padStart(2, '0')}</td><td><strong>${result.item.name}</strong></td><td>${result.item.country}</td><td>${formatDistance(result.distance)}</td><td>${result.score.toLocaleString()}</td></tr>`).join('');
  }
  $('start-game').addEventListener('click', () => startGame(false)); $('start-daily').addEventListener('click', () => startGame(true)); $('accept-guess').addEventListener('click', () => resolveRound(false)); $('continue-game').addEventListener('click', () => { if (state.round === state.pool.length - 1) showResults(); else { state.round += 1; show('game'); startRound(); } }); $('return-home').addEventListener('click', () => { setMode('READY TO EXPLORE'); show('home'); }); $('brand-home').addEventListener('click', () => { clearInterval(state.timerId); setMode('READY TO EXPLORE'); show('home'); });
  $('credits-open').addEventListener('click', () => $('credits-dialog').showModal()); $('credits-close').addEventListener('click', () => $('credits-dialog').close());
})();

// Сигнал «я работаю»: раз в минуту, пока вкладка открыта и человек
// действительно что-то делает. Забытая на ночь вкладка часов не намотает —
// без движений мышью, клавиатуры и прокрутки сигнал просто не уходит.
(() => {
  const BEAT = 60 * 1000;
  const IDLE = 5 * 60 * 1000;   // столько без действий — считаем, что отошли
  let lastInput = Date.now();

  const mark = () => { lastInput = Date.now(); };

  const beat = () => {
    if (document.visibilityState !== 'visible') return;
    if (Date.now() - lastInput > IDLE) return;
    fetch('/api/ping', { method: 'POST' }).catch(() => {});
  };

  ['mousemove', 'mousedown', 'keydown', 'wheel', 'touchstart', 'scroll']
    .forEach((e) => window.addEventListener(e, mark, { passive: true }));

  document.addEventListener('visibilitychange', () => {
    // Вернулись на вкладку — это тоже действие, отсчёт простоя начинаем заново.
    if (document.visibilityState === 'visible') { mark(); beat(); }
  });

  beat();
  setInterval(beat, BEAT);
})();

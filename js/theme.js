'use strict';

// Loaded synchronously in <head> so the theme is applied before first paint.
(() => {
  const KEY = 'byui-scheduler-theme';
  const MODES = ['auto', 'light', 'dark'];
  const LABELS = { auto: 'Theme: Auto', light: 'Theme: Light', dark: 'Theme: Dark' };
  const dark = matchMedia('(prefers-color-scheme: dark)');
  const stored = localStorage.getItem(KEY);
  let mode = MODES.includes(stored) ? stored : 'auto';

  const apply = () => {
    const resolved = mode === 'auto' ? (dark.matches ? 'dark' : 'light') : mode;
    document.documentElement.dataset.theme = resolved;
    const btn = document.getElementById('themeToggle');
    if (btn) {
      btn.textContent = LABELS[mode];
      btn.setAttribute('aria-label', `${LABELS[mode]} (click to change)`);
    }
  };

  apply();
  dark.addEventListener('change', apply);
  document.addEventListener('DOMContentLoaded', () => {
    apply();
    document.getElementById('themeToggle').addEventListener('click', () => {
      mode = MODES[(MODES.indexOf(mode) + 1) % MODES.length];
      localStorage.setItem(KEY, mode);
      apply();
    });
  });
})();

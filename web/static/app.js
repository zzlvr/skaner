// Small enhancements. The page is fully usable without this script.
(function () {
  // Language menu: close on outside click or Escape.
  var menu = document.querySelector('.langmenu');
  if (menu) {
    document.addEventListener('click', function (e) {
      if (menu.open && !menu.contains(e.target)) menu.open = false;
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') menu.open = false;
    });
  }

  if (!('IntersectionObserver' in window)) return;

  // Pause the hero demo while it is off screen (saves battery on phones).
  var win = document.querySelector('.win');
  if (win) {
    new IntersectionObserver(function (entries) {
      win.classList.toggle('paused', !entries[0].isIntersecting);
    }).observe(win);
  }

  // Fade blocks in when they are scrolled to. Blocks are hidden only once this script has run.
  var items = document.querySelectorAll('[data-reveal]');
  if (!items.length) return;
  document.documentElement.classList.add('js-reveal');
  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (en) {
      if (en.isIntersecting) {
        en.target.classList.add('in');
        io.unobserve(en.target);
      }
    });
  }, { threshold: 0.12 });
  items.forEach(function (el, i) {
    el.style.transitionDelay = (i % 3) * 60 + 'ms';
    io.observe(el);
  });
})();

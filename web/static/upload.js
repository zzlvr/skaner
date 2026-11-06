// Cabinet enhancements. Every page works without this script (plain form post; batch page: reload).
(function () {
  // ---- Upload page: drag & drop, several camera shots, file list, early size checks ----
  var form = document.getElementById('upform');
  if (form) {
    var multi = document.getElementById('files');
    var camera = document.getElementById('camera');
    var drop = document.getElementById('updrop');
    var list = document.getElementById('uplist');
    var msg = document.getElementById('upmsg');
    var send = document.getElementById('upsend');
    var maxFiles = parseInt(form.getAttribute('data-max-files'), 10) || 20;
    var maxBytes = (parseInt(form.getAttribute('data-max-mb'), 10) || 20) * 1024 * 1024;
    var bag = null;
    try { bag = new DataTransfer(); } catch (e) { bag = null; }  // old browsers: no merging, inputs still work

    function render() {
      while (list.firstChild) list.removeChild(list.firstChild);
      var files = multi.files || [];
      for (var i = 0; i < files.length; i++) {
        var li = document.createElement('li');
        var name = document.createElement('span');
        name.textContent = files[i].name;  // text only, never HTML
        var size = document.createElement('span');
        size.textContent = (files[i].size / 1048576).toFixed(1) + ' MB';
        li.appendChild(name);
        li.appendChild(size);
        if (bag) {
          var rm = document.createElement('button');
          rm.type = 'button';
          rm.className = 'uprm';
          rm.textContent = form.getAttribute('data-msg-remove');
          rm.setAttribute('data-i', String(i));
          rm.addEventListener('click', remove);
          li.appendChild(rm);
        }
        list.appendChild(li);
      }
      check();
    }

    function check() {
      var files = multi.files || [];
      var problem = '';
      if (files.length > maxFiles) problem = form.getAttribute('data-msg-files');
      for (var i = 0; i < files.length && !problem; i++) {
        if (files[i].size > maxBytes) problem = form.getAttribute('data-msg-size');
      }
      msg.textContent = problem;
      return !problem;
    }

    function add(fileList) {
      if (!bag) return;
      for (var i = 0; i < fileList.length; i++) bag.items.add(fileList[i]);
      multi.files = bag.files;
      render();
    }

    function remove(e) {
      var skip = parseInt(e.currentTarget.getAttribute('data-i'), 10);
      var next = new DataTransfer();
      for (var i = 0; i < bag.files.length; i++) if (i !== skip) next.items.add(bag.files[i]);
      bag = next;
      multi.files = bag.files;
      render();
    }

    multi.addEventListener('change', function () {
      if (bag) {
        var chosen = Array.prototype.slice.call(multi.files);
        multi.files = bag.files;
        add(chosen);
      } else {
        render();
      }
    });
    camera.addEventListener('change', function () {
      if (bag && camera.files.length) {
        add(camera.files);
        camera.value = '';  // the shot now travels in the main input; do not send it twice
      }
    });
    ['dragenter', 'dragover'].forEach(function (ev) {
      drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add('over'); });
    });
    ['dragleave', 'drop'].forEach(function (ev) {
      drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove('over'); });
    });
    drop.addEventListener('drop', function (e) {
      if (e.dataTransfer && e.dataTransfer.files) add(e.dataTransfer.files);
    });
    // Format: describe the chosen kind; the PDF formats have no document type and no "one document" mode.
    var fmt = document.getElementById('upfmt');
    var desc = document.getElementById('fmtdesc');
    var doctypeRow = document.getElementById('updoctype');
    var oneRow = document.getElementById('upone');
    function onFormat() {
      var opt = fmt.options[fmt.selectedIndex];
      if (!opt) return;
      var kind = opt.getAttribute('data-kind');
      desc.textContent = opt.getAttribute('data-desc') || '';  // text only, never HTML
      doctypeRow.hidden = kind !== 'table';
      oneRow.hidden = kind !== 'table';
      send.textContent = form.getAttribute(kind === 'scan' ? 'data-msg-submit-scan' : 'data-msg-submit');
      var keyBox = document.getElementById('byok');
      if (keyBox) keyBox.hidden = kind === 'scan';  // the local scan uses no model: no key
    }
    if (fmt && desc && doctypeRow && oneRow) {
      fmt.addEventListener('change', onFormat);
      onFormat();  // the browser may restore a previous choice on "back"
    }

    // Own provider key: remembered only in this browser, and only when the box is ticked.
    var keyIn = document.getElementById('byokkey');
    var keyProv = document.getElementById('byokprov');
    var keyMem = document.getElementById('byokremember');
    if (keyIn && keyProv && keyMem) {
      var KEY = 'skaner_byok';
      try {
        var saved = JSON.parse(localStorage.getItem(KEY) || 'null');
        if (saved && saved.k) { keyProv.value = saved.p; keyIn.value = saved.k; keyMem.checked = true; }
      } catch (e) { /* storage blocked: the field simply starts empty */ }
      var hint = function () { keyIn.placeholder = keyProv.value === 'gemini' ? 'AIza...' : 'sk-ant-...'; };
      keyProv.addEventListener('change', hint);
      hint();
      keyMem.addEventListener('change', function () {
        if (!keyMem.checked) { try { localStorage.removeItem(KEY); } catch (e) { /* ignore */ } }
      });
      form.addEventListener('submit', function () {
        try {
          if (keyMem.checked && keyIn.value) localStorage.setItem(KEY, JSON.stringify({ p: keyProv.value, k: keyIn.value }));
        } catch (e) { /* ignore */ }
      });
    }

    form.addEventListener('submit', function (e) {
      if (!check()) { e.preventDefault(); return; }
      send.disabled = true;
      send.textContent = form.getAttribute('data-msg-sending');
    });
  }

  // ---- Batch page: poll the status every 1.5 s, reload when finished ----
  var box = document.getElementById('bstat');
  if (box && window.fetch) {
    var url = box.getAttribute('data-url');
    var tpl = box.getAttribute('data-tpl');
    var prog = document.getElementById('bprog');
    var text = document.getElementById('btext');
    var state = document.getElementById('bstate');
    var tick = function () {
      fetch(url, { credentials: 'same-origin', cache: 'no-store' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (s) {
          if (!s) return;
          if (s.status === 'done' || s.status === 'failed') { window.location.reload(); return; }
          prog.max = s.total; prog.value = s.done;
          text.textContent = tpl.replace('{done}', s.done).replace('{total}', s.total);
          state.textContent = s.label;
          setTimeout(tick, 1500);
        })
        .catch(function () { setTimeout(tick, 5000); });
    };
    setTimeout(tick, 1500);
  }
})();

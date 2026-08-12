// ============================
// LIBCHAT FUNCTIONS
// ============================
let libchatScriptPromise = null;

function openExternalWindow(url) {
  const opened = window.open(url, '_blank', 'noopener,noreferrer');
  if (opened) opened.opener = null;
}

function loadLibchatScript() {
  if (libchatScriptPromise) return libchatScriptPromise;

  const existingScript = document.querySelector('script[src*="uaeu.libanswers.com/load_chat.php"]');
  if (existingScript) {
    libchatScriptPromise = Promise.resolve();
    return libchatScriptPromise;
  }

  libchatScriptPromise = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = 'https://uaeu.libanswers.com/load_chat.php?hash=1989346ba8efc72bf598fe6803917b73';
    script.async = true;
    script.onload = resolve;
    script.onerror = reject;
    document.body.appendChild(script);
  });
  return libchatScriptPromise;
}

function waitForLibchatWidget(timeoutMs = 4000) {
  const container = document.getElementById('libchat_1989346ba8efc72bf598fe6803917b73');
  if (!container) return Promise.reject(new Error('LibChat container missing'));
  if (container.querySelector('iframe') || container.children.length > 0) {
    return Promise.resolve(container);
  }

  return new Promise((resolve, reject) => {
    const observer = new MutationObserver(() => {
      if (container.querySelector('iframe') || container.children.length > 0) {
        observer.disconnect();
        resolve(container);
      }
    });
    observer.observe(container, { childList: true, subtree: true });
    setTimeout(() => {
      observer.disconnect();
      if (container.querySelector('iframe') || container.children.length > 0) {
        resolve(container);
      } else {
        reject(new Error('LibChat widget did not initialize'));
      }
    }, timeoutMs);
  });
}

async function openLibchat() {
  const libchatWidget = document.getElementById('libchat_1989346ba8efc72bf598fe6803917b73');
  const closeBtn = document.getElementById('libchatCloseBtn');

  try {
    await loadLibchatScript();
    await waitForLibchatWidget();
  } catch {
    openExternalWindow(
      isArabic
        ? 'https://www.uaeu.ac.ae/ar/library/ask-us.shtml'
        : 'https://www.uaeu.ac.ae/en/library/ask-us.shtml'
    );
    return;
  }

  if (libchatWidget) {
    libchatWidget.classList.add('active');
  }
  if (closeBtn) {
    closeBtn.classList.add('active');
  }
}
function closeLibchat() {
  const libchatWidget = document.getElementById('libchat_1989346ba8efc72bf598fe6803917b73');
  const closeBtn = document.getElementById('libchatCloseBtn');

  if (libchatWidget) {
    libchatWidget.classList.remove('active');
    closeBtn.classList.remove('active');
  }
}

// ============================
// DATABASE LINK FUNCTION
// ============================
function openDatabaseLink() {
  const url = isArabic
    ? 'https://www.uaeu.ac.ae/ar/library/databases.shtml'
    : 'https://www.uaeu.ac.ae/en/library/databases.shtml';
  openExternalWindow(url);
}

// Copyright (c) 2026 Asadik Hamed. All rights reserved. See LICENSE.

// ============================
// WELCOME POPUP FUNCTIONS
// ============================
let searchMode = 'all'; // Automatic routing: books, articles, and library-service questions.

function normalizeSearchMode(mode) {
  return ['books', 'research', 'all'].includes(mode) ? mode : 'all';
}

function getSearchModePlaceholder(mode) {
  const normalizedMode = normalizeSearchMode(mode);
  if (normalizedMode === 'research') {
    return isArabic
      ? 'ابحث عن مقالات، دوريات، أو أبحاث...'
      : 'Search for articles, journals, or research...';
  }
  if (normalizedMode === 'books') {
    return isArabic
      ? 'ابحث عن كتب، مؤلفين، أو مواضيع...'
      : 'Search for books, authors, or topics...';
  }
  return isArabic
    ? 'اسأل عن الكتب، المقالات، قواعد البيانات، أو خدمات المكتبة...'
    : 'Ask about books, articles, databases, or library services...';
}

function updateSearchModeControls() {
  searchMode = normalizeSearchMode(searchMode);

  const input = document.getElementById('msgInput');
  if (input) {
    input.placeholder = getSearchModePlaceholder(searchMode);
  }
  updateFilterControls();
}

function closeFilterPanel() {
  const panel = document.getElementById('filterPanel');
  const button = document.getElementById('filterBtn');
  if (panel) {
    panel.classList.remove('open');
    panel.setAttribute('aria-hidden', 'true');
  }
  if (button) {
    button.classList.remove('active');
    button.setAttribute('aria-expanded', 'false');
  }
}

function toggleFilterPanel() {
  const panel = document.getElementById('filterPanel');
  const button = document.getElementById('filterBtn');
  if (!panel || !button) return;

  updateFilterControls();
  const isOpen = panel.classList.toggle('open');
  panel.setAttribute('aria-hidden', isOpen ? 'false' : 'true');
  button.classList.toggle('active', isOpen);
  button.setAttribute('aria-expanded', String(isOpen));
}

function loadSavedFilters() {
  try {
    const saved = JSON.parse(localStorage.getItem('searchFilters') || '{}');
    activeFilters = {
      format: saved.format || 'any',
      year_from: saved.year_from || '',
      year_to: saved.year_to || '',
      open_access_only: Boolean(saved.open_access_only),
    };
  } catch {
    activeFilters = { format: 'any', year_from: '', year_to: '', open_access_only: false };
  }
}

function hasActiveFilters() {
  const supportsBookFilters = searchMode === 'books' || searchMode === 'all';
  const supportsArticleFilters = searchMode === 'research' || searchMode === 'all';
  const hasBookFormat = supportsBookFilters && activeFilters.format && activeFilters.format !== 'any';
  const hasOpenAccess = supportsArticleFilters && activeFilters.open_access_only;
  return Boolean(hasBookFormat || hasOpenAccess || activeFilters.year_from || activeFilters.year_to);
}

function updateFilterButtonState() {
  const button = document.getElementById('filterBtn');
  if (!button) return;
  button.classList.toggle('has-filters', hasActiveFilters());
  button.setAttribute('aria-label', isArabic ? 'مرشحات البحث' : 'Search filters');
  button.setAttribute('title', isArabic ? 'مرشحات البحث' : 'Search filters');
}

function updateFilterControls() {
  const bookFormat = document.getElementById('bookFormatFilter');
  const openAccess = document.getElementById('articleOpenAccessFilter');
  const supportsBookFilters = searchMode === 'books' || searchMode === 'all';
  const supportsArticleFilters = searchMode === 'research' || searchMode === 'all';
  if (bookFormat) bookFormat.classList.toggle('is-hidden', !supportsBookFilters);
  if (openAccess) openAccess.classList.toggle('is-hidden', !supportsArticleFilters);

  const currentYear = new Date().getFullYear();
  ['filterYearFrom', 'filterYearTo'].forEach(id => {
    const input = document.getElementById(id);
    if (input) input.max = String(currentYear + 2);
  });

  const labels = {
    filterTitle: ['Search filters', 'مرشحات البحث'],
    filterFormatLabel: ['Book format', 'صيغة الكتاب'],
    filterFormatAny: ['Any format', 'أي صيغة'],
    filterFormatEbook: ['eBook', 'كتاب إلكتروني'],
    filterFormatPrint: ['Print book', 'كتاب مطبوع'],
    filterFormatAudio: ['Audiobook', 'كتاب صوتي'],
    filterOpenAccessLabel: ['Open Access only', 'المقالات المفتوحة فقط'],
    filterYearFromLabel: ['From year', 'من سنة'],
    filterYearToLabel: ['To year', 'إلى سنة'],
    filterClearBtn: ['Clear', 'مسح'],
    filterApplyBtn: ['Apply', 'تطبيق'],
  };
  Object.entries(labels).forEach(([id, text]) => {
    const el = document.getElementById(id);
    if (el) el.textContent = isArabic ? text[1] : text[0];
  });

  const format = document.getElementById('filterFormat');
  const open = document.getElementById('filterOpenAccess');
  const from = document.getElementById('filterYearFrom');
  const to = document.getElementById('filterYearTo');
  if (format) format.value = activeFilters.format || 'any';
  if (open) open.checked = Boolean(activeFilters.open_access_only);
  if (from) from.value = activeFilters.year_from || '';
  if (to) to.value = activeFilters.year_to || '';
  updateFilterButtonState();
}

function applyFilters() {
  const format = document.getElementById('filterFormat')?.value || 'any';
  const openAccessOnly = Boolean(document.getElementById('filterOpenAccess')?.checked);
  const fromValue = document.getElementById('filterYearFrom')?.value || '';
  const toValue = document.getElementById('filterYearTo')?.value || '';
  const fromYear = fromValue ? Number(fromValue) : '';
  const toYear = toValue ? Number(toValue) : '';
  const maximumYear = new Date().getFullYear() + 2;

  const invalidYear = [fromYear, toYear]
    .filter(Boolean)
    .some(year => !Number.isInteger(year) || year < 1800 || year > maximumYear);
  if (invalidYear) {
    alert(isArabic
      ? `يجب أن تكون السنة بين 1800 و${maximumYear}.`
      : `Years must be between 1800 and ${maximumYear}.`);
    return;
  }

  if (fromYear && toYear && fromYear > toYear) {
    alert(isArabic ? 'سنة البداية يجب أن تكون قبل سنة النهاية.' : 'From year must be before To year.');
    return;
  }

  activeFilters = {
    format,
    year_from: fromYear,
    year_to: toYear,
    open_access_only: openAccessOnly,
  };
  localStorage.setItem('searchFilters', JSON.stringify(activeFilters));
  updateFilterControls();
  closeFilterPanel();
}

function clearFilters() {
  activeFilters = { format: 'any', year_from: '', year_to: '', open_access_only: false };
  localStorage.removeItem('searchFilters');
  updateFilterControls();
  closeFilterPanel();
}


function getFiltersForPayload() {
  const filters = {};
  const supportsBookFilters = searchMode === 'books' || searchMode === 'all';
  const supportsArticleFilters = searchMode === 'research' || searchMode === 'all';
  if (supportsBookFilters && activeFilters.format && activeFilters.format !== 'any') {
    filters.format = activeFilters.format;
  }
  if (supportsArticleFilters && activeFilters.open_access_only) {
    filters.open_access_only = true;
  }
  if (activeFilters.year_from) {
    filters.year_from = Number(activeFilters.year_from);
  }
  if (activeFilters.year_to) {
    filters.year_to = Number(activeFilters.year_to);
  }
  return Object.keys(filters).length ? filters : null;
}

// ============================
// STATE
// ============================
let isArabic = false;
let isProcessing = false;
let sessionId = '';
let csrfToken = '';
let sessionExpiresAt = 0;
let lastSearchText = '';
let progressTimer = null;
let progressStepIndex = 0;
let progressStartedAt = 0;
let lastModalTrigger = null;
let activeFilters = {
  format: 'any',
  year_from: '',
  year_to: '',
  open_access_only: false,
};

// ============================
// CONFIGURATION
// ============================
function resolveApiBaseUrl() {
  const configured = window.APP_CONFIG?.API_BASE_URL;
  if (configured) {
    return configured.replace(/\/+$/, '');
  }

  const { protocol, hostname, port, origin } = window.location;
  if (protocol === 'file:') {
    return 'http://127.0.0.1:8000';
  }
  if (hostname === 'localhost' || hostname === '127.0.0.1') {
    return 'http://127.0.0.1:8000';
  }
  if (port && port !== '8000') {
    return `${protocol}//${hostname}:8000`;
  }
  return origin;
}

const CONFIG = {
  API_BASE_URL: resolveApiBaseUrl(),

  get SESSION_URL() { return this.API_BASE_URL + '/session'; },
  get API_URL() { return this.API_BASE_URL + '/ai-search'; },
  TYPING_SPEED: 8,
  TYPING_CHUNK_SIZE: 5,
  MIN_PROGRESS_MS: 650,
  MAX_ANIMATED_CHARS: 900,
  MAX_QUERY_LENGTH: 500,
  REQUEST_TIMEOUT_MS: 30000
};

// ============================
// UTILITIES
// ============================
function appendLinkedText(container, text) {
  text = String(text ?? '');
  const urlRegex = /(https?:\/\/[^\s<>"']+)/gi;
  let lastIndex = 0;
  let match;
  const regex = new RegExp(urlRegex.source, 'gi');

  while ((match = regex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      container.appendChild(document.createTextNode(text.slice(lastIndex, match.index)));
    }

    let url = match[0];
    let trailing = '';
    const trailingMatch = url.match(/[.,;:!?)]+$/);
    if (trailingMatch) {
      trailing = trailingMatch[0];
      url = url.slice(0, -trailingMatch[0].length);
    }

    try {
      const parsed = new URL(url);
      if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
        const link = document.createElement('a');
        link.href = parsed.href;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.className = 'result-link';
        link.textContent = url;
        container.appendChild(link);
      } else {
        container.appendChild(document.createTextNode(url));
      }
    } catch {
      container.appendChild(document.createTextNode(url));
    }

    if (trailing) {
      container.appendChild(document.createTextNode(trailing));
    }

    lastIndex = match.index + match[0].length;
  }

  if (lastIndex < text.length) {
    container.appendChild(document.createTextNode(text.slice(lastIndex)));
  }
}


function isMarkdownTableLine(line) {
  return /^\s*\|.+\|\s*$/.test(line || '');
}

function splitMarkdownTableRow(line) {
  return String(line || '')
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map(cell => cell.trim());
}

function isMarkdownTableSeparator(line) {
  const cells = splitMarkdownTableRow(line);
  return cells.length > 1 && cells.every(cell => /^:?-{3,}:?$/.test(cell.replace(/\s/g, '')));
}

function appendMarkdownTable(container, tableLines) {
  const headers = splitMarkdownTableRow(tableLines[0]);
  const rows = tableLines.slice(2).map(splitMarkdownTableRow).filter(row => row.length);
  const wrap = document.createElement('div');
  wrap.className = 'policy-table-wrap';
  const table = document.createElement('table');
  table.className = 'policy-table';

  const thead = document.createElement('thead');
  const headerRow = document.createElement('tr');
  headers.forEach(header => {
    const th = document.createElement('th');
    appendLinkedText(th, header);
    headerRow.appendChild(th);
  });
  thead.appendChild(headerRow);
  table.appendChild(thead);

  const tbody = document.createElement('tbody');
  rows.forEach(row => {
    const tr = document.createElement('tr');
    headers.forEach((_, index) => {
      const td = document.createElement('td');
      appendLinkedText(td, row[index] || '');
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  wrap.appendChild(table);
  container.appendChild(wrap);
  return wrap;
}

function isResultStartLine(line) {
  return /^\s*\d+\.\s+\S/.test(line || '');
}

function isSectionDividerLine(line) {
  return /^\s*(=+|-{6,})\s*$/.test(line || '');
}

function isResultMetadataLine(line) {
  return /^\s*(Authors?|Year|Journal|Format|Why This|Link|Article Link|Database|Open Access|Title|Call Number|المؤلفون|المؤلف|السنة|المجلة|الصيغة|الرابط|العنوان|رقم التصنيف|لماذا)/i
    .test(line || '');
}

function collectResultBlock(lines, startIndex) {
  if (!isResultStartLine(lines[startIndex])) return null;

  const detailLines = [];
  let cursor = startIndex + 1;

  while (cursor < lines.length) {
    const line = lines[cursor];
    if (!String(line || '').trim()) break;
    if (isResultStartLine(line) || isSectionDividerLine(line) || isMarkdownTableLine(line)) break;
    detailLines.push(line);
    cursor++;
  }

  const metadataCount = detailLines.filter(isResultMetadataLine).length;
  if (metadataCount < 2) return null;

  if (cursor < lines.length && !String(lines[cursor] || '').trim()) {
    cursor++;
  }

  return {
    titleLine: lines[startIndex],
    detailLines,
    nextIndex: cursor
  };
}

function setRevealDelay(element, revealIndex, enabled = true) {
  if (!enabled || prefersReducedMotion()) return;
  element.classList.add('output-reveal-block');
  element.style.setProperty('--result-delay', `${Math.min(revealIndex * 38, 520)}ms`);
}

function appendOutputLine(container, line, revealIndex, animateBlocks) {
  const row = document.createElement('div');
  const value = String(line ?? '');
  row.className = 'output-line';
  if (!value.trim()) {
    row.classList.add('output-line-spacer');
  } else if (isSectionDividerLine(value)) {
    row.classList.add('output-divider-line');
    row.setAttribute('aria-hidden', 'true');
  } else {
    appendLinkedText(row, value);
  }
  setRevealDelay(row, revealIndex, animateBlocks);
  container.appendChild(row);
}

function appendResultBlock(container, block, resultIndex = 0, animateBlocks = true) {
  const details = document.createElement('details');
  details.className = 'result-collapse result-card-animated';
  details.open = window.innerWidth > 768;
  setRevealDelay(details, resultIndex, animateBlocks);

  const summary = document.createElement('summary');
  summary.className = 'result-collapse-title';
  appendLinkedText(summary, block.titleLine.trim());
  details.appendChild(summary);

  const body = document.createElement('div');
  body.className = 'result-collapse-body';
  block.detailLines.forEach((line, lineIndex) => {
    appendLinkedText(body, String(line || '').trim());
    if (lineIndex < block.detailLines.length - 1) {
      body.appendChild(document.createElement('br'));
    }
  });
  details.appendChild(body);
  container.appendChild(details);
}


function appendFormattedText(container, text, options = {}) {
  const animateBlocks = Boolean(options.animateBlocks);
  const lines = String(text ?? '').split('\n');
  let index = 0;
  let revealIndex = 0;

  while (index < lines.length) {
    if (
      isMarkdownTableLine(lines[index]) &&
      index + 1 < lines.length &&
      isMarkdownTableSeparator(lines[index + 1])
    ) {
      const tableLines = [lines[index], lines[index + 1]];
      index += 2;
      while (index < lines.length && isMarkdownTableLine(lines[index])) {
        tableLines.push(lines[index]);
        index++;
      }
      const table = appendMarkdownTable(container, tableLines);
      setRevealDelay(table, revealIndex, animateBlocks);
      revealIndex++;
      continue;
    }

    const resultBlock = collectResultBlock(lines, index);
    if (resultBlock) {
      appendResultBlock(container, resultBlock, revealIndex, animateBlocks);
      revealIndex++;
      index = resultBlock.nextIndex;
      continue;
    }

    appendOutputLine(container, lines[index], revealIndex, animateBlocks);
    revealIndex++;
    index++;
  }
}


function autoResize(textarea) {
  const lineCount = textarea.value.split('\n').length;
  textarea.rows = Math.min(Math.max(lineCount, 1), 4);
}

function scrollToBottom() {
  const chat = document.getElementById('chat');
  chat.scrollTop = chat.scrollHeight;
}

function prefersReducedMotion() {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

function uiText(en, ar) {
  return isArabic ? ar : en;
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ============================
// TYPING EFFECT
// ============================
function renderMessageNow(element, text, callback, animateBlocks = true) {
  element.textContent = '';
  appendFormattedText(element, text, {
    animateBlocks: animateBlocks && element.classList.contains('ai')
  });
  scrollToBottom();
  if (callback) callback();
}

function shouldAnimateMessage(text) {
  const value = String(text ?? '');
  if (prefersReducedMotion()) return false;
  if (value.length > CONFIG.MAX_ANIMATED_CHARS) return false;
  if (value.includes('============================================================')) return false;
  if (value.split('\n').length > 12) return false;
  return true;
}

function typeMessage(element, text, callback) {
  text = String(text ?? '');

  if (!shouldAnimateMessage(text)) {
    renderMessageNow(element, text, callback);
    return;
  }

  element.textContent = '';
  const chars = text.split('');
  let charIndex = 0;

  function typeChunk() {
    if (charIndex < chars.length) {
      const nextIndex = Math.min(charIndex + CONFIG.TYPING_CHUNK_SIZE, chars.length);
      element.appendChild(document.createTextNode(chars.slice(charIndex, nextIndex).join('')));
      charIndex = nextIndex;
      scrollToBottom();
      window.setTimeout(typeChunk, CONFIG.TYPING_SPEED);
      return;
    }

    renderMessageNow(element, text, callback, false);
  }

  typeChunk();
}

// ============================
// UI CONTROLS
// ============================
function setMenuOpen(isOpen) {
  const sidebar = document.getElementById('sidebar');
  const overlay = document.getElementById('sidebarOverlay');
  const menuButton = document.getElementById('menuBtn');
  const closeButton = document.getElementById('closeSidebarBtn');

  sidebar.classList.toggle('open', isOpen);
  overlay.classList.toggle('visible', isOpen);
  sidebar.setAttribute('aria-hidden', String(!isOpen));
  overlay.setAttribute('aria-hidden', String(!isOpen));
  menuButton.setAttribute('aria-expanded', String(isOpen));
  sidebar.inert = !isOpen;

  if (isOpen) {
    closeButton.focus();
  } else if (document.activeElement && sidebar.contains(document.activeElement)) {
    menuButton.focus();
  }
}

function toggleMenu() {
  const sidebar = document.getElementById('sidebar');
  setMenuOpen(!sidebar.classList.contains('open'));
}

function startNewConversation() {
  resetSession();
  lastSearchText = '';
  removeProgressIndicator();
  const chat = document.getElementById('chat');
  chat.replaceChildren();
  showWelcome();
  setMenuOpen(false);
  document.getElementById('msgInput').focus();
}

function getFocusableElements(container) {
  return Array.from(container.querySelectorAll(
    'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
  )).filter(element => !element.hidden && element.offsetParent !== null);
}

function setAboutOpen(isOpen, trigger = null) {
  const modal = document.getElementById('aboutModal');
  modal.classList.toggle('open', isOpen);
  modal.setAttribute('aria-hidden', String(!isOpen));
  document.body.classList.toggle('modal-open', isOpen);

  if (isOpen) {
    lastModalTrigger = trigger || document.activeElement;
    if (document.getElementById('sidebar').classList.contains('open')) {
      setMenuOpen(false);
    }
    window.setTimeout(() => document.getElementById('aboutCloseBtn').focus(), 0);
  } else {
    const sidebar = document.getElementById('sidebar');
    const restoreTarget = lastModalTrigger && sidebar.contains(lastModalTrigger)
      ? document.getElementById('menuBtn')
      : lastModalTrigger;
    lastModalTrigger = null;
    if (restoreTarget && typeof restoreTarget.focus === 'function') {
      restoreTarget.focus();
    }
  }
}

function toggleAbout(event) {
  const modal = document.getElementById('aboutModal');
  setAboutOpen(!modal.classList.contains('open'), event?.currentTarget || null);
}

function toggleDark() {
  const isDark = document.body.classList.toggle('dark');
  localStorage.setItem('darkMode', String(isDark));
  const button = document.getElementById('headerThemeBtn');
  button.querySelector('span').textContent = isDark ? '☀️' : '🌙';
  button.setAttribute('aria-pressed', String(isDark));
  button.setAttribute('aria-label', isArabic
    ? (isDark ? 'التبديل إلى الوضع الفاتح' : 'التبديل إلى الوضع الداكن')
    : (isDark ? 'Switch to light mode' : 'Switch to dark mode'));
}

function toggleLang() {
  isArabic = !isArabic;
  updateLanguage();
}

function updateLanguage() {
  const t = isArabic ? {
    appTitle: 'مساعد مكتبات جامعة الإمارات الذكي',
    betaBadge: 'تجريبي',
    sidebarTitle: 'روابط سريعة',
    newConversationText: 'محادثة جديدة',
    officialResourcesText: 'مصادر رسمية',
    linkLibText: 'موقع المكتبة',
    linkHoursText: 'مواعيد المكتبة',
    linkFaqText: 'الأسئلة الشائعة',
    linkDatabaseText: 'قواعد البيانات',
    linkRequestText: 'طلب شراء مصدر',
    linkAccountText: 'حسابي',
    linkAboutText: 'حول',
    aboutTitle: 'حول مساعد مكتبات جامعة الإمارات الذكي',
    aboutDesc1: 'مساعد مكتبات جامعة الإمارات الذكي هو النسخة التجريبية الرسمية من دليل تفاعلي لخدمة طلبة الجامعة وأعضاء هيئة التدريس والموظفين والباحثين وزوار المكتبة.',
    aboutDesc2: '<b>المميزات:</b>',
    aboutDesc3: '<b>المصادر المعتمدة:</b>',
    aboutDesc4: '<b>تحتاج مساعدة بشرية؟</b> اضغط على زر <b>💬 دردشة المكتبة</b> للتحدث مع أمين المكتبة.',
    aboutDisclaimer: 'تنبيه النسخة التجريبية: للمعلومات العاجلة أو المتعلقة بحسابك، يرجى التحقق من المصدر الرسمي المرفق أو التواصل مع أمين مكتبة.',
    aboutVersion: 'نسخة تجريبية · الإصدار 0.1',
    aboutDev: 'مكتبات جامعة الإمارات',
    aboutYear: '2026',
    aboutDevelopedLabel: 'تم التطوير بواسطة',
    aboutCopyright: 'حقوق النشر © 2026 أسدك حامد. جميع الحقوق محفوظة.',
    placeholder: 'ابحث عن مقالات، دوريات، كتب... (مثال: مقالات عن التشفير)',
    welcome: 'مرحباً! أنا مساعد مكتبات جامعة الإمارات الذكي (نسخة تجريبية).\n\nيمكنني مساعدتك في:\n• البحث عن الكتب والكتب الإلكترونية والمقالات والدوريات\n• اقتراح قواعد البيانات المناسبة لموضوعك\n• الإجابة عن أسئلة خدمات المكتبة مثل الإعارة، ساعات العمل، المواقع، والنماذج\n\nكيف يمكنني مساعدتك اليوم؟',
    libchatText: 'دردشة المكتبة',
    sendBtn: 'إرسال',
    messageInputLabel: 'اسأل مساعد مكتبات جامعة الإمارات الذكي'
  } : {
    appTitle: 'UAEU Libraries AI Assistant',
    betaBadge: 'Beta',
    sidebarTitle: 'Quick Access',
    newConversationText: 'New conversation',
    officialResourcesText: 'Official resources',
    linkLibText: 'Library Website',
    linkHoursText: 'Library Hours',
    linkFaqText: 'Frequently Asked Questions',
    linkDatabaseText: 'Databases',
    linkRequestText: 'Request a resource',
    linkAccountText: 'My Account',
    linkAboutText: 'About',
    aboutTitle: 'About the UAEU Libraries AI Assistant',
    aboutDesc1: 'The UAEU Libraries AI Assistant is the official beta demonstration of an interactive library guide for UAEU students, faculty, staff, researchers, and visitors.',
    aboutDesc2: '<b>Features:</b>',
    aboutDesc3: '<b>Approved sources:</b>',
    aboutDesc4: '<b>Need human help?</b> Click the <b>💬 LibChat</b> button to chat with a librarian.',
    aboutDisclaimer: 'Beta notice: For time-sensitive or account-specific matters, confirm the information through the linked official source or contact a librarian.',
    aboutVersion: 'Beta Demo · Version 0.1',
    aboutDev: 'UAEU Libraries',
    aboutYear: '2026',
    aboutDevelopedLabel: 'Developed by',
    aboutCopyright: 'Copyright © 2026 Asadik Hamed. All rights reserved.',
    placeholder: 'Search for articles, journals, books... (e.g., articles about cryptography)',
    welcome: "Hello! I'm the UAEU Libraries AI Assistant (Beta).\n\nI can help you:\n• Find books, eBooks, articles, and journals\n• Recommend UAEU databases for your topic\n• Answer library service questions such as borrowing, hours, locations, and forms\n\nHow can I help you today?",
    libchatText: 'LibChat',
    sendBtn: 'Send',
    messageInputLabel: 'Ask the UAEU Libraries AI Assistant'
  };

  // Update about features list
  const aboutFeatures = document.getElementById('aboutFeatures');
  if (aboutFeatures) {
    const featureTexts = isArabic
      ? [
        'الإجابة عن أسئلة خدمات مكتبات جامعة الإمارات بالاعتماد على المصادر الرسمية والمعتمدة',
        'البحث عن الكتب والكتب الإلكترونية في كتالوج مكتبات الجامعة',
        'استكشاف المقالات العلمية عبر خدمات الاكتشاف الأكاديمي المتاحة',
        'اقتراح قواعد بيانات جامعة الإمارات المناسبة لموضوع البحث',
        'توفير روابط الكتالوج ومعرّف DOI وقواعد البيانات والنماذج والسياسات عند توفرها',
        'دعم المحادثات باللغة العربية والإنجليزية'
      ]
      : [
        'Answer UAEU Libraries service questions using approved and official sources',
        'Search books and eBooks in the UAEU Libraries catalog',
        'Discover scholarly articles through available academic discovery services',
        'Recommend suitable UAEU databases for a research topic',
        'Provide catalog, DOI, database, form, and policy links when available',
        'Support conversations in English and Arabic'
      ];
    aboutFeatures.replaceChildren(...featureTexts.map(text => {
      const li = document.createElement('li');
      li.textContent = text;
      return li;
    }));
  }

  // Update UI text
  Object.keys(t).forEach(key => {
    const el = document.getElementById(key);
    if (el) {
      if (key.includes('Desc') || key === 'aboutDesc1' || key === 'aboutDesc2') {
        el.textContent = String(t[key]).replace(/<\/?b>/g, '');
      } else if (key === 'placeholder') {
        document.getElementById('msgInput').placeholder = t[key];
      } else {
        el.textContent = t[key];
      }
    }
  });

  document.body.classList.toggle('rtl', isArabic);
  document.documentElement.lang = isArabic ? 'ar' : 'en';
  document.documentElement.dir = isArabic ? 'rtl' : 'ltr';

  const libraryWebsiteLink = document.getElementById('libraryWebsiteLink');
  const libraryHoursLink = document.getElementById('libraryHoursLink');
  const libraryFaqLink = document.getElementById('libraryFaqLink');
  const resourceRequestLink = document.getElementById('resourceRequestLink');
  if (libraryWebsiteLink) {
    libraryWebsiteLink.href = isArabic
      ? 'https://www.uaeu.ac.ae/ar/library/'
      : 'https://www.uaeu.ac.ae/en/library/';
  }
  if (libraryHoursLink) {
    libraryHoursLink.href = isArabic
      ? 'https://www.uaeu.ac.ae/ar/library/libraryhours.shtml'
      : 'https://www.uaeu.ac.ae/en/library/libraryhours.shtml';
  }
  if (libraryFaqLink) {
    libraryFaqLink.href = isArabic
      ? 'https://www.uaeu.ac.ae/ar/library/faqs.shtml'
      : 'https://www.uaeu.ac.ae/en/library/faqs.shtml';
  }
  if (resourceRequestLink) {
    resourceRequestLink.href = isArabic
      ? 'https://www.uaeu.ac.ae/ar/library/forms/requesttitle.shtml'
      : 'https://www.uaeu.ac.ae/en/library/forms/requesttitle.shtml';
  }

  document.getElementById('menuBtn').setAttribute('aria-label', isArabic ? 'فتح قائمة الوصول السريع' : 'Open quick access menu');
  document.getElementById('closeSidebarBtn').setAttribute('aria-label', isArabic ? 'إغلاق قائمة الوصول السريع' : 'Close quick access menu');
  document.getElementById('libchatBtn').setAttribute('aria-label', isArabic ? 'التحدث مع أمين مكتبة' : 'Talk to a librarian');
  document.getElementById('headerLangBtn').setAttribute('aria-label', isArabic ? 'تبديل اللغة' : 'Switch language');
  document.getElementById('libchatCloseBtn').setAttribute('aria-label', isArabic ? 'إغلاق دردشة المكتبة' : 'Close librarian chat');
  document.getElementById('chat').setAttribute('aria-label', isArabic ? 'المحادثة' : 'Conversation');
  const isDark = document.body.classList.contains('dark');
  const themeButton = document.getElementById('headerThemeBtn');
  themeButton.querySelector('span').textContent = isDark ? '☀️' : '🌙';
  themeButton.setAttribute('aria-pressed', String(isDark));
  themeButton.setAttribute('aria-label', isArabic
    ? (isDark ? 'التبديل إلى الوضع الفاتح' : 'التبديل إلى الوضع الداكن')
    : (isDark ? 'Switch to light mode' : 'Switch to dark mode'));

  localStorage.setItem('language', isArabic ? 'ar' : 'en');
  updateSearchModeControls();

  // If the chat only contains the first welcome message, redraw it in the
  // selected language without clearing a real conversation.
  const chat = document.getElementById('chat');
  const hasOnlyWelcome = chat.children.length === 1
    && chat.children[0].classList.contains('welcome-message');
  if (chat.children.length === 0 || hasOnlyWelcome) {
    chat.innerHTML = '';
    showWelcome();
  }

  const aboutSources = document.getElementById('aboutSources');
  if (aboutSources) {
    const sourceTexts = isArabic
      ? [
        'الكتالوج: خدمة OCLC WorldCat Discovery لسجلات مكتبات جامعة الإمارات',
        'المقالات: Crossref وOpenAlex وPubMed وEurope PMC وDOAJ وCORE وSemantic Scholar وWorldCat عند توفر الخدمة',
        'معلومات المكتبة: صفحات مكتبات جامعة الإمارات الرسمية ووثائق السياسات المعتمدة'
      ]
      : [
        'Catalog: OCLC WorldCat Discovery for UAEU Libraries records',
        'Articles: Crossref, OpenAlex, PubMed, Europe PMC, DOAJ, CORE, Semantic Scholar, and WorldCat when available',
        'Library information: Official UAEU Libraries webpages and approved policy documents'
      ];
    aboutSources.replaceChildren(...sourceTexts.map(text => {
      const li = document.createElement('li');
      li.textContent = text;
      return li;
    }));
  }
}

// ============================
// MESSAGE HANDLING
// ============================
function appendMessage(text, sender, animate = false, suggestions = [], afterRender = null, extraClass = '') {
  const chat = document.getElementById('chat');
  const bubble = document.createElement('div');
  bubble.className = 'msg ' + sender;
  if (extraClass) {
    bubble.classList.add(...String(extraClass).split(/\s+/).filter(Boolean));
  }

  chat.appendChild(bubble);

  if (animate && sender === 'ai') {
    typeMessage(bubble, text, () => {
      if (sender === 'ai' || (suggestions && suggestions.length > 0)) {
        addSuggestions(bubble, suggestions || []);
      }
      if (afterRender) afterRender(bubble);
    });
  } else {
    bubble.textContent = '';
    appendFormattedText(bubble, text, { animateBlocks: sender === 'ai' });
    if (sender === 'ai' || (suggestions && suggestions.length > 0)) {
      addSuggestions(bubble, suggestions || []);
    }
    if (afterRender) afterRender(bubble);
  }

  scrollToBottom();
  return bubble;
}

function isHumanHelpSuggestion(value) {
  const normalized = String(value || '').trim().toLowerCase();
  return [
    'talk to a person',
    'talk to real person',
    'talk to a real person',
    'talk to human',
    'live chat',
    'libchat',
    'ask a librarian',
    'التحدث مع شخص',
    'تحدث مع شخص',
    'دردشة مباشرة',
    'دردشة المكتبة'
  ].includes(normalized);
}

function isRetrySuggestion(value) {
  return ['try again', 'retry', 'حاول مرة أخرى', 'إعادة المحاولة']
    .includes(String(value || '').trim().toLowerCase());
}

function addSuggestions(bubble, suggestions) {
  const container = document.createElement('div');
  container.className = 'suggestions-container';
  const chipValues = [...(suggestions || [])];
  const shouldOfferHumanHelp = !bubble.classList.contains('welcome-message');
  const humanHelpLabel = uiText('Talk to a person', 'التحدث مع شخص');

  if (shouldOfferHumanHelp && !chipValues.some(isHumanHelpSuggestion)) {
    chipValues.push(humanHelpLabel);
  }

  chipValues.forEach(suggestion => {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'suggestion-chip';
    chip.textContent = suggestion;

    if (isHumanHelpSuggestion(suggestion)) {
      chip.classList.add('human-help-chip');
      chip.addEventListener('click', openLibchat);
      container.appendChild(chip);
      return;
    }

    chip.addEventListener('click', () => {
      const input = document.getElementById('msgInput');
      const effectiveSuggestion = isRetrySuggestion(suggestion) && lastSearchText
        ? lastSearchText
        : suggestion;
      input.value = effectiveSuggestion;
      input.dataset.queryOverride = buildSuggestionQuery(effectiveSuggestion);
      sendMessage();
    });
    container.appendChild(chip);
  });

  bubble.appendChild(container);
}

function isGenericFollowUp(text) {
  return /^(more|give me more|show me more|more results|more books|more articles|more recent articles|more recent|recent articles|newer articles|latest articles)$/i
    .test(String(text || '').trim());
}

function buildSuggestionQuery(suggestion) {
  if (isGenericFollowUp(suggestion) && lastSearchText) {
    return `${suggestion} for ${lastSearchText}`;
  }
  return suggestion;
}


function makeExportItem(type, item) {
  if (!item || !item.title) return null;
  return {
    type,
    title: item.title || '',
    creators: type === 'article' ? (item.authors || '') : (item.author || ''),
    year: item.year || '',
    journal: item.journal || '',
    format: item.format || '',
    database: item.database || '',
    doi: item.doi || '',
    link: item.direct_link || item.link || '',
    why: item.why_recommended || ''
  };
}

function getResultTypeLabel(type) {
  return type === 'article' ? uiText('Article', 'مقال') : uiText('Book', 'كتاب');
}


function getPanelItems(panel, all = false) {
  const checkboxes = Array.from(panel.querySelectorAll('.export-result-checkbox'));
  return checkboxes
    .filter(checkbox => all || checkbox.checked)
    .map(checkbox => {
      try {
        return JSON.parse(decodeURIComponent(checkbox.dataset.item || ''));
      } catch {
        return null;
      }
    })
    .filter(Boolean);
}

function getPrintableUrl(value) {
  const trimmed = String(value || '').trim();
  if (!trimmed) return '';

  try {
    const parsed = new URL(trimmed);
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
      return parsed.href;
    }
  } catch {
    return '';
  }

  return '';
}

function getDoiUrl(doi) {
  const trimmed = String(doi || '').trim();
  if (!trimmed) return '';
  const existingUrl = getPrintableUrl(trimmed);
  if (existingUrl) return existingUrl;
  return `https://doi.org/${encodeURIComponent(trimmed).replace(/%2F/g, '/')}`;
}

function printLinkHtml(url, label = '') {
  const href = getPrintableUrl(url);
  const safeLabel = escapeHtml(label || href || url);
  if (!href) return safeLabel;
  return `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${safeLabel}</a>`;
}


function printResultItems(items) {
  if (!items.length) {
    window.alert(uiText('Select at least one result first.', 'اختر نتيجة واحدة على الأقل أولاً.'));
    return;
  }

  const direction = isArabic ? 'rtl' : 'ltr';
  const title = uiText('UAEU Library Search Results', 'نتائج البحث - مكتبة جامعة الإمارات');
  const itemsHtml = items.map((item, index) => {
    const doiUrl = getDoiUrl(item.doi);
    const itemLink = getPrintableUrl(item.link);

    return `<section class="item">
      <h2>${index + 1}. ${escapeHtml(item.title)}</h2>
      <p>${escapeHtml(getResultTypeLabel(item.type))}${item.year ? ' | ' + escapeHtml(item.year) : ''}</p>
      ${item.creators ? `<p>${escapeHtml(item.creators)}</p>` : ''}
      ${item.journal || item.format ? `<p>${escapeHtml(item.journal || item.format)}</p>` : ''}
      ${item.database ? `<p>${escapeHtml(uiText('Database', 'قاعدة البيانات'))}: ${escapeHtml(item.database)}</p>` : ''}
      ${item.doi ? `<p>DOI: ${printLinkHtml(doiUrl, item.doi)}</p>` : ''}
      ${itemLink ? `<p>${escapeHtml(uiText('Link', 'الرابط'))}: ${printLinkHtml(itemLink)}</p>` : ''}
      ${item.why ? `<p>${escapeHtml(item.why)}</p>` : ''}
    </section>`;
  }).join('');

  const printWindow = window.open('', '_blank');
  if (!printWindow) {
    window.print();
    return;
  }

  printWindow.document.open();
  printWindow.document.write(`<!doctype html><html dir="${direction}"><head><meta charset="utf-8"><title>${escapeHtml(title)}</title><style>
    body{font-family:Arial,sans-serif;line-height:1.5;margin:32px;color:#222}
    h1{color:#9d2235;border-bottom:2px solid #9d2235;padding-bottom:8px}
    .item{break-inside:avoid;border-bottom:1px solid #ddd;padding:12px 0}
    .item h2{font-size:18px;margin:0 0 6px;color:#222}
    .item p{margin:3px 0}
    a{color:#0645ad;text-decoration:underline;word-break:break-all}
  </style></head><body><h1>${escapeHtml(title)}</h1>${itemsHtml}</body></html>`);
  printWindow.document.close();
  printWindow.focus();
  printWindow.print();
}


function addSaveResultsPanel(bubble, data) {
  const articles = (data.articles || []).map(article => makeExportItem('article', article));
  const books = (data.books || []).map(book => makeExportItem('book', book));
  const results = [...articles, ...books].filter(Boolean);

  if (!bubble || results.length === 0) return;

  const panel = document.createElement('div');
  panel.className = 'result-export-panel';

  const title = document.createElement('div');
  title.className = 'result-export-title';
  title.textContent = uiText(
    'Print results. Print all results, or choose selected results:',
    'اطبع النتائج. اطبع كل النتائج أو اختر نتائج محددة:'
  );
  panel.appendChild(title);

  const primaryActions = document.createElement('div');
  primaryActions.className = 'result-export-actions';

  const printAll = document.createElement('button');
  printAll.type = 'button';
  printAll.className = 'small-action-btn';
  printAll.textContent = uiText('Print all', 'طباعة الكل');
  printAll.onclick = () => printResultItems(results);
  primaryActions.appendChild(printAll);

  const showSelection = document.createElement('button');
  showSelection.type = 'button';
  showSelection.className = 'small-action-btn';
  showSelection.textContent = uiText('Select results', 'اختيار النتائج');
  primaryActions.appendChild(showSelection);

  panel.appendChild(primaryActions);

  const selectionActions = document.createElement('div');
  selectionActions.className = 'result-export-actions result-selection-actions';
  selectionActions.hidden = true;

  const selectAll = document.createElement('button');
  selectAll.type = 'button';
  selectAll.className = 'small-action-btn';
  selectAll.textContent = uiText('Select all', 'تحديد الكل');
  selectAll.onclick = () => {
    panel.querySelectorAll('.export-result-checkbox').forEach(checkbox => {
      checkbox.checked = true;
    });
  };
  selectionActions.appendChild(selectAll);

  const clearSelection = document.createElement('button');
  clearSelection.type = 'button';
  clearSelection.className = 'small-action-btn';
  clearSelection.textContent = uiText('Clear selection', 'إلغاء التحديد');
  clearSelection.onclick = () => {
    panel.querySelectorAll('.export-result-checkbox').forEach(checkbox => {
      checkbox.checked = false;
    });
  };
  selectionActions.appendChild(clearSelection);

  const printSelected = document.createElement('button');
  printSelected.type = 'button';
  printSelected.className = 'small-action-btn';
  printSelected.textContent = uiText('Print selected', 'طباعة المحدد');
  printSelected.onclick = () => printResultItems(getPanelItems(panel));
  selectionActions.appendChild(printSelected);

  panel.appendChild(selectionActions);

  const selectionList = document.createElement('div');
  selectionList.className = 'result-selection-list';
  selectionList.hidden = true;

  results.forEach((item, index) => {
    const row = document.createElement('label');
    row.className = 'export-result-item';

    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'export-result-checkbox';
    checkbox.checked = true;
    checkbox.dataset.item = encodeURIComponent(JSON.stringify(item));

    const label = document.createElement('span');
    label.className = 'export-result-title';
    label.textContent = `${index + 1}. ${getResultTypeLabel(item.type)}: ${item.title}`;

    row.appendChild(checkbox);
    row.appendChild(label);
    selectionList.appendChild(row);
  });

  showSelection.onclick = () => {
    const shouldShow = selectionList.hidden;
    selectionList.hidden = !shouldShow;
    selectionActions.hidden = !shouldShow;
    showSelection.textContent = shouldShow
      ? uiText('Hide selection', 'إخفاء التحديد')
      : uiText('Select results', 'اختيار النتائج');
  };

  panel.appendChild(selectionList);
  bubble.appendChild(panel);
  scrollToBottom();
}


function showWelcome() {
  // Show welcome based on current search mode
  let welcome = '';
  let suggestions = [];

  if (searchMode === 'books') {
    if (isArabic) {
      welcome = 'مرحباً! أنا مساعد مكتبات جامعة الإمارات الذكي (نسخة تجريبية).\n\nيمكنني مساعدتك في:\n• البحث عن الكتب والكتب الإلكترونية والمقالات والدوريات\n• اقتراح قواعد البيانات المناسبة لموضوعك\n• الإجابة عن أسئلة خدمات المكتبة مثل الإعارة، ساعات العمل، المواقع، والنماذج\n\nكيف يمكنني مساعدتك اليوم؟';
      suggestions = ['ساعات عمل المكتبة', 'كم كتاب أقدر أستعير؟', 'كتب عن الذكاء الاصطناعي'];
    } else {
      welcome = 'Hello! I\'m the UAEU Libraries AI Assistant (Beta).\n\nI can help you:\n• Find books, eBooks, articles, and journals\n• Recommend UAEU databases for your topic\n• Answer library service questions such as borrowing, hours, locations, and forms\n\nHow can I help you today?';
      suggestions = ['Library hours', 'How many books can I borrow?', 'Books about AI'];
    }
  } else if (searchMode === 'research') {
    if (isArabic) {
      welcome = '📄 مرحباً! أنا هنا لمساعدتك في إيجاد المقالات والأبحاث العلمية.\n\nيمكنك أن تسألني:\n• "مقالات عن التشفير من 2020"\n• "أبحاث عن الطاقة المتجددة"\n• "دوريات علمية عن التعليم الإلكتروني"\n\nسأبحث في خدمات الاكتشاف الأكاديمي المتاحة وأقترح قواعد بيانات جامعة الإمارات المناسبة لمواصلة البحث.';
      suggestions = ['مقالات عن التشفير', 'أبحاث عن تعلم الآلة', 'دوريات الصحة العامة'];
    } else {
      welcome = '📄 Hello! I\'m here to help you find scholarly articles and research papers.\n\nYou can ask me:\n• "Articles about cryptography from 2020"\n• "Research on renewable energy"\n• "Journals about e-learning"\n\nI\'ll search available academic discovery services and recommend suitable UAEU databases where you can continue.';
      suggestions = ['Articles about cryptography', 'Machine learning research', 'Healthcare journals'];
    }
  } else {
    // Default "all" mode
    if (isArabic) {
      welcome = 'مرحباً! أنا مساعد مكتبات جامعة الإمارات الذكي (نسخة تجريبية).\n\nيمكنني مساعدتك في:\n• البحث عن الكتب والكتب الإلكترونية والمقالات والدوريات\n• اقتراح قواعد البيانات المناسبة لموضوعك\n• الإجابة عن أسئلة خدمات المكتبة مثل الإعارة، ساعات العمل، المواقع، والنماذج\n\nكيف يمكنني مساعدتك اليوم؟';
      suggestions = ['ساعات عمل المكتبة', 'مقالات عن الذكاء الاصطناعي', 'كتب عن تعلم الآلة'];
    } else {
      welcome = "Hello! I'm the UAEU Libraries AI Assistant (Beta).\n\nI can help you:\n• Find books, eBooks, articles, and journals\n• Recommend UAEU databases for your topic\n• Answer library service questions such as borrowing, hours, locations, and forms\n\nHow can I help you today?";
      suggestions = ['Library hours', 'Articles about cryptography', 'Books about artificial intelligence'];
    }
  }

  const bubble = appendMessage(welcome, 'ai', true, suggestions, null, 'welcome-message');
  bubble.dataset.language = isArabic ? 'ar' : 'en';
  bubble.dataset.searchMode = searchMode;
}

function getProgressSteps(mode) {
  if (mode === 'books') {
    return isArabic
      ? ['فهم طلبك', 'اختيار أفضل طريقة للبحث', 'فحص كتالوج المكتبة', 'صياغة الرد']
      : ['Understanding your request', 'Choosing the best search path', 'Checking the library catalog', 'Writing the answer'];
  }
  if (mode === 'similar') {
    return isArabic
      ? ['فهم الكتاب المختار', 'البحث عن موارد قريبة', 'ترتيب النتائج', 'صياغة الرد']
      : ['Understanding the selected book', 'Finding related resources', 'Ranking results', 'Writing the answer'];
  }
  return isArabic
    ? ['فهم طلبك', 'اختيار المصادر المناسبة', 'فحص المقالات والكتالوج', 'ترتيب النتائج', 'صياغة الرد']
    : ['Understanding your request', 'Choosing the right sources', 'Checking articles and catalog records', 'Ranking results', 'Writing the answer'];
}

function setProgressStep(index) {
  const indicator = document.getElementById('progressIndicator');
  if (!indicator) return;
  const steps = indicator.querySelectorAll('.progress-step');
  steps.forEach((step, stepIndex) => {
    step.classList.toggle('done', stepIndex < index);
    step.classList.toggle('active', stepIndex === index);
  });
}

function showProgressIndicator(mode = 'all') {
  removeProgressIndicator();

  const chat = document.getElementById('chat');
  const indicator = document.createElement('div');
  indicator.className = 'msg ai progress-card';
  indicator.id = 'progressIndicator';
  indicator.setAttribute('role', 'status');
  indicator.setAttribute('aria-live', 'polite');

  const title = document.createElement('div');
  title.className = 'progress-title';
  const titleText = document.createElement('span');
  titleText.textContent = uiText('UAEU Libraries AI Assistant is working', 'مساعد مكتبات جامعة الإمارات يعمل');
  const dots = document.createElement('span');
  dots.className = 'agent-typing-dots';
  dots.setAttribute('aria-hidden', 'true');
  dots.append(document.createElement('i'), document.createElement('i'), document.createElement('i'));
  title.append(titleText, dots);
  indicator.appendChild(title);

  const stepsContainer = document.createElement('div');
  stepsContainer.className = 'progress-steps';

  getProgressSteps(mode).forEach(stepText => {
    const step = document.createElement('div');
    step.className = 'progress-step';

    const dot = document.createElement('span');
    dot.className = 'progress-dot';

    const label = document.createElement('span');
    label.textContent = stepText;

    step.appendChild(dot);
    step.appendChild(label);
    stepsContainer.appendChild(step);
  });

  indicator.appendChild(stepsContainer);
  chat.appendChild(indicator);
  progressStartedAt = performance.now();
  progressStepIndex = 0;
  setProgressStep(progressStepIndex);

  progressTimer = window.setInterval(() => {
    const steps = indicator.querySelectorAll('.progress-step');
    if (progressStepIndex < steps.length - 1) {
      progressStepIndex += 1;
      setProgressStep(progressStepIndex);
    }
  }, 1300);

  scrollToBottom();
}

function removeProgressIndicator() {
  if (progressTimer) {
    window.clearInterval(progressTimer);
    progressTimer = null;
  }
  const indicator = document.getElementById('progressIndicator');
  if (indicator) indicator.remove();
}

function waitForAgentMoment() {
  const elapsed = performance.now() - progressStartedAt;
  const remaining = Math.max(0, CONFIG.MIN_PROGRESS_MS - elapsed);
  return new Promise(resolve => window.setTimeout(resolve, remaining));
}

// ============================
// API COMMUNICATION
// ============================
function validateInput(text) {
  // Check length
  if (text.length > CONFIG.MAX_QUERY_LENGTH) {
    return { valid: false, error: 'Query is too long. Please shorten your search.' };
  }
  // Check for potentially dangerous patterns
  const dangerousPatterns = /<script|javascript:|onerror=|onload=/i;
  if (dangerousPatterns.test(text)) {
    return { valid: false, error: 'Invalid characters in query.' };
  }
  return { valid: true };
}

async function fetchWithTimeout(url, options = {}, timeoutMs = CONFIG.REQUEST_TIMEOUT_MS) {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    window.clearTimeout(timeoutId);
  }
}

async function ensureSession() {
  const now = Math.floor(Date.now() / 1000);
  if (sessionId && csrfToken && sessionExpiresAt - 30 > now) {
    return;
  }

  const response = await fetchWithTimeout(CONFIG.SESSION_URL, {
    method: 'POST',
    headers: secureHeaders(false)
  });

  if (!response.ok) {
    throw new Error(await getApiErrorMessage(response));
  }

  const data = await response.json();
  sessionId = data.session_id;
  csrfToken = data.csrf_token;
  sessionExpiresAt = data.expires_at || 0;
}

function secureHeaders(includeCsrf = true) {
  const headers = { 'Content-Type': 'application/json' };
  if (includeCsrf) {
    headers['X-CSRF-Token'] = csrfToken;
  }
  return headers;
}

async function getApiErrorMessage(response) {
  let message = `HTTP ${response.status}`;
  try {
    const body = await response.json();
    if (typeof body.detail === 'string') {
      message = body.detail;
    } else if (Array.isArray(body.detail)) {
      message = body.detail
        .map(item => item?.msg || item?.message || String(item))
        .join('; ');
    }
  } catch {
    // Keep the HTTP status fallback.
  }
  return message;
}

function resetSession() {
  sessionId = '';
  csrfToken = '';
  sessionExpiresAt = 0;
}

async function requestSearch(payload, allowSessionRetry = true) {
  const response = await fetchWithTimeout(CONFIG.API_URL, {
    method: 'POST',
    headers: secureHeaders(),
    body: JSON.stringify(payload)
  });

  if ((response.status === 401 || response.status === 403) && allowSessionRetry) {
    resetSession();
    await ensureSession();
    payload.session_id = sessionId;
    return requestSearch(payload, false);
  }

  if (!response.ok) {
    const requestError = new Error(await getApiErrorMessage(response));
    requestError.status = response.status;
    throw requestError;
  }
  return response.json();
}

function getFriendlyRequestError(error) {
  if (error?.name === 'AbortError') {
    return isArabic
      ? 'استغرق الطلب وقتاً أطول من المتوقع. حاول مرة أخرى.'
      : 'The request took longer than expected. Please try again.';
  }
  if (error instanceof TypeError) {
    return isArabic
      ? 'تعذر الاتصال بخدمة المكتبة. تحقق من الاتصال ثم حاول مرة أخرى.'
      : 'The library service could not be reached. Check your connection and try again.';
  }
  if (error?.status === 429) {
    return isArabic
      ? 'تم إرسال عدد كبير من الطلبات. انتظر قليلاً ثم حاول مرة أخرى.'
      : 'Too many requests were sent. Wait a moment and try again.';
  }
  if (error?.status >= 500) {
    return isArabic
      ? 'خدمة المكتبة غير متاحة مؤقتاً. حاول مرة أخرى بعد قليل.'
      : 'The library service is temporarily unavailable. Please try again shortly.';
  }
  return error?.message || (isArabic ? 'حدث خطأ غير متوقع.' : 'An unexpected error occurred.');
}

async function sendMessage() {
  if (isProcessing) return;

  const input = document.getElementById('msgInput');
  const sendBtn = document.getElementById('sendBtn');
  const text = input.value.trim();
  const queryText = input.dataset.queryOverride || text;
  delete input.dataset.queryOverride;

  if (!text) return;

  // Validate input
  const validation = validateInput(text);
  if (!validation.valid) {
    appendMessage(validation.error, 'ai');
    return;
  }

  isProcessing = true;
  sendBtn.disabled = true;
  appendMessage(text, 'user');
  if (!isGenericFollowUp(text)) {
    lastSearchText = text;
  }
  input.value = '';
  input.rows = 1;
  closeFilterPanel();

  showProgressIndicator(searchMode);

  try {
    await ensureSession();
    const filters = getFiltersForPayload();

    const payload = {
      query: queryText,
      limit: 6,
      session_id: sessionId,
      search_mode: searchMode
    };
    if (filters) {
      payload.filters = filters;
    }

    const data = await requestSearch(payload);

    await waitForAgentMoment();
    removeProgressIndicator();

    // Get suggestions from response
    const suggestions = data.suggestions || [];

    appendMessage(data.ai_response, 'ai', false, suggestions, bubble => addSaveResultsPanel(bubble, data));

  } catch (error) {
    console.error('Error:', error);
    removeProgressIndicator();

    const detail = `\n\n${getFriendlyRequestError(error)}`;
    const errorMsg = isArabic
      ? `❌ تعذر إكمال الطلب. يرجى المحاولة مرة أخرى.${detail}\n\nاقتراحات:\n- تحقق من اتصالك بالإنترنت\n- جرب بحثاً أبسط`
      : `❌ Could not complete the request. Please try again.${detail}\n\nSuggestions:\n- Check your internet connection\n- Try a simpler search`;

    appendMessage(errorMsg, 'ai', false, [isArabic ? 'إعادة المحاولة' : 'Try again']);
  } finally {
    isProcessing = false;
    sendBtn.disabled = false;
    input.focus();
  }
}

function handleKeyPress(event) {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    sendMessage();
  }
}

function bindStaticEvents() {
  const bind = (id, eventName, handler) => {
    const element = document.getElementById(id);
    if (!element || element.dataset.bound === 'true') return;
    element.addEventListener(eventName, handler);
    element.dataset.bound = 'true';
  };

  bind('menuBtn', 'click', toggleMenu);
  bind('newConversationBtn', 'click', startNewConversation);
  bind('libchatBtn', 'click', openLibchat);
  bind('headerLangBtn', 'click', toggleLang);
  bind('headerThemeBtn', 'click', toggleDark);
  bind('sidebarOverlay', 'click', toggleMenu);
  bind('closeSidebarBtn', 'click', toggleMenu);
  bind('aboutCloseBtn', 'click', toggleAbout);
  bind('libchatCloseBtn', 'click', closeLibchat);
  bind('filterClearBtn', 'click', clearFilters);
  bind('filterApplyBtn', 'click', applyFilters);
  bind('filterBtn', 'click', toggleFilterPanel);
  bind('sendBtn', 'click', sendMessage);

  const databaseLink = document.getElementById('databaseLink');
  if (databaseLink && databaseLink.dataset.bound !== 'true') {
    databaseLink.addEventListener('click', event => {
      event.preventDefault();
      openDatabaseLink();
    });
    databaseLink.dataset.bound = 'true';
  }

  const aboutLink = document.getElementById('aboutLink');
  if (aboutLink && aboutLink.dataset.bound !== 'true') {
    aboutLink.addEventListener('click', event => {
      event.preventDefault();
      toggleAbout(event);
    });
    aboutLink.dataset.bound = 'true';
  }

  const aboutModal = document.getElementById('aboutModal');
  if (aboutModal && aboutModal.dataset.bound !== 'true') {
    aboutModal.addEventListener('click', event => {
      if (event.target === aboutModal) setAboutOpen(false);
    });
    aboutModal.dataset.bound = 'true';
  }

  const input = document.getElementById('msgInput');
  if (input && input.dataset.bound !== 'true') {
    input.addEventListener('keydown', handleKeyPress);
    input.addEventListener('input', () => autoResize(input));
    input.dataset.bound = 'true';
  }
}

// ============================
// INITIALIZATION
// ============================
window.onload = function() {
  // Restore preferences
  if (localStorage.getItem('darkMode') === 'true') {
    document.body.classList.add('dark');
  }

  if (localStorage.getItem('language') === 'ar') {
    isArabic = true;
    document.body.classList.add('rtl');
  }

  loadSavedFilters();

  // The header mode switch was removed; each message is routed automatically.
  searchMode = 'all';
  localStorage.setItem('searchMode', searchMode);

  updateLanguage();

  document.getElementById('msgInput').focus();
};

// Keyboard shortcuts
document.addEventListener('keydown', function(event) {
  const modal = document.getElementById('aboutModal');
  if (event.key === 'Tab' && modal.classList.contains('open')) {
    const focusable = getFocusableElements(modal);
    if (focusable.length === 0) {
      event.preventDefault();
      modal.querySelector('.modal-box').focus();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  if (event.key === 'Escape') {
    const filterPanel = document.getElementById('filterPanel');
    if (filterPanel && filterPanel.classList.contains('open')) {
      closeFilterPanel();
      return;
    }

    // Close LibChat first if open
    const libchatWidget = document.getElementById('libchat_1989346ba8efc72bf598fe6803917b73');
    if (libchatWidget && libchatWidget.classList.contains('active')) {
      closeLibchat();
      return;
    }

    if (modal.classList.contains('open')) {
      setAboutOpen(false);
      return;
    }
    if (document.getElementById('sidebar').classList.contains('open')) {
      toggleMenu();
    }
  }
});

document.addEventListener('click', function(event) {
  const panel = document.getElementById('filterPanel');
  const button = document.getElementById('filterBtn');
  if (!panel || !button || !panel.classList.contains('open')) return;
  if (panel.contains(event.target) || button.contains(event.target)) return;
  closeFilterPanel();
});

document.addEventListener('DOMContentLoaded', function() {
  bindStaticEvents();
});




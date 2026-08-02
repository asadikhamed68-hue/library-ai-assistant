(() => {
// Copyright (c) 2026 Asadik Hamed. All rights reserved. See LICENSE.

  const configuredApiUrl = typeof window.APP_CONFIG?.API_BASE_URL === 'string'
    ? window.APP_CONFIG.API_BASE_URL.trim()
    : '';

  window.APP_CONFIG = Object.freeze({
    API_BASE_URL: configuredApiUrl || 'http://127.0.0.1:8000',
  });
})();

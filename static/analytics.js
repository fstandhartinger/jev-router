'use strict';

(() => {
  const privacySignal = navigator.doNotTrack || navigator.msDoNotTrack || window.doNotTrack;
  const doNotTrack = privacySignal === true || /^(1|yes)$/i.test(String(privacySignal || ''));
  const trackingAllowed = !doNotTrack && navigator.globalPrivacyControl !== true;

  const websiteId = 'c1dafe3d-a89f-4b1e-9ee7-5543589b6d60';
  const pageTitles = Object.freeze({
    '/': 'Jev Router', '/models-page': 'Models · Jev Router', '/pricing': 'Pricing · Jev Router',
    '/docs': 'API docs · Jev Router', '/status': 'Status · Jev Router', '/terms': 'Terms of service · Jev Router',
    '/privacy': 'Privacy policy · Jev Router', '/refunds': 'Refund policy · Jev Router',
    '/impressum': 'Impressum · Jev Router',
  });
  const allowedSources = new Set([
    'decision-models.com', 'decisionmodels.io', 'decisionmodels.cloud', 'decisionmodels.online',
    'system-one.io', 'system-one.cloud', 'system-one.online',
  ]);

  // Only fixed public pageviews reach Umami. Referrers, fragments, arbitrary
  // queries, events, and any future non-marketing routes are discarded.
  if (trackingAllowed) {
    window.jevRouterUmamiBeforeSend = (type, payload) => {
      if (type !== 'event' || !payload || typeof payload !== 'object') return false;
      if (Object.prototype.hasOwnProperty.call(payload, 'name') ||
          Object.prototype.hasOwnProperty.call(payload, 'data')) return false;

      let page;
      try { page = new URL(payload.url, window.location.origin); }
      catch { return false; }
      const title = pageTitles[page.pathname];
      if (page.origin !== window.location.origin || !title) return false;

      const sources = page.searchParams.getAll('utm_source');
      const source = sources.length === 1 && allowedSources.has(sources[0]) ? sources[0] : '';
      return {
        website: websiteId,
        hostname: 'jev-router.com',
        url: page.pathname + (source ? `?utm_source=${encodeURIComponent(source)}` : ''),
        title,
        referrer: '',
      };
    };

    const script = document.createElement('script');
    script.defer = true;
    script.src = 'https://bh-analytics.app.mintapis.com/script.js';
    script.integrity = 'sha384-ZMxgpYfO/phGz4GiYTIZhcauuGKTb2onmOB5gsiigjmBR38DGAmIna5J1Y/dM/13';
    script.crossOrigin = 'anonymous';
    script.referrerPolicy = 'no-referrer';
    script.dataset.websiteId = websiteId;
    script.dataset.domains = 'jev-router.com';
    script.dataset.doNotTrack = 'true';
    script.dataset.excludeHash = 'true';
    script.dataset.beforeSend = 'jevRouterUmamiBeforeSend';
    document.head.append(script);
  }

  const counter = document.querySelector('#visit-counter');
  if (counter) {
    fetch('/api/analytics/visits', {
      credentials: 'omit', cache: 'no-store', referrerPolicy: 'no-referrer',
      headers: { Accept: 'application/json' },
    }).then(response => response.ok ? response.json() : null)
      .then(data => {
        if (!data || !Number.isSafeInteger(data.visits) || data.visits < 0) return;
        counter.textContent = `${data.visits} ${data.visits === 1 ? 'visit' : 'visits'}`;
        counter.hidden = false;
      }).catch(() => {});
  }
})();

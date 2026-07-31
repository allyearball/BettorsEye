// Service worker for the WNBA Box Score Explorer PWA.
// Handles: incoming push notifications, and tapping a notification to open the app.
// This file does NOT cache the app itself (the app already embeds all its data and
// is small enough to just reload normally) — its only job is push notifications.

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('push', (event) => {
  let payload = { title: 'WNBA Explorer', body: 'A new gold-tier bet just appeared.' };
  try {
    if (event.data) {
      payload = event.data.json();
    }
  } catch (e) {
    // if the payload isn't JSON for some reason, fall back to the default text above
  }

  const title = payload.title || 'WNBA Explorer';
  const options = {
    body: payload.body || '',
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    data: { url: payload.url || '/' },
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if ('focus' in client) return client.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow(url);
    })
  );
});

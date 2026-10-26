self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('push', (event) => {
  let message = {};
  try {
    message = event.data ? event.data.json() : {};
  } catch (_) {
    message = {body: event.data ? event.data.text() : ''};
  }
  event.waitUntil(self.registration.showNotification(message.title || 'ถึงคิวของคุณแล้ว', {
    body: message.body || 'กรุณาเปิด Smartcare Link เพื่อดูรายละเอียดคิว',
    icon: '/app-icon-192.png',
    badge: '/app-icon-192.png',
    data: {url: message.url || '/screening'}
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || '/screening', self.location.origin);
  if (target.origin !== self.location.origin) return;
  event.waitUntil((async () => {
    const clients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const client of clients) {
      if (new URL(client.url).origin === self.location.origin) {
        await client.navigate(target.href);
        return client.focus();
      }
    }
    return self.clients.openWindow(target.href);
  })());
});

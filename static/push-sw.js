// Service worker for phone push alerts (flying session time warnings,
// admin test alerts). Served from the site root as /push-sw.js (see
// push_service_worker in app.py) so it covers the whole app.
//
// Each push normally carries its alert as an encrypted payload (push.py
// encrypt_payload) - title, body, tag, url - so it can be shown straight
// away without needing the login cookie (which an iPhone home-screen app
// often doesn't have in the background). After showing it we also check
// /flight/push/pending for anything else queued (and to clear the queue);
// that part quietly does nothing if the phone isn't logged in.

var ICON = '/static/icons/flywithkate/icon-192.png';

self.addEventListener('install', function (event) {
  self.skipWaiting();
});

self.addEventListener('activate', function (event) {
  event.waitUntil(self.clients.claim());
});

function show(a) {
  return self.registration.showNotification(a.title || 'Fly with Kate!', {
    body: a.body || '',
    icon: ICON,
    badge: ICON,
    tag: a.tag || undefined,
    data: { url: a.url || '/flight/log/active' }
  });
}

function fetchPending() {
  return fetch('/flight/push/pending', { credentials: 'include', redirect: 'manual' })
    .then(function (r) {
      if (!r.ok) return [];
      return r.json().then(function (d) { return d.alerts || []; }, function () { return []; });
    })
    .catch(function () { return []; });
}

self.addEventListener('push', function (event) {
  var payload = null;
  try { payload = event.data ? event.data.json() : null; } catch (e) { payload = null; }
  event.waitUntil(
    (payload ? show(payload) : Promise.resolve()).then(function () {
      return fetchPending();
    }).then(function (alerts) {
      // Same tag replaces the notification already shown, so the copy of
      // the payload alert that's also in the queue doesn't double up.
      if (payload) {
        alerts = alerts.filter(function (a) {
          return !(a.title === payload.title && (a.body || '') === (payload.body || ''));
        });
      }
      if (alerts.length) return Promise.all(alerts.map(show));
      if (!payload) {
        // A push must always show something (iPhone turns alerts off for
        // sites whose pushes show nothing) - point them at the app.
        return show({ title: 'Fly with Kate!', body: 'You have a new alert - tap to open.', tag: 'opshub-alert' });
      }
    })
  );
});

self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || '/flight/log/active';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (clientList) {
      for (var i = 0; i < clientList.length; i++) {
        var client = clientList[i];
        if ('focus' in client) {
          client.navigate(url);
          return client.focus();
        }
      }
      if (self.clients.openWindow) return self.clients.openWindow(url);
    })
  );
});

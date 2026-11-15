"""Who is actually looking at the Navigator right now.

The Navigator page polls the server every 10 s (static/index.html), so a browser
tab - or the phone through the Cloudflare tunnel - shows up as a request with a
browser User-Agent. Scripts (nav.py, the titler, the supervisor cycle) do not:
they send Python-urllib or requests, so they never count as a view.

The Mac alarm (bin/alert-alarm.py) uses this: an alert nobody can see is not
worth speaking out loud. The rule Uri asked for is simple - the Navigator does
not keep going (voice, banner, alarm) without a real Navigator open somewhere.
"""


class Presence:
    """A rolling window of the clients that contacted the server recently.

    One entry per (ip, user-agent). An entry is live while it was seen less
    than `window` seconds ago; the page polls every 10 s, so a tab that stays
    open never falls out of the window, and a closed tab falls out after one.
    """

    def __init__(self, window=45.0):
        self.window = float(window)
        self.clients = {}          # key -> last seen (epoch)

    @staticmethod
    def is_view(headers):
        """True when the request came from a browser or the phone app.

        Every browser sends "Mozilla/"; no script does. Bots and headless
        clients are excluded: they are not a person looking at the screen.
        """
        ua = (headers.get("User-Agent") or "").lower()
        if not ua or "mozilla/" not in ua:
            return False
        for bad in ("bot", "headless", "curl", "wget", "python", "httpie", "postman", "electron"):
            if bad in ua:
                return False
        return True

    def note(self, headers, now=None):
        """Record a view. Returns True when this request was one."""
        if not self.is_view(headers):
            return False
        ip = (headers.get("Cf-Connecting-Ip") or headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        if not ip:
            ip = headers.get("Host") or "local"      # a browser on this Mac: no proxy header
        ua = (headers.get("User-Agent") or "")[:120]
        self.clients["%s | %s" % (ip, ua)] = time_now() if now is None else now
        return True

    def views(self, now=None):
        """The clients seen inside the window."""
        now = time_now() if now is None else now
        return sorted(k for k, t in self.clients.items() if now - t <= self.window)

    def count(self, now=None):
        """How many live views there are right now."""
        return len(self.views(now))

    def last_seen(self):
        """When a view was last seen (epoch, 0 when never)."""
        return max(self.clients.values()) if self.clients else 0.0

    def prune(self, now=None):
        """Drop entries that fell out of the window."""
        now = time_now() if now is None else now
        for k in [k for k, t in self.clients.items() if now - t > self.window]:
            self.clients.pop(k, None)


def time_now():
    import time
    return time.time()

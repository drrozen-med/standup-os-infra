"""Static checks of the loud alerts in static/index.html (docs/supervisor.md, "Loud alerts")."""
import re
import unittest
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text()


def fn(name):
    return re.search(r"(?:async )?function %s\([^)]*\) \{(.*?)\n  \}" % name, HTML, re.S).group(1)


class AlertUiTests(unittest.TestCase):
    def test_banner_is_first_in_the_page_and_fixed_on_top(self):
        body = HTML.index("<body>")
        self.assertLess(body, HTML.index('<div id="alertBar"'))
        self.assertLess(HTML.index('<div id="alertBar"'), HTML.index('<nav id="side"'))
        self.assertIn('role="alert"', HTML[HTML.index('<div id="alertBar"'):HTML.index('<nav id="side"')])
        css = re.search(r"#alertBar\{([^}]*)\}", HTML).group(1)
        for frag in ("position:fixed", "top:0", "z-index:150", "height:var(--alert-h)"):
            self.assertIn(frag, css)
        # above the drawer (91), menus and toast (99); dialogs (200) stay usable on top of it
        self.assertIn("body.has-alert{--alert-h:46px;padding-top:var(--alert-h)}", HTML)
        # phones: fixed two-row bar, the panes shrink by exactly its height (no jump, no overlap)
        self.assertIn("body.has-alert{--alert-h:calc(78px + env(safe-area-inset-top))}", HTML)
        self.assertIn("body.has-alert #side,body.has-alert #main{height:calc(100dvh - var(--alert-h));padding-top:0}", HTML)

    def test_banner_shows_newest_title_count_and_buttons(self):
        for b in ('id="abOpen"', 'id="abAck"', ">I'm on it<", 'id="abSound"', ">Turn on alert sound<", 'id="abMute"'):
            self.assertIn(b, HTML)
        r = fn("renderAlerts")
        self.assertIn("const a = open[0];", r)
        self.assertIn("$('#abTitle').textContent = a.title;", r)
        self.assertIn("'+' + (open.length - 1) + ' more'", r)
        self.assertIn("document.body.classList.toggle('has-alert', on);", r)

    def test_ack_call_is_local_first(self):
        ack = fn("ackAlert")
        self.assertLess(ack.index("dropAlerts([id]);"), ack.index("fetch('/api/alerts/ack'"))
        self.assertIn("JSON.stringify({ id })", ack)
        self.assertIn("if (e.target.closest('#abAck')) return ackAlert(a.id);", HTML)
        # opening the linked decision acks it and shows it in the Decisions screen
        op = fn("openAlert")
        self.assertLess(op.index("ackAlert(a.id);"), op.index("openInbox();"))
        self.assertIn(".dcard[data-dec=", op)
        self.assertIn("openChatSession(l.ref", op)
        self.assertIn("window.open(l.ref, '_blank', 'noopener')", op)
        # answering a decision drops the alerts the server acked with it
        self.assertIn("if (r.alerts_acked && r.alerts_acked.length) dropAlerts(r.alerts_acked);", fn("answerDecision"))

    def test_alerts_ride_the_existing_refresh(self):
        refresh = fn("refresh")
        self.assertIn("await loadAlerts();", refresh)
        self.assertIn("renderAlerts();", refresh)
        self.assertIn("fetch('/api/alerts'", fn("loadAlerts"))
        self.assertNotIn("setInterval", fn("loadAlerts"))
        self.assertEqual(HTML.count("setInterval("), 2)    # no new poller: the 10 s cycle also reads alerts when hidden
        self.assertIn("else loadAlerts().then(renderAlerts); }, 10000);", HTML)

    def test_audio_chime_and_unlock(self):
        self.assertIn("window.AudioContext || window.webkitAudioContext", HTML)
        ch = fn("chime")
        self.assertEqual(ch.count("createOscillator"), 1)
        self.assertRegex(ch, r"\[\[880, 0\], \[1318\.5, 0\.24\]\]")      # two short tones
        self.assertIn("o.start(", ch)
        self.assertIn("CHIME_MS = 20000", HTML)
        self.assertIn("Date.now() - alarm.lastChime >= CHIME_MS", fn("alarmTick"))
        un = fn("unlockAudio")
        self.assertIn("c.resume()", un)
        # any click or tap unlocks; the button too
        self.assertIn("for (const ev of ['click', 'touchend', 'keydown']) document.addEventListener(ev, () => { unlockAudio(); askNotify(); }, true);", HTML)
        self.assertIn("$('#abSound').hidden = soundReady() ||", fn("renderAlerts"))
        self.assertNotRegex(HTML, r"\.(mp3|wav|ogg)['\"].*chime")

    def test_mute_one_hour_in_localstorage_only(self):
        self.assertIn("store.set('alertMuteUntil', muted() ? 0 : Date.now() + 3600 * 1000)", HTML)
        self.assertIn("store.get('alertMuteUntil', 0)", HTML)
        # store wraps localStorage in try/catch
        self.assertRegex(HTML, r"get\(k, d\) \{ try \{ const v = localStorage\.getItem")
        self.assertIn("if (!muted() && ", fn("alarmTick"))

    def test_title_flash_every_second_and_restored(self):
        t = fn("alarmTick")
        self.assertIn("document.title = alarm.flash ? '(!) ' + open[0].title : BASE_TITLE;", t)
        self.assertIn("setTimeout(alarmTick, 1000)", t)
        self.assertIn("document.title = BASE_TITLE;", fn("stopAlarm"))
        self.assertIn("if (!on) return stopAlarm();", fn("renderAlerts"))

    def test_notification_once_per_alert_and_click_opens(self):
        n = fn("notifyAlerts")
        self.assertIn("Notification.permission !== 'granted'", n)
        self.assertIn("if (done.includes(a.id)) continue;", n)
        self.assertIn("tag: 'nav-alert-' + a.id", n)
        self.assertIn("window.focus()", n)
        self.assertIn("openAlert(a)", n)
        ask = fn("askNotify")
        self.assertIn("Notification.requestPermission()", ask)
        self.assertIn("!openAlerts().length", ask)       # only once an alert exists
        self.assertIn("alarm.asked = true;", ask)


if __name__ == "__main__":
    unittest.main()

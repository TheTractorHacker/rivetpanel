"""'Tap, don't type' ticket window: helpers and a full headless user journey.
Run: xvfb-run -a python3 tests/test_ticket_window.py"""
import os, sys, tempfile, time
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))
import tkinter as tk
import requests
import core
from psa.base import Category, Ticket

CATS = [Category("1", "Hardware"), Category("2", "Printing"), Category("3", "Email"), Category("4", "Network / Wi-Fi"), Category("5", "Accounts & Passwords")]
# --- pure helpers ---
sc = core.suggest_category
assert sc("my printer is jammed", CATS).name == "Printing"
assert sc("Outlook won't open", CATS).name == "Email"
assert sc("no wifi in the office", CATS).name == "Network / Wi-Fi"
assert sc("I forgot my password", CATS).name == "Accounts & Passwords"
assert sc("the screen is flickering", CATS).name == "Hardware"
assert sc("something odd", CATS) is None and sc("printer", []) is None
assert sc("anything", CATS, "print").name == "Printing"                       # a tapped chip wins over the text
assert sc("printer", [Category("9", "Billing")]) is None                      # no matching server category: never invent one
ds = core.derive_subject
assert ds("Printer is offline. It started today.") == "Printer is offline."
assert ds("line one\nline two") == "line one"
assert ds("", "Printer problem") == "Printer problem"
assert ds("It stopped", "Printer problem") == "Printer problem: It stopped"
assert ds("It stopped", "Something else") == "It stopped" and ds("", "Something else") == ""
assert len(ds("word " * 60)) <= 81 and ds("word " * 60).endswith("…")
assert ds("   ") == "" and ds(None) == ""

# --- window journey ---
class P:
    supports_chat = True; supports_stream_chat = False
    def __init__(self): self.calls = []; self.fail = None
    def create_ticket(self, subject, details, *, category_id=None, files=None, request_id=None):
        if self.fail: raise self.fail
        self.calls.append(dict(subject=subject, details=details, category_id=category_id, files=files, request_id=request_id))
        return Ticket(41, "41", subject, "New")

root = tk.Tk(); root.withdraw(); core.setup_style(root)
result = {}
def main():
    def pump(sec=0.4):
        end = time.time() + sec
        while time.time() < end: root.update(); time.sleep(0.01)
    def wait_for(cond, sec=6):
        end = time.time() + sec
        while time.time() < end:
            root.update()
            if cond(): return True
            time.sleep(0.02)
        return False
    def all_text(widget):
        out = []
        for ch in [widget] + list(widget.winfo_children()):
            try: out.append(str(ch.cget("text")))
            except tk.TclError: pass
            if ch is not widget: out.append(all_text(ch))
        return " ".join(out)
    def make(cfg=None):
        p = P(); w = core.TicketWindow(root, {"include_system_info": False, **(cfg or {})}, p); w.categories = CATS; return p, w

    # 1. one tap, zero typing: chip only is a valid ticket; screenshot auto-attached; category from the chip
    p, w = make(); w.show(); pump()
    assert w.screenshot_bytes and w.window.winfo_exists()
    w._pick_chip("Printer problem", "print"); w.submit()
    assert wait_for(lambda: p.calls), "ticket not sent"
    c = p.calls[0]
    assert c["subject"] == "Printer problem" and c["category_id"] == "2" and c["request_id"]
    assert c["files"] and c["files"][0][1] == "screenshot.png"
    assert "Quick report" in c["details"]
    pump(); assert "Request #41 sent" in all_text(w.window), all_text(w.window)
    w._close(); assert w.window is None and w.screenshot_bytes is None

    # 2. typed sentence: subject derived, category suggested live, chat button wired
    p, w = make(); opened = []; w.open_chat = lambda tid, n, s: opened.append((tid, n, s)); w.show(); pump()
    w.details_text.insert("1.0", "Outlook keeps crashing when I open attachments. Started Monday."); w._on_text()
    assert "Email" in w.filed_label.cget("text")
    w.submit(); assert wait_for(lambda: p.calls)
    assert p.calls[0]["subject"] == "Outlook keeps crashing when I open attachments." and p.calls[0]["category_id"] == "3"
    pump()
    def buttons(widget):
        for ch in widget.winfo_children():
            if ch.winfo_class() == "TButton": yield ch
            yield from buttons(ch)
    chat = [b for b in buttons(w.window) if "Chat" in str(b.cget("text"))]
    assert chat, "chat button missing"; chat[0].invoke(); assert opened and opened[0][0] == 41

    # 3. nothing tapped or typed: refused with guidance, nothing sent
    p, w = make(); w.show(); pump(); w.submit(); pump(0.3)
    assert not p.calls and "Tap what's wrong" in w.status_label.cget("text"); w._close()

    # 4. privacy: auto capture can be switched off, and the user can remove it
    p, w = make({"auto_screenshot": False}); w.show(); pump(); assert w.screenshot_bytes is None; w._close()
    p, w = make(); w.show(); pump(); assert w.screenshot_bytes; w.remove_screenshot(); assert w.screenshot_bytes is None
    w._pick_chip("Something else", None); w.details_text.insert("1.0", "weird noise"); w.submit(); assert wait_for(lambda: p.calls)
    assert p.calls[0]["files"] is None and p.calls[0]["subject"] == "weird noise" and p.calls[0]["category_id"] is None

    # 5. manual category choice sticks and is not overwritten by typing
    p, w = make(); w.show(); pump(); w._toggle_more(); w.category_var.set("Hardware"); w._on_category_selected()
    w.details_text.insert("1.0", "printer on fire"); w._on_text(); w.submit(); assert wait_for(lambda: p.calls)
    assert p.calls[0]["category_id"] == "1"

    # 6. offline: the request is queued with its request id; the user is told it will be sent
    p, w = make(); p.fail = requests.exceptions.ConnectionError("down"); w.show(); pump()
    w.details_text.insert("1.0", "no internet at all"); w.submit()
    assert wait_for(lambda: "Saved" in all_text(w.window)), all_text(w.window)
    q = core.queue_dir(); items = os.listdir(q); assert len(items) == 1
    import json; assert json.load(open(os.path.join(q, items[0], "payload.json")))["request_id"]

    # 7. a server error leaves the form usable and shows a redacted message
    p, w = make(); p.fail = RuntimeError("boom rvpd_" + "a" * 64); w.show(); pump()
    w.details_text.insert("1.0", "x"); w.submit(); assert wait_for(lambda: "Couldn't send" in w.status_label.cget("text"))
    assert "rvpd_" not in w.status_label.cget("text") and str(w.submit_btn.cget("state")) == "normal"
    print("ticket window journey ok")

def run():
    try:
        main(); result["ok"] = True
    except BaseException as exc:
        import traceback; traceback.print_exc(); result["ok"] = False
    root.quit()
root.after(0, run); root.mainloop()
sys.exit(0 if result.get("ok") else 1)

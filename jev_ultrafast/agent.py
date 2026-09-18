"""The complete agent loop. Typed choices, observable state, bounded execution."""

import base64
import time
from pathlib import Path

from . import planner
from .browser import Browser, InvalidValue, StalePage
from .model import action_space, choose, field_context, field_text
from .questions import MAX_STEPS


class Agent:
    def __init__(self, url, goals, *, record_dir=None, screenshots=False, max_steps=MAX_STEPS, planner_model=None):
        task = goals.strip() if isinstance(goals, str) else "\n".join(goals).strip()
        if not task:
            raise ValueError("Supply a task")
        if type(max_steps) is not int or max_steps < 1:
            raise ValueError("max_steps must be a positive integer")
        if planner_model is not None and (not isinstance(planner_model, str) or not planner_model.strip()):
            raise ValueError("planner_model must be a model name or None")
        self.max_steps = max_steps
        plan = [task]
        self.pending_text = None
        self.browser = Browser(url)
        self.record_dir = Path(record_dir) if record_dir else None
        self.screenshots = screenshots or bool(record_dir)
        try:
            page = self.browser.observe(screenshot=self.screenshots)
        except Exception:
            self.browser.close()
            raise
        self.state = dict(
            browser=self.browser,
            goal="\n".join(plan),
            page=page,
            decision=None,
            history=[],
            status="ready",
            plan=plan,
            plan_index=0,
            decisions=[],
            text_calls=[],
            memory=[],
            text_errors=[],
            recoveries=[],
            elapsed_ms=0,
            started_at=None,
            record=bool(self.record_dir),
            planner_model=planner_model,
            controller=None,
            planner_calls=[],
            observations=[],
            checkpoint_step=0,
            checkpoint_errors=0,
            stale_count=0,
            no_progress=0,
            final_answer="",
        )
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            (self.record_dir / "000000.jpg").write_bytes(base64.b64decode(page["screenshot"]))

    def snapshot(self):
        return {
            **{k: v for k, v in self.state.items() if k != "browser"},
            "elements": action_space(self.state["page"]["actions"])[0],
        }

    def command(self, name, body=None):
        body = body or {}
        state = self.state
        if name == "tick":
            try:
                self.command("predict", {})
                if state["status"] in {"done", "blocked"}:
                    return self.snapshot()
                return self.command("act", {"fingerprint": state["page"]["fingerprint"]})
            except StalePage:
                state["decision"] = None
                state["status"] = "ready"
                state["stale_count"] = state.get("stale_count", 0) + 1
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
        elif name == "predict":
            if not state["browser"]:
                raise ValueError("Start a demo first")
            if state["started_at"] is None:
                state["started_at"] = time.perf_counter()
            if not state["browser"].fresh(state["page"]):
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
            state["decision"] = None
            if state["status"] in {"done", "blocked"}:
                raise ValueError("This run has stopped. Start a fresh demo.")
            if len(state["decisions"]) >= self.max_steps * 2:
                raise ValueError("Reached the demo's model-call budget")
            if state.get("planner_model"):
                reason = self.checkpoint_reason()
                if reason:
                    self.checkpoint(reason)
                    if state["status"] in {"done", "blocked"}:
                        return self.snapshot()
                controller = state["controller"]
                state["page"]["objective"] = controller["objective"]
                state["page"]["requirements"] = controller["requirements"]
                state["page"]["checks"] = controller["checks"]
            state["page"]["memory"] = [p for p in state.get("memory", []) if p["url"] != state["page"]["url"]][-6:]
            state["decision"] = choose(state["page"], state["goal"], state["history"])
            state["decisions"].append(
                {
                    **state["decision"],
                    "fingerprint": state["page"]["fingerprint"],
                    "elapsed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                }
            )
            state["status"] = "predicted"
        elif name == "act":
            decision, page = state["decision"], state["page"]
            if not decision or body.get("fingerprint") != page["fingerprint"]:
                raise ValueError("Observe and choose before acting")
            # Consume once, before any mutation or model call. A retry cannot double-click.
            state["decision"] = None
            selected = decision["choice"]
            if selected in {"DONE", "BLOCKED"}:
                if not state["browser"].fresh(page):
                    state["status"] = "ready"
                    raise StalePage("Page changed since the decision. Choose again.")
                if state.get("planner_model"):
                    self.checkpoint("Jev requests verification" if selected == "DONE" else "Jev requests recovery")
                    return self.snapshot()
                if selected == "BLOCKED" and not state.get("recoveries"):
                    # One fresh look handles hydration/temporary blank pages; no browser mutation is replayed.
                    state.setdefault("recoveries", []).append({"reason": "First BLOCKED re-observation"})
                    time.sleep(0.5)
                    state["page"] = state["browser"].observe(screenshot=self.screenshots)
                    state["page"]["feedback"] = (
                        "Previous choice was BLOCKED. Check newly loaded controls, dismiss a blocking dialog, "
                        "scroll to relevant controls, or use Back/Search for another source. "
                        "Do not repeat a failed action."
                    )
                    state["status"] = "ready"
                    state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                    return self.snapshot()
                state["status"] = "done" if selected == "DONE" else "blocked"
                state["plan_index"] = int(selected == "DONE")
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
            action = next(a for a in page["actions"] if a["id"] == selected)
            if len(state["history"]) >= self.max_steps:
                state["status"] = "blocked"
                raise ValueError(f"Stopped at the {self.max_steps}-action budget")
            text, helper = None, None
            if action["kind"] in {"fill", "set_value", "upload", "search"}:
                if not state["browser"].fresh(page, action):
                    raise StalePage("Page changed before text generation. Choose again.")
                context = field_context(state["goal"], action, page, state["history"])
                if self.pending_text and self.pending_text[0] == context:
                    _, text, helper = self.pending_text
                else:
                    try:
                        text, helper = field_text(context)
                    except ValueError as exc:
                        # Missing/invalid text is feedback, not an automatic whole-task failure.
                        state.setdefault("text_errors", []).append({"field": action["label"], "error": str(exc)})
                        page.setdefault("rejected_actions", []).append(selected)
                        page["feedback"] = f"Cannot fill {action['label']}: {exc}. Choose another useful action."
                        state["status"] = "ready"
                        state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                        return self.snapshot()
                    self.pending_text = (context, text, helper)
                    state["text_calls"].append({**helper, "field": action["label"], "value": text})
            # Browser.act checks freshness immediately before input, including after text generation.
            try:
                state["browser"].act(action, page, text=text)
            except InvalidValue as exc:
                self.pending_text = None
                state.setdefault("text_errors", []).append({"field": action["label"], "error": str(exc)})
                page.setdefault("rejected_actions", []).append(selected)
                page["feedback"] = f"Cannot enter {action['label']}: {exc}. Choose another useful action."
                state["status"] = "ready"
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
            self.pending_text = None
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            # Record execution before observing. A stale post-action observation must not erase the action.
            state["history"].append(
                {
                    "step": len(state["history"]) + 1,
                    "action": action["label"],
                    "kind": action["kind"],
                    "choice": selected,
                    "probability": decision["probabilities"][selected],
                    "confidence": decision["confidence"],
                    "latency_ms": decision["latency_ms"],
                    "text": text,
                    "text_helper": helper["model"] if helper else None,
                    "text_latency_ms": helper["latency_ms"] if helper else 0,
                    "operation": decision["operation"],
                    "target": decision["target"],
                    "page_changed": None,
                    "url": page["url"],
                    "usage": decision["usage"],
                    "executed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                    "elapsed_ms": state["elapsed_ms"],
                }
            )
            evidence = {"url": page["url"], "text": page.get("document_text", page["text"])[:4000]}
            memory = state.setdefault("memory", [])
            if evidence["text"]:
                memory[:] = [p for p in memory if p["url"] != evidence["url"]]
                memory.append(evidence)
                del memory[:-8]
            state["page"] = state["browser"].observe(screenshot=self.screenshots)
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            state["history"][-1].update(
                page_changed=state["page"]["fingerprint"] != page["fingerprint"],
                url=state["page"]["url"],
                elapsed_ms=state["elapsed_ms"],
            )
            state["stale_count"] = 0
            state["no_progress"] = (state.get("no_progress", 0) + 1
                                    if not state["history"][-1]["page_changed"] else 0)
            if state.get("planner_model"):
                planner.remember(state)
            if state["record"]:
                (self.record_dir / f"{state['elapsed_ms']:06d}.jpg").write_bytes(
                    base64.b64decode(state["page"]["screenshot"])
                )
            repeated = state["history"][-3:]
            state["status"] = "ready"
            if len(repeated) == 3 and all(h["page_changed"] is False and h["kind"] != "wait" for h in repeated):
                state["page"]["feedback"] = (
                    "The last three actions changed nothing. Try a different control or recovery."
                )
        else:
            raise ValueError("Unknown command")
        return self.snapshot()

    def checkpoint_reason(self):
        state = self.state
        if not state.get("controller"):
            return "Initial plan"
        if state.get("stale_count", 0) >= 3:
            return "Three stale decisions without execution"
        if len(state.get("text_errors", [])) - state.get("checkpoint_errors", 0) >= 2:
            return "Repeated text or value failure"
        if state.get("no_progress", 0) >= 3:
            return "Three actions without observable progress"
        if len(state["history"]) - state.get("checkpoint_step", 0) >= 8:
            return "Review progress after eight actions"
        return None

    def checkpoint(self, reason):
        state = self.state
        # Refresh before verification; a terminal label never verifies its own evidence.
        state["page"] = state["browser"].observe(screenshot=self.screenshots)
        controller = planner.checkpoint(state, reason, state["planner_model"])
        state["controller"] = controller
        state["checkpoint_step"] = len(state["history"])
        state["checkpoint_errors"] = len(state.get("text_errors", []))
        state["stale_count"] = state["no_progress"] = 0
        state["final_answer"] = controller["answer"]
        state["decision"] = None
        state["status"] = "done" if controller["complete"] else "blocked" if controller["blocked"] else "ready"
        state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)

    def run(self):
        while self.state["status"] not in {"done", "blocked"}:
            yield self.command("tick")

    def close(self):
        self.browser.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

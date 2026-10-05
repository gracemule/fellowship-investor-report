"""Running the agent on its own.

The agent is a background worker, not a request handler. A person starts it by putting files
in their folder (or presses one button); it then reads, writes, checks and renders without
anyone watching, and the interface is a window onto what the database says about it.

What this file is responsible for, and why each is here:

* Narration  -- every tool call becomes an event the interface can replay.
* Asking     -- the agent's questions pause the run; the answer (or new files) resume it.
* Recovery   -- transient failures retry from the last checkpoint with backoff; hard failures
                stop with a plain explanation and the work saved; a crashed process is
                detected by its stale heartbeat and its run picked up where it stopped.
* Steering   -- a message from the user is applied at the next step boundary.
* Supervision-- a model ending its turn is not the work being finished; check, and send it back.
* Consistency-- one run at a time per workspace, working on a snapshot of the files taken when
                it starts, so a sync mid-run cannot change what it is reading. Changes that
                arrive meanwhile are queued as the next run.
"""

from __future__ import annotations

import os
import queue
import socket
import threading
import time
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from .. import config
from .. import period as pr
from ..agent.run import repair_dangling_tool_calls, unfinished
from ..agent.store import Store
from ..workspace import sync as ws_sync
from ..workspace.slots import BY_ID
from . import narrator, state, versions
from .compaction import DEFAULT_BUDGET, make_hook, store_brief
from .failures import backoff, classify
from .guard import LoopGuard

SEGMENT_STEPS = 120         # graph steps in one stretch of work
MAX_SEGMENTS = 10           # stretches in one run
MAX_NUDGES = 6
HEARTBEAT_EVERY = 15.0
STALE_AFTER = 75

FUND_NAME = "Chui Ventures Fund I"


class Stopped(Exception):
    pass


class RunFailed(Exception):
    def __init__(self, message: str, kind: str = "unknown"):
        super().__init__(message)
        self.kind = kind


class _Heartbeat:
    def __init__(self, store: Store, run_id: str):
        self._ev = threading.Event()
        self._t = threading.Thread(target=self._loop, args=(store, run_id), daemon=True)
        self._t.start()

    def _loop(self, store, run_id):
        while not self._ev.wait(HEARTBEAT_EVERY):
            try:
                state.touch(store, run_id)
            except Exception:           # noqa: BLE001 - a missed beat is recoverable, a crash here is not
                pass

    def stop(self):
        self._ev.set()


class Runtime:
    def __init__(self, store: Store | None = None, *, workdir: str | Path | None = None, saver=None,
                 agent_factory=None, prepare: bool = True, snapshot: bool = True,
                 sleep=time.sleep, debounce: float = 6.0, max_nudges: int = MAX_NUDGES):
        self.store = store or Store()
        self.workdir = Path(workdir or os.environ.get("CHUI_WORKDIR", ".workspace")).resolve()
        self.saver, self._pool = saver, None
        self._agent_factory = agent_factory
        self._prepare_files, self._snapshot = prepare, snapshot
        self._sleep, self._debounce, self._max_nudges = sleep, debounce, max_nudges
        self._q: queue.Queue[str] = queue.Queue()
        self._lock = threading.RLock()
        self._stop_run = threading.Event()
        self._steer: list[str] = []
        self._pending = ws_sync.Changes()
        self._announced: tuple = ()
        self._timer: threading.Timer | None = None
        self._thread: threading.Thread | None = None
        self._shutdown = threading.Event()
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}"
        self._budget_factor = 1.0
        self.provider_override: str | None = None
        self._rs: Store | None = None
        self._ws: dict | None = None
        self._limit_hit = False
        self._ws_at = 0.0

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self.saver is None and self._agent_factory is None:
            from ..agent.graph import build_pooled_checkpointer
            self.saver, self._pool = build_pooled_checkpointer()
        self.recover()
        self._thread = threading.Thread(target=self._worker, name="chui-runner", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._shutdown.set()
        self._stop_run.set()
        if self._timer:
            self._timer.cancel()
        if self._pool:
            try:
                self._pool.close()
            except Exception:           # noqa: BLE001
                pass

    def recover(self) -> int:
        """Pick up what a previous process left behind. Queued runs are re-queued; runs marked
        running whose heartbeat has gone quiet were killed mid-flight and are resumed from their
        last checkpoint."""
        n = 0
        for r in state.stale_runs(self.store, STALE_AFTER):
            state.requeue(self.store, r["id"])
            state.update_run(self.store, r["id"], status="queued", error=None)
            state.emit(self.store, "recovered", "The server restarted mid-run. Picking up from the last saved step.",
                       run_id=r["id"])
            self._q.put(r["id"])
            n += 1
        with self.store.conn() as c:
            queued = [r["id"] for r in c.execute(f"SELECT id FROM {self.store._t('run')} WHERE status='queued'")]
        for rid in queued:
            self._q.put(rid)
        return n

    def _worker(self) -> None:
        last_janitor = time.time()
        while not self._shutdown.is_set():
            try:
                rid = self._q.get(timeout=2.0)
            except queue.Empty:
                if time.time() - last_janitor > 30:
                    last_janitor = time.time()
                    try:
                        self.recover()
                    except Exception:   # noqa: BLE001
                        pass
                continue
            try:
                self.execute(rid)
            except Exception as exc:    # noqa: BLE001 - the worker must outlive any one run
                try:
                    state.update_run(self.store, rid, status="failed", error=str(exc)[:300])
                except Exception:       # noqa: BLE001
                    pass

    # ------------------------------------------------------------------ workspace helpers

    def ws_row(self, fresh: bool = False) -> dict:
        """The workspace row, cached for a couple of seconds (it is read on almost every request)."""
        now = time.time()
        if fresh or self._ws is None or now - self._ws_at > 2.0:
            self._ws, self._ws_at = state.workspace(self.store), now
        return self._ws

    def period(self) -> pr.Period:
        return pr.Period.parse(self.ws_row()["period"])

    def report_store(self) -> Store:
        p = self.period()
        rid = f"fund-i-{p.code}"
        if self._rs is None or self._rs.report_id != rid:
            self._rs = Store(url=self.store.url, schema=self.store.schema, report_id=rid)
            self._rs.ensure_report(FUND_NAME, p.label)
        return self._rs

    def thread_id(self) -> str:
        return f"{state.WS}-{self.period().code}"

    def coverage(self):
        return ws_sync.coverage(self.store)

    # ------------------------------------------------------------------ requests from the UI

    def on_sync(self, changes: ws_sync.Changes) -> dict:
        """The folder changed. Record it, resume anything waiting for it, and (if automatic
        updating is on) schedule the update."""
        if not changes.any:
            return {"started": None}
        slots = ws_sync.affected_slots(changes.paths)
        sections = ws_sync.affected_sections(changes.paths)
        state.emit(self.store, "source.sync", self._sync_label(changes), detail={
            **changes.as_dict(), "slots": [s.id for s in slots], "sections": sections})
        with self._lock:
            self._pending.added += [p for p in changes.added if p not in self._pending.added]
            self._pending.modified += [p for p in changes.modified if p not in self._pending.modified]
            self._pending.removed += [p for p in changes.removed if p not in self._pending.removed]
        self._resume_waiting_for_files(changes)
        if self.auto():
            self._schedule_kick()
        return {"started": None, "sections": sections}

    @staticmethod
    def _sync_label(ch) -> str:
        bits = []
        for n, w in ((len(ch.added), "added"), (len(ch.modified), "updated"), (len(ch.removed), "removed")):
            if n:
                bits.append(f"{n} {w}")
        return "Your folder changed: " + ", ".join(bits)

    def auto(self) -> bool:
        return bool(self.ws_row()["settings"].get("auto", True))

    def _schedule_kick(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce, self.kick)
            self._timer.daemon = True
            self._timer.start()

    def kick(self) -> str | None:
        """Start the update that pending changes call for, if the sources allow and nothing is running."""
        with self._lock:
            if state.active_run(self.store):
                return None
            missing = ws_sync.required_missing(self.coverage())
            if missing:
                key = tuple(sorted(m.slot.id for m in missing))
                if key != self._announced:
                    self._announced = key
                    state.emit(self.store, "waiting.sources",
                               "Waiting for " + self._list_labels([m.slot.label for m in missing]),
                               detail={"slots": list(key)})
                return None
            self._announced = ()
            first = state.latest_version(self.report_store()) is None
            if not first and not self._pending.any:
                return None
            instruction = self._instruction(first)
            self._pending = ws_sync.Changes()
        return self._create("build" if first else "update", instruction)

    @staticmethod
    def _list_labels(labels: list[str]) -> str:
        return labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]

    def _instruction(self, first: bool) -> str:
        P = self.period()
        if first:
            return (f"Build the {P.label} investor report for {FUND_NAME} from the source documents. "
                    f"Start with list_sources, then follow your workflow through to a rendered, inspected report.")
        ch = self._pending
        names = [Path(p).name for p in ch.added + ch.modified]
        sections = ws_sync.affected_sections(ch.paths)
        parts = ["The user's source files have changed since the last version of the report."]
        if names:
            parts.append("Added or updated: " + "; ".join(names[:12]) + (f" and {len(names) - 12} more" if len(names) > 12 else "") + ".")
        if ch.removed:
            parts.append("Removed: " + "; ".join(Path(p).name for p in ch.removed[:8]) + ".")
        if sections:
            parts.append("Sections that depend on them: " + ", ".join(sections) + ".")
        parts.append("Re-read what changed, update every section and table that depends on it (rebuild tables "
                     "with the build tools), leave the rest as it is, then render and inspect again.")
        return " ".join(parts)

    def _create(self, kind: str, instruction: str) -> str:
        rid = state.create_run(self.store, kind, instruction, self.thread_id())
        self._q.put(rid)
        state.emit(self.store, "run.queued", "", run_id=rid, detail={"kind": kind})
        return rid

    def request_run(self, kind: str = "update", instruction: str | None = None) -> dict:
        """The one button. Starts a run, or says why it did not."""
        with self._lock:
            active = state.active_run(self.store)
            if active:
                return {"ok": False, "reason": "already_running", "run": active["id"]}
            missing = ws_sync.required_missing(self.coverage())
            if missing:
                return {"ok": False, "reason": "needs_sources", "slots": [m.slot.id for m in missing]}
            first = state.latest_version(self.report_store()) is None
            if instruction is None:
                if not first and not self._pending.any:
                    instruction = (f"Check the {self.period().label} report against the source documents and "
                                   f"correct anything that no longer matches them; re-render and inspect.")
                else:
                    instruction = self._instruction(first)
                    self._pending = ws_sync.Changes()
                kind = "build" if first else "update"
        return {"ok": True, "run": self._create(kind, instruction)}

    def resume_failed(self) -> dict:
        with self._lock:
            if state.active_run(self.store):
                return {"ok": False, "reason": "already_running"}
            last = state.latest_run(self.store)
            if not last or last["status"] not in ("failed", "incomplete", "stopped"):
                return {"ok": False, "reason": "nothing_to_resume"}
        return {"ok": True, "run": self._create("resume", last.get("instruction") or "Continue the previous work.")}

    def steer(self, text: str) -> dict:
        text = text.strip()
        if not text:
            return {"ok": False, "reason": "empty"}
        with self._lock:
            active = state.active_run(self.store)
            state.emit(self.store, "steer", text[:400], run_id=active["id"] if active else None)
            if active:
                self._steer.append(text)
                return {"ok": True, "applied": "next_step", "run": active["id"]}
            if state.latest_version(self.report_store()) is None:
                return {"ok": False, "reason": "no_report"}
        return {"ok": True, "applied": "new_run", "run": self._create("steer", text)}

    def answer(self, qid: str, text: str) -> dict:
        q = state.get_question(self.store, qid)
        if not q:
            return {"ok": False, "reason": "no_such_question"}
        done = state.answer_question(self.store, qid, text)
        if not done:
            return {"ok": False, "reason": "already_answered"}
        state.emit(self.store, "question.answered", text[:300], run_id=q["run_id"], detail={"qid": qid})
        if not state.open_questions(self.store, q["run_id"]):
            state.requeue(self.store, q["run_id"])
            self._q.put(q["run_id"])
        return {"ok": True}

    def stop(self) -> dict:
        with self._lock:
            active = state.active_run(self.store)
            if not active:
                return {"ok": False, "reason": "nothing_running"}
            if active["status"] in ("waiting_user", "waiting_data", "queued"):
                state.update_run(self.store, active["id"], status="stopped")
                for q in state.open_questions(self.store, active["id"]):
                    state.answer_question(self.store, q["id"], "(stopped)")
                state.emit(self.store, "run.end", "Stopped.", run_id=active["id"], detail={"status": "stopped"})
                return {"ok": True}
            self._stop_run.set()
        return {"ok": True, "when": "after_current_step"}

    def _resume_waiting_for_files(self, changes) -> None:
        """A run waiting for source files carries on by itself when they arrive."""
        cov = {c.slot.id: c for c in self.coverage()}
        for q in state.open_questions(self.store):
            if q["kind"] != "sources":
                continue
            wanted = q["slots"] or []
            if wanted and all(cov[s].state == "ready" for s in wanted if s in cov):
                names = [Path(p).name for s in wanted if s in cov for p in cov[s].files][:8]
                self.answer(q["id"], "FILES_ARRIVED: the user has added " + "; ".join(names)
                            + ". They are in the folder now; read them.")

    # ------------------------------------------------------------------ one run

    def execute(self, run_id: str) -> str:
        if not state.claim(self.store, run_id, self.worker_id):
            return "skipped"
        run = state.get_run(self.store, run_id)
        hb = _Heartbeat(self.store, run_id)
        self._stop_run.clear()
        try:
            outcome = self._drive(run)
        except Stopped:
            outcome = "stopped"
        except RunFailed as exc:
            state.update_run(self.store, run_id, status="failed", error=str(exc))
            state.emit(self.store, "run.end", str(exc), run_id=run_id, detail={"status": "failed", "kind": exc.kind})
            outcome = "failed"
        except Exception as exc:        # noqa: BLE001
            f = classify(exc)
            state.update_run(self.store, run_id, status="failed", error=f.message)
            state.emit(self.store, "run.end", f.message, run_id=run_id, detail={"status": "failed", "kind": f.kind})
            outcome = "failed"
        finally:
            hb.stop()
        if outcome == "stopped":
            state.update_run(self.store, run_id, status="stopped")
            state.emit(self.store, "run.end", "Stopped.", run_id=run_id, detail={"status": "stopped"})
        if outcome in ("done", "stopped", "failed", "incomplete") and not self._shutdown.is_set():
            if self.auto() and self._pending.any:
                self._schedule_kick()
        return outcome

    # -- preparation

    def _prepare(self, run: dict) -> Store:
        from ..agent import tools as T
        from ..render import report_writer
        from ..render.fonts import install_brand_fonts

        P = self.period()
        pr.set_current(P)
        rs = self.report_store()
        T.set_store(rs)
        T.reset_questions()
        if self._prepare_files:
            src = self.workdir / "source"
            n = ws_sync.materialize(self.store, src)
            config.set_root(src)
            report_writer.OUT_DIR = self.workdir / "out"
            try:
                install_brand_fonts(src)
            except Exception as exc:    # noqa: BLE001
                raise RunFailed("The report's typeface is not available. Add the Branding folder "
                                "(with Fonts/Larken) to your folder and try again.", "fonts") from exc
            state.emit(self.store, "run.prepared", f"Working from {len([1 for _ in src.rglob('*') if _.is_file()])} files "
                       f"in your folder", run_id=run["id"], detail={"written": n})
        return rs

    # -- agent

    def _provider(self) -> str | None:
        return self.provider_override

    def _make_agent(self, rs: Store):
        if self._agent_factory:
            return self._agent_factory(self._provider())
        from ..agent.graph import build_agent
        hook = make_hook(int(DEFAULT_BUDGET * self._budget_factor), brief=lambda: store_brief(rs),
                         on_compact=lambda s: state.emit(
                             self.store, "compacted",
                             f"Condensed the earlier conversation ({s['messages_before']} → {s['messages_after']} messages)",
                             detail=s))
        return build_agent(self.saver, provider=self._provider(), pre_model_hook=hook)

    # -- the loop

    def _drive(self, run: dict) -> str:
        rid = run["id"]
        rs = self._prepare(run)
        self._agent = agent = self._make_agent(rs)
        cfg = {"configurable": {"thread_id": run["thread_id"]}, "recursion_limit": SEGMENT_STEPS}
        state.emit(self.store, "run.start", run.get("instruction") or "", run_id=rid, detail={"kind": run["kind"]})

        repair_dangling_tool_calls(agent, cfg)          # (leaves a parked question alone)
        pending = self._pending_interrupts(agent, cfg)
        if pending:
            answers = {}
            for i in pending:
                q = state.get_question(self.store, i.id)
                if not q or q["status"] != "answered":
                    return self._park(rid, pending)
                answers[i.id] = q["answer"]
            payload = Command(resume=answers)
        else:
            snap = agent.get_state(cfg)
            continuing = run["kind"] in ("recover", "resume") or run["attempts"] > 1
            payload = None if (continuing and snap.next) else \
                {"messages": [HumanMessage(content=run["instruction"] or "Continue.")]}

        guard, nudges, segments = LoopGuard(), run.get("nudges", 0), 0
        while True:
            segments += 1
            agent = self._agent
            outcome, info = self._segment(agent, cfg, payload, run, guard)
            if outcome == "interrupted":
                return self._park(rid, info)
            if outcome == "steered":
                repair_dangling_tool_calls(agent, cfg)
                payload = {"messages": [HumanMessage(content=info)]}
                continue
            if outcome == "limit" and segments < MAX_SEGMENTS:
                payload = {"messages": [HumanMessage(content=(
                    "Carry on from where you were. Keep going until the report is rendered and inspected."))]}
                continue
            missing = unfinished(rs)
            if guard.triggered >= 3:
                raise RunFailed("The agent kept repeating the same step and was stopped. Your work is saved; "
                                "tell it what to do differently, or resume.", "loop")
            if missing and nudges < self._max_nudges and segments < MAX_SEGMENTS:
                nudges += 1
                state.update_run(self.store, rid, nudges=nudges)
                state.emit(self.store, "nudge", "Not finished yet: " + "; ".join(missing), run_id=rid)
                repair_dangling_tool_calls(agent, cfg)
                payload = {"messages": [HumanMessage(content=(
                    "You have not finished. Still to do: " + "; ".join(missing) + ". Do it now by calling the "
                    "tools. Do not describe what you are about to do; do it."))]}
                continue
            break
        return self._finish(run, rs, incomplete=bool(missing))

    def _finish(self, run: dict, rs: Store, *, incomplete: bool) -> str:
        rid = run["id"]
        snap = None
        if self._snapshot:
            try:
                snap = versions.snapshot(rs, rid)
            except Exception as exc:    # noqa: BLE001
                state.emit(self.store, "issue", f"Could not store the rendered report: {exc}", run_id=rid)
        if snap and snap["new"]:
            state.emit(self.store, "version", f"Version {snap['version']} is ready", run_id=rid,
                       detail={"version": snap["version"], "pages": snap["pages"], "changed": snap["changed"]})
        if incomplete:
            state.update_run(self.store, rid, status="incomplete",
                             error="The agent stopped before the report was rendered and checked.")
            state.emit(self.store, "run.end", "The agent stopped before finishing. Your work is saved.",
                       run_id=rid, detail={"status": "incomplete"})
            return "incomplete"
        state.update_run(self.store, rid, status="done")
        state.emit(self.store, "run.end", "Done.", run_id=rid, detail={"status": "done",
                   "version": (snap or {}).get("version")})
        return "done"

    # -- one stretch of work, with retries

    def _segment(self, agent, cfg, payload, run, guard: LoopGuard):
        rid = run["id"]
        attempt, switched = 0, False
        while True:
            try:
                return self._stream(agent, cfg, payload, rid, guard)
            except Stopped:
                raise
            except Exception as exc:    # noqa: BLE001
                f = classify(exc)
                if f.kind == "step_limit":
                    return "limit", None
                if f.kind == "history" and attempt < 2:
                    attempt += 1
                    n = repair_dangling_tool_calls(agent, cfg)
                    state.emit(self.store, "recovered", "Repaired the saved conversation and continued.", run_id=rid)
                    payload = self._after_failure(agent, cfg, payload)
                    continue
                if f.kind == "context" and attempt < 3:
                    attempt += 1
                    self._budget_factor *= 0.6
                    state.emit(self.store, "compacted", "The conversation was too long; trimming harder and continuing.",
                               run_id=rid)
                    self._agent = agent = self._make_agent(self.report_store())
                    payload = self._after_failure(agent, cfg, payload)
                    continue
                if f.retry and attempt < f.attempts:
                    wait = backoff(f, attempt, exc)
                    attempt += 1
                    state.emit(self.store, "retry", f"{f.message} Trying again in {int(round(wait))} seconds.",
                               run_id=rid, detail={"attempt": attempt, "of": f.attempts, "wait": wait, "kind": f.kind})
                    self._interruptible_sleep(wait)
                    payload = self._after_failure(agent, cfg, payload)
                    continue
                if f.retry and not switched and self._fallback():
                    switched = True
                    attempt = 0
                    state.emit(self.store, "provider.switch",
                               f"{f.message} Switching to {self.provider_override} for the rest of this run.", run_id=rid)
                    self._agent = agent = self._make_agent(self.report_store())
                    payload = self._after_failure(agent, cfg, payload)
                    continue
                raise RunFailed(f.message + ("" if f.kind in ("auth", "quota") else " Your work is saved; "
                                "resume when it is back."), f.kind) from exc

    def _fallback(self) -> bool:
        from ..agent.llm import PROVIDERS, api_key, resolve_provider
        want = os.environ.get("CHUI_FALLBACK_PROVIDER", "").strip().lower()
        if not want or want not in PROVIDERS or want == resolve_provider(self._provider()).name:
            return False
        if not api_key(PROVIDERS[want]):
            return False
        self.provider_override = want
        return True

    def _interruptible_sleep(self, seconds: float) -> None:
        remaining = seconds
        while remaining > 0:
            if self._stop_run.is_set() or self._shutdown.is_set():
                raise Stopped()
            step = min(1.0, remaining)
            self._sleep(step)
            remaining -= step

    def _after_failure(self, agent, cfg, payload):
        """What to send when re-entering the graph after a failure part-way through it."""
        repair_dangling_tool_calls(agent, cfg)
        snap = agent.get_state(cfg)
        pending = self._pending_interrupts(agent, cfg)
        if isinstance(payload, Command):
            return payload if pending else None
        if not isinstance(payload, dict):
            return payload
        msgs = (snap.values or {}).get("messages", [])
        want = payload["messages"][-1].content
        if msgs and isinstance(msgs[-1], HumanMessage) and msgs[-1].content == want:
            return None                    # the attempt that failed had already recorded it
        return payload

    @staticmethod
    def _pending_interrupts(agent, cfg) -> list:
        snap = agent.get_state(cfg)
        return [i for t in (snap.tasks or ()) for i in (t.interrupts or ())]

    def _stream(self, agent, cfg, payload, rid, guard: LoopGuard):
        """Run the graph, narrating as it goes. A steer or a loop warning is delivered only at a
        clean boundary -- after a tool node has answered every call in flight -- so the saved
        conversation is never left with a call that has no result."""
        interrupts, nudge = None, None
        self._limit_hit = False
        for chunk in agent.stream(payload, cfg, stream_mode="updates"):
            answered = False
            for node, update in chunk.items():
                if node == "__interrupt__":
                    interrupts = list(update)
                    continue
                if not isinstance(update, dict):
                    continue
                for msg in update.get("messages") or []:
                    if isinstance(msg, ToolMessage):
                        answered = True
                    nudge = self._narrate(rid, msg, guard) or nudge
            if self._stop_run.is_set():
                raise Stopped()
            if answered:
                if nudge:
                    return "steered", nudge
                with self._lock:
                    if self._steer:
                        text = " ".join(self._steer)
                        self._steer.clear()
                        return "steered", "The user says: " + text
        if interrupts:
            return "interrupted", interrupts
        if self._limit_hit:                 # the graph's own step allowance for this stretch ran out
            return "limit", None
        return "finished", None

    def _narrate(self, rid: str, msg, guard: LoopGuard) -> str | None:
        if isinstance(msg, AIMessage):
            calls = msg.tool_calls or []
            if not calls:
                text = msg.content if isinstance(msg.content, str) else \
                    " ".join(b.get("text", "") for b in msg.content if isinstance(b, dict))
                if text.strip().startswith("Sorry, need more steps"):
                    self._limit_hit = True           # LangGraph's step-limit message: not the agent speaking
                    return None
                if text.strip():
                    state.emit(self.store, "note", text.strip()[:700], run_id=rid)
                return None
            steer = None
            for c in calls:
                chapter, label = narrator.describe_call(c["name"], c.get("args") or {})
                state.emit(self.store, "step", label, run_id=rid, chapter=chapter,
                           detail={"call": c["id"], "tool": c["name"], "status": "running"})
                steer = steer or guard.observe(c["name"], c.get("args") or {})
            return steer
        if isinstance(msg, ToolMessage):
            level, text = narrator.describe_result(msg.name or "", msg.content, getattr(msg, "status", None))
            state.emit(self.store, "step.done", text or "", run_id=rid,
                       detail={"call": msg.tool_call_id, "tool": msg.name, "level": level})
        return None

    def _park(self, rid: str, interrupts) -> str:
        kinds = set()
        for i in interrupts:
            v = i.value if isinstance(i.value, dict) else {"prompt": str(i.value), "kind": "info"}
            kinds.add(v.get("kind", "info"))
            state.open_question(self.store, i.id, rid, v.get("kind", "info"), v.get("prompt", ""),
                                v.get("why", ""), v.get("options") or [], v.get("slots") or [])
            state.emit(self.store, "question", v.get("prompt", ""), run_id=rid, detail={
                "qid": i.id, "kind": v.get("kind", "info"), "why": v.get("why", ""),
                "options": v.get("options") or [], "slots": v.get("slots") or []})
        status = "waiting_data" if kinds == {"sources"} else "waiting_user"
        state.update_run(self.store, rid, status=status)
        return status

    # ------------------------------------------------------------------ what the UI shows

    def snapshot_state(self) -> dict:
        from . import view
        return view.build(self)

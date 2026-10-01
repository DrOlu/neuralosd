"""Engine pool — ONE loaded engine, N instance views.

The expensive part of neuralOS is the loaded weights (~95 MB). The pool keeps
a per-instance `Needle` view (the instance's own probe subset) and shares the
underlying library state, so view #N costs a menu swap, not a weights reload.
Wedge recovery: every ask runs under a timeout; a wedged view is dropped and
rebuilt on the next ask (the demo_server recovery pattern, daemonized).
"""
import concurrent.futures
import threading
import time


class EnginePool:
    def __init__(self, build_timeout: int = 180, ask_timeout: int = 120):
        self._views = {}
        self._build_timeout = build_timeout
        self._ask_timeout = ask_timeout
        self._lock = threading.Lock()
        self._infer_lock = threading.Lock()   # libneedle inference: serialize
        self.builds = 0          # times a view was (re)built
        self.last_build_s = 0.0

    def stats(self):
        return {"views": sorted(self._views), "builds": self.builds,
                "last_build_s": round(self.last_build_s, 2)}

    def _view(self, instance_name, tools):
        with self._lock:
            v = self._views.get(instance_name)
            if v is None:
                import needle
                t0 = time.time()
                v = needle.Needle(tools=tools,
                                  system="answer from the menu probes.",
                                  auto_date=False)
                self._views[instance_name] = v
                self.builds += 1
                self.last_build_s = time.time() - t0
                print(f"[engine] view built: {instance_name} "
                      f"({self.last_build_s:.1f}s, build #{self.builds})",
                      flush=True)
        return v

    def embed(self, text: str):
        """Embed text via a shared tool-less view (needle 3 embeddings)."""
        v = self._view("_embedder", [])
        return v.embed(text)

    def fallback(self, instance_name, tools_by_name):
        """Returns the Router model_fallback callable for an instance.

        fallback(normalized_question, retrieved_metas) -> results (list) —
        executed under a timeout; a wedged view is dropped for rebuild.
        """
        def fallback(normalized, retrieved):
            tools = [tools_by_name[m.name] for m in retrieved
                     if m.name in tools_by_name]
            if not tools:
                return []
            agent = self._view(instance_name, tools)

            # NOTE: no lock held across inference — a wedged run must never
            # block the daemon. The per-ask timeout bounds the client wait.
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(agent.run, normalized)
                try:
                    resp = fut.result(timeout=self._ask_timeout)
                except concurrent.futures.TimeoutError:
                    self._views.pop(instance_name, None)
                    raise RuntimeError(
                        "engine wedge — view dropped, ask again") from None

            results = resp.get("results")
            if isinstance(results, list) and results:
                rank1 = retrieved[0].name
                for item in results:
                    if isinstance(item, dict) and "rows" in item \
                            and "_tool" not in item:
                        item["_tool"] = rank1
            return results
        return fallback

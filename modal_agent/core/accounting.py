"""Compact charge records, deliberately not a tracing system."""
import math
import os
import uuid


def record(payload, step, duration, success=True, **identity):
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    cost = usage.get("cost")
    known = type(cost) in (int, float) and math.isfinite(cost) and cost >= 0
    return {**identity, "record_id": payload.get("id") or uuid.uuid4().hex,
            "request_id": payload.get("id"), "step": step, "model": payload.get("model"),
            "provider": payload.get("provider"), "input_tokens": usage.get("prompt_tokens", usage.get("input_tokens")),
            "output_tokens": usage.get("completion_tokens", usage.get("output_tokens")),
            "cost": cost if known else None, "cost_status": "reported" if known else "unknown",
            "duration": round(duration, 3), "success": success}


def deduplicate(records):
    return list({r.get("request_id") or r["record_id"]: r for r in records}.values())


def compute_estimate(cpu_seconds, gpu_seconds=0, index_seconds=0):
    values = [("cpu", cpu_seconds, "MODAL_CPU_USD_PER_SECOND"),
              ("gpu", gpu_seconds, "MODAL_GPU_USD_PER_SECOND"),
              ("index", index_seconds, "MODAL_STORAGE_USD_PER_SECOND")]
    details = []
    for resource, seconds, name in values:
        try:
            if seconds is None:
                raise ValueError("Resource measurement unavailable")
            rate = float(os.environ[name])
            if not math.isfinite(rate) or rate < 0:
                raise ValueError("Invalid unit rate")
            estimate = seconds * rate
        except (KeyError, ValueError):
            rate, estimate = None, None
        details.append({"resource": resource, "seconds": round(seconds, 3) if seconds is not None else None, "usd_per_second": rate, "estimate": estimate})
    return {"details": details, "estimate": sum(d["estimate"] or 0 for d in details),
            "status": "estimated" if any(d["estimate"] is not None for d in details) else "unknown",
            "explanation": "Estimates cover measured worker/transcription/index time only. Cold starts, shared idle, storage RPC overhead and notification time are not allocated; missing rates are unavailable."}


def summary(records, compute=None):
    if records is None:
        return "Cost not recorded"
    records = deduplicate(records)
    cost = sum(r.get("cost") or 0 for r in records if r.get("cost_status") != "reused")
    incomplete = any(r.get("cost_status") == "unknown" for r in records)
    if compute and compute.get("status") == "estimated":
        return f"Estimated total ${cost + compute['estimate']:.8f} · incomplete"
    return f"Recorded AI cost ${cost:.8f}" + (" · incomplete" if incomplete else "")

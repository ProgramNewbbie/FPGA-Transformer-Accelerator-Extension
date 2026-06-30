"""
task_partitioning.py - Stage 1: Host CPU Task Orchestrator
===========================================================
Divides the input dataset into discrete tasks and drives the three-stage
asynchronous pipeline:

    ┌─────────────────────────────────────────────────────────────┐
    │  Stage 1  (this module - host ARM CPU)                      │
    │    • Pre-process: acquire free buffer, write input data      │
    │    • Dispatch: push buffer index to fpga_task_queue         │
    │    • Post-process: read result, release buffer               │
    ├─────────────────────────────────────────────────────────────┤
    │  Stage 2  (async_scheduler - background threads)            │
    │    • fpga_async_worker  : run FPGA kernel (AP_START)        │
    │    • interrupt_worker   : wait for AP_DONE, sync cache       │
    └─────────────────────────────────────────────────────────────┘

Ping-pong overlap
-----------------
Two buffers alternate between Stage 1 and Stage 2.  While the FPGA
processes buffer N, the CPU fills buffer 1-N.  Throughput approaches
max(T_cpu_preprocess, T_fpga) rather than their sum.

Usage - simulation (no hardware)
---------------------------------
    python task_partitioning.py

Usage - KV260 hardware
-----------------------
    from pynq import Overlay
    from task_partitioning import run_pipeline

    overlay = Overlay("/home/xilinx/design.bit")
    data    = [np.random.randint(-128, 127, (64, 768), dtype='int8')
               for _ in range(8)]
    run_pipeline(data, sim=False, overlay=overlay)
"""

import time
import queue

import ping_pong_buffer as ppb
from async_scheduler import (
    fpga_task_queue,
    fpga_done_queue,
    cpu_post_queue,
    build_scheduler_threads,
    init_kv260_scheduler,
    reset_queues,
)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _post_process(buf_idx: int, result, start_time: float) -> None:
    """
    Stage 1 post-processing: log the result and return the buffer to the pool.
    On KV260 replace the print with whatever downstream logic is needed
    (e.g. write result to host memory, trigger next layer inference, etc.).
    """
    elapsed = time.time() - start_time
    print(
        f"[t={elapsed:.3f}s] [Stage 1 | Post]  "
        f"buffer={buf_idx}  result='{result}'"
    )
    ppb.free_buffer_queue.put(buf_idx)   # return buffer to free pool


def _drain_post_queue(start_time: float, counters: dict) -> None:
    """
    Non-blocking drain of cpu_post_queue.
    Increments counters['completed'] for each task finished.
    """
    while not cpu_post_queue.empty():
        done_idx = cpu_post_queue.get()
        result   = ppb.read_output(done_idx)
        _post_process(done_idx, result, start_time)
        counters["completed"] += 1


# ── Public API ─────────────────────────────────────────────────────────────────

def run_pipeline(data_items, sim: bool = False, overlay=None) -> None:
    """
    Execute the full ping-pong FPGA pipeline for *data_items*.

    Parameters
    ----------
    data_items : iterable
        Input dataset.  Each element is one task payload:
          • Simulation : any Python object (e.g. a string "Data 1").
          • KV260      : numpy array of shape (64, 768) and dtype int8.
    sim : bool
        True  → software simulation (no FPGA hardware required).
        False → KV260 hardware path; *overlay* must be supplied.
    overlay : pynq.Overlay or None
        Pre-loaded PYNQ overlay.  Required when sim=False.
    """
    # ── Initialise buffers and scheduler ──────────────────────────────────────
    if sim:
        ppb.init_sim_buffers()
    else:
        if overlay is None:
            raise ValueError("overlay must be provided when sim=False")
        init_kv260_scheduler(overlay)
        ppb.init_kv260_buffers(overlay)

    reset_queues()   # clear any residual queue state from a previous run

    tasks = list(data_items)   # materialise so we know the total count
    total     = len(tasks)
    counters  = {"completed": 0}
    start_time = time.time()

    print(f"[t=0.000s] Pipeline START - {total} tasks, "
          f"{'simulation' if sim else 'KV260 hardware'} mode\n")

    # ── Launch Stage-2 threads ────────────────────────────────────────────────
    t_fpga, t_irq = build_scheduler_threads(sim=sim)
    t_fpga.start()
    t_irq.start()

    # ── Stage 1: pre-process loop ─────────────────────────────────────────────
    for task_num, data in enumerate(tasks, start=1):

        buf_idx    = None
        is_waiting = False

        # Acquire a free buffer; drain post-queue while waiting to avoid
        # deadlock when both buffers are occupied by the FPGA.
        while buf_idx is None:
            _drain_post_queue(start_time, counters)

            try:
                buf_idx    = ppb.free_buffer_queue.get_nowait()
                is_waiting = False
            except queue.Empty:
                if not is_waiting:
                    elapsed = time.time() - start_time
                    print(
                        f"[t={elapsed:.3f}s] [Stage 1 | Pre]   "
                        f"task={task_num}  waiting for free buffer …"
                    )
                    is_waiting = True
                time.sleep(0.01)   # yield CPU; avoid busy-wait

        # Write task payload into the acquired buffer
        ppb.write_input(buf_idx, data)

        elapsed = time.time() - start_time
        print(
            f"[t={elapsed:.3f}s] [Stage 1 | Pre]   "
            f"task={task_num}  buffer={buf_idx}  "
            f"data='{data}'  → dispatching to FPGA"
        )

        # Hand off to Stage 2
        fpga_task_queue.put(buf_idx)

    # ── Drain remaining results after all tasks are submitted ─────────────────
    while counters["completed"] < total:
        _drain_post_queue(start_time, counters)
        time.sleep(0.01)

    # ── Shut down Stage-2 threads ─────────────────────────────────────────────
    fpga_task_queue.put(None)   # sentinel → fpga_async_worker exits
    fpga_done_queue.put(None)   # sentinel → interrupt_worker exits
    t_fpga.join(timeout=5)
    t_irq.join(timeout=5)

    if not sim:
        ppb.free_all_buffers()

    elapsed = time.time() - start_time
    print(
        f"\n[t={elapsed:.3f}s] Pipeline DONE - "
        f"{counters['completed']}/{total} tasks completed"
    )


# ── Entry point (simulation) ───────────────────────────────────────────────────

if __name__ == "__main__":
    run_pipeline(
        data_items=[f"Data {i}" for i in range(1, 5)],
        sim=True,
    )

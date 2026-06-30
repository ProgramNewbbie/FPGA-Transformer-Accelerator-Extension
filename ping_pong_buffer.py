"""
ping_pong_buffer.py — Ping-Pong Double Buffer Manager
======================================================
Maintains two CMA-backed buffers (indices 0 and 1) that alternate ownership:
while the FPGA processes one buffer, the CPU pre-fills the other.

Simulation mode  : plain Python dicts, no hardware needed.
KV260 / PYNQ mode: pynq.allocate() places buffers in contiguous DDR memory
                   accessible by both the ARM CPU and the FPGA DMA engine.

Deployment note
---------------
Call init_kv260_buffers(overlay) AFTER loading the bitstream via Overlay().
All other modules import this module (not individual names) to avoid Python
aliasing issues when the buffer dicts are updated in-place.
"""

import queue

# ── Constants ─────────────────────────────────────────────────────────────────
NUM_BUFFERS  = 2
BUFFER_SHAPE = (64, 768)   # rows × cols expected by the HLS IP (int8)
BUFFER_DTYPE = "int8"

# ── Free-buffer pool ──────────────────────────────────────────────────────────
# Both buffers start in the free pool.  Stage 1 acquires one before writing;
# it is released back here after Stage 1 post-processing finishes.
free_buffer_queue: queue.Queue = queue.Queue()
free_buffer_queue.put(0)   # buffer 0 (ping) available at startup
free_buffer_queue.put(1)   # buffer 1 (pong) available at startup

# ── Buffer storage ────────────────────────────────────────────────────────────
# Simulation mode : dict values are plain Python objects (strings, etc.)
# KV260 mode      : dict values are pynq.Buffer objects with .physical_address,
#                   .flush(), and .invalidate() methods.
# Always update VALUES in-place; never reassign these names so that other
# modules that hold a reference to these dicts stay in sync.
input_buffers:  dict = {0: None, 1: None}
output_buffers: dict = {0: None, 1: None}


# ── Initialisation helpers ────────────────────────────────────────────────────

def init_sim_buffers() -> None:
    """Reset to simulation-mode (no hardware). Safe to call multiple times."""
    input_buffers[0]  = None
    input_buffers[1]  = None
    output_buffers[0] = None
    output_buffers[1] = None
    # Drain and refill free-buffer pool
    while not free_buffer_queue.empty():
        try:
            free_buffer_queue.get_nowait()
        except queue.Empty:
            break
    free_buffer_queue.put(0)
    free_buffer_queue.put(1)


def init_kv260_buffers(overlay, shape=BUFFER_SHAPE, dtype=BUFFER_DTYPE) -> None:
    """
    Allocate contiguous DDR memory (CMA) for DMA transfers on KV260.

    Parameters
    ----------
    overlay : pynq.Overlay
        A fully loaded overlay (bitstream already downloaded).
    shape   : tuple
        Numpy-style shape for each buffer, e.g. (64, 768).
    dtype   : str
        Numpy dtype string, e.g. 'int8'.
    """
    from pynq import allocate  # available on KV260 / PYNQ runtime

    free_all_buffers()  # release any previously allocated CMA memory

    for i in range(NUM_BUFFERS):
        input_buffers[i]  = allocate(shape=shape, dtype=dtype)
        output_buffers[i] = allocate(shape=shape, dtype=dtype)

    # Reset free-buffer pool
    while not free_buffer_queue.empty():
        try:
            free_buffer_queue.get_nowait()
        except queue.Empty:
            break
    free_buffer_queue.put(0)
    free_buffer_queue.put(1)


# ── Buffer I/O helpers ────────────────────────────────────────────────────────

def write_input(buf_idx: int, data) -> None:
    """
    Copy *data* into input_buffers[buf_idx].

    KV260  : data must be a numpy array matching BUFFER_SHAPE / BUFFER_DTYPE.
             The slice-assignment copies into CMA memory without reallocation.
    Sim    : data can be any Python object; stored by reference.
    """
    if hasattr(input_buffers[buf_idx], "__setitem__"):
        input_buffers[buf_idx][:] = data   # CMA numpy buffer slice copy
    else:
        input_buffers[buf_idx] = data       # simulation fallback


def read_output(buf_idx: int):
    """Return the contents of output_buffers[buf_idx]."""
    return output_buffers[buf_idx]


# ── Resource cleanup ──────────────────────────────────────────────────────────

def free_all_buffers() -> None:
    """Release CMA memory held by all buffers. Call once on shutdown."""
    for buf in list(input_buffers.values()) + list(output_buffers.values()):
        if buf is not None and hasattr(buf, "freebuffer"):
            buf.freebuffer()

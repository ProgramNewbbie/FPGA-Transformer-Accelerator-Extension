# KV260 Deployment Guide
## Uploading the Ping-Pong FPGA Pipeline

**Target board:** AMD Kria KV260 Vision AI Starter Kit
**Accelerator:** `mmult_accel` — INT8 matrix multiplication (N×K × K×M → N×M)
**Runtime:** PYNQ 3.0.1 on KV260 Ubuntu image
**Tool versions:** Vitis HLS 2022.2 · Vivado 2022.2

---

## Overview of the Flow

```
Host PC                                      KV260 Board
──────────────────────────────────────────   ───────────────────────────
mmult_accel.cpp / .hpp
        │
        ▼
[Step 1] Vitis HLS
  • C simulation
  • C synthesis
  • Export RTL as IP (.zip)
        │
        ▼
[Step 2] Vivado Block Design
  • Add HLS IP to catalog
  • Connect to Zynq UltraScale+ PS
  • Wire AXI-Lite + AXI Master + Interrupt
  • Generate bitstream
        │
        ├── design.bit  ──────────────────►  /home/xilinx/design.bit
        └── design.hwh  ──────────────────►  /home/xilinx/design.hwh
                                             /home/xilinx/ping_pong_buffer.py
                                             /home/xilinx/async_scheduler.py
                                             /home/xilinx/task_partitioning.py
                                                      │
                                                      ▼
                                             [Step 5] python3 task_partitioning.py
```

---

## Prerequisites

### Host PC
- [ ] Vitis HLS 2022.2 installed (`vitis_hls` on PATH)
- [ ] Vivado 2022.2 installed
- [ ] KV260 board files installed in Vivado
  (Help → Manage Vivado Store → install "Kria KV260")
- [ ] SCP / WinSCP / FileZilla for file transfer

### KV260 Board
- [ ] PYNQ 3.0.1 SD card image flashed
  Download: `KV260-3.0.1.img.xz` from the PYNQ GitHub releases page
  Flash with: Balena Etcher or `dd`
- [ ] Board powered on, connected via Ethernet (DHCP)
  Default login — username: `xilinx` · password: `xilinx`
- [ ] Able to SSH: `ssh xilinx@<board-ip>`

---

## Step 1 — Vitis HLS: Synthesize the Accelerator IP

### 1.1  Launch and create project

1. Open **Vitis HLS 2022.2**.
2. Click **Create Project**.
3. Set **Project name:** `mmult_accel_hls`
4. Set **Location:** any folder on your PC (e.g. `C:/capstone/hls`)
5. Click **Next**.

### 1.2  Add source files

1. On the **Add/Remove Files** screen click **Add Files**.
2. Select both:
   - `mmult_accel.cpp`
   - `mmult_accel.hpp`
3. In the **Top Function** field type: `mmult_accel`
4. Click **Next**.

### 1.3  Add testbench

1. Click **Add Files** again (testbench section).
2. Select `mmult_accel_tb.cpp`.
3. Click **Next**.

### 1.4  Set the solution and target device

1. **Solution name:** `solution1`
2. Click the `...` button next to **Part**.
3. In the search box type `xck26` and select:
   ```
   xck26-sfvc784-2LV-c
   ```
   (This is the exact part number for the KV260 SOM.)
4. **Clock period:** `10` ns (100 MHz) — adjust if your design requires a different frequency.
5. Click **Finish**.

### 1.5  Run C Simulation

1. Click **Run C Simulation** (green play button → "C Simulation").
2. Confirm the testbench passes. Look for `PASS` or zero errors in the console.
3. If the simulation fails, fix the source before continuing.

### 1.6  Run C Synthesis

1. Click **Run C Synthesis** (green play button → "C Synthesis").
2. Wait for synthesis to complete (~2–5 minutes).
3. Review the **Synthesis Report**:
   - **Latency** — cycles per call and wall-clock time at your clock
   - **Resource utilization** — LUT, FF, BRAM, DSP usage (must fit on KV260)
   - **AXI-Lite register map** — **copy these offsets** into `async_scheduler.py` (see Step 1.8)

### 1.7  (Optional) Run Co-Simulation

1. Click **Run Co-Simulation** to verify RTL matches C model.
2. Select **Verilog** as the RTL language and click **OK**.
3. A `PASS` result confirms the RTL is functionally correct.

### 1.8  Record the AXI-Lite register offsets

Open the synthesis report (`solution1/syn/report/mmult_accel_csynth.rpt`) and find the
**Interface** section. You will see a table similar to:

```
+--------------+--------+----------+
| Port         | Offset | Protocol |
+--------------+--------+----------+
| return       | 0x00   | s_axilite|
| A            | 0x10   | s_axilite|
| B            | 0x1C   | s_axilite|
| C            | 0x28   | s_axilite|
| N            | 0x34   | s_axilite|
| K            | 0x3C   | s_axilite|
| M            | 0x44   | s_axilite|
| update_A     | 0x4C   | s_axilite|
+--------------+--------+----------+
```

> **Important:** Offsets may differ slightly from the values shown above.
> Always use the values from your actual synthesis report.

Update `_real_fpga_kernel()` in **`async_scheduler.py`** with these offsets:

```python
def _real_fpga_kernel(buf_idx: int) -> None:
    ppb.input_buffers[buf_idx].flush()           # CPU Cache → DDR (matrix A)
    ppb.weight_buffer.flush()                    # matrix B (if reloading)

    _fpga_ip.write(0x10, int(ppb.input_buffers[buf_idx].physical_address))   # A
    _fpga_ip.write(0x14, 0)                                                  # A upper 32 bits
    _fpga_ip.write(0x1C, int(ppb.weight_buffer.physical_address))            # B
    _fpga_ip.write(0x20, 0)                                                  # B upper 32 bits
    _fpga_ip.write(0x28, int(ppb.output_buffers[buf_idx].physical_address))  # C
    _fpga_ip.write(0x2C, 0)                                                  # C upper 32 bits
    _fpga_ip.write(0x34, 64)                                                 # N
    _fpga_ip.write(0x3C, 768)                                                # K
    _fpga_ip.write(0x44, 768)                                                # M
    _fpga_ip.write(0x4C, 1)                                                  # update_A
    _fpga_ip.write(0x00, 0x01)                                               # AP_START
```

### 1.9  Export the RTL as IP

1. Click **Export RTL** (green play button → "Export RTL").
2. **Format:** `Vivado IP (.zip)`
3. **Output location:** note the folder (e.g. `C:/capstone/hls/mmult_accel_hls/solution1/impl/ip`)
4. Click **OK**. A `.zip` archive named `xilinx_com_hls_mmult_accel_1_0.zip` will be created.

---

## Step 2 — Vivado: Block Design and Bitstream

### 2.1  Create a new Vivado project

1. Open **Vivado 2022.2** → **Create Project** → **Next**.
2. **Project name:** `kv260_mmult` · **Location:** e.g. `C:/capstone/vivado`
3. **Project type:** RTL Project · uncheck "Do not specify sources" → **Next**.
4. Skip adding sources → **Next** → **Next**.
5. On the **Default Part** screen click the **Boards** tab.
6. Search for `kv260` and select **Kria KV260 Vision AI Starter Kit** → **Next** → **Finish**.

### 2.2  Add the HLS IP to the IP catalog

1. In the **Flow Navigator** click **IP Catalog**.
2. Right-click anywhere in the IP Catalog panel → **Add Repository**.
3. Browse to the HLS export folder from Step 1.9 (the folder that contains the `.zip`, or the unzipped IP folder).
4. Click **Select** — Vivado will show "Added 1 IP" in the console.
5. Confirm `mmult_accel` appears in the catalog under **User Repository**.

### 2.3  Create the block design

1. In **Flow Navigator** → **IP Integrator** → click **Create Block Design**.
2. **Design name:** `design_1` → **OK**.
3. The block design canvas opens.

### 2.4  Add and configure the Zynq UltraScale+ MPSoC

1. In the block design canvas, click the **+** icon (Add IP) and search for
   `Zynq UltraScale+ MPSoC`.
2. Double-click it to add it.
3. Click the green banner **"Run Block Automation"** that appears.
4. Select **Apply Board Preset** (this auto-configures DDR, MIO, clocks for KV260).
5. Click **OK**.

### 2.5  Add the mmult_accel HLS IP

1. Click **+** → search `mmult_accel` → double-click to add.
2. The IP block appears on the canvas with the following visible ports:
   - `s_axi_control` (AXI-Lite slave — register access)
   - `m_axi_gmem0` (AXI master — reads matrix A from DDR)
   - `m_axi_gmem1` (AXI master — reads matrix B from DDR)
   - `m_axi_gmem2` (AXI master — writes matrix C to DDR)
   - `interrupt` (AP_DONE interrupt output)
   - `ap_clk`, `ap_rst_n`

> **Note:** Port names depend on your HLS `#pragma HLS INTERFACE` directives.
> Adjust the connection steps below if your port names differ.

### 2.6  Add AXI SmartConnects

You need one AXI SmartConnect for the control path (PS → IP AXI-Lite)
and one for the data path (IP AXI master → PS DDR).

**Control SmartConnect (PS → mmult_accel s_axi_control):**

1. Click **+** → search `AXI SmartConnect` → add one instance.
2. Double-click it → set **Number of Slave Interfaces:** `1`
   and **Number of Master Interfaces:** `1` → **OK**.
3. Name it `axi_smc_ctrl` (right-click → Rename).

**Data SmartConnect (mmult_accel m_axi ports → PS DDR):**

1. Add another **AXI SmartConnect**.
2. Double-click → set **Number of Slave Interfaces:** `3`
   (one per `gmem` port) and **Number of Master Interfaces:** `1` → **OK**.
3. Name it `axi_smc_data`.

### 2.7  Add a Processor System Reset

1. Click **+** → search `Processor System Reset` → add.
2. Click **"Run Connection Automation"** if the banner appears,
   connect it to the `pl_clk0` from the Zynq PS.

### 2.8  Connect the block design

Make the following connections (drag-and-drop between ports, or right-click → Make Connection):

| From | To | Notes |
|------|----|-------|
| Zynq PS `M_AXI_HPM0_FPD` | `axi_smc_ctrl` S00_AXI | PS initiates register writes |
| `axi_smc_ctrl` M00_AXI | mmult_accel `s_axi_control` | AXI-Lite control |
| mmult_accel `m_axi_gmem0` | `axi_smc_data` S00_AXI | DMA read (matrix A) |
| mmult_accel `m_axi_gmem1` | `axi_smc_data` S01_AXI | DMA read (matrix B) |
| mmult_accel `m_axi_gmem2` | `axi_smc_data` S02_AXI | DMA write (matrix C) |
| `axi_smc_data` M00_AXI | Zynq PS `S_AXI_HP0_FPD` | High-performance DDR port |
| Zynq PS `pl_clk0` | mmult_accel `ap_clk` | PL clock |
| Zynq PS `pl_clk0` | both SmartConnects `aclk` | Same clock domain |
| Proc System Reset `peripheral_aresetn` | mmult_accel `ap_rst_n` | Active-low reset |
| Proc System Reset `peripheral_aresetn` | both SmartConnects `aresetn` | |

### 2.9  Connect the interrupt

1. Click **+** → search `Concat` → add one `xlconcat` block.
2. Double-click → set **Number of Ports:** `1` → **OK**.
3. Connect: mmult_accel `interrupt` → `xlconcat` `In0`.
4. Connect: `xlconcat` `dout` → Zynq PS `pl_ps_irq0`.

This routes the AP_DONE interrupt from the HLS IP to the ARM PS interrupt controller,
which PYNQ's `interrupt.wait()` listens for.

### 2.10  Assign the AXI-Lite base address

1. Click the **Address Editor** tab (next to Diagram).
2. Find `mmult_accel` under the `M_AXI_HPM0_FPD` master.
3. Assign the offset address (e.g. `0xA000_0000`) and a 64 KB range.
4. Note this address — you may need it if writing bare-metal code,
   but PYNQ discovers it automatically from the `.hwh` file.

### 2.11  Validate the block design

1. Click **Validate Design** (the tick icon in the toolbar) or press **F6**.
2. Fix any critical errors before continuing.
3. Warnings about unconnected debug ports are typically safe to ignore.

### 2.12  Generate the HDL wrapper

1. In the **Sources** panel, right-click `design_1.bd` →
   **Create HDL Wrapper** → **Let Vivado manage wrapper** → **OK**.
2. `design_1_wrapper.v` appears as the top-level.

### 2.13  Run synthesis, implementation, and generate bitstream

1. In **Flow Navigator** click **Generate Bitstream**.
2. When prompted to run synthesis and implementation first, click **Yes**.
3. This takes **20–60 minutes** depending on your PC.
4. On completion click **Open Implemented Design** to inspect timing/resource reports.
5. Confirm:
   - **Timing:** Worst Negative Slack (WNS) ≥ 0 ns (timing closed).
   - **Utilization:** LUT, BRAM, DSP within KV260 capacity.

### 2.14  Export the hardware files

Two files are needed by PYNQ:

**Bitstream (`.bit`):**
```
<vivado_project>/kv260_mmult.runs/impl_1/design_1_wrapper.bit
```

**Hardware Hand-off (`.hwh`) — PYNQ uses this to auto-discover IP and addresses:**
```
<vivado_project>/kv260_mmult.gen/sources_1/bd/design_1/hw_handoff/design_1.hwh
```

Rename both files to the same stem so PYNQ can pair them:
```
design_1_wrapper.bit  →  design.bit
design_1.hwh          →  design.hwh
```

---

## Step 3 — Transfer Files to the KV260

### 3.1  Confirm board IP address

Connect the KV260 to your router via Ethernet.
Find the assigned IP from your router's DHCP list, or use:
```bash
# On Windows (host PC)
arp -a | findstr "dc-a6"    # Xilinx MAC prefix
```
Or connect via USB-UART (115200 baud) and check `ip addr`.

### 3.2  Transfer via SCP (Linux / macOS host)

```bash
BOARD=xilinx@<board-ip>

# Hardware files
scp design.bit   $BOARD:/home/xilinx/design.bit
scp design.hwh   $BOARD:/home/xilinx/design.hwh

# Python pipeline files
scp ping_pong_buffer.py  $BOARD:/home/xilinx/ping_pong_buffer.py
scp async_scheduler.py   $BOARD:/home/xilinx/async_scheduler.py
scp task_partitioning.py $BOARD:/home/xilinx/task_partitioning.py
```

### 3.3  Transfer via WinSCP (Windows host)

1. Open **WinSCP** → New Session.
2. **File protocol:** SCP · **Host:** `<board-ip>` · **Port:** 22
3. **Username:** `xilinx` · **Password:** `xilinx`
4. Drag and drop the six files listed above into `/home/xilinx/`.

### 3.4  Expected directory on KV260

```
/home/xilinx/
├── design.bit            ← bitstream
├── design.hwh            ← hardware hand-off
├── ping_pong_buffer.py
├── async_scheduler.py
└── task_partitioning.py
```

---

## Step 4 — Update `async_scheduler.py` for Real Hardware

Before running on KV260, open `async_scheduler.py` and update `_real_fpga_kernel()`
with the exact AXI-Lite offsets from the Vitis HLS synthesis report (Step 1.8).

The function must:
1. Call `.flush()` on input CMA buffers (writeback CPU cache → DDR).
2. Write physical addresses of A, B, C buffers to their AXI-Lite registers.
3. Write scalar parameters N, K, M, update_A.
4. Assert AP_START by writing `0x01` to offset `0x00`.

The interrupt worker (`real_interrupt_worker`) is already correct — it calls
`_my_interrupt.wait()` which blocks until the AP_DONE IRQ fires.

Also update `ping_pong_buffer.py` if your buffer shapes differ:
```python
BUFFER_SHAPE = (64, 768)   # matrix A: N×K rows of int8
BUFFER_DTYPE = "int8"
```
Add a separate weight buffer for matrix B (fixed across inference calls):
```python
# In init_kv260_buffers(), after allocating input/output buffers:
from pynq import allocate
weight_buffer = allocate(shape=(768, 768), dtype='int8')  # matrix B: K×M
```

---

## Step 5 — Run on KV260

### 5.1  SSH into the board

```bash
ssh xilinx@<board-ip>
# password: xilinx
```

### 5.2  Verify PYNQ is available

```bash
python3 -c "import pynq; print(pynq.__version__)"
# Expected: 3.0.1
```

### 5.3  Load the overlay and run the pipeline

```python
# run_kv260.py  — create this file on the board or paste into python3 REPL
import numpy as np
from pynq import Overlay
from task_partitioning import run_pipeline

# Load bitstream into the FPGA programmable logic
overlay = Overlay("/home/xilinx/design.bit")
print("Overlay loaded:", overlay.ip_dict.keys())

# Prepare 4 input batches: each is a 64×768 INT8 matrix
data = [
    np.random.randint(-128, 127, size=(64, 768), dtype='int8')
    for _ in range(4)
]

# Execute the ping-pong pipeline on real hardware
run_pipeline(data, sim=False, overlay=overlay)
```

Run it:
```bash
cd /home/xilinx
python3 run_kv260.py
```

### 5.4  Expected console output

```
Overlay loaded: dict_keys(['mmult_accel_0', ...])
[t=0.000s] Pipeline START - 4 tasks, KV260 hardware mode

[t=0.003s] [Stage 1 | Pre]   task=1  buffer=0  → dispatching to FPGA
[t=0.004s] [Stage 1 | Pre]   task=2  buffer=1  → dispatching to FPGA
[t=0.XXXs] [Stage 1 | Post]  buffer=0  result=<np.ndarray shape=(64,768) dtype=int32>
[t=0.XXXs] [Stage 1 | Pre]   task=3  buffer=0  → dispatching to FPGA
...
[t=X.XXXs] Pipeline DONE - 4/4 tasks completed
```

---

## Step 6 — Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `Overlay()` raises `RuntimeError: No bitstream` | `.bit` and `.hwh` filenames don't match | Rename both to `design.bit` / `design.hwh` |
| `KeyError: 'mmult_accel_0'` in `overlay.ip_dict` | IP name mismatch | Check `overlay.ip_dict` and update `init_kv260_scheduler()` in `async_scheduler.py` |
| Pipeline hangs after dispatching task 1 | Interrupt not wired or wrong offset | Confirm `xlconcat → pl_ps_irq0` connection in Vivado; check `interrupt` port name |
| Wrong results in output buffer | Cache not flushed / invalidated | Confirm `.flush()` before AP_START and `.invalidate()` after AP_DONE in `_real_fpga_kernel` / `real_interrupt_worker` |
| Timing not closed in Vivado (WNS < 0) | Clock too fast for the design | Increase HLS clock period to 15 ns (66 MHz) and re-synthesize |
| `pynq.allocate` fails | PYNQ image not booted from SD card | Ensure KV260 boot mode DIP switches are set to SD card (SW1: 0110) |

---

## Quick Reference — File Checklist

| File | Created in | Destination on KV260 |
|------|-----------|----------------------|
| `design.bit` | Vivado Step 2.13 | `/home/xilinx/design.bit` |
| `design.hwh` | Vivado Step 2.14 | `/home/xilinx/design.hwh` |
| `ping_pong_buffer.py` | This repo | `/home/xilinx/ping_pong_buffer.py` |
| `async_scheduler.py` | This repo (update offsets first) | `/home/xilinx/async_scheduler.py` |
| `task_partitioning.py` | This repo | `/home/xilinx/task_partitioning.py` |

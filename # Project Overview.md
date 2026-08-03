A portable, battery-powered camera system to record amateur Australian Rules Football (AFL) matches autonomously. The system mounts on a 3m telescopic pole or grandstand, 5m off the boundary line, capturing end-to-end action across the oval. Recorded footage is processed post-match to stitch and track the play for team analysis, with future potential for live streaming and productisation.
# Problem Statement

- **Field Coverage:** AFL ovals have no fixed dimensions, ranging from 135–185 metres long and 110–155 metres wide. A single standard camera lens set 5m from the boundary cannot capture both goal squares and the far wing without significant field-of-view limits.

- **Tracking Accuracy:** The ball in AFL is small, moves quickly, and is frequently obscured by player clusters or the sky. Physical gimbals lag behind fast play, introduce mechanical points of failure, and consume significant battery power.

- **Edge Compute Bottlenecks:** Real-time stitching and tracking of multiple high-resolution video streams on field-deployed hardware creates severe computational, thermal, and storage bandwidth bottlenecks.

- **Cost:** Commercial automatic sports cameras rely on high hardware costs and mandatory subscription fees. The target build cost for this custom system is under $1,000 AUD, with an ideal spend of $500 AUD.

# Explored Ideas and Alternative Approaches

- **Raspberry Pi with 3x 4K Modules:** Rejected. Single-board computers like the Raspberry Pi lack dedicated hardware encoders for multi-stream 4K video, leading to dropped frames, CPU overload, and extreme storage write rates.

- **Mechanical Gimbal Tracking System:** Rejected. Physical panning introduces mechanical lag, mechanical failure risks, and higher power consumption compared to digital cropping from a static panoramic view.

- **All-in-One 360 Action Camera:** Evaluated as a simple alternative, but discarded for the primary path due to lens fisheye distortion, lower optical pixel density at the far boundary, and higher unit hardware costs.

- **NVIDIA Jetson (TX2 / Orin NX) with MIPI or GMSL2 Cameras:** Capable of multi-4K hardware encoding and sub-millisecond sensor synchronization, but specialised carrier boards and industrial cables push total costs between $1,500 and $3,000 AUD.

- **Raspberry Pi 5 with Dual 1080p IMX219 Modules:** Fits within budget ($400 AUD), but strictly limited to 1080p resolution due to software encoding constraints on the Pi CPU.


# Current MVP Strategy

- **Compute:** Intel NUC 7 (i3-7100U, 8GB RAM) running Ubuntu 22.04 LTS. The integrated Intel hardware should hopedfully help handle video pipeline overhead

- **Capture Hardware:** Two Sony IMX415 USB 2.0 camera modules featuring 95-degree lenses, onboard MJPEG hardware compression, digital microphones, and 3m USB cables. Combined, they provide the necessary wide-angle coverage of the oval with central overlap for stitching.

- **Storage:** Internal 1TB NVMe SSD directly mounted in the NUC to support sustained write speeds for full-game recordings.

- **Power System:** Direct 12V LiFePO4 battery connected to the NUC DC barrel jack, eliminating heavy step-down voltage regulators.

- **Housing and Deployment:** Custom 3D printed housing elevated on a 3m pole or grandstand rail, positioned 5m from the boundary line.

- **Workflow:** Capture raw dual wide-angle feeds to storage during the game without live edge processing. Offload footage post-match to a desktop system to perform stitching, player-density tracking, and reframing.
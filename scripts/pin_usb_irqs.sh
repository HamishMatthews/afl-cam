#!/bin/bash
# Move the USB3 host controller interrupts off CPU0 (an A55 that also takes
# everything else) onto the A76 cores 4 and 5, alternating, so the two 4K
# camera streams do not queue behind other work. Not persistent across a
# reboot, so deploy/systemd/afl-cam-irq.service runs it at boot.
set -u
cpu=4
grep -E 'xhci|xhc' /proc/interrupts | while read -r irq rest; do
    irq=${irq%:}
    mask=$(printf '%x' $((1 << cpu)))
    if echo "$mask" > "/proc/irq/$irq/smp_affinity" 2>/dev/null; then
        echo "irq $irq -> cpu$cpu"
    else
        echo "irq $irq: could not set affinity" >&2
    fi
    cpu=$((cpu == 4 ? 5 : 4))
done

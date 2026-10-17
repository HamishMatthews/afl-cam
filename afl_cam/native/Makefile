# cam2stitch, cam2h264 and the stitch shader. `make` builds all three; the
# daemon calls `make cam2stitch` / `make cam2h264` on demand (config.py).
# WARN is -Wall -Wextra. The field build keeps warnings non-fatal (a newer
# compiler on the board must not stop the daemon building); the rig test
# (`make warnings`) fails on any warning.
CC       ?= gcc
MPP_INC  ?= /usr/local/include/rockchip
MPP_LIB  ?= /usr/local/lib
WARN     ?= -Wall -Wextra
WERROR   ?=
CFLAGS   ?= -O2
ALLFLAGS  = $(CFLAGS) $(WARN) $(WERROR) -I$(MPP_INC)

all: cam2stitch cam2h264 stitch_nv12.spv

cam2stitch: cam2stitch.c
	$(CC) $(ALLFLAGS) -o $@ $< -L$(MPP_LIB) -lrockchip_mpp -lvulkan -lpthread -lm

cam2h264: cam2h264.c
	$(CC) $(ALLFLAGS) -o $@ $< -L$(MPP_LIB) -lrockchip_mpp -lpthread

# glslangValidator output is byte-reproducible; CI rebuilds it and diffs
stitch_nv12.spv: stitch_nv12.comp
	glslangValidator -V $< -o $@

# compile only (so the optimiser's flow warnings show too), keep no binaries
warnings:
	@for f in cam2stitch cam2h264; do \
	  echo "== $$f"; \
	  $(CC) $(CFLAGS) $(WARN) -I$(MPP_INC) -c -o /dev/null $$f.c 2>&1; \
	done

.PHONY: all warnings

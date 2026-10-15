#!/usr/bin/env bash
TREE=$1; cd $TREE
gcc -O1 -o probe/xuread probe/xuread.c && for v in /dev/video0 /dev/video2; do echo "== $v"; probe/xuread $v; done

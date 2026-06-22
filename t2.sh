#!/bin/sh
i=0
while kill -0 454 2>/dev/null && [ $i -lt 100 ]; do sleep 0.01; i=$((i+1)); done
echo "exited loop at i=$i"

#!/bin/bash
pip install --upgrade pip
apt-get update && apt-get install -y ffmpeg libjpeg-dev zlib1g-dev
pip install --no-cache-dir -r gereksinimler.txt

#!/bin/bash

bash /nfs/volume-902-16/tangwenbo/ofs-1.sh

cd /nfs/ofs-902-1/object-detection/jiangjing/experiments/ && CUDA_VISIBLE_DEVICES=0 /home/luban/apps/miniconda/miniconda/envs/torch1101/bin/python -m CEPC.src.MainThread \
  --cmd da_m_mine1 \
  --itr 1 \
  --model_path /nfs/ofs-902-1/object-detection/jiangjing/experiments/MS-MMDA/pretrain_model/bert-base-uncased/ \
  --data_path /nfs/ofs-902-1/object-detection/jiangjing/experiments/CEPC/sandoogh/crisis.dataset \
  --output_dir /nfs/ofs-902-1/object-detection/jiangjing/experiments/CEPC/output \
  --device 0 \
  --seed 666

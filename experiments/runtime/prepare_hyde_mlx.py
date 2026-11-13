"""Assemble an mlx-lm model directory from Ollama's own qwen3.5:4b-mlx blobs (read-only), so the MLX
row runs the SAME weights Ollama serves. Ollama stores one safetensors file per tensor and names an
nvfp4 scale `<w>.weight.scale`; mlx-lm wants `<w>.scales`.
  prepare_hyde_mlx.py <out_dir>
"""
import json, shutil, sys
from collections import Counter
from pathlib import Path

import mlx.core as mx

STORE = Path("/Volumes/Disk Pro 2 1TB/ollama-models")
MAN = STORE / "manifests/registry.ollama.ai/library/qwen3.5/4b-mlx"
out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
blob = lambda d: STORE / "blobs" / d.replace(":", "-")

layers = json.loads(MAN.read_text())["layers"]
weights, suffixes, meta = {}, Counter(), Counter()
for l in layers:
    name = l.get("name", "")
    if l["mediaType"] == "application/vnd.ollama.image.json":
        shutil.copyfile(blob(l["digest"]), out / name)
    elif l["mediaType"] == "application/vnd.ollama.image.tensor":
        if name.startswith("vision_tower"):
            continue
        t, md = mx.load(str(blob(l["digest"])), format="safetensors", return_metadata=True)
        meta[json.dumps(md, sort_keys=True)] += 1
        for k, v in t.items():
            if k.endswith(".weight.scale"):
                k = k[: -len(".weight.scale")] + ".scales"; suffixes["scale→scales"] += 1
            elif k.endswith(".weight.bias"):
                k = k[: -len(".weight.bias")] + ".biases"; suffixes["bias→biases"] += 1
            weights[k] = v
print("tensor metadata variants:", dict(meta))
print("renames:", dict(suffixes), "tensors:", len(weights))
cfg = json.loads((out / "config.json").read_text())
cfg["quantization"] = {"group_size": 16, "bits": 4, "mode": "nvfp4"}
(out / "config.json").write_text(json.dumps(cfg, indent=2))
mx.save_safetensors(str(out / "model.safetensors"), weights, metadata={"format": "mlx"})
print("wrote", out)

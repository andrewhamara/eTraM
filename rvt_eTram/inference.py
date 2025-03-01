#!/usr/bin/env python3
import os
from pathlib import Path
import torch
from torch.backends import cuda, cudnn
from models.detection.yolox.utils.boxes import postprocess
from data.utils.representations import StackedHistogram


# env settings
os.environ['CUDA_DEVICE_ORDER'] = 'PCI_BUS_ID'
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"] = "1"

# faster matmul
cuda.matmul.allow_tf32 = True
cudnn.allow_tf32 = True
torch.multiprocessing.set_sharing_strategy('file_system')

import hydra
from hydra import initialize, compose
from omegaconf import DictConfig, OmegaConf
from modules.utils.fetch import fetch_model_module


# yolo prediction classes
classes = {
    0: "pedestrian",
    1: "vehicle",
    2: "micromobility"
}


# decode bytes to tensors
def decode_event_bytes(event_bytes):
    dtype = torch.int32
    num_events = len(event_bytes) // 16

    events = torch.frombuffer(event_bytes, dtype=dtype).reshape(num_events, 4)

    x = events[:, 0]
    y = events[:, 1]
    p = events[:, 2]
    t = events[:, 3]

    return x.cuda(), y.cuda(), p.cuda(), t.cuda()


def main(event_bytes):

    # setup
    initialize(config_path="config/model", version_base=None)
    config = compose(config_name="rnndet")
    in_res_hw = tuple(config.model.backbone.in_res_hw)  # Example: (720, 1280)
    ckpt_path = Path(config.checkpoint)
    module = fetch_model_module(config=config)
    module = module.load_from_checkpoint(str(ckpt_path), **{'full_config': config})
    module = module.to('cuda')
    module.eval()

    # bytes to tensors
    x, y, p, t = decode_event_bytes(event_bytes)

    # sort events by time
    i = torch.argsort(t)
    x, y, p, t = x[i], y[i], p[i], t[i]

    # tensors to histogram
    histogram = StackedHistogram(bins=10, height=720, width=1280)
    histogram_rep = histogram.construct(x=x, y=y, pol=p, time=t)
    histogram_rep = histogram_rep.to(torch.float32)
    histogram_rep = histogram_rep.unsqueeze(0)

    # model stuff
    num_classes = config.model.head.num_classes
    confidence_threshold = config.model.postprocess.confidence_threshold
    nms_threshold = config.model.postprocess.nms_threshold

    # init variables
    output = None
    predictions = None
    hidden_states = None

    # forward pass
    with torch.inference_mode():
        output, hidden_states, _ = module(
            event_tensor=histogram_rep,
            previous_states=hidden_states,
            retrieve_detections=True
        )

        class_logits = output[:, :, 5:]
        print(class_logits.shape)

        class_probabilities = torch.softmax(class_logits, dim=-1)
        predicted_classes = torch.argmax(class_probabilities, dim=-1)


        predictions = postprocess(prediction=output,
                                  num_classes=num_classes,
                                  #conf_thre=confidence_threshold,
                                  conf_thre=.001,
                                  nms_thre=nms_threshold)

    for batch_idx, pred in enumerate(predictions):
        print(f"batch {batch_idx} predictions:")
        if pred is not None:
            for i, box in enumerate(pred): 
                class_idx = predicted_classes[batch_idx][i].item()
                class_label = classes.get(class_idx, "unknown")
                print(class_label)
        else:
            print("No predictions found.")

if __name__ == '__main__':
    dummy_event_bytes = b'\x00' * 2000
    main(dummy_event_bytes)

import os
import torch
import colossalai
import torch.distributed as dist
from mmengine.runner import set_random_seed

from openvid.datasets import save_sample
from openvid.registry import MODELS, SCHEDULERS, build_module
from openvid.utils.config_utils import parse_configs
from openvid.utils.misc import to_torch_dtype
from openvid.acceleration.parallel_states import set_sequence_parallel_group
from colossalai.cluster import DistCoordinator


def load_prompts(prompt_path, start_idx, end_idx):
    with open(prompt_path, "r") as f:
        prompts = [line.strip() for line in f.readlines()]
    if start_idx is not None and end_idx is not None:
        return prompts[start_idx:end_idx]
    return prompts


def main():
    # ======================================================
    # 1. cfg and init distributed env
    # ======================================================
    cfg = parse_configs(training=False)
    print(cfg)
    enable_sequence_parallelism = False

    # ======================================================
    # 2. runtime variables
    # ======================================================
    torch.set_grad_enabled(False)
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = to_torch_dtype(cfg.dtype)
    set_random_seed(seed=cfg.seed)
    prompts = load_prompts(cfg.prompt_path, cfg.start_idx, cfg.end_idx)

    # ======================================================
    # 3. build model & load weights
    # ======================================================
    # 3.1. build model
    #input_size = (cfg.num_frames, *cfg.image_size)
    input_size = (cfg.num_frames, cfg.image_size[0], cfg.image_size[1])
    vae = build_module(cfg.vae, MODELS)
    latent_size = vae.get_latent_size(input_size)
    text_encoder = build_module(cfg.text_encoder, MODELS, device=device)  # T5 must be fp32
    model = build_module(
        cfg.model,
        MODELS,
        input_size=latent_size,
        in_channels=vae.out_channels,
        caption_channels=text_encoder.output_dim,
        model_max_length=text_encoder.model_max_length,
        dtype=dtype,
        enable_sequence_parallelism=enable_sequence_parallelism,
    )
    text_encoder.y_embedder = model.y_embedder  # hack for classifier-free guidance

    # 3.2. move to device & eval
    vae = vae.to(device, dtype).eval()
    model = model.to(device, dtype).eval()

    # 3.3. build scheduler
    scheduler = build_module(cfg.scheduler, SCHEDULERS)

    # 3.4. support for multi-resolution
    model_args = dict()
    if cfg.multi_resolution:
        image_size = cfg.image_size
        hw = torch.tensor([image_size], device=device, dtype=dtype).repeat(cfg.batch_size, 1)
        ar = torch.tensor([[image_size[0] / image_size[1]]], device=device, dtype=dtype).repeat(cfg.batch_size, 1)
        model_args["data_info"] = dict(ar=ar, hw=hw)

    # ======================================================
    # 4. inference
    # ======================================================
    sample_idx = cfg.start_idx
    save_dir = cfg.save_dir
    os.makedirs(save_dir, exist_ok=True)
    for i in range(0, len(prompts), cfg.batch_size):
        batch_prompts = prompts[i : i + cfg.batch_size]
        samples = scheduler.sample(
            model,
            text_encoder,
            #z_size=(vae.out_channels, *latent_size),
            z_size=(vae.out_channels, latent_size[0], latent_size[1], latent_size[2]),
            prompts=batch_prompts,
            device=device,
            additional_args=model_args,
        )
        samples = vae.decode(samples.to(dtype))

        #if coordinator.is_master():
        for idx, sample in enumerate(samples):
            print("Prompt:", batch_prompts[idx])
            save_path = os.path.join(save_dir, "sample_"+str(sample_idx))
            save_sample(sample, fps=cfg.fps, save_path=save_path)
            sample_idx += 1


if __name__ == "__main__":
    main()

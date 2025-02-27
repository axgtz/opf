import pytorch_lightning as pl
import torch
from src.opf.utils import model_from_parameters
from glob import glob
import wandb
import os
from tqdm import tqdm

api = wandb.Api()
runs = api.runs("guti/DamOwerkoOPFGNN", filters={"tag": "foo"})
# runs = [api.run(f"damowerko/opf/{id}") for id in ["2zdzkf7r", "2ng1ftuc", "2lllzf30", "h3lsl9hr"]]
print(runs)

for run in tqdm(runs):
    param = run.config
    root_dir = "./"
    data_dir = os.path.join(root_dir, "data")
    log_dir = os.path.join(root_dir, "logs")
    logger = pl.loggers.WandbLogger(
        project="opf", save_dir=log_dir, reinit=True, resume="must", id=run.id
    )
    barrier, trainer, dm = model_from_parameters(param, logger=logger)

    files = list(glob(f"{log_dir}/opf/{run.id}/checkpoints/*.ckpt"))
    assert len(files) == 1
    checkpoint = torch.load(files[0], map_location=lambda storage, loc: storage)
    barrier.load_state_dict(checkpoint["state_dict"], strict=False)

    trainer.test(barrier, datamodule=dm, verbose=False)
    logger.finalize("finished")
    wandb.finish()

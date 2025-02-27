import wandb
import wandb.apis.public
from src.opf.modules import OPFLogBarrier
from src.opf.utils import create_model
import torch
import glob
import pytorch_lightning as pl
import pandas as pd
import tempfile
import glob
import os
from IPython.display import HTML
import io
import base64
import matplotlib.pyplot as plt


class CacheOutputs(pl.callbacks.Callback):
    def on_test_epoch_start(self, trainer, module):
        self.outputs = []

    def on_test_batch_end(
        self, trainer, pl_module, outputs, batch, batch_idx, dataloader_idx
    ):
        self.outputs.append(outputs)


def load_model(dm, id: str):
    api = wandb.Api()
    run: wandb.apis.public.Run = api.run(f"guti/DamOwerkoOPFGNN/{id}")

    # get the weights
    checkpoint_artifacts = list(
        filter(lambda a: a.type == "model", run.logged_artifacts())
    )
    assert len(checkpoint_artifacts) <= 1
    with tempfile.TemporaryDirectory() as dir:
        checkpoint_artifacts[0].download(dir)
        print(os.listdir(dir))
        files = list(glob.glob(os.path.join(dir, "*.ckpt")))
        if len(files) == 1:
            raise RuntimeError(f"Expected one file found {len(files)}:\n {files}")
        model = create_model(dm, run.config)
        model.load_state_dict(torch.load(files[0]), strict=False)
        return model


def load_checkpoint(barrier: OPFLogBarrier, id: str, log_dir: str):
    checkpoint_directory = f"{log_dir}opf/{id}/checkpoints/"
    files = list(glob.glob(checkpoint_directory + "*.ckpt"))
    assert len(files) == 1
    checkpoint = torch.load(files[0], map_location="cpu")
    barrier.load_state_dict(checkpoint["state_dict"], strict=False)


def test(barrier, dm):
    cache = CacheOutputs()
    trainer = pl.Trainer(precision=64, callbacks=[cache], logger=False, gpus=0)
    trainer.test(barrier, datamodule=dm, verbose=False)
    return pd.DataFrame(cache.outputs).applymap(torch.Tensor.item)


class FlowLayout(object):
    """A class / object to display plots in a horizontal / flow layout below a cell"""

    def __init__(self):
        # string buffer for the HTML: initially some CSS; images to be appended
        self.sHtml = """
        <style>
        .floating-box {
        display: inline-block;
        margin: 10px;
        border: 3px solid #888888;  
        }
        </style>
        """

    def add_plot(self, oAxes):
        """Saves a PNG representation of a Matplotlib Axes object"""
        Bio = io.BytesIO()  # bytes buffer for the plot
        fig = oAxes.get_figure()
        fig.canvas.print_png(Bio)  # make a png of the plot in the buffer

        # encode the bytes as string using base 64
        sB64Img = base64.b64encode(Bio.getvalue()).decode()
        self.sHtml += (
            '<div class="floating-box">'
            + '<img src="data:image/png;base64,{}\n">'.format(sB64Img)
            + "</div>"
        )

    def all_open(self):
        for i in plt.get_fignums():
            fig = plt.figure(i)
            self.add_plot(fig)
            plt.close(i)
        return self
            
    def _repr_html_(self):
        return self.sHtml

    def PassHtmlToCell(self):
        """Final step - display the accumulated HTML"""
        display(HTML(self.sHtml))

#!/usr/bin/env python3

import logging

import torch
import torch.nn.functional as F
from torch import nn
from torch.autograd import Variable

from .aaronvanderood_vqvae.vqvaeimpl import VectorQuantizer, VectorQuantizerEMA

# Implementation from https://github.com/ritheshkumar95/pytorch-vqvae

logger = logging.getLogger(__name__)

class VQVAE(nn.Module):
    def __init__(self, LATENT_SPACE_DIM=320, K=512, decay=0.0):
        super().__init__()
        if decay:
            assert not "Not supported."
        else:
            self.codebook_bottom = VectorQuantizer(K, 128)
            self.codebook_top = VectorQuantizer(K, 32)

        # Top
        self.fc_e1 = nn.Conv2d( 3,   8, 9, stride=2)
        self.fc_e2 = nn.Conv2d( 8,  16, 9, stride=2)
        self.fc_e3 = nn.Conv2d(16,  32, 5, stride=2)

        # Bottom
        self.fc_e4 = nn.Conv2d(32,  64, 5, stride=2)
        self.fc_e5 = nn.Conv2d(64, 128, 3, stride=1)

        # Bottom
        self.fc_d2 = nn.ConvTranspose2d(128, 64, 3, stride=1)
        self.fc_d3 = nn.ConvTranspose2d(64, 32, 5, stride=2)

        self.fc_d3_x_top = nn.ConvTranspose2d(32,  32, 1, stride=1)
        self.fc_d3_z = nn.ConvTranspose2d(32,  32, 1, stride=1)

        # Top
        self.fc_d4 = nn.ConvTranspose2d(32, 16, 5, stride=2)
        self.fc_d5 = nn.ConvTranspose2d(16,  8, 9, stride=2)
        self.fc_d6 = nn.ConvTranspose2d( 8,  3, 9, stride=2)

    def encode(self, x):
        assert list(x.size())[1:] == [3, 240, 320]
        x = F.relu(self.fc_e1(x))
        x = F.relu(self.fc_e2(x))
        x = F.relu(self.fc_e3(x))
        x_top = x

        x = F.relu(self.fc_e4(x))
        x = F.relu(self.fc_e5(x))
        x_bottom = x

        return x_top, x_bottom

    def decode(self, x_top, x_bottom):
        nb = x_bottom.size()[0]
        z_bottom = x_bottom
        z_bottom_quantized, perplexity_bottom = self.codebook_bottom(x_bottom)

        z = z_bottom_quantized
        z = F.relu(self.fc_d2(z, output_size=[nb, 64, 11, 16]))
        z = F.relu(self.fc_d3(z, output_size=[nb, 32, 25, 35]))

        # Combine
        z_top = z + self.fc_d3_x_top(x_top, output_size=[nb, 32, 25, 35])
        z_top_quantized, perplexity_top = self.codebook_top(z_top)
        z = z_top_quantized + self.fc_d3_z(z, output_size=[nb, 32, 25, 35])

        z = F.relu(self.fc_d4(z, output_size=[nb, 16, 54, 74]))
        z = F.relu(self.fc_d5(z, output_size=[nb,  8, 116, 156]))
        z = torch.sigmoid(self.fc_d6(z, output_size=[nb,  3, 240, 320]))
        return z, z_bottom, z_bottom_quantized, z_top, z_top_quantized

    def forward(self, inp):
        x_top, x_bottom = self.encode(inp)
        x_recon, z_bottom, z_bottom_quantized, z_top, z_top_quantized = self.decode(x_top, x_bottom)

        return x_recon, x_top, z_top, z_top_quantized, x_bottom, z_bottom, z_bottom_quantized

# model is a torch.nn.Module that contains the model definition.
global model, BETA, COMMITMENT_COST
model = None
BETA = 1.0
COMMITMENT_COST = 1.0
DATA_VARIANCE = 0.00025 # This is the general range for our datasets

# Use MSE loss as distance from input to output:
def loss(Ypred, Yactual, X):
    """loss function for learning problem

    Arguments:
        Ypred {Model output type} -- predicted output
        Yactual {Data output type} -- output from data
        X {torch.Variable[input]} -- input

    Returns:
        Tuple[nn.Variable] -- Parts of the loss function; the first element is passed to the optimizer
        nn.Variable -- the loss to optimize
    """

    x_recon, x_top, z_top, z_top_quantized, x_bottom, z_bottom, z_bottom_quantized = Ypred

    #data_variance = (torch.var(X) / X.size(0)).detach()
    #logger.info(f"Batch variance {data_variance}")

    # Loss
    e_top_latent_loss = torch.mean((z_top_quantized.detach() - z_top)**2)
    q_top_latent_loss = torch.mean((z_top_quantized - z_top.detach())**2)

    e_bottom_latent_loss = torch.mean((z_bottom_quantized.detach() - z_bottom)**2)
    q_bottom_latent_loss = torch.mean((z_bottom_quantized - z_bottom.detach())**2)

    recon_loss = torch.mean((x_recon - Yactual)**2)/DATA_VARIANCE

    loss = recon_loss + q_top_latent_loss + q_bottom_latent_loss + COMMITMENT_COST * (e_top_latent_loss + e_bottom_latent_loss)

    return (loss, recon_loss, e_top_latent_loss, e_bottom_latent_loss, q_top_latent_loss, q_bottom_latent_loss)

def loss_flatten(x):
    return x

def loss_labels():
    return ("loss", "recon_loss", "e_top_latent_loss", "e_bottom_latent_loss", "q_top_latent_loss", "q_bottom_latent_loss")

global last_epoch
last_epoch = -1
def summary(epoch, summarywriter, Ypred, X):
    global last_epoch
    if epoch <= last_epoch:
        return
    last_epoch = epoch

    x_recon, x_top, z_top, z_top_quantized, x_bottom, z_bottom, z_bottom_quantized = Ypred
    # summarywriter.add_embedding(z.data, label_img=X.data, global_step=epoch, tag="learned_embedding")
    summarywriter.add_images("reconstructed", x_recon, global_step=epoch)

    Xrandstd = X.std().detach().item()
    Xrand = torch.normal(mean=torch.zeros_like(X), std=Xrandstd)
    Ypredrand = model(Xrand.to(Ypred[0]))
    summarywriter.add_images("random", Ypredrand[0], global_step=epoch)

def configure(props):
    global model, COMMITMENT_COST

    # K must be the same as the number of channels in the image.
    K = props["codebook_size"] if "codebook_size" in props else 512
    lsd = props["latent_space_dim"] if "latent_space_dim" in props else 128
    decay = props["ema_decay"] if "ema_decay" in props else 0
    COMMITMENT_COST = props["commitment_cost"] if "commitment_cost" in props else 1.0
    model = VQVAE(lsd, K, decay=decay)

    logger.info(f"Latent space is 1x{lsd}, codebook has {K} entries.")

    try:
        global BETA
        BETA = float(props["beta"])
    except KeyError:
        pass
    logger.info(f"BETA (commiment loss multiplier) {BETA}.")
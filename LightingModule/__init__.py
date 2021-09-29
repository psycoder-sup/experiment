# from LightingModule.VAE import VAE
from LightingModule.BaseClassifier import BaseClassifier
from LightingModule.NoiseInjection import NoiseInjection
from LightingModule.NoiseInj_resnet import NoiseInj_resnet
# from LightingModule.CVAE_mixup import CVAE
from LightingModule.NoiseDist import NoiseDist


model_dict = {
    # 'vae': VAE,
    'BaseClassifier': BaseClassifier,
    'NoiseInjection': NoiseInjection,
    "NoiseInj_resnet": NoiseInj_resnet,
    'NoiseDist': NoiseDist,
    # "CVAE_mixup": CVAE,
}
from typing import List, Any, Mapping, Dict

from torch import Tensor

import torch
import torch.nn as nn
# import torch.nn.functional as F

# from torchvision.models import mobilenet_v3_small, MobileNet_V3_Small_Weights
# from torchvision.models import mobilenet_v3_large, MobileNet_V3_Large_Weights

from .component import ConvNormActive, InvertedResidual, UpSample, CSENet
from .component import MobileNetFeatures, DinoFeatures
from .basic import Basic


class TrimNetX(Basic):
    '''A light-weight and easy-to-train model base the mobilenetv3

    Args:
        num_waves: Number of the global wave blocks.
        wave_depth: Depth of the wave block.
        backbone: Specify a basic model as a feature extraction module.
        backbone_pretrained: Whether to load backbone pretrained weights.
    '''

    def __init__(
            self,
            num_waves:           int=2,
            wave_depth:          int=3,
            backbone:            str='mobilenet_v3_small',
            backbone_pretrained: bool=True,
        ):

        super().__init__()

        self.num_waves  = num_waves
        self.wave_depth = wave_depth
        self.backbone   = backbone

        if backbone == 'mobilenet_v3_small':
            # features = mobilenet_v3_small(
            #     weights=MobileNet_V3_Small_Weights.DEFAULT
            #     if backbone_pretrained else None,
            # ).features
            #
            # self.features_dim = 48 + 96
            # self.merged_dim   = 160
            #
            # self.features_d = features[:9] # 48, 32, 32
            # self.features_u = features[9:-1] # 96, 16, 16
            self.features = MobileNetFeatures(
                backbone, backbone_pretrained)
            self.features_dim = self.features.features_dim
            self.merged_dim   = 160

        elif backbone == 'mobilenet_v3_large':
            # features = mobilenet_v3_large(
            #     weights=MobileNet_V3_Large_Weights.DEFAULT
            #     if backbone_pretrained else None,
            # ).features
            #
            # self.features_dim = 112 + 160
            # self.merged_dim   = 320
            #
            # self.features_d = features[:13] # 112, 32, 32
            # self.features_u = features[13:-1] # 160, 16, 16
            self.features = MobileNetFeatures(
                backbone, backbone_pretrained)
            self.features_dim = self.features.features_dim
            self.merged_dim   = 320

        elif backbone == 'dinov2_vits14':
            self.features     = DinoFeatures(backbone)
            self.features_dim = self.features.features_dim
            self.merged_dim   = self.features_dim

        else:
            raise ValueError(f'Unsupported backbone `{backbone}`')

        self.cell_size = self.features.cell_size

        self.merge = ConvNormActive(
            self.features_dim, self.merged_dim, 1, activation=None)

        expand_ratio = 2
        expanded_dim = self.merged_dim * expand_ratio

        self.cluster = nn.ModuleList()
        self.csenets = nn.ModuleList()
        self.normals = nn.ModuleList()
        for _ in range(num_waves):
            modules = []
            modules.append(InvertedResidual(
                self.merged_dim, expanded_dim, expand_ratio, stride=2))
            for r in range(wave_depth):
                modules.append(
                    InvertedResidual(
                        expanded_dim, expanded_dim, 1, dilation=2**r, activation=None))
            modules.append(nn.Sequential(
                UpSample(expanded_dim),
                ConvNormActive(expanded_dim, self.merged_dim, 1),
            ))
            self.cluster.append(nn.Sequential(*modules))
            self.csenets.append(CSENet(
                self.merged_dim * 2, self.merged_dim, kernel_size=3, shrink_factor=4))
            self.normals.append(nn.BatchNorm2d(self.merged_dim))

    def forward(self, x:Tensor) -> List[Tensor]:
        if not self._keep_features:
            # fd = self.features_d(x)
            # fu = self.features_u(fd)
            x = self.features(x)
        else:
            with torch.no_grad():
                # fd = self.features_d(x)
                # fu = self.features_u(fd)
                x = self.features(x)

        # x = self.merge(torch.cat([
        #     fd,
        #     F.interpolate(fu, scale_factor=2, mode='bilinear'),
        # ], dim=1))
        x = self.merge(x)
        fs = [x]
        for i, cluster_i in enumerate(self.cluster):
            x = x + self.csenets[i](torch.cat([x, cluster_i(x)], dim=1))
            x = self.normals[i](x)
            fs.append(x)
        return fs

    @classmethod
    def load_from_state(cls, state:Mapping[str, Any]) -> 'TrimNetX':
        hyps = state['hyperparameters']
        model = cls(
            num_waves           = hyps['num_waves'],
            wave_depth          = hyps['wave_depth'],
            backbone            = hyps['backbone'],
            backbone_pretrained = False,
        )
        model.load_state_dict(state['model'])
        return model

    def hyperparameters(self) -> Dict[str, Any]:
        return dict(
            num_waves  = self.num_waves,
            wave_depth = self.wave_depth,
            backbone   = self.backbone,
        )

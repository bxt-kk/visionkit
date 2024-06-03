from torch import Tensor
import torch.nn as nn
from torchvision.ops.misc import SqueezeExcitation


class BasicConvBD(nn.Sequential):

    def __init__(
            self,
            in_planes:   int,
            out_planes:  int,
            kernel_size: int=3,
            stride:      int | tuple[int, int]=1
        ):

        padding = (kernel_size - 1) // 2
        super().__init__(
            nn.Conv2d(in_planes, in_planes, kernel_size, stride, padding, groups=in_planes, bias=False),
            nn.BatchNorm2d(in_planes),
            nn.ReLU(inplace=True),
            nn.Conv2d(in_planes, out_planes, 1, bias=False),
            nn.BatchNorm2d(out_planes),
            nn.ReLU(inplace=True))


class LinearBasicConvBD(nn.Module):

    def __init__(
            self,
            in_planes:   int,
            out_planes:  int,
            kernel_size: int=3,
            dilation:    int=1,
            stride:      int | tuple[int, int]=1
        ):

        super().__init__()

        padding = (kernel_size + 2 * (dilation - 1) - 1) // 2
        self.layers = nn.Sequential(
            nn.Conv2d(
                in_planes, in_planes, kernel_size, stride, padding,
                dilation=dilation, groups=in_planes, bias=False),
            nn.BatchNorm2d(in_planes),
            nn.Conv2d(in_planes, out_planes, 1, bias=False),
            nn.BatchNorm2d(out_planes))

        self.use_res_connect = in_planes == out_planes

    def forward(self, x:Tensor) -> Tensor:
        result = self.layers(x)
        if self.use_res_connect:
            result = result + x
        return result



class BasicConvDB(nn.Sequential):

    def __init__(
            self,
            in_planes:   int,
            out_planes:  int,
            kernel_size: int=3,
            stride:      int | tuple[int, int]=1
        ):

        padding = (kernel_size - 1) // 2
        super().__init__(
            nn.Conv2d(in_planes, out_planes, 1, bias=False),
            nn.BatchNorm2d(out_planes),
            nn.Hardswish(inplace=True),
            nn.Conv2d(out_planes, out_planes, kernel_size, stride, padding, groups=out_planes, bias=False),
            nn.BatchNorm2d(out_planes),
            nn.Hardswish(inplace=True))


class LinearBasicConvDB(nn.Sequential):

    def __init__(
            self,
            in_planes:   int,
            out_planes:  int,
            kernel_size: int=3,
            dilation:    int=1,
            stride:      int | tuple[int, int]=1
        ):

        padding = (kernel_size + 2 * (dilation - 1) - 1) // 2
        super().__init__(
            nn.Conv2d(in_planes, out_planes, 1, bias=False),
            nn.BatchNorm2d(out_planes),
            nn.Conv2d(out_planes, out_planes, kernel_size, stride, padding,
                dilation=dilation, groups=out_planes, bias=False),
            nn.BatchNorm2d(out_planes))


class UpSample(nn.Sequential):

    def __init__(
            self,
            in_planes:  int,
            out_planes: int,
        ):

        super().__init__(
            nn.ConvTranspose2d(in_planes, in_planes, 3, 2, 1, output_padding=1, groups=in_planes, bias=False),
            nn.BatchNorm2d(in_planes),
            BasicConvDB(in_planes, out_planes, 3),
        )


class PixelShuffleSample(nn.Sequential):

    def __init__(
            self,
            in_planes:  int,
            out_planes: int,
        ):

        super().__init__(
            nn.Conv2d(in_planes, in_planes * 2, 1, bias=False),
            nn.PixelShuffle(2),
            nn.BatchNorm2d(in_planes // 2),
            BasicConvBD(in_planes // 2, out_planes, 3),
        )


class CSENet(nn.Module):

    def __init__(
            self,
            in_planes:     int,
            out_planes:    int,
            kernel_size:   int=3,
            shrink_factor: int=4,
        ):

        super().__init__()

        shrink_dim = in_planes // shrink_factor
        self.fusion = nn.Sequential(
            BasicConvDB(in_planes, shrink_dim, kernel_size),
            nn.Conv2d(shrink_dim, in_planes, 1, bias=False),
            nn.Hardsigmoid(inplace=True),
        )
        self.project = nn.Sequential(
            nn.Conv2d(in_planes, out_planes, 1, bias=False),
            nn.BatchNorm2d(out_planes),
        )

    def forward(self, x:Tensor) -> Tensor:
        return self.project(x * self.fusion(x))


class LocalSqueezeExcitation(nn.Module):

    def __init__(
            self,
            input_channels:   int,
            squeeze_channels: int,
        ):

        super().__init__()
        self.fc1 = nn.Conv2d(input_channels, squeeze_channels, 1)
        self.fc2 = nn.Conv2d(squeeze_channels, input_channels, 1)
        self.activation = nn.ReLU(inplace=True)
        self.scale_activation = nn.Hardsigmoid(inplace=True)

    @classmethod
    def load_from_se_module(
            cls,
            se_module:   SqueezeExcitation,
        ) -> 'LocalSqueezeExcitation':

        squeeze_channels, input_channels, _, _ = se_module.fc1.weight.shape
        lse_module = cls(input_channels, squeeze_channels)
        lse_module.fc1.load_state_dict(se_module.fc1.state_dict())
        lse_module.fc2.load_state_dict(se_module.fc2.state_dict())
        return lse_module

    def _scale(self, x:Tensor) -> Tensor:
        scale = self.fc1(x)
        scale = self.activation(scale)
        scale = self.fc2(scale)
        return self.scale_activation(scale)

    def forward(self, x:Tensor) -> Tensor:
        scale = self._scale(x)
        return scale * x

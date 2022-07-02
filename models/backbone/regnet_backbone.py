# Modified from
# https://github.com/facebookresearch/ClassyVision/blob/main/classy_vision/models/anynet.py
# https://github.com/facebookresearch/ClassyVision/blob/main/classy_vision/models/regnet.py


import math
from collections import OrderedDict
from functools import partial
from typing import Any, Callable, List, Optional, Tuple
import torch
from torch import nn, Tensor
import torch.utils.model_zoo as model_zoo

model_urls = {
    "regnet_y_400mf": "https://download.pytorch.org/models/regnet_y_400mf-c65dace8.pth",
    "regnet_y_800mf": "https://download.pytorch.org/models/regnet_y_800mf-1b27b58c.pth",
    "regnet_y_1_6gf": "https://download.pytorch.org/models/regnet_y_1_6gf-b11a554e.pth",
    "regnet_y_3_2gf": "https://download.pytorch.org/models/regnet_y_3_2gf-b5a9779c.pth",
    "regnet_y_8gf": "https://download.pytorch.org/models/regnet_y_8gf-d0d0e4a8.pth",
    "regnet_y_16gf": "https://download.pytorch.org/models/regnet_y_16gf-9e6ed7dd.pth",
    "regnet_y_32gf": "https://download.pytorch.org/models/regnet_y_32gf-4dee3f7a.pth",
    "regnet_x_400mf": "https://download.pytorch.org/models/regnet_x_400mf-adf1edd5.pth",
    "regnet_x_800mf": "https://download.pytorch.org/models/regnet_x_800mf-ad17e45c.pth",
    "regnet_x_1_6gf": "https://download.pytorch.org/models/regnet_x_1_6gf-e3633e7f.pth",
    "regnet_x_3_2gf": "https://download.pytorch.org/models/regnet_x_3_2gf-f342aeae.pth",
    "regnet_x_8gf": "https://download.pytorch.org/models/regnet_x_8gf-03ceed89.pth",
    "regnet_x_16gf": "https://download.pytorch.org/models/regnet_x_16gf-2007eb11.pth",
    "regnet_x_32gf": "https://download.pytorch.org/models/regnet_x_32gf-9d47f8d0.pth",
}


class ConvNormActivation(torch.nn.Sequential):
    """
    Configurable block used for Convolution-Normalzation-Activation blocks.
    Args:
        in_channels (int): Number of channels in the input image
        out_channels (int): Number of channels produced by the Convolution-Normalzation-Activation block
        kernel_size: (int, optional): Size of the convolving kernel. Default: 3
        stride (int, optional): Stride of the convolution. Default: 1
        padding (int, tuple or str, optional): Padding added to all four sides of the input. Default: None, in wich case it will calculated as ``padding = (kernel_size - 1) // 2 * dilation``
        groups (int, optional): Number of blocked connections from input channels to output channels. Default: 1
        norm_layer (Callable[..., torch.nn.Module], optional): Norm layer that will be stacked on top of the convolutiuon layer. If ``None`` this layer wont be used. Default: ``torch.nn.BatchNorm2d``
        activation_layer (Callable[..., torch.nn.Module], optinal): Activation function which will be stacked on top of the normalization layer (if not None), otherwise on top of the conv layer. If ``None`` this layer wont be used. Default: ``torch.nn.ReLU``
        dilation (int): Spacing between kernel elements. Default: 1
        inplace (bool): Parameter for the activation layer, which can optionally do the operation in-place. Default ``True``
    """

    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        kernel_size: int = 3,
        stride: int = 1,
        padding: Optional[int] = None,
        groups: int = 1,
        norm_layer: Optional[Callable[..., torch.nn.Module]] = torch.nn.BatchNorm2d,
        activation_layer: Optional[Callable[..., torch.nn.Module]] = torch.nn.ReLU,
        dilation: int = 1,
        inplace: bool = True,
    ) -> None:
        if padding is None:
            padding = (kernel_size - 1) // 2 * dilation
        layers = [
            torch.nn.Conv2d(
                in_channels,
                out_channels,
                kernel_size,
                stride,
                padding,
                dilation=dilation,
                groups=groups,
                bias=norm_layer is None,
            )
        ]
        if norm_layer is not None:
            layers.append(norm_layer(out_channels))
        if activation_layer is not None:
            layers.append(activation_layer(inplace=inplace))
        super().__init__(*layers)
        self.out_channels = out_channels


class SqueezeExcitation(torch.nn.Module):
    """
    This block implements the Squeeze-and-Excitation block from https://arxiv.org/abs/1709.01507 (see Fig. 1).
    Parameters ``activation``, and ``scale_activation`` correspond to ``delta`` and ``sigma`` in in eq. 3.
    Args:
        input_channels (int): Number of channels in the input image
        squeeze_channels (int): Number of squeeze channels
        activation (Callable[..., torch.nn.Module], optional): ``delta`` activation. Default: ``torch.nn.ReLU``
        scale_activation (Callable[..., torch.nn.Module]): ``sigma`` activation. Default: ``torch.nn.Sigmoid``
    """

    def __init__(
        self,
        input_channels: int,
        squeeze_channels: int,
        activation: Callable[..., torch.nn.Module] = torch.nn.ReLU,
        scale_activation: Callable[..., torch.nn.Module] = torch.nn.Sigmoid,
    ) -> None:
        super().__init__()
        self.avgpool = torch.nn.AdaptiveAvgPool2d(1)
        self.fc1 = torch.nn.Conv2d(input_channels, squeeze_channels, 1)
        self.fc2 = torch.nn.Conv2d(squeeze_channels, input_channels, 1)
        self.activation = activation()
        self.scale_activation = scale_activation()

    def _scale(self, input: Tensor) -> Tensor:
        scale = self.avgpool(input)
        scale = self.fc1(scale)
        scale = self.activation(scale)
        scale = self.fc2(scale)
        return self.scale_activation(scale)

    def forward(self, input: Tensor) -> Tensor:
        scale = self._scale(input)
        return scale * input


class SimpleStemIN(ConvNormActivation):
    """Simple stem for ImageNet: 3x3, BN, ReLU."""

    def __init__(
        self,
        width_in: int,
        width_out: int,
        norm_layer: Callable[..., nn.Module],
        activation_layer: Callable[..., nn.Module],
    ) -> None:
        super().__init__(
            width_in,
            width_out,
            kernel_size=3,
            stride=1,
            norm_layer=norm_layer,
            activation_layer=activation_layer,
        )


class BottleneckTransform(nn.Sequential):
    """Bottleneck transformation: 1x1, 3x3 [+SE], 1x1."""

    def __init__(
        self,
        width_in: int,
        width_out: int,
        stride: int,
        norm_layer: Callable[..., nn.Module],
        activation_layer: Callable[..., nn.Module],
        group_width: int,
        bottleneck_multiplier: float,
        se_ratio: Optional[float],
    ) -> None:
        layers: OrderedDict[str, nn.Module] = OrderedDict()
        w_b = int(round(width_out * bottleneck_multiplier))
        g = w_b // group_width

        layers["a"] = ConvNormActivation(
            width_in,
            w_b,
            kernel_size=1,
            stride=1,
            norm_layer=norm_layer,
            activation_layer=activation_layer,
        )
        layers["b"] = ConvNormActivation(
            w_b,
            w_b,
            kernel_size=3,
            stride=stride,
            groups=g,
            norm_layer=norm_layer,
            activation_layer=activation_layer,
        )

        if se_ratio:
            # The SE reduction ratio is defined with respect to the
            # beginning of the block
            width_se_out = int(round(se_ratio * width_in))
            layers["se"] = SqueezeExcitation(
                input_channels=w_b,
                squeeze_channels=width_se_out,
                activation=activation_layer,
            )

        layers["c"] = ConvNormActivation(
            w_b,
            width_out,
            kernel_size=1,
            stride=1,
            norm_layer=norm_layer,
            activation_layer=None,
        )
        super().__init__(layers)


class ResBottleneckBlock(nn.Module):
    """Residual bottleneck block: x + F(x), F = bottleneck transform."""

    def __init__(
        self,
        width_in: int,
        width_out: int,
        stride: int,
        norm_layer: Callable[..., nn.Module],
        activation_layer: Callable[..., nn.Module],
        group_width: int = 1,
        bottleneck_multiplier: float = 1.0,
        se_ratio: Optional[float] = None,
    ) -> None:
        super().__init__()

        # Use skip connection with projection if shape changes
        self.proj = None
        should_proj = (width_in != width_out) or (stride != 1)
        if should_proj:
            self.proj = ConvNormActivation(
                width_in,
                width_out,
                kernel_size=1,
                stride=stride,
                norm_layer=norm_layer,
                activation_layer=None,
            )
        self.f = BottleneckTransform(
            width_in,
            width_out,
            stride,
            norm_layer,
            activation_layer,
            group_width,
            bottleneck_multiplier,
            se_ratio,
        )
        self.activation = activation_layer(inplace=True)

    def forward(self, x: Tensor) -> Tensor:
        if self.proj is not None:
            x = self.proj(x) + self.f(x)
        else:
            x = x + self.f(x)
        return self.activation(x)


class AnyStage(nn.Sequential):
    """AnyNet stage (sequence of blocks w/ the same output shape)."""

    def __init__(
        self,
        width_in: int,
        width_out: int,
        stride: int,
        depth: int,
        block_constructor: Callable[..., nn.Module],
        norm_layer: Callable[..., nn.Module],
        activation_layer: Callable[..., nn.Module],
        group_width: int,
        bottleneck_multiplier: float,
        se_ratio: Optional[float] = None,
        stage_index: int = 0,
    ) -> None:
        super().__init__()

        for i in range(depth):
            block = block_constructor(
                width_in if i == 0 else width_out,
                width_out,
                stride if i == 0 else 1,
                norm_layer,
                activation_layer,
                group_width,
                bottleneck_multiplier,
                se_ratio,
            )

            self.add_module(f"block{stage_index}-{i}", block)


class BlockParams:
    def __init__(
        self,
        depths: List[int],
        widths: List[int],
        group_widths: List[int],
        bottleneck_multipliers: List[float],
        strides: List[int],
        se_ratio: Optional[float] = None,
    ) -> None:
        self.depths = depths
        self.widths = widths
        self.group_widths = group_widths
        self.bottleneck_multipliers = bottleneck_multipliers
        self.strides = strides
        self.se_ratio = se_ratio

    @classmethod
    def from_init_params(
        cls,
        depth: int,
        w_0: int,
        w_a: float,
        w_m: float,
        group_width: int,
        bottleneck_multiplier: float = 1.0,
        se_ratio: Optional[float] = None,
        **kwargs: Any,
    ) -> "BlockParams":
        """
        Programatically compute all the per-block settings,
        given the RegNet parameters.

        The first step is to compute the quantized linear block parameters,
        in log space. Key parameters are:
        - `w_a` is the width progression slope
        - `w_0` is the initial width
        - `w_m` is the width stepping in the log space

        In other terms
        `log(block_width) = log(w_0) + w_m * block_capacity`,
        with `bock_capacity` ramping up following the w_0 and w_a params.
        This block width is finally quantized to multiples of 8.

        The second step is to compute the parameters per stage,
        taking into account the skip connection and the final 1x1 convolutions.
        We use the fact that the output width is constant within a stage.
        """

        QUANT = 8
        STRIDE = 2

        if w_a < 0 or w_0 <= 0 or w_m <= 1 or w_0 % 8 != 0:
            raise ValueError("Invalid RegNet settings")
        # Compute the block widths. Each stage has one unique block width
        widths_cont = torch.arange(depth) * w_a + w_0
        block_capacity = torch.round(torch.log(widths_cont / w_0) / math.log(w_m))
        block_widths = (
            (
                torch.round(torch.divide(w_0 * torch.pow(w_m, block_capacity), QUANT))
                * QUANT
            )
            .int()
            .tolist()
        )
        num_stages = len(set(block_widths))

        # Convert to per stage parameters
        split_helper = zip(
            block_widths + [0],
            [0] + block_widths,
            block_widths + [0],
            [0] + block_widths,
        )
        splits = [w != wp or r != rp for w, wp, r, rp in split_helper]

        stage_widths = [w for w, t in zip(block_widths, splits[:-1]) if t]
        stage_depths = (
            torch.diff(torch.tensor([d for d, t in enumerate(splits) if t]))
            .int()
            .tolist()
        )

        strides = [STRIDE] * num_stages
        bottleneck_multipliers = [bottleneck_multiplier] * num_stages
        group_widths = [group_width] * num_stages

        # Adjust the compatibility of stage widths and group widths
        stage_widths, group_widths = cls._adjust_widths_groups_compatibilty(
            stage_widths, bottleneck_multipliers, group_widths
        )

        return cls(
            depths=stage_depths,
            widths=stage_widths,
            group_widths=group_widths,
            bottleneck_multipliers=bottleneck_multipliers,
            strides=strides,
            se_ratio=se_ratio,
        )

    def _get_expanded_params(self):
        return zip(
            self.widths,
            self.strides,
            self.depths,
            self.group_widths,
            self.bottleneck_multipliers,
        )

    @staticmethod
    def _adjust_widths_groups_compatibilty(
        stage_widths: List[int], bottleneck_ratios: List[float], group_widths: List[int]
    ) -> Tuple[List[int], List[int]]:
        """
        Adjusts the compatibility of widths and groups,
        depending on the bottleneck ratio.
        """
        # Compute all widths for the current settings

        def _make_divisible(
            v: float, divisor: int, min_value: Optional[int] = None
        ) -> int:
            """
            This function is taken from the original tf repo.
            It ensures that all layers have a channel number that is divisible by 8
            It can be seen here:
            https://github.com/tensorflow/models/blob/master/research/slim/nets/mobilenet/mobilenet.py
            """
            if min_value is None:
                min_value = divisor
            new_v = max(min_value, int(v + divisor / 2) // divisor * divisor)
            # Make sure that round down does not go down by more than 10%.
            if new_v < 0.9 * v:
                new_v += divisor
            return new_v

        widths = [int(w * b) for w, b in zip(stage_widths, bottleneck_ratios)]
        group_widths_min = [min(g, w_bot) for g, w_bot in zip(group_widths, widths)]

        # Compute the adjusted widths so that stage and group widths fit
        ws_bot = [
            _make_divisible(w_bot, g) for w_bot, g in zip(widths, group_widths_min)
        ]
        stage_widths = [int(w_bot / b) for w_bot, b in zip(ws_bot, bottleneck_ratios)]
        return stage_widths, group_widths_min


class RegNetBackbone(nn.Module):
    def __init__(
        self,
        mf: int = 400,
        pretrained: bool = True,
        stem_width: int = 32,
        stem_type: Optional[Callable[..., nn.Module]] = None,
        block_type: Optional[Callable[..., nn.Module]] = None,
        norm_layer: Optional[Callable[..., nn.Module]] = None,
        activation: Optional[Callable[..., nn.Module]] = None,
    ) -> None:
        super().__init__()

        self.mf = mf
        if self.mf == 400:
            block_params = BlockParams.from_init_params(
                depth=16, w_0=48, w_a=27.89, w_m=2.09, group_width=8, se_ratio=0.25
            )
        elif self.mf == 800:
            block_params = BlockParams.from_init_params(
                depth=14, w_0=56, w_a=38.84, w_m=2.4, group_width=16, se_ratio=0.25
            )

        norm_layer = nn.BatchNorm2d
        block_type = ResBottleneckBlock
        activation = nn.ReLU

        # Ad hoc stem
        self.stem = SimpleStemIN(
            3,  # width_in
            stem_width,
            norm_layer,
            activation,
        )

        current_width = stem_width

        for i, (
            width_out,
            stride,
            depth,
            group_width,
            bottleneck_multiplier,
        ) in enumerate(block_params._get_expanded_params()):
            setattr(
                self,
                f"block{i+1}",
                AnyStage(
                    current_width,
                    width_out,
                    stride,
                    depth,
                    block_type,
                    norm_layer,
                    activation,
                    group_width,
                    bottleneck_multiplier,
                    block_params.se_ratio,
                    stage_index=i + 1,
                ),
            )
            current_width = width_out

        if pretrained:
            self.load_pre_trained_weights()

    def forward(self, x: Tensor) -> Tensor:
        x = self.stem(x)
        x = self.block1(x)
        x = self.block2(x)
        out1 = self.block3(x)
        out2 = self.block4(out1)
        return out1, out2

    def load_pre_trained_weights(self) -> None:
        arch = "regnet_y_{}mf".format(self.mf)
        if arch not in model_urls:
            raise ValueError(f"No checkpoint is available for model type {arch}")
        print("Loading Pytorch pretrained weights...")
        state_dict = model_zoo.load_url(model_urls[arch])
        for key in list(state_dict.keys()):
            if key.startswith("trunk_output."):
                state_dict[key[13:]] = state_dict[key]
                state_dict.pop(key)
            if key.startswith("fc."):
                state_dict.pop(key)
        self.load_state_dict(state_dict, strict=True)

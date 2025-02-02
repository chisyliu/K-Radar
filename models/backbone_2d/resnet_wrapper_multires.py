from typing import OrderedDict
import torch
import torch.nn as nn
import numpy as np
import torch.nn.functional as F
from torch.hub import load_state_dict_from_url

model_urls = {
    'resnet18': 'https://download.pytorch.org/models/resnet18-5c106cde.pth',
    'resnet34': 'https://download.pytorch.org/models/resnet34-333f7ec4.pth',
    'resnet50': 'https://download.pytorch.org/models/resnet50-19c8e357.pth',
    'resnet101': 'https://download.pytorch.org/models/resnet101-5d3b4d8f.pth',
    'resnet152': 'https://download.pytorch.org/models/resnet152-b121ed2d.pth',
    'resnext50_32x4d': 'https://download.pytorch.org/models/resnext50_32x4d-7cdf4587.pth',
    'resnext101_32x8d': 'https://download.pytorch.org/models/resnext101_32x8d-8ba56ff5.pth',
    'wide_resnet50_2': 'https://download.pytorch.org/models/wide_resnet50_2-95faca4d.pth',
    'wide_resnet101_2': 'https://download.pytorch.org/models/wide_resnet101_2-32ee1156.pth',
}

class ResNetFpnMultiRes(nn.Module):
    def __init__(self, cfg):
        super(ResNetFpnMultiRes, self).__init__()
        self.cfg = cfg
        self.resnet = ResNetWrapper(
            resnet=cfg.MODEL.BACKBONE.RESNET,
            pretrained=cfg.MODEL.BACKBONE.PRETRAINED,
            replace_stride_with_dilation=cfg.MODEL.BACKBONE.STRIDE_TO_DILATION,
            out_conv=cfg.MODEL.BACKBONE.OUT_CONV,
            in_channels=cfg.MODEL.BACKBONE.IN_CHANNELS,
            input_dim=cfg.MODEL.BACKBONE.INPUT_DIM,
            is_get_all_feat=True,
            cfg=cfg
        )

        self.fpn_mode = None
        # print(cfg.MODEL.BACKBONE.FPN_MODE)
        if cfg.MODEL.BACKBONE.FPN_MODE == 'bilinear':
            self.fpn_mode = 0
            self.list_upsample_func = []
            for rate in cfg.MODEL.BACKBONE.FPN_RATE:
                upsample_func = nn.UpsamplingBilinear2d(scale_factor=rate) if rate > 1 else None
                self.list_upsample_func.append(upsample_func)
        elif cfg.MODEL.BACKBONE.FPN_MODE == 'transconv2d':
            self.fpn_mode = 1
            self.list_upsample_func = [[] for _ in range(len(cfg.MODEL.BACKBONE.FPN_RATE))]
            in_channels = cfg.MODEL.BACKBONE.FPN_IN_CHANNELS
            out_channels = cfg.MODEL.BACKBONE.FPN_OUT_CHANNELS

            '''
            * (1) Each list in self.list_upsample_func is responsible for each output feat of layer.
            * (2) For each list in self.list_upsample_func,
            *     the 1st func is rate 1 (or Identity), the 2nd func is rate 2, the 3rd func is rate 2.
            *     As such, upsample3(upsample2(upsample1(feat))) is rate 4.
            '''

            self.fpn_rate = cfg.MODEL.BACKBONE.FPN_RATE

            for idx_rate, rate in enumerate(self.fpn_rate):
                if rate == 1:
                    upsample_func = nn.ConvTranspose2d(in_channels[idx_rate], \
                        out_channels[idx_rate], 3, 1, padding=1)
                    self.list_upsample_func[idx_rate].append(upsample_func.cuda())
                elif rate == 2:
                    # 1st
                    self.list_upsample_func[idx_rate].append(nn.Identity().cuda())
                    upsample_func = nn.ConvTranspose2d(in_channels[idx_rate], \
                        out_channels[idx_rate], 3, rate, padding=1, output_padding=(1,1))
                    # 2nd
                    self.list_upsample_func[idx_rate].append(upsample_func.cuda())
                elif rate == 4:
                    # 1st
                    self.list_upsample_func[idx_rate].append(nn.Identity().cuda())
                    upsample_func = nn.ConvTranspose2d(in_channels[idx_rate], \
                        out_channels[idx_rate], 3, 2, padding=1, output_padding=(1,1))
                    # 2nd
                    self.list_upsample_func[idx_rate].append(upsample_func.cuda())
                    # 3rd
                    self.list_upsample_func[idx_rate].append(
                        nn.ConvTranspose2d(out_channels[idx_rate], out_channels[idx_rate], \
                                                3, 2, padding=1, output_padding=(1,1)).cuda())
        elif cfg.MODEL.BACKBONE.FPN_MODE == 'bifpn_transconv2d':
            self.fpn_mode = 2
            self.list_upsample_func = []
            in_channels = cfg.MODEL.BACKBONE.FPN_IN_CHANNELS
            out_channels = cfg.MODEL.BACKBONE.FPN_OUT_CHANNELS
            for idx_rate, rate in enumerate(cfg.MODEL.BACKBONE.FPN_RATE):
                if rate == 1:
                    upsample_func = nn.ConvTranspose2d(in_channels[idx_rate], \
                        out_channels[idx_rate], 3, rate, padding=1)
                elif rate == 2:
                    upsample_func = nn.ConvTranspose2d(in_channels[idx_rate], \
                        out_channels[idx_rate], 3, rate, padding=1, output_padding=(1,1))
                elif rate == 4:
                    upsample_func = nn.ConvTranspose2d(in_channels[idx_rate], \
                        out_channels[idx_rate], 3, rate, padding=1, output_padding=(3,3))
                self.list_upsample_func.append(upsample_func.cuda())
            bi_fpn_in_channels = cfg.MODEL.BACKBONE.BIFPN_IN_CHANNELS
            bi_fpn_out_channels = cfg.MODEL.BACKBONE.BIFPN_OUT_CHANNELS
            self.list_downsample_func = []
            self.list_bifpn_upsample_func = []

            self.bi_fpn_rate = cfg.MODEL.BACKBONE.BIFPN_RATE
            for idx_rate, rate in enumerate(self.bi_fpn_rate):
                if idx_rate != 0:
                    self.list_downsample_func.append(nn.MaxPool2d(kernel_size=2, stride=2))
                if rate == 1:
                    upsample_func = nn.ConvTranspose2d(bi_fpn_in_channels, \
                        bi_fpn_out_channels[idx_rate], 3, 1, padding=1)
                elif rate == 2:
                    upsample_func = nn.ConvTranspose2d(bi_fpn_in_channels, \
                        bi_fpn_out_channels[idx_rate], 3, 2, padding=1, output_padding=(1,1))
                elif rate == 4:
                    upsample_func = nn.Sequential(
                        nn.ConvTranspose2d(bi_fpn_in_channels, \
                            bi_fpn_out_channels[idx_rate], 3, 2, padding=1, output_padding=(1,1)),
                        nn.ConvTranspose2d(bi_fpn_out_channels[idx_rate], \
                            bi_fpn_out_channels[idx_rate], 3, 2, padding=1, output_padding=(1,1)))
                self.list_bifpn_upsample_func.append(upsample_func.cuda())

    def forward(self, dict_item):
        rdr_cube_bev = dict_item['rdr_cube_bev']
        list_feat = self.resnet(rdr_cube_bev)
        
        out_feat = getattr(self, f'fpn_mr_mode_{self.fpn_mode}')(list_feat)
        dict_item['mr_feats'] = out_feat
        
        return dict_item

    def fpn_mr_mode_0(self, list_feat):
        list_upsampled = []
        for upsample_func, feat in zip(self.list_upsample_func, list_feat):
            # print(feat.shape)
            upsampled = upsample_func(feat) if upsample_func is not None else feat
            # print(upsampled.shape)
            list_upsampled.append(upsampled)

        return torch.cat(list_upsampled, dim=1)

    def fpn_mr_mode_1(self, list_feat):
        dict_list_feats = {
            'stride1' : [],
            'stride2' : [],
            'stride4' : [],
        }

        for idx_feat, feat in enumerate(list_feat):
            list_upsample_module = self.list_upsample_func[idx_feat]
            # print('in: ', feat.shape)
            rate = self.fpn_rate[idx_feat]
            # print('rate: ', rate)

            if rate == 1:
                upsampled = list_upsample_module[0](feat)
                dict_list_feats['stride1'].append(upsampled)
            elif rate == 2:
                dict_list_feats['stride2'].append(feat)
                upsampled = list_upsample_module[1](feat)
                dict_list_feats['stride1'].append(upsampled)
            elif rate == 4:
                dict_list_feats['stride4'].append(feat)
                upsampled2 = list_upsample_module[1](feat)
                dict_list_feats['stride2'].append(upsampled2)
                upsampled1 = list_upsample_module[2](upsampled2)
                dict_list_feats['stride1'].append(upsampled1)

        dict_mr_feats = dict()
        for k, v in dict_list_feats.items():
            # print('='*30, k)
            # for idx_feat, feat in enumerate(v):
            #     print(f'* {idx_feat}: {feat.shape}')
            dict_mr_feats.update({
                k: torch.cat(v, dim=1)
            })

        return dict_mr_feats
        
    def fpn_mr_mode_2(self, list_feat):
        list_upsampled = []
        for upsample_func, feat in zip(self.list_upsample_func, list_feat):
            # print('in  1: ', feat.shape)
            upsampled = upsample_func(feat)
            # print('out 1: ', upsampled.shape)
            list_upsampled.append(upsampled)

        fpn_feat = torch.cat(list_upsampled, dim=1)
        list_fpn_feat = [fpn_feat]

        for idx_feat, downsample_func in enumerate(self.list_downsample_func):
            list_fpn_feat.append(downsample_func(list_fpn_feat[idx_feat]))

        dict_list_feats = {
            'stride1' : [],
            'stride2' : [],
            'stride4' : [],
        }

        for idx_feat, (upsample_func, feat) in enumerate(zip(self.list_bifpn_upsample_func, list_fpn_feat)):
            rate = self.bi_fpn_rate[idx_feat]
            
            # stride 1
            if rate in [1, 2, 4]:
                dict_list_feats['stride1'].append(upsample_func(feat))

            # stride 2
            if rate in [2, 4]:
                upsampled = feat if rate == 2 else upsample_func[0](feat)
                dict_list_feats['stride2'].append(upsampled)

            if rate in [4]:
                dict_list_feats['stride4'].append(feat)

        dict_mr_feats = dict()
        for k, v in dict_list_feats.items():
            # print('='*30, k)
            # for idx_feat, feat in enumerate(v):
            #     print(f'* {idx_feat}: {feat.shape}')
            dict_mr_feats.update({
                k: torch.cat(v, dim=1)
            })

        return dict_mr_feats


def conv3x3(in_planes, out_planes, stride=1, groups=1, dilation=1):
    """3x3 convolution with padding"""
    return nn.Conv2d(in_planes, out_planes, kernel_size=3, stride=stride,
                     padding=dilation, groups=groups, bias=False, dilation=dilation)


def conv1x1(in_planes, out_planes, stride=1):
    """1x1 convolution"""
    return nn.Conv2d(in_planes, out_planes, kernel_size=1, stride=stride, bias=False)


class BasicBlock(nn.Module):
    expansion = 1

    def __init__(self, inplanes, planes, stride=1, downsample=None, groups=1,
                 base_width=64, dilation=1, norm_layer=None):
        super(BasicBlock, self).__init__()
        if norm_layer is None:
            norm_layer = nn.BatchNorm2d
        if groups != 1 or base_width != 64:
            raise ValueError(
                'BasicBlock only supports groups=1 and base_width=64')
        # if dilation > 1:
        #     raise NotImplementedError(
        #         "Dilation > 1 not supported in BasicBlock")
        # Both self.conv1 and self.downsample layers downsample the input when stride != 1
        self.conv1 = conv3x3(inplanes, planes, stride, dilation=dilation)
        self.bn1 = norm_layer(planes)
        self.relu = nn.ReLU(inplace=False)
        self.conv2 = conv3x3(planes, planes, dilation=dilation)
        self.bn2 = norm_layer(planes)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        identity = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)

        if self.downsample is not None:
            identity = self.downsample(x)

        out += identity
        out = self.relu(out)

        return out


class Bottleneck(nn.Module):
    expansion = 4

    def __init__(self, inplanes, planes, stride=1, downsample=None, groups=1,
                 base_width=64, dilation=1, norm_layer=None):
        super(Bottleneck, self).__init__()
        if norm_layer is None:
            norm_layer = nn.BatchNorm2d
        width = int(planes * (base_width / 64.)) * groups
        # Both self.conv2 and self.downsample layers downsample the input when stride != 1
        self.conv1 = conv1x1(inplanes, width)
        self.bn1 = norm_layer(width)
        self.conv2 = conv3x3(width, width, stride, groups, dilation)
        self.bn2 = norm_layer(width)
        self.conv3 = conv1x1(width, planes * self.expansion)
        self.bn3 = norm_layer(planes * self.expansion)
        self.relu = nn.ReLU(inplace=False)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        identity = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)
        out = self.relu(out)

        out = self.conv3(out)
        out = self.bn3(out)

        if self.downsample is not None:
            identity = self.downsample(x)

        out += identity
        out = self.relu(out)

        return out

class ResNetWrapper(nn.Module):

    def __init__(self, 
                resnet = 'resnet18',
                pretrained=True,
                replace_stride_with_dilation=[False, False, False],
                out_conv=False,
                fea_stride=8,
                out_channel=128,
                in_channels=[64, 128, 256, 512],
                input_dim=3,
                is_get_all_feat=False,
                cfg=None):
        super(ResNetWrapper, self).__init__()
        self.cfg = cfg
        self.in_channels = in_channels
        self.input_dim = input_dim

        self.model = eval(resnet)(
            pretrained=pretrained, \
            replace_stride_with_dilation=replace_stride_with_dilation, \
            in_channels=self.in_channels, input_dim=self.input_dim, \
            is_get_all_feat=is_get_all_feat)
        self.out = None
        if out_conv:
            out_channel = 512
            for chan in reversed(self.in_channels):
                if chan < 0: continue
                out_channel = chan
                break
            self.out = conv1x1(
                out_channel * self.model.expansion, cfg.featuremap_out_channel)

    def forward(self, x):
        x = self.model(x)
        if self.out:
            x = self.out(x)
        return x


class ResNet(nn.Module):

    def __init__(self, block, layers, zero_init_residual=False,
                 groups=1, width_per_group=64, replace_stride_with_dilation=None,
                 norm_layer=None, in_channels=None, input_dim=None, is_get_all_feat=False):
        super(ResNet, self).__init__()
        if norm_layer is None:
            norm_layer = nn.BatchNorm2d
        self._norm_layer = norm_layer

        self.inplanes = 64
        self.dilation = 1
        self.is_get_all_feat = is_get_all_feat
        if replace_stride_with_dilation is None:
            # each element in the tuple indicates if we should replace
            # the 2x2 stride with a dilated convolution instead
            replace_stride_with_dilation = [False, False, False]
        if len(replace_stride_with_dilation) != 3:
            raise ValueError("replace_stride_with_dilation should be None "
                             "or a 3-element tuple, got {}".format(replace_stride_with_dilation))
        self.groups = groups
        self.base_width = width_per_group

        self.conv1 = nn.Conv2d(input_dim, self.inplanes, kernel_size=7, stride=1, padding=3,
                               bias=False)
        # self.conv1 = nn.Conv2d(input_dim, self.inplanes, kernel_size=7, stride=2, padding=3,
        #                        bias=False)
        self.bn1 = norm_layer(self.inplanes)
        self.relu = nn.ReLU(inplace=False)
        # self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)

        # print(in_channels)
        self.in_channels = in_channels
        self.layer1 = self._make_layer(block, in_channels[0], layers[0])
        self.layer2 = self._make_layer(block, in_channels[1], layers[1], stride=2,
                                       dilate=replace_stride_with_dilation[0])
        if in_channels[2] > 0:
            self.layer3 = self._make_layer(block, in_channels[2], layers[2], stride=2,
                                        dilate=replace_stride_with_dilation[1])
        if in_channels[3] > 0:
            self.layer4 = self._make_layer(block, in_channels[3], layers[3], stride=2,
                                           dilate=replace_stride_with_dilation[2])
        self.expansion = block.expansion

        # self.avgpool = nn.AdaptiveAvgPool2d((1, 1))
        # self.fc = nn.Linear(512 * block.expansion, num_classes)

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(
                    m.weight, mode='fan_out', nonlinearity='relu')
            elif isinstance(m, (nn.BatchNorm2d, nn.GroupNorm)):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

        # Zero-initialize the last BN in each residual branch,
        # so that the residual branch starts with zeros, and each residual block behaves like an identity.
        # This improves the model by 0.2~0.3% according to https://arxiv.org/abs/1706.02677
        if zero_init_residual:
            for m in self.modules():
                if isinstance(m, Bottleneck):
                    nn.init.constant_(m.bn3.weight, 0)
                elif isinstance(m, BasicBlock):
                    nn.init.constant_(m.bn2.weight, 0)

    def _make_layer(self, block, planes, blocks, stride=1, dilate=False):
        norm_layer = self._norm_layer
        downsample = None
        previous_dilation = self.dilation
        if dilate:
            self.dilation *= stride
            stride = 1
        if stride != 1 or self.inplanes != planes * block.expansion:
            downsample = nn.Sequential(
                conv1x1(self.inplanes, planes * block.expansion, stride),
                norm_layer(planes * block.expansion),
            )

        layers = []
        layers.append(block(self.inplanes, planes, stride, downsample, self.groups,
                            self.base_width, previous_dilation, norm_layer))
        self.inplanes = planes * block.expansion
        for _ in range(1, blocks):
            layers.append(block(self.inplanes, planes, groups=self.groups,
                                base_width=self.base_width, dilation=self.dilation,
                                norm_layer=norm_layer))

        return nn.Sequential(*layers)

    def forward(self, x):
        if self.is_get_all_feat:
            list_feat = []
            x0 = self.conv1(x)
            x0 = self.bn1(x0)
            x0 = self.relu(x0)
            list_feat.append(x0)

            x1 = self.layer1(x0)
            list_feat.append(x1)
            x2 = self.layer2(x1)
            list_feat.append(x2)
            if self.in_channels[2] > 0:
                x3 = self.layer3(x2)
                list_feat.append(x3)
            if self.in_channels[3] > 0:
                x4 = self.layer4(x3)
                list_feat.append(x4)

            return list_feat
        else:
            x = self.conv1(x)
            x = self.bn1(x)
            x = self.relu(x)
            # x = self.maxpool(x)

            x = self.layer1(x)
            x = self.layer2(x)
            if self.in_channels[2] > 0:
                x = self.layer3(x)
            if self.in_channels[3] > 0:
                x = self.layer4(x)

            # x = self.avgpool(x)
            # x = torch.flatten(x, 1)
            # x = self.fc(x)

            return x


def _resnet(arch, block, layers, pretrained, progress, **kwargs):
    model = ResNet(block, layers, **kwargs)
    if pretrained:
        print('pretrained model: ', model_urls[arch])
        # state_dict = torch.load(model_urls[arch])['net']
        state_dict = load_state_dict_from_url(model_urls[arch])
        # print(state_dict.keys())
        # print(state_dict['conv1.weight'].shape)

        state_dict_first_layer = OrderedDict(
            {
                'conv1.weight': state_dict['conv1.weight'][:,0:1,:,:],
                'bn1.running_mean': state_dict['bn1.running_mean'],
                'bn1.running_var': state_dict['bn1.running_var'],
                'bn1.weight': state_dict['bn1.weight'],
                'bn1.bias': state_dict['bn1.bias']
            }
        )

        model.load_state_dict(state_dict_first_layer, strict=False)
    return model


def resnet18(pretrained=False, progress=True, **kwargs):
    r"""ResNet-18 model from
    `"Deep Residual Learning for Image Recognition" <https://arxiv.org/pdf/1512.03385.pdf>`_
    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    return _resnet('resnet18', BasicBlock, [2, 2, 2, 2], pretrained, progress,
                   **kwargs)


def resnet34(pretrained=False, progress=True, **kwargs):
    r"""ResNet-34 model from
    `"Deep Residual Learning for Image Recognition" <https://arxiv.org/pdf/1512.03385.pdf>`_
    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    return _resnet('resnet34', BasicBlock, [3, 4, 6, 3], pretrained, progress,
                   **kwargs)


def resnet50(pretrained=False, progress=True, **kwargs):
    r"""ResNet-50 model from
    `"Deep Residual Learning for Image Recognition" <https://arxiv.org/pdf/1512.03385.pdf>`_
    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    return _resnet('resnet50', Bottleneck, [3, 4, 6, 3], pretrained, progress,
                   **kwargs)


def resnet101(pretrained=False, progress=True, **kwargs):
    r"""ResNet-101 model from
    `"Deep Residual Learning for Image Recognition" <https://arxiv.org/pdf/1512.03385.pdf>`_
    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    return _resnet('resnet101', Bottleneck, [3, 4, 23, 3], pretrained, progress,
                   **kwargs)


def resnet152(pretrained=False, progress=True, **kwargs):
    r"""ResNet-152 model from
    `"Deep Residual Learning for Image Recognition" <https://arxiv.org/pdf/1512.03385.pdf>`_
    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    return _resnet('resnet152', Bottleneck, [3, 8, 36, 3], pretrained, progress,
                   **kwargs)


def resnext50_32x4d(pretrained=False, progress=True, **kwargs):
    r"""ResNeXt-50 32x4d model from
    `"Aggregated Residual Transformation for Deep Neural Networks" <https://arxiv.org/pdf/1611.05431.pdf>`_
    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    kwargs['groups'] = 32
    kwargs['width_per_group'] = 4
    return _resnet('resnext50_32x4d', Bottleneck, [3, 4, 6, 3],
                   pretrained, progress, **kwargs)


def resnext101_32x8d(pretrained=False, progress=True, **kwargs):
    r"""ResNeXt-101 32x8d model from
    `"Aggregated Residual Transformation for Deep Neural Networks" <https://arxiv.org/pdf/1611.05431.pdf>`_
    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    kwargs['groups'] = 32
    kwargs['width_per_group'] = 8
    return _resnet('resnext101_32x8d', Bottleneck, [3, 4, 23, 3],
                   pretrained, progress, **kwargs)


def wide_resnet50_2(pretrained=False, progress=True, **kwargs):
    r"""Wide ResNet-50-2 model from
    `"Wide Residual Networks" <https://arxiv.org/pdf/1605.07146.pdf>`_
    The model is the same as ResNet except for the bottleneck number of channels
    which is twice larger in every block. The number of channels in outer 1x1
    convolutions is the same, e.g. last block in ResNet-50 has 2048-512-2048
    channels, and in Wide ResNet-50-2 has 2048-1024-2048.
    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    kwargs['width_per_group'] = 64 * 2
    return _resnet('wide_resnet50_2', Bottleneck, [3, 4, 6, 3],
                   pretrained, progress, **kwargs)


def wide_resnet101_2(pretrained=False, progress=True, **kwargs):
    r"""Wide ResNet-101-2 model from
    `"Wide Residual Networks" <https://arxiv.org/pdf/1605.07146.pdf>`_
    The model is the same as ResNet except for the bottleneck number of channels
    which is twice larger in every block. The number of channels in outer 1x1
    convolutions is the same, e.g. last block in ResNet-50 has 2048-512-2048
    channels, and in Wide ResNet-50-2 has 2048-1024-2048.
    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    kwargs['width_per_group'] = 64 * 2
    return _resnet('wide_resnet101_2', Bottleneck, [3, 4, 23, 3],
                   pretrained, progress, **kwargs)

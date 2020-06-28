import torch
import torch.nn as nn
from mmcv.cnn import NORM_LAYERS


class BatchNormFunction(torch.autograd.Function):

    @staticmethod
    def forward(ctx, x, weight, bias, running_var, eps, momentum, buffer_x2,
                buffer_gz, iters, buffer_size, warmup_iters):
        ctx.eps = eps
        ctx.buffer_size = buffer_size
        current_iter = iters.item()
        ctx.current_iter = current_iter
        ctx.warmup_iters = warmup_iters

        N, C, H, W = x.size()
        x2 = (x * x).mean(dim=3).mean(dim=2).mean(dim=0)

        buffer_x2[current_iter % buffer_size].copy_(x2)

        if current_iter <= buffer_size or current_iter < warmup_iters:
            var = x2.view(1, C, 1, 1)
        else:
            var = buffer_x2.mean(dim=0).view(1, C, 1, 1)

        z = x / (var + eps).sqrt()
        r = (var + eps).sqrt() / (running_var.view(1, C, 1, 1) + eps).sqrt()

        if current_iter <= max(1000, warmup_iters):
            r = torch.clamp(r, 1, 1)
        else:
            r = torch.clamp(r, 1 / 5, 5)

        y = r * z

        ctx.save_for_backward(z, var, weight, buffer_gz, r)

        running_var.copy_(momentum * running_var + (1 - momentum) * var)
        y = weight.view(1, C, 1, 1) * y + bias.view(1, C, 1, 1)
        return y

    @staticmethod
    def backward(ctx, grad_output):
        eps = ctx.eps
        buffer_size = ctx.buffer_size
        current_iter = ctx.current_iter
        warmup_iters = ctx.warmup_iters

        N, C, H, W = grad_output.size()
        z, var, weight, buffer_gz, r = ctx.saved_variables

        y = r * z
        g = grad_output * weight.view(1, C, 1, 1)
        g = g * r
        gz = (g * z).mean(dim=3).mean(dim=2).mean(dim=0)

        buffer_gz[current_iter % buffer_size].copy_(gz)

        if current_iter <= buffer_size or current_iter < warmup_iters:
            mean_gz = gz.view(1, C, 1, 1)
        else:
            mean_gz = buffer_gz.mean(dim=0).view(1, C, 1, 1)

        gx = 1. / torch.sqrt(var + eps) * (g - z * mean_gz)
        return gx, (grad_output * y).sum(dim=3).sum(dim=2).sum(
            dim=0), grad_output.sum(dim=3).sum(dim=2).sum(
                dim=0
            ), None, None, None, None, None, None, None, None, None, None, None


@NORM_LAYERS.register_module('MABN')
class MABN2d(nn.Module):

    abbr = 'mabn'

    def __init__(self,
                 num_features,
                 eps=1e-5,
                 momentum=0.98,
                 B=2,
                 real_B=32,
                 warmup_iters=100):
        super(MABN2d, self).__init__()
        assert real_B % B == 0
        self.buffer_size = real_B // B
        self.register_parameter('weight',
                                nn.Parameter(torch.ones(num_features)))
        self.register_parameter('bias',
                                nn.Parameter(torch.zeros(num_features)))
        self.register_buffer('running_var', torch.ones(1, num_features, 1, 1))
        self.register_buffer('iters', torch.zeros(1).type(torch.LongTensor))
        self.register_buffer('buffer_x2',
                             torch.zeros(self.buffer_size, num_features))
        self.register_buffer('buffer_gz',
                             torch.zeros(self.buffer_size, num_features))

        self.eps = eps
        self.momentum = momentum
        self.warmup_iters = warmup_iters

    def forward(self, x):
        if self.training:
            self.iters.copy_(self.iters + 1)
            x = BatchNormFunction.apply(x, self.weight, self.bias,
                                        self.running_var, self.eps,
                                        self.momentum, self.buffer_x2,
                                        self.buffer_gz, self.iters,
                                        self.buffer_size, self.warmup_iters)
            return x
        else:
            N, C, H, W = x.size()
            var = self.running_var.view(1, C, 1, 1)
            x = x / (var + self.eps).sqrt()

        return self.weight.view(1, C, 1, 1) * x + self.bias.view(1, C, 1, 1)

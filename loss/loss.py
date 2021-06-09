# -*- encoding: utf-8 -*-
# @File    :   ResUnet.py
# @Time    :   2021/06/05 21:41:27
# @Author  :   Gorkem Can Ates
# @Contact :   g.canates@gmail.com
# @Desc    :   None

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import torchvision


class IoULoss(torch.nn.Module):
    def __init__(self, num_classes=3, reduction='mean'):
        super(IoULoss, self).__init__()
        self.num_classes = num_classes
        self.reduction = reduction

    def forward(self, logit, target, eps=1e-7):
        B = target.shape[0]
        target_1_hot = torch.eye(self.num_classes)[target.type(
            torch.LongTensor).squeeze(1)]
        target_1_hot = target_1_hot.permute(0, 3, 1, 2).float()
        probas = F.softmax(logit, dim=1)

        target_1_hot = target_1_hot.type(logit.type())

        intersection = probas * target_1_hot
        union = probas + target_1_hot - intersection

        # BCHW -> BC
        intersection = intersection.view(B, self.num_classes, -1).sum(2)
        union = union.view(B, self.num_classes, -1).sum(2)

        jacc_loss = (intersection / (union + eps))
        loss = 1 - jacc_loss

        # BC -> B
        loss = loss.mean(dim=1)

        if self.reduction == 'sum': return torch.sum(loss)
        if self.reduction == 'mean': return torch.mean(loss)
        return loss


class DiceLoss(torch.nn.Module):
    def __init__(self, num_classes=3, smooth=0, reduction='mean'):
        super(DiceLoss, self).__init__()
        self.num_classes = num_classes
        self.reduction = reduction
        self.smooth = smooth

    def forward(self, logit, target, eps=1e-7):
        B = target.shape[0]
        target_1_hot = torch.eye(self.num_classes)[target.type(
            torch.LongTensor).squeeze(1)]
        target_1_hot = target_1_hot.permute(0, 3, 1, 2).float()
        probas = F.softmax(logit, dim=1)
        target_1_hot = target_1_hot.type(logit.type())
        intersection = probas * target_1_hot
        cardinality = probas + target_1_hot

        intersection = intersection.view(B, self.num_classes, -1).sum(2)
        cardinality = cardinality.view(B, self.num_classes, -1).sum(2)
        dice_loss = ((2. * intersection + self.smooth) /
                     (cardinality + eps + self.smooth))

        loss = 1 - dice_loss
        loss = loss.mean(dim=1)

        assert loss.shape[0] == target.shape[0]
        if self.reduction == 'sum': return torch.sum(loss)
        if self.reduction == 'mean': return torch.mean(loss)
        return loss


class FocalLoss(torch.nn.Module):
    def __init__(self, weight=None, gamma=2, alpha=1, reduction='mean'):
        super(FocalLoss, self).__init__()
        self.gamma = gamma
        self.weight = weight
        self.reduction = reduction
        self.alpha = alpha

    def forward(self, logit, target):
        ce_loss = F.cross_entropy(logit,
                                  target,
                                  reduction=self.reduction,
                                  weight=self.weight)
        pt = torch.exp(-ce_loss)
        loss = (self.alpha * ((1 - pt) ** self.gamma) * ce_loss)

        assert loss.shape[0] == target.shape[0]
        if self.reduction == 'sum': return torch.sum(loss)
        if self.reduction == 'mean': return torch.mean(loss)
        return loss


class EnhancedMixingLoss(torch.nn.Module):
    def __init__(self, gamma=1.1, alpha=0.48, smooth=1., reduction='mean'):
        super(EnhancedMixingLoss, self).__init__()
        self.focal_loss = FocalLoss(gamma=gamma,
                                    alpha=alpha,
                                    reduction=reduction)
        self.dice_loss = DiceLoss(smooth=smooth, reduction=reduction)
        self.reduction = reduction

    def forward(self, logit, target):
        fcloss = self.focal_loss(logit, target)
        dcloss = self.dice_loss(logit, target)

        loss = fcloss - torch.log(dcloss)

        assert loss.shape[0] == target.shape[0]
        if self.reduction == 'sum': return torch.sum(loss)
        if self.reduction == 'mean': return torch.mean(loss)
        return loss


class TrevskyLoss(torch.nn.Module):
    def __init__(self, num_classes=3, reduction='mean'):
        super(TrevskyLoss, self).__init__()
        self.num_classes = num_classes
        self.reduction = reduction

    def forward(self, logit, true, eps=1e-7):
        target_1_hot = torch.eye(self.num_classes)[target.type(
            torch.LongTensor).squeeze(1)]
        true_1_hot = true_1_hot.permute(0, 3, 1, 2).float()
        probas = F.softmax(logit, dim=1)

        true_1_hot = true_1_hot.type(logit.type())
        dims = (0,) + tuple(range(2, true.ndimension()))
        intersection = torch.sum(probas * true_1_hot, dims)
        fps = torch.sum(probas * (1 - true_1_hot), dims)
        fns = torch.sum((1 - probas) * true_1_hot, dims)
        num = intersection
        denom = intersection + (alpha * fps) + (beta * fns)
        tversky_loss = (num / (denom + eps)).mean()
        return 1 - tversky_loss


########### VGGLOSS ###########
class VGGExtractor(nn.Module):
    def __init__(self, layers=[3, 8, 17, 26], device='cuda'):
        super(VGGExtractor, self).__init__()
        self.layers = layers
        self.mu = torch.tensor([0.485, 0.456, 0.406],
                               requires_grad=False).view(
            (1, 3, 1, 1)).to(device)
        self.sigma = torch.tensor([0.229, 0.224, 0.225],
                                  requires_grad=False).view(
            (1, 3, 1, 1)).to(device)
        features = torchvision.models.vgg19(
            pretrained=True).features[:max(layers) + 1].to(device)
        for param in features.parameters():
            param.requires_grad = False

        self.features = nn.ModuleList(list(features)).eval()

    def forward(self, x):
        x = (x - self.mu) / self.sigma

        results = []
        for i, vgg in enumerate(self.features):
            x = vgg(x)
            if i in self.layers:
                results.append(x)

        return results


class VGGLoss(nn.Module):
    def __init__(self,
                 extractor,
                 criterion,
                 num_classes=3,
                 reduction='mean',
                 device='cuda'):
        super(VGGLoss, self).__init__()
        self.extractor = extractor
        self.criterion = criterion
        # assert self.criterion.reduction == 'mean'
        self.num_classes = num_classes
        self.device = device
        self.reduction = reduction

    def forward(self, preds, target):

        probs = F.softmax(preds, dim=1)
        target_hot = torch.eye(self.num_classes).to(
            self.device)[target.squeeze(1)]
        target_hot = target_hot.permute(0, 3, 1, 2).float()

        probs = self.extractor(probs)
        target_hot = self.extractor(target_hot)

        N = len(probs)

        loss = 0.
        for j in range(N):
            _loss = self.criterion(probs[j], target_hot[j])
            _loss = _loss.mean(dim=(1, 2, 3))
            loss += _loss
        loss = loss / N

        assert loss.shape[0] == target.shape[0]
        if self.reduction == 'sum':
            return torch.sum(loss)
        if self.reduction == 'mean':
            return torch.mean(loss)
        return loss


from scipy.ndimage import distance_transform_edt as distance
from skimage import segmentation as skimage_seg


class BoundaryLoss(nn.Module):
    def __init__(self, reduction='mean', device='cuda'):
        super(BoundaryLoss, self).__init__()
        self.reduction = reduction
        self.device = device

    def forward(self, pred, target):

        out_shape = pred.shape

        img_gt = target.type(torch.uint8)
        gt_sdf = torch.zeros(out_shape)

        for b in range(out_shape[0]):
            for c in range(1, out_shape[1]):
                posmask = img_gt[b] / (1 if img_gt[b].max() == 0 else img_gt[b].max())
                negmask = 1 - posmask
                posdis = torch.tensor(
                    distance(
                        posmask.detach().cpu().numpy()),
                    dtype=torch.uint8).to(self.device)

                negdis = torch.tensor(
                    distance(
                        negmask.detach().cpu().numpy()),
                    dtype=torch.uint8).to(self.device)

                boundary = torch.tensor(
                    skimage_seg.find_boundaries(
                        posmask.detach().cpu().numpy(),
                        mode='inner'),
                    dtype=torch.uint8).to(self.device)

                sdf = negdis - posdis
                sdf[boundary == 1] = 0
                gt_sdf[b][c] = sdf

        pred = F.softmax(pred, dim=1)
        pc = pred[:, 1:, ...]
        dc = gt_sdf[:, 1:, ...].to(self.device)

        multipled = torch.einsum("bcxy,bcxy->bcxy", pc, dc)
        loss = multipled.mean(dim=(1, 2, 3))
        assert loss.shape[0] == target.shape[0]
        if self.reduction == 'sum':
            return torch.sum(loss)
        if self.reduction == 'mean':
            return torch.mean(loss)
        return loss


class CombinedLoss(nn.Module):
    def __init__(self,
                 main_criterion,
                 combined_criterion,
                 weight=[1, 0.1],
                 reduction='mean',
                 balance=False,
                 adopt_weight=False):
        super(CombinedLoss, self).__init__()
        self.combined_criterion = combined_criterion
        self.main_criterion = main_criterion
        self.weight = weight
        self.reduction = reduction
        self.adopt_weight = adopt_weight
        self.balance = balance
        self.epoch = 1

    def forward(self, pred, target):
        if self.balance:

            loss = (1 - self.epoch * self.weight[1]) * self.main_criterion(pred, target) + \
                   (self.epoch * self.weight[1]) * self.combined_criterion(pred, target)


        else:

            loss = self.weight[0] * self.main_criterion(pred, target) + \
                   (epoch * self.weight[1]) * self.combined_criterion(pred, target)

        assert loss.shape[0] == target.shape[0]
        if self.reduction == 'sum':
            return torch.sum(loss)
        if self.reduction == 'mean':
            return torch.mean(loss)
        return loss

# class RecursiveCombinedVGGLoss(nn.Module):
#     def __init__(self, main_criterion, vgg_criterion, balance=[1, 0.1], K=1, reduction='mean'):
#         super(RecursiveCombinedVGGLoss, self).__init__()
#         self.vgg_loss = vgg_criterion
#         self.main_loss = main_criterion
#         self.balance = balance
#         self.K = K
#         self.reduction = reduction

#     def forward(self, pred, target):
#         return self.main_loss(pred, target), self.vgg_loss(pred, target)

# class RecurisiveLoss(nn.Module):
#     def __init__(self, criterion):
#         super(RecurisiveLoss, self).__init__()
#         self.criterion = criterion
#
#     def forward(self, preds, target):
#         K = len(preds)
#         losses = 0
#         for i in range(K):
#             pred = preds[i]
#             losses += (i + 1) * self.criterion(pred, target)
#
#         coeff = 0.5 * K * (K + 1)
#         loss = losses / coeff
#         return loss

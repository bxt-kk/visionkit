from typing import List, Any, Dict, Tuple, Mapping
import math

from torch import Tensor

import torch
import torch.nn as nn
import torch.nn.functional as F

from torchvision.ops import (
    sigmoid_focal_loss,
    generalized_box_iou_loss,
    box_convert,
    boxes as box_ops,
)
from torchvision import tv_tensors
from torchvision.transforms import v2
from torchvision.models import mobilenet_v3_small, MobileNet_V3_Small_Weights

from torchmetrics.detection import MeanAveragePrecision

from PIL import Image

try:
    from .component import LinearBasicConvBD
except ImportError:
    from component import LinearBasicConvBD


class TrimNetX(nn.Module):
    '''A light-weight and easy-to-train model for object detection

    Args:
        num_classes: Number of target categories.
        anchors: Preset anchor boxes.
        num_dilation_blocks: Number of layers in the dilation module.
        num_dilation_ranges: Number of linear dilation convolution layers
            in a single diffusion module.
        num_tries: Number of attempts to guess.
        swap_size: Dimensions of the exchanged data.
        dropout: Dropout parameters in the classifier.
    '''

    def __init__(
            self,
            num_classes:         int,
            anchors:             List[Tuple[float, float]],
            num_dilation_blocks: int=3,
            num_dilation_ranges: int=4,
            num_tries:           int=3,
            swap_size:           int=4,
            dropout:             float=0.2,
        ):
        super().__init__()

        self.num_classes         = num_classes
        self.anchors             = torch.tensor(anchors, dtype=torch.float32)
        self.num_dilation_blocks = num_dilation_blocks
        self.num_dilation_ranges = num_dilation_ranges
        self.num_tries           = num_tries
        self.swap_size           = swap_size
        self.dropout             = dropout

        self.num_anchors = len(anchors)
        self.cell_size   = 16
        self.m_ap_metric = MeanAveragePrecision(
            iou_type='bbox', backend='faster_coco_eval')

        backbone = mobilenet_v3_small(
            weights=MobileNet_V3_Small_Weights.DEFAULT,
        ).features

        features_dim = 24 * 4 + 40 + 96
        self.features_d = backbone[:4] # 24, 64, 64
        self.features_c = backbone[4] # 40, 32, 32
        self.features_u = backbone[5:-1] # 96, 16, 16

        merged_dim = 160
        self.merge = nn.Sequential(
            nn.Conv2d(features_dim, merged_dim, 1, bias=False),
            nn.BatchNorm2d(merged_dim),
        )

        self.cluster = nn.ModuleList()
        for _ in range(num_dilation_blocks):
            modules = []
            for r in range(num_dilation_ranges):
                modules.append(
                    LinearBasicConvBD(merged_dim, merged_dim, dilation=2**r))
            modules.append(nn.Sequential(
                nn.Hardswish(inplace=True),
                nn.Conv2d(merged_dim, merged_dim, 1, bias=False),
                nn.BatchNorm2d(merged_dim),
            ))
            self.cluster.append(nn.Sequential(*modules))

        ex_anchor_dim = (swap_size + 1) * self.num_anchors

        self.predict_conf_tries = nn.ModuleList([nn.Conv2d(
            merged_dim,
            ex_anchor_dim,
            kernel_size=3,
            padding=1,
        )])
        for _ in range(1, num_tries):
            self.predict_conf_tries.append(nn.Conv2d(
                merged_dim + ex_anchor_dim,
                ex_anchor_dim,
                kernel_size=3,
                padding=1,
            ))

        expanded_dim = 320
        object_dim = 4 + num_classes
        self.predict_objs = nn.Sequential(
            nn.Conv2d(merged_dim + ex_anchor_dim, expanded_dim, kernel_size=1, bias=False),
            nn.BatchNorm2d(expanded_dim),
            nn.Hardswish(inplace=True),
            nn.Dropout(p=dropout, inplace=True),
            nn.Conv2d(expanded_dim, self.num_anchors * object_dim, kernel_size=1),
        )

    def forward_features(
            self,
            x:              Tensor,
            train_features: bool,
        ) -> Tensor:

        if train_features:
            fd = self.features_d(x)
            fc = self.features_c(fd)
            fu = self.features_u(fc)
        else:
            with torch.no_grad():
                fd = self.features_d(x)
                fc = self.features_c(fd)
                fu = self.features_u(fc)

        x = self.merge(torch.cat([
            F.pixel_unshuffle(fd, 2),
            fc,
            F.interpolate(fu, scale_factor=2, mode='bilinear'),
        ], dim=1))
        for layer in self.cluster:
            x = x + layer(x)
        return x

    def forward(
            self,
            x:              Tensor,
            train_features: bool=True,
        ) -> Tensor:

        x = self.forward_features(x, train_features)
        confs = [self.predict_conf_tries[0](x)]
        for layer in self.predict_conf_tries[1:]:
            confs.append(layer(torch.cat([x, confs[-1]], dim=1)))
        p_objs = self.predict_objs(torch.cat([x, confs[-1]], dim=1))
        bs, _, ny, nx = p_objs.shape
        p_tryx = torch.cat([
            conf.view(bs, self.num_anchors, -1, ny, nx)[:, :, :1]
            for conf in confs], dim=2)
        p_objs = p_objs.view(bs, self.num_anchors, -1, ny, nx)
        return torch.cat([p_tryx, p_objs], dim=2).permute(0, 1, 3, 4, 2).contiguous()

    @classmethod
    def load_from_state(cls, state:Mapping[str, Any]) -> 'TrimNetX':
        model_state = state['model']
        model = cls(
            num_classes         = model_state['num_classes'],
            anchors             = model_state['anchors'].tolist(),
            num_dilation_blocks = model_state['num_dilation_blocks'],
            num_dilation_ranges = model_state['num_dilation_ranges'],
            num_tries           = model_state['num_tries'],
            swap_size           = model_state['swap_size'],
            dropout             = model_state['dropout'],
        )
        model.load_state_dict(model_state['weights'])
        return model

    def dump_to_state(self, state:Mapping[str, Any]) -> Mapping[str, Any]:
        state['model'] = dict(
            num_classes         = self.num_classes,
            anchors             = self.anchors,
            num_dilation_blocks = self.num_dilation_blocks,
            num_dilation_ranges = self.num_dilation_ranges,
            num_tries           = self.num_tries,
            swap_size           = self.swap_size,
            dropout             = self.dropout,
            weights             = self.state_dict(),
        )
        return state

    def _detect_preprocess(
            self,
            image:      Image.Image,
            align_size: int,
        ) -> Tuple[Tensor, float, int, int]:

        _align_size = math.ceil(align_size / 64) * 64
        src_w, src_h = image.size
        scale = min(1, _align_size / max(src_w, src_h))
        dst_w, dst_h = round(scale * src_w), round(scale * src_h)
        sample = image.resize((dst_w, dst_h), resample=Image.Resampling.BILINEAR)
        frame = Image.new('RGB', (_align_size, _align_size), color=(127, 127, 127))
        pad_x = (align_size - dst_w) // 2
        pad_y = (align_size - dst_h) // 2
        frame.paste(sample, box=(pad_x, pad_y))
        inputs = v2.Compose([
            v2.ToImage(),
            v2.ToDtype(torch.float32, scale=True),
            v2.Normalize(
                mean=[0.485, 0.456, 0.406],
                std=[0.229, 0.224, 0.225],
            )
        ])(frame).unsqueeze(dim=0)
        return inputs, scale, pad_x, pad_y

    def detect(
            self,
            image:       Image.Image,
            conf_thresh: float=0.6,
            iou_thresh:  float=0.55,
            align_size:  int=448,
            device:      torch.device=None,
        ) -> List[Dict[str, Any]]:

        x, scale, pad_x, pad_y = self._detect_preprocess(image, align_size)
        if device is not None:
            x = x.to(device)
        x = self.forward_features(x, train_features=False)

        confs = [self.predict_conf_tries[0](x)]
        for layer in self.predict_conf_tries[1:]:
            confs.append(layer(torch.cat([x, confs[-1]], dim=1)))
        bs, _, ny, nx = x.shape
        p_tryx = torch.cat([
            conf.view(bs, self.num_anchors, -1, ny, nx)[:, :, :1]
            for conf in confs], dim=2).permute(0, 1, 3, 4, 2)
        mix = torch.cat([x, confs[-1]], dim=1)

        p_conf = torch.ones_like(p_tryx[..., 0])
        for conf_id in range(p_tryx.shape[-1] - 1):
            p_conf[p_tryx[..., conf_id] < 0] = 0.
        p_conf *= torch.sigmoid(p_tryx[..., -1])

        mask = p_conf.max(dim=1, keepdim=True).values > conf_thresh
        index = torch.nonzero(mask, as_tuple=True)
        if len(index[0]) == 0: return []

        # p_objs = self.predict_objs(mix)[index[0], :, index[2], index[3]]
        p_objs = self.predict_objs(
            mix[index[0], :, index[2], index[3]].reshape(len(index[0]), -1, 1, 1))

        p_objs = p_objs.reshape(len(index[0]), self.num_anchors, -1)

        anchor_mask = p_conf[index[0], :, index[2], index[3]] > conf_thresh
        sub_ids, anchor_ids = torch.nonzero(anchor_mask, as_tuple=True)
        # bids = index[0][sub_ids]
        rids = index[2][sub_ids]
        cids = index[3][sub_ids]
        objs = p_objs[sub_ids, anchor_ids]
        conf = p_conf[index[0], :, index[2], index[3]][sub_ids, anchor_ids]

        cx = (cids + torch.tanh(objs[:, 0]) + 0.5) * self.cell_size
        cy = (rids + torch.tanh(objs[:, 1]) + 0.5) * self.cell_size
        anchors = self.anchors.type_as(objs)[anchor_ids]
        rw = torch.exp(objs[:, 2]) * anchors[:, 0]
        rh = torch.exp(objs[:, 3]) * anchors[:, 1]
        x1, y1 = cx - rw / 2, cy - rh / 2
        x2, y2 = x1 + rw, y1 + rh

        raw_w, raw_h = image.size
        x1 = torch.clamp((x1 - pad_x) / scale, 0, raw_w - 1)
        y1 = torch.clamp((y1 - pad_y) / scale, 0, raw_h - 1)
        x2 = torch.clamp((x2 - pad_x) / scale, 1, raw_w)
        y2 = torch.clamp((y2 - pad_y) / scale, 1, raw_h)

        boxes = torch.stack([x1, y1, x2, y2]).T
        # final_ids = nms(boxes, conf, iou_thresh)
        clss = torch.softmax(objs[:, 4:], dim=-1).max(dim=-1)
        labels, probs = clss.indices, clss.values
        final_ids = box_ops.batched_nms(boxes, conf, labels, iou_thresh)
        # bids = bids[final_ids]
        conf = conf[final_ids]
        boxes = boxes[final_ids]
        labels = labels[final_ids]
        probs = probs[final_ids]

        result = []
        for score, box, label, prob in zip(conf, boxes, labels, probs):
            result.append(dict(
                score=round(score.item(), 5),
                box=box.round().tolist(),
                label=label.item(),
                prob=round(prob.item(), 5),
            ))
        return result

    def test_target2outputs(
            self,
            _outputs:      Tensor,
            target_index:  List[Tensor],
            target_labels: Tensor,
            target_bboxes: Tensor,
        ) -> Tensor:

        outputs = torch.full_like(_outputs, -1000)
        num_confs = len(self.predict_conf_tries)
        eps = 1e-5

        objects = outputs[target_index]
        objects[:, :num_confs] = 1000.

        targ_cxcywh = box_convert(target_bboxes, 'xyxy', 'cxcywh')
        anchors = self.anchors.type_as(targ_cxcywh)
        inverse_sigmoid = lambda x: torch.log(x / (1 - x))
        targ_cxcywh[:, :2] = inverse_sigmoid(torch.clamp(
            targ_cxcywh[:, :2] % self.cell_size / self.cell_size, eps, 1 - eps))
        targ_cxcywh[:, 2:] = torch.log(targ_cxcywh[:, 2:] / anchors[target_index[1]])
        objects[:, num_confs:num_confs + 4] = targ_cxcywh
        objects[:, num_confs + 4:].scatter_(-1, target_labels.unsqueeze(dim=-1), 1000.)

        outputs[target_index] = objects
        return outputs

    def focal_boost(
            self,
            inputs:       Tensor,
            target_index: List[Tensor],
            sample_mask:  Tensor | None,
            conf_id:      int,
        ) -> Tuple[Tensor, Tensor, Tensor]:

        reduction = 'mean'

        pred_conf = inputs[..., conf_id]
        targ_conf = torch.zeros_like(pred_conf)
        targ_conf[target_index] = 1.

        if sample_mask is None:
            sample_mask = targ_conf >= -1

        sampled_pred = torch.masked_select(pred_conf, sample_mask)
        sampled_targ = torch.masked_select(targ_conf, sample_mask)
        sampled_loss = sigmoid_focal_loss(
            sampled_pred, sampled_targ, reduction=reduction)

        obj_loss = 0.
        obj_mask = torch.logical_and(sample_mask, targ_conf > 0.5)
        if obj_mask.sum() > 0:
            obj_pred = torch.masked_select(pred_conf, obj_mask)
            obj_targ = torch.masked_select(targ_conf, obj_mask)
            obj_loss = F.binary_cross_entropy_with_logits(
                obj_pred, obj_targ, reduction=reduction)

            obj_pred_min = obj_pred.detach().min()
            sample_mask = pred_conf.detach() >= obj_pred_min

        alpha = (math.cos(math.pi / len(self.predict_conf_tries) * conf_id) + 1) / 2
        num_foreground_per_img = (sampled_targ.sum() / len(pred_conf)).numel()

        conf_loss = (
            obj_loss * alpha / max(1, num_foreground_per_img) +
            sampled_loss)

        return conf_loss, sampled_loss, sample_mask

    def pred2boxes(
            self,
            cxcywh: Tensor,
            index:  List[Tensor],
            fmt:    str='xyxy',
        ) -> Tensor:

        anchors = self.anchors.type_as(cxcywh)
        boxes_x  = (torch.tanh(cxcywh[:, 0]) + 0.5 + index[3].type_as(cxcywh)) * self.cell_size
        boxes_y  = (torch.tanh(cxcywh[:, 1]) + 0.5 + index[2].type_as(cxcywh)) * self.cell_size
        boxes_s  = torch.exp(cxcywh[:, 2:]) * anchors[index[1]]
        bboxes = torch.cat([boxes_x.unsqueeze(-1), boxes_y.unsqueeze(-1), boxes_s], dim=-1)
        return box_convert(bboxes, 'cxcywh', fmt)

    def calc_loss(
            self,
            inputs:        Tensor,
            target_index:  List[Tensor],
            target_labels: Tensor,
            target_bboxes: Tensor,
            weights:       Dict[str, float] | None=None,
        ) -> Dict[str, Any]:

        reduction = 'mean'
        num_confs = len(self.predict_conf_tries)

        conf_loss, sampled_loss, sample_mask = self.focal_boost(
            inputs, target_index, None, 0)
        for conf_id in range(1, num_confs):
            conf_loss_i, sampled_loss, sample_mask = self.focal_boost(
                inputs, target_index, sample_mask, conf_id)
            conf_loss += conf_loss_i

        pred_conf = inputs[..., 0]
        targ_conf = torch.zeros_like(pred_conf)
        targ_conf[target_index] = 1.

        objects = inputs[target_index]

        bbox_loss = torch.zeros_like(conf_loss)
        clss_loss = torch.zeros_like(conf_loss)
        if objects.shape[0] > 0:
            pred_cxcywh = objects[:, num_confs:num_confs + 4]
            pred_xyxy = self.pred2boxes(pred_cxcywh, target_index)
            bbox_loss = generalized_box_iou_loss(
                pred_xyxy, target_bboxes, reduction=reduction)

            pred_clss = objects[:, num_confs + 4:]
            clss_loss = F.cross_entropy(
                pred_clss, target_labels, reduction=reduction)

        weights = weights or dict()

        loss = (
            weights.get('conf', 1.) * conf_loss +
            weights.get('bbox', 1.) * bbox_loss +
            weights.get('clss', 1.) * clss_loss
        )

        return dict(
            loss=loss,
            conf_loss=conf_loss,
            bbox_loss=bbox_loss,
            clss_loss=clss_loss,
            sampled_loss=sampled_loss,
        )

    def calc_score(
            self,
            inputs:        Tensor,
            target_index:  List[Tensor],
            target_labels: Tensor,
            target_bboxes: Tensor,
            conf_thresh:   float=0.5,
            eps:           float=1e-5,
        ) -> Dict[str, Any]:

        num_confs = len(self.predict_conf_tries)

        targ_conf = torch.zeros_like(inputs[..., 0])
        targ_conf[target_index] = 1.

        pred_obj = targ_conf > -1
        for conf_id in range(num_confs):
            pred_conf = torch.sigmoid(inputs[..., conf_id])
            thresh = 0.5 if conf_id < num_confs - 1 else conf_thresh
            pred_obj = torch.logical_and(pred_obj, pred_conf > thresh)

        pred_obj_true = torch.masked_select(targ_conf, pred_obj).sum()
        conf_precision = pred_obj_true / torch.clamp_min(pred_obj.sum(), eps)
        conf_recall = pred_obj_true / torch.clamp_min(targ_conf.sum(), eps)
        conf_f1 = 2 * conf_precision * conf_recall / torch.clamp_min(conf_precision + conf_recall, eps)
        proposals = pred_obj.sum() / pred_conf.shape[0]

        objects = inputs[target_index]

        iou_score = torch.ones_like(conf_f1)
        clss_accuracy = torch.ones_like(conf_f1)
        obj_conf_min = torch.zeros_like(conf_f1)
        if objects.shape[0] > 0:
            pred_cxcywh = objects[:, num_confs:num_confs + 4]
            pred_xyxy = self.pred2boxes(pred_cxcywh, target_index)
            targ_xyxy = target_bboxes

            max_x1y1 = torch.maximum(pred_xyxy[:, :2], targ_xyxy[:, :2])
            min_x2y2 = torch.minimum(pred_xyxy[:, 2:], targ_xyxy[:, 2:])
            inter_size = torch.clamp_min(min_x2y2 - max_x1y1, 0)
            intersection = inter_size[:, 0] * inter_size[:, 1]
            pred_size = pred_xyxy[:, 2:] - pred_xyxy[:, :2]
            targ_size = targ_xyxy[:, 2:] - targ_xyxy[:, :2]
            pred_area = pred_size[:, 0] * pred_size[:, 1]
            targ_area = targ_size[:, 0] * targ_size[:, 1]
            union = pred_area + targ_area - intersection
            iou_score = (intersection / union).mean()

            pred_labels = torch.argmax(objects[:, num_confs + 4:], dim=-1)
            clss_accuracy = (pred_labels == target_labels).sum() / len(pred_labels)

            obj_conf = torch.sigmoid(objects[:, :num_confs])
            if num_confs == 1:
                obj_conf_min = obj_conf[:, 0].min()
            else:
                sample_mask = obj_conf[:, 0] > conf_thresh
                for conf_id in range(1, num_confs - 1):
                    sample_mask = torch.logical_and(
                        sample_mask, obj_conf[:, conf_id] > conf_thresh)
                if sample_mask.sum() > 0:
                    obj_conf_min = torch.masked_select(obj_conf[:, -1], sample_mask).min()
                else:
                    obj_conf_min = torch.zeros_like(proposals)

        return dict(
            conf_precision=conf_precision,
            conf_recall=conf_recall,
            conf_f1=conf_f1,
            iou_score=iou_score,
            clss_accuracy=clss_accuracy,
            proposals=proposals,
            obj_conf_min=obj_conf_min,
        )

    def update_metric(
            self,
            inputs:        Tensor,
            target_index:  List[Tensor],
            target_labels: Tensor,
            target_bboxes: Tensor,
            conf_thresh:   float=0.5,
            iou_thresh:    float=0.5,
        ):

        preds_mask = None
        num_confs = len(self.predict_conf_tries)

        preds_mask = torch.sigmoid(inputs[..., 0]) > 0.5
        for conf_id in range(1, num_confs):
            thresh = 0.5 if conf_id < num_confs - 1 else conf_thresh
            preds_mask = torch.logical_and(
                preds_mask, torch.sigmoid(inputs[..., conf_id]) > thresh)
        preds_index = torch.nonzero(preds_mask, as_tuple=True)

        objects = inputs[preds_index]

        pred_scores = torch.sigmoid(objects[:, num_confs - 1])
        pred_cxcywh = objects[:, num_confs:num_confs + 4]
        pred_bboxes = torch.clamp_min(self.pred2boxes(pred_cxcywh, preds_index), 0.)
        pred_labels = torch.argmax(objects[:, num_confs + 4:], dim=-1)

        preds = []
        target = []
        batch_size = inputs.shape[0]
        for batch_id in range(batch_size):
            preds_ids = (preds_index[0] == batch_id).nonzero(as_tuple=True)[0]
            target_ids = (target_index[0] == batch_id).nonzero(as_tuple=True)[0]

            scores=pred_scores[preds_ids]
            boxes=pred_bboxes[preds_ids]
            labels=pred_labels[preds_ids]
            final_ids = box_ops.batched_nms(boxes, scores, labels, iou_thresh)

            preds.append(dict(
                scores=scores[final_ids],
                boxes=boxes[final_ids],
                labels=labels[final_ids],
            ))
            target.append(dict(
                boxes=target_bboxes[target_ids],
                labels=target_labels[target_ids],
            ))
        self.m_ap_metric.update(preds, target)

    def compute_metric(self) -> Dict[str, Any]:
        return self.m_ap_metric.compute()

    def _select_anchor(self, boxes:Tensor) -> Tensor:
        sizes = boxes[:, 2:] - boxes[:, :2]
        inter_size = torch.minimum(sizes[:, None, ...], self.anchors)
        inter_area = inter_size[..., 0] * inter_size[..., 1]
        boxes_area = sizes[..., 0] * sizes[..., 1]
        union_area = (
            boxes_area[:, None] +
            self.anchors[..., 0] * self.anchors[..., 1] -
            inter_area)
        ious = inter_area / union_area
        anchor_ids = torch.argmax(ious, dim=1)
        return anchor_ids

    def _select_row(self, boxes:Tensor) -> Tensor:
        cy = (boxes[:, 1] + boxes[:, 3]) * 0.5
        cell_size = self.cell_size
        noise = torch.rand_like(cy) * cell_size - cell_size / 2
        hs = boxes[:, 3] - boxes[:, 1]
        noise[hs < 2 * cell_size] = 0.
        cy = cy + noise 
        cell_row = (cy / self.cell_size).type(torch.int64)
        return cell_row

    def _select_column(self, boxes:Tensor) -> Tensor:
        cx = (boxes[:, 0] + boxes[:, 2]) * 0.5
        cell_size = self.cell_size
        noise = torch.rand_like(cx) * cell_size - cell_size / 2
        ws = boxes[:, 2] - boxes[:, 0]
        noise[ws < 2 * cell_size] = 0.
        cx = cx + noise
        cell_col = (cx / self.cell_size).type(torch.int64)
        return cell_col

    def collate_fn(
            self,
            batch: List[Any],
        ) -> Any:

        batch_ids   = []
        anchor_ids  = []
        row_ids     = []
        column_ids  = []
        list_image  = []
        list_labels = []
        list_bboxes = []

        for i, (image, target) in enumerate(batch):
            labels = target['labels']
            boxes = target['boxes']
            list_image.append(image.unsqueeze(dim=0))
            list_labels.append(labels)
            list_bboxes.append(boxes)

            batch_ids.append(torch.full_like(labels, i))
            anchor_ids.append(self._select_anchor(boxes))
            row_ids.append(self._select_row(boxes))
            column_ids.append(self._select_column(boxes))

        inputs = torch.cat(list_image, dim=0)
        target_labels = torch.cat(list_labels, dim=0)
        target_bboxes = torch.cat(list_bboxes, dim=0)
        target_index = [
            torch.cat(batch_ids),
            torch.cat(anchor_ids),
            torch.cat(row_ids),
            torch.cat(column_ids),
        ]
        return inputs, target_index, target_labels, target_bboxes

    @classmethod
    def get_transforms(
            cls,
            task_name:str,
        ) -> Tuple[v2.Transform, v2.Transform]:

        train_transforms = None
        test_transforms  = None

        if task_name == 'coco2017det':
            train_transforms = v2.Compose([
                v2.ToImage(),
                # v2.RandomIoUCrop(min_scale=0.3),
                v2.ScaleJitter(
                    target_size=(448, 448),
                    scale_range=(0.9, 1.1),
                    antialias=True),
                v2.RandomPhotometricDistort(p=1),
                v2.RandomHorizontalFlip(p=0.5),
                v2.RandomCrop(
                    size=(448, 448),
                    pad_if_needed=True,
                    fill={tv_tensors.Image: 127, tv_tensors.Mask: 0}),
                v2.SanitizeBoundingBoxes(min_size=10),
                v2.ToDtype(torch.float32, scale=True),
                v2.Normalize(
                    mean=[0.485, 0.456, 0.406],
                    std=[0.229, 0.224, 0.225],
                )
            ])
            test_transforms = v2.Compose([
                v2.ToImage(),
                v2.Resize(
                    size=447,
                    max_size=448,
                    antialias=True),
                v2.CenterCrop(448),
                v2.SanitizeBoundingBoxes(min_size=5),
                v2.ToDtype(torch.float32, scale=True),
                v2.Normalize(
                    mean=[0.485, 0.456, 0.406],
                    std=[0.229, 0.224, 0.225],
                )
            ])

        elif task_name == 'oxford_iiit_pet_det':
            train_transforms = v2.Compose([
                v2.ToImage(),
                v2.ScaleJitter(
                    target_size=(448, 448),
                    scale_range=(0.9, 1.1),
                    antialias=True),
                v2.RandomPhotometricDistort(p=1),
                v2.RandomHorizontalFlip(p=0.5),
                v2.RandomCrop(
                    size=(448, 448),
                    pad_if_needed=True,
                    fill={tv_tensors.Image: 127, tv_tensors.Mask: 0}),
                v2.SanitizeBoundingBoxes(min_size=5),
                v2.ToDtype(torch.float32, scale=True),
                v2.Normalize(
                    mean=[0.485, 0.456, 0.406],
                    std=[0.229, 0.224, 0.225],
                )
            ])
            test_transforms = v2.Compose([
                v2.ToImage(),
                v2.Resize(
                    size=447,
                    max_size=448,
                    antialias=True),
                v2.CenterCrop(448),
                v2.SanitizeBoundingBoxes(min_size=5),
                v2.ToDtype(torch.float32, scale=True),
                v2.Normalize(
                    mean=[0.485, 0.456, 0.406],
                    std=[0.229, 0.224, 0.225],
                )
            ])

        else:
            raise ValueError(f'Unsupported the task `{task_name}`')

        return train_transforms, test_transforms
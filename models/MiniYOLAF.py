import torch
import torch.nn as nn
import torch.nn.functional as F
from utils import Conv2d, SPP
from backbone import *
import numpy as np
import tools

class MiniYOLAF(nn.Module):
    def __init__(self, device, input_size=None, trainable=False, conf_thresh=0.01, nms_thresh=0.3, anchor_size=None, hr=False):
        super(MiniYOLAF, self).__init__()
        self.device = device
        self.input_size = input_size
        self.trainable = trainable
        self.conf_thresh = conf_thresh
        self.nms_thresh = nms_thresh
        self.stride = [8, 16, 32]
        self.anchor_size = torch.tensor(anchor_size).view(3, len(anchor_size) // 3, 2)
        self.anchor_number = self.anchor_size.size(1)

        self.grid_cell, self.stride_tensor, self.all_anchors_wh = self.create_grid(input_size)
        self.scale = np.array([[[input_size, input_size, input_size, input_size]]])
        self.scale_torch = torch.tensor(self.scale.copy(), device=device).float()

        # backbone darknet-tiny
        self.backbone = darknet_light(pretrained=trainable, hr=hr)

        # neck: FPN
        ## Top layer
        self.toplayer = nn.Conv2d(1024, 128, kernel_size=1)

        ## Lateral layers
        self.latlayer4 = nn.Conv2d(256, 128, kernel_size=1)
        self.latlayer3 = nn.Conv2d(128, 128, kernel_size=1)

        ## Smooth layers
        self.smooth5 = nn.Conv2d(128, 128, kernel_size=3, stride=1, padding=1)
        self.smooth4 = nn.Conv2d(128, 128, kernel_size=3, stride=1, padding=1)
        self.smooth3 = nn.Conv2d(128, 128, kernel_size=3, stride=1, padding=1)

        # head
        self.confhead = nn.Sequential(
            Conv2d(128, 128, 3, padding=1),
            nn.Conv2d(128, self.anchor_number * 1, 1)
        )

        self.bboxhead = nn.Sequential(
            Conv2d(128, 128, 3, padding=1),
            nn.Conv2d(128, self.anchor_number * 4, 1)
        )

    def _upsample_add(self, x, y):
        _, _, H , W = y.size()
        return F.interpolate(x, size=(H,W), mode='bilinear', align_corners=True) + y

    def create_grid(self, input_size):
        total_grid_xy = []
        total_stride = []
        total_anchor_wh = []
        w, h = input_size, input_size
        for ind, s in enumerate(self.stride):
            # generate grid cells
            ws, hs = w // s, h // s
            grid_y, grid_x = torch.meshgrid([torch.arange(hs), torch.arange(ws)])
            grid_xy = torch.stack([grid_x, grid_y], dim=-1).float()
            grid_xy = grid_xy.view(1, hs*ws, 1, 2)

            # generate stride tensor
            stride_tensor = torch.ones([1, hs*ws, self.anchor_number, 2]) * s

            # generate anchor_wh tensor
            anchor_wh = self.anchor_size[ind].repeat(hs*ws, 1, 1)

            total_grid_xy.append(grid_xy)
            total_stride.append(stride_tensor)
            total_anchor_wh.append(anchor_wh)

        total_grid_xy = torch.cat(total_grid_xy, dim=1).to(self.device)
        total_stride = torch.cat(total_stride, dim=1).to(self.device)
        total_anchor_wh = torch.cat(total_anchor_wh, dim=0).to(self.device).unsqueeze(0)

        return total_grid_xy, total_stride, total_anchor_wh

    def set_grid(self, input_size):
        self.grid_cell, self.stride_tensor, self.all_anchors_wh = self.create_grid(input_size)
        self.scale = np.array([[[input_size, input_size, input_size, input_size]]])
        self.scale_torch = torch.tensor(self.scale.copy(), device=self.device).float()

    def decode_xywh(self, txtytwth_pred):
        """
            Input:
                txtytwth_pred : [B, H*W, anchor_n, 4] containing [tx, ty, tw, th]
            Output:
                xywh_pred : [B, H*W*anchor_n, 4] containing [x, y, w, h]
        """
        # b_x = sigmoid(tx) + gride_x,  b_y = sigmoid(ty) + gride_y
        B, HW, ab_n, _ = txtytwth_pred.size()
        c_xy_pred = (torch.sigmoid(txtytwth_pred[:, :, :, :2]) + self.grid_cell) * self.stride_tensor
        # b_w = anchor_w * exp(tw),     b_h = anchor_h * exp(th)
        b_wh_pred = torch.exp(txtytwth_pred[:, :, :, 2:]) * self.all_anchors_wh
        # [B, H*W, anchor_n, 4] -> [B, H*W*anchor_n, 4]
        xywh_pred = torch.cat([c_xy_pred, b_wh_pred], -1).view(B, HW*ab_n, 4)

        return xywh_pred

    def decode_boxes(self, txtytwth_pred):
        """
            Input:
                txtytwth_pred : [B, H*W, anchor_n, 4] containing [tx, ty, tw, th]
            Output:
                x1y1x2y2_pred : [B, H*W, anchor_n, 4] containing [xmin, ymin, xmax, ymax]
        """
        # [B, H*W*anchor_n, 4]
        xywh_pred = self.decode_xywh(txtytwth_pred)

        # [center_x, center_y, w, h] -> [xmin, ymin, xmax, ymax]
        x1y1x2y2_pred = torch.zeros_like(xywh_pred)
        x1y1x2y2_pred[:, :, 0] = (xywh_pred[:, :, 0] - xywh_pred[:, :, 2] / 2)
        x1y1x2y2_pred[:, :, 1] = (xywh_pred[:, :, 1] - xywh_pred[:, :, 3] / 2)
        x1y1x2y2_pred[:, :, 2] = (xywh_pred[:, :, 0] + xywh_pred[:, :, 2] / 2)
        x1y1x2y2_pred[:, :, 3] = (xywh_pred[:, :, 1] + xywh_pred[:, :, 3] / 2)
        
        return x1y1x2y2_pred

    def nms(self, dets, scores):
        """"Pure Python NMS baseline."""
        x1 = dets[:, 0]  #xmin
        y1 = dets[:, 1]  #ymin
        x2 = dets[:, 2]  #xmax
        y2 = dets[:, 3]  #ymax

        areas = (x2 - x1) * (y2 - y1)                 # the size of bbox
        order = scores.argsort()[::-1]                        # sort bounding boxes by decreasing order

        keep = []                                             # store the final bounding boxes
        while order.size > 0:
            i = order[0]                                      #the index of the bbox with highest confidence
            keep.append(i)                                    #save it to keep
            xx1 = np.maximum(x1[i], x1[order[1:]])
            yy1 = np.maximum(y1[i], y1[order[1:]])
            xx2 = np.minimum(x2[i], x2[order[1:]])
            yy2 = np.minimum(y2[i], y2[order[1:]])

            w = np.maximum(1e-28, xx2 - xx1)
            h = np.maximum(1e-28, yy2 - yy1)
            inter = w * h

            # Cross Area / (bbox + particular area - Cross Area)
            ovr = inter / (areas[i] + areas[order[1:]] - inter)
            #reserve all the boundingbox whose ovr less than thresh
            inds = np.where(ovr <= self.nms_thresh)[0]
            order = order[inds + 1]

        return keep

    def postprocess(self, all_local, all_conf):
        """
        bbox_pred: (HxW, 4), bsize = 1
        prob_pred: (HxW, 1), bsize = 1
        """
        bbox_pred = all_local
        prob_pred = all_conf

        scores = prob_pred.copy()
        
        # threshold
        keep = np.where(scores >= self.conf_thresh)
        bbox_pred = bbox_pred[keep]
        scores = scores[keep]

        # NMS
        keep = np.zeros(len(bbox_pred), dtype=np.int)
        c_keep = self.nms(bbox_pred, scores)
        keep[c_keep] = 1

        keep = np.where(keep > 0)
        bbox_pred = bbox_pred[keep]
        scores = scores[keep]

        return bbox_pred, scores

    def forward(self, x, target=None):
        # backbone
        c3, c4, c5 = self.backbone(x)
        B = c3.size(0)

        # Top-down
        p5 = self.toplayer(c5)
        p4 = self._upsample_add(p5, self.latlayer4(c4))
        p3 = self._upsample_add(p4, self.latlayer3(c3))

        # Smooth
        p5 = self.smooth5(p5)
        p4 = self.smooth4(p4)
        p3 = self.smooth3(p3)

        # head
        # p5
        conf_pred_5 = self.confhead(p5).permute(0, 2, 3, 1).contiguous().view(B, -1, self.anchor_number * 1).view(B, -1, 1)
        bbox_pred_5 = self.bboxhead(p5).permute(0, 2, 3, 1).contiguous().view(B, -1, self.anchor_number * 4).view(B, -1, 4)

        # p4
        conf_pred_4 = self.confhead(p4).permute(0, 2, 3, 1).contiguous().view(B, -1, self.anchor_number * 1).view(B, -1, 1)
        bbox_pred_4 = self.bboxhead(p4).permute(0, 2, 3, 1).contiguous().view(B, -1, self.anchor_number * 4).view(B, -1, 4)

        # p3
        conf_pred_3 = self.confhead(p3).permute(0, 2, 3, 1).contiguous().view(B, -1, self.anchor_number * 1).view(B, -1, 1)
        bbox_pred_3 = self.bboxhead(p3).permute(0, 2, 3, 1).contiguous().view(B, -1, self.anchor_number * 4).view(B, -1, 4)

        conf_pred = torch.cat([conf_pred_3, conf_pred_4, conf_pred_5], dim=1)
        txtytwth_pred = torch.cat([bbox_pred_3, bbox_pred_4, bbox_pred_5], dim=1)

        # test
        if not self.trainable:
            txtytwth_pred = txtytwth_pred.view(B, HW, self.anchor_number, 4)
            with torch.no_grad():
                # batch size = 1                
                all_obj = torch.sigmoid(conf_pred[0, :, 0])           # 0 is because that these is only 1 batch.
                all_bbox = torch.clamp((self.decode_boxes(txtytwth_pred) / self.scale_torch)[0], 0., 1.)
                # separate box pred and class conf
                all_obj = all_obj.to('cpu').numpy()
                all_bbox = all_bbox.to('cpu').numpy()

                bboxes, scores = self.postprocess(all_bbox, all_obj)

                return bboxes, scores

        else:
            # compute loss
            conf_loss, txtytwth_loss, total_loss = tools.loss(pred_obj=conf_pred, pred_txtytwth=txtytwth_pred, label=target)

            return conf_loss, txtytwth_loss, total_loss
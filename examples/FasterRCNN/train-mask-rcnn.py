#!/usr/bin/env python
# -*- coding: utf-8 -*-
# File: train-mask-rcnn.py

import sys, os
import argparse
import cv2
import shutil
import itertools
import tqdm
import math
import numpy as np
import json
import tensorflow as tf

os.environ['TENSORPACK_TRAIN_API'] = 'v2'   # will become default soon
from tensorpack import *
import tensorpack.tfutils.symbolic_functions as symbf
from tensorpack.tfutils.summary import add_moving_summary
from tensorpack.tfutils import optimizer, gradproc
import tensorpack.utils.viz as tpviz
from tensorpack.utils.concurrency import subproc_call
from tensorpack.utils.gpu import get_nr_gpu


from coco import COCODetection
from basemodel import (
    image_preprocess, pretrained_resnet_conv4, resnet_conv5)
from model import (
    rpn_head, rpn_losses,
    decode_bbox_target, encode_bbox_target,
    generate_rpn_proposals, sample_fast_rcnn_targets,
    roi_align, fastrcnn_head, fastrcnn_losses, fastrcnn_predict_boxes,
    maskrcnn_head, maskrcnn_loss)
from data import (
    get_train_dataflow, get_eval_dataflow,
    get_all_anchors)
from viz import (
    draw_annotation, draw_proposal_recall,
    draw_predictions, draw_final_outputs)
from common import clip_boxes, CustomResize, print_config
from eval import (
    eval_on_dataflow, detect_one_image, segment_one_image,
    print_evaluation_scores, nms_fastrcnn_results)
import config


def get_batch_factor():
    nr_gpu = get_nr_gpu()
    assert nr_gpu in [1, 2, 4, 8], nr_gpu
    return 8 // nr_gpu


class Model(ModelDesc):
    def _get_inputs(self):
        return [
            InputDesc(tf.float32, (None, None, 3), 'image'),
            InputDesc(tf.int32, (None, None, config.NUM_ANCHOR), 'anchor_labels'),
            InputDesc(tf.float32, (None, None, config.NUM_ANCHOR, 4), 'anchor_boxes'),
            InputDesc(tf.float32, (None, 4), 'gt_boxes'),
            InputDesc(tf.int64, (None,), 'gt_labels'),
            InputDesc(tf.uint8, (None, None, None), 'gt_masks'),   # NR_GT x height x width
        ]

    def _preprocess(self, image):
        image = tf.expand_dims(image, 0)
        image = image_preprocess(image, bgr=True)
        return tf.transpose(image, [0, 3, 1, 2])

    def _get_anchors(self, image):
        """
        Returns:
            FSxFSxNAx4 anchors,
        """
        # FSxFSxNAx4 (FS=MAX_SIZE//ANCHOR_STRIDE)
        with tf.name_scope('anchors'):
            all_anchors = tf.constant(get_all_anchors(), name='all_anchors', dtype=tf.float32)
            fm_anchors = tf.slice(
                all_anchors, [0, 0, 0, 0], tf.stack([
                    tf.shape(image)[0] // config.ANCHOR_STRIDE,
                    tf.shape(image)[1] // config.ANCHOR_STRIDE,
                    -1, -1]), name='fm_anchors')
            return fm_anchors

    def _build_graph(self, inputs):
        is_training = get_current_tower_context().is_training
        image, anchor_labels, anchor_boxes, gt_boxes, gt_labels, gt_masks = inputs
        fm_anchors = self._get_anchors(image)
        image = self._preprocess(image)

        anchor_boxes_encoded = encode_bbox_target(anchor_boxes, fm_anchors)
        featuremap = pretrained_resnet_conv4(image, config.RESNET_NUM_BLOCK[:3])
        rpn_label_logits, rpn_box_logits = rpn_head('rpn', featuremap, 1024, config.NUM_ANCHOR)

        decoded_boxes = decode_bbox_target(rpn_box_logits, fm_anchors)  # (fHxfWxNA)x4, floatbox
        proposal_boxes, proposal_scores = generate_rpn_proposals(
            decoded_boxes,
            tf.reshape(rpn_label_logits, [-1]),
            tf.shape(image)[2:])

        # sample proposal boxes in training
        rcnn_sampled_boxes, rcnn_encoded_boxes, rcnn_labels, fg_target_masks = sample_fast_rcnn_targets(
            proposal_boxes, gt_boxes, gt_labels, gt_masks)
        if is_training:
            boxes_on_featuremap = rcnn_sampled_boxes * (1.0 / config.ANCHOR_STRIDE)
        else:
            # use all proposal boxes in inference
            boxes_on_featuremap = proposal_boxes * (1.0 / config.ANCHOR_STRIDE)

        roi_resized = roi_align(featuremap, boxes_on_featuremap, 14)
        feature_fastrcnn = resnet_conv5(roi_resized, config.RESNET_NUM_BLOCK[-1])    # nxcx7x7
        fastrcnn_label_logits, fastrcnn_box_logits = fastrcnn_head('fastrcnn', feature_fastrcnn, config.NUM_CLASS)

        if is_training:
            # rpn
            rpn_label_loss, rpn_box_loss = rpn_losses(
                anchor_labels, anchor_boxes_encoded, rpn_label_logits, rpn_box_logits)

            # fastrcnn
            fastrcnn_label_loss, fastrcnn_box_loss = fastrcnn_losses(
                rcnn_labels, rcnn_encoded_boxes, fastrcnn_label_logits, fastrcnn_box_logits)

            # maskrcnn
            fg_ind = tf.reshape(tf.where(rcnn_labels > 0), [-1])    # nfg, index into sampled rois
            fg_labels = tf.gather(rcnn_labels, fg_ind)
            fg_feature = tf.gather(feature_fastrcnn, fg_ind)
            mask_logits = maskrcnn_head('maskrcnn', fg_feature, config.NUM_CLASS)   # #fg x #cat x 14x14
            mrcnn_loss = maskrcnn_loss(mask_logits, fg_labels, fg_target_masks)

            wd_cost = regularize_cost(
                '(?:group1|group2|group3|rpn|fastrcnn|maskrcnn)/.*W',
                l2_regularizer(1e-4), name='wd_cost')

            self.cost = tf.add_n([
                rpn_label_loss, rpn_box_loss,
                fastrcnn_label_loss, fastrcnn_box_loss,
                mrcnn_loss,
                wd_cost], 'total_cost')

            for k in self.cost, wd_cost:
                add_moving_summary(k)
        else:
            label_probs = tf.nn.softmax(fastrcnn_label_logits, name='fastrcnn_all_probs')  # NP,
            labels = tf.argmax(fastrcnn_label_logits, axis=1)
            fg_ind, fg_box_logits = fastrcnn_predict_boxes(labels, fastrcnn_box_logits)
            fg_label_probs = tf.gather(label_probs, fg_ind, name='fastrcnn_fg_probs')
            fg_boxes = tf.gather(proposal_boxes, fg_ind)

            fg_box_logits = fg_box_logits / tf.constant(config.FASTRCNN_BBOX_REG_WEIGHTS)
            decoded_boxes = decode_bbox_target(fg_box_logits, fg_boxes)  # Nfx4, floatbox
            decoded_boxes = tf.identity(decoded_boxes, name='fastrcnn_fg_boxes')

            roi_resized = roi_align(featuremap, decoded_boxes * (1.0 / config.ANCHOR_STRIDE))
            feature_maskrcnn = resnet_conv5(roi_resized, config.RESNET_NUM_BLOCK[-1])
            mask_logits = maskrcnn_head('maskrcnn', feature_maskrcnn, config.NUM_CLASS)   # #fg x #cat x 14x14
            indices = tf.stack([tf.range(tf.size(fg_ind)), tf.to_int32(labels) - 1], axis=1)  # #fgx14x14
            mask_prediction = tf.gather_nd(mask_logits, indices, name='mask_prediction')

    def _get_optimizer(self):
        lr = tf.get_variable('learning_rate', initializer=0.003, trainable=False)
        tf.summary.scalar('learning_rate', lr)

        factor = get_batch_factor()
        if factor != 1:
            lr = lr / float(factor)
            opt = tf.train.MomentumOptimizer(lr, 0.9)
            opt = optimizer.AccumGradOptimizer(opt, factor)
        else:
            opt = tf.train.MomentumOptimizer(lr, 0.9)
        return opt


def visualize(model_path, nr_visualize=50, output_dir='output'):
    pred = OfflinePredictor(PredictConfig(
        model=Model(),
        session_init=get_model_loader(model_path),
        input_names=['image', 'gt_boxes', 'gt_labels', 'gt_masks'],
        output_names=[
            'generate_rpn_proposals/boxes',
            'generate_rpn_proposals/probs',
            'fastrcnn_all_probs',
            'fastrcnn_fg_probs',
            'fastrcnn_fg_boxes',
            'sample_fast_rcnn_targets/sampled_fg_boxes',
            'sample_fast_rcnn_targets/sampled_fg_mask_targets',
        ]))
    df = get_train_dataflow(add_mask=True)
    df.reset_state()

    if os.path.isdir(output_dir):
        shutil.rmtree(output_dir)
    utils.fs.mkdir_p(output_dir)
    with tqdm.tqdm(total=nr_visualize) as pbar:
        for idx, dp in itertools.islice(enumerate(df.get_data()), nr_visualize):
            img, _, _, gt_boxes, gt_labels, gt_masks = dp

            rpn_boxes, rpn_scores, all_probs, fg_probs, fg_boxes, \
                sampled_fg_boxes, sampled_fg_mask_targets = pred(img, gt_boxes, gt_labels, gt_masks)
            # from tensorpack.dataflow.imgaug import ResizeShortestEdge
            # aug = ResizeShortestEdge(400)
            # for fgbox, fgmask in zip(sampled_fg_boxes, sampled_fg_mask_targets):
            #     fgbox = fgbox.astype('int32')
            #     patch = img[fgbox[1]:fgbox[3], fgbox[0]:fgbox[2]]
            #     patch = aug.augment(patch)
            #     fgmask = cv2.resize(fgmask * 255, patch.shape[:2][::-1])

            #     viz = tpviz.stack_patches([patch, fgmask], 1, 2, pad=True)
            #     tpviz.interactive_imshow(viz)

            gt_viz = draw_annotation(img, gt_boxes, gt_labels)
            proposal_viz, good_proposals_ind = draw_proposal_recall(img, rpn_boxes, rpn_scores, gt_boxes)
            score_viz = draw_predictions(img, rpn_boxes[good_proposals_ind], all_probs[good_proposals_ind])

            fg_boxes = clip_boxes(fg_boxes, img.shape[:2])
            fg_viz = draw_predictions(img, fg_boxes, fg_probs)

            results = nms_fastrcnn_results(fg_boxes, fg_probs)
            final_viz = draw_final_outputs(img, results)

            viz = tpviz.stack_patches([
                gt_viz, proposal_viz, score_viz,
                fg_viz, final_viz], 2, 3)

            if os.environ.get('DISPLAY', None):
                tpviz.interactive_imshow(viz)
            cv2.imwrite("{}/{:03d}.png".format(output_dir, idx), viz)
            pbar.update()


def offline_evaluate(model_path, output_file):
    pred = OfflinePredictor(PredictConfig(
        model=Model(),
        session_init=get_model_loader(model_path),
        input_names=['image'],
        output_names=[
            'fastrcnn_fg_probs',
            'fastrcnn_fg_boxes',
        ]))
    df = get_eval_dataflow()
    df = PrefetchDataZMQ(df, 1)
    all_results = eval_on_dataflow(df, lambda img: detect_one_image(img, pred))
    with open(output_file, 'w') as f:
        json.dump(all_results, f)
    print_evaluation_scores(output_file)


def predict(model_path, input_file):
    pred = OfflinePredictor(PredictConfig(
        model=Model(),
        session_init=get_model_loader(model_path),
        input_names=['image'],
        output_names=[
            'fastrcnn_fg_probs',
            'fastrcnn_fg_boxes',
            'mask_prediction'
        ]))
    img = cv2.imread(input_file, cv2.IMREAD_COLOR)
    results = detect_one_image(img, pred)
    final = draw_final_outputs(img, results)
    viz = np.concatenate((img, final), axis=1)
    tpviz.interactive_imshow(viz)


def predict_mask(model_path, input_file):
    pred = OfflinePredictor(PredictConfig(
        model=Model(),
        session_init=get_model_loader(model_path),
        input_names=['image'],
        output_names=[
            'fastrcnn_fg_probs',
            'fastrcnn_fg_boxes',
            'mask_prediction'
        ]))
    img = cv2.imread(input_file, cv2.IMREAD_COLOR)
    results = segment_one_image(img, pred)
    final = draw_final_outputs(img, results)
    viz = np.concatenate((img, final), axis=1)
    tpviz.interactive_imshow(viz)


class EvalCallback(Callback):
    def _setup_graph(self):
        self.pred = self.trainer.get_predictor(['image'], ['fastrcnn_fg_probs', 'fastrcnn_fg_boxes'])
        self.df = PrefetchDataZMQ(get_eval_dataflow(), 1)

    def _before_train(self):
        EVAL_TIMES = 5  # eval 5 times during training
        interval = self.trainer.max_epoch // (EVAL_TIMES + 1)
        self.epochs_to_eval = set([interval * k for k in range(1, EVAL_TIMES)])
        self.epochs_to_eval.add(self.trainer.max_epoch)

    def _eval(self):
        all_results = eval_on_dataflow(self.df, lambda img: detect_one_image(img, self.pred))
        output_file = os.path.join(
            logger.get_logger_dir(), 'outputs{}.json'.format(self.global_step))
        with open(output_file, 'w') as f:
            json.dump(all_results, f)
        print_evaluation_scores(output_file)

    def _trigger_epoch(self):
        if self.epoch_num in self.epochs_to_eval:
            self._eval()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu', help='comma separated list of GPU(s) to use.')
    parser.add_argument('--load', help='load model')
    parser.add_argument('--logdir', help='logdir', default='train_log/fastrcnn')
    parser.add_argument('--datadir', help='override config.BASEDIR')
    parser.add_argument('--visualize', action='store_true')
    parser.add_argument('--evaluate', help='path to the output json eval file')
    parser.add_argument('--predict', help='path to the input image file')
    args = parser.parse_args()
    if args.datadir:
        config.BASEDIR = args.datadir

    # from IPython import embed
    # import traceback
    # def excepthook(type, value, tb):
    #     traceback.print_tb(tb)
    #     embed()
    # sys.excepthook = excepthook

    if args.gpu:
        os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu

    if args.visualize or args.evaluate or args.predict:
        assert args.load
        print_config()
        if args.visualize:
            visualize(args.load)
        elif args.evaluate:
            assert args.evaluate.endswith('.json')
            # autotune is too slow for inference
            os.environ['TF_CUDNN_USE_AUTOTUNE'] = '0'
            offline_evaluate(args.load, args.evaluate)
        elif args.predict:
            COCODetection(config.BASEDIR, 'train2014')   # to load the class names into caches
            predict(args.load, args.predict)
    else:
        logger.set_logger_dir(args.logdir)
        print_config()
        stepnum = 300
        warmup_epoch = max(math.ceil(500.0 / stepnum), 5)
        factor = get_batch_factor()

        cfg = TrainConfig(
            model=Model(),
            dataflow=get_train_dataflow(add_mask=True),
            callbacks=[
                PeriodicTrigger(ModelSaver(), every_k_epochs=5),
                # linear warmup
                ScheduledHyperParamSetter(
                    'learning_rate',
                    [(0, 3e-3), (warmup_epoch * factor, 1e-2)], interp='linear'),
                # step decay
                ScheduledHyperParamSetter(
                    'learning_rate',
                    [(warmup_epoch * factor, 1e-2),
                     (150000 * factor // stepnum, 1e-3),
                     (210000 * factor // stepnum, 1e-4)]),
                HumanHyperParamSetter('learning_rate'),
                EvalCallback(),
                GPUUtilizationTracker(),
            ],
            steps_per_epoch=stepnum,
            max_epoch=230000 * factor // stepnum,
            session_init=get_model_loader(args.load) if args.load else None,
        )
        trainer = SyncMultiGPUTrainerReplicated(get_nr_gpu())
        launch_train_with_config(cfg, trainer)

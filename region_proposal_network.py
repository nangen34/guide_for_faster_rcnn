import tensorflow as tf
from tensorflow.contrib import slim

import numpy as np

from utils.anchor_utils import encode_bboxes, generate_anchors
from utils.losses import smooth_l1_loss_rpn

import faster_rcnn_configs as frc


def rpn(features, batch_image_shape, batch_gt_bboxes, gt_batch_indices, is_training):
    """
    The Region proposal Network. Return rpn losses(rpn_cls_loss, rpn_bbox_loss), classification accuracy(rpn_cls_acc)
    and samples for training faster r-cnn roi net(rois, labels, bbox_targets).

    :param features: feature map from backbone
    :param batch_image_shape: image shape [height, width]
    :param batch_gt_bboxes: ground truth bouding box [x_up_left, y_up_left, x_down_right, y_down_right, label]
    :return: rpn_cls_loss, rpn_cls_acc, rpn_bbox_loss, rois, labels, bbox_targets
    """
    with tf.variable_scope('rpn'):
        # rpn_cls_score
        # Get a binary classification results
        # with size [batch_size, feature_map_height, feature_map_width 2 * anchor_numbers]
        # anchor_numbers = len(anchor_scale) * len(anchor_rate) according to paper the default maybe 9 from
        # 3 kind scales [2 ** 3, 2 ** 4, 2 ** 5]
        # 3 kind ratio [0.5, 1.0, 2.0]
        # This means each pixel in feature map represents a stride receptive field.
        # For example, the CNN backbone has 2 x 2 pooling 4 times and only 1 stride 3 x 3 convolution, the
        # stride should equal to 2 ** 4 = 16.
        # So each pixel in feature map represents a 16 x 16 base region to generate anchor boxes.
        # 2 * anchor_number generating anchor boxes with a confidence to judge the anchor box is foreground or background.
        rpn_cls_score = slim.conv2d(features, 2 * frc.ANCHOR_NUM, [1, 1],
                                    normalizer_fn=slim.batch_norm,
                                    normalizer_params={'decay': frc.RPN_BN_DECACY, 'epsilon': frc.RPN_BN_EPS},
                                    weights_regularizer=slim.l2_regularizer(frc.RPN_WEIGHTS_L2_PENALITY_FACTOR),
                                    activation_fn=None, trainable=is_training, scope='rpn_cls_score')
        rpn_cls_score = tf.reshape(rpn_cls_score, [-1, 2])
        rpn_cls_prob = slim.softmax(rpn_cls_score, scope='rpn_cls_pred')

        # rpn_bbox_pred
        # This generate encoded anchor coordinates with 4 dimensions.
        # According to the paper, the encoded coordinates are [t_x, t_y, t_w, t_h] to adjust predicted bounding box.
        rpn_bbox_pred = slim.conv2d(features, frc.ANCHOR_NUM * 4, [1, 1],
                                    activation_fn=None, trainable=is_training, scope='rpn_bbox_pred')
        rpn_bbox_pred = tf.reshape(rpn_bbox_pred, [-1, 4])

        # Get the feature map size and generate anchor box according to the size in original image.
        featuremap_height, featuremap_width = tf.shape(features)[1], tf.shape(features)[2]
        featuremap_height = tf.cast(featuremap_height, dtype=tf.float32)
        featuremap_width = tf.cast(featuremap_width, dtype=tf.float32)

        image_anchors = make_anchors_in_image(frc.ANCHOR_BASE_SIZE, featuremap_width, featuremap_height,
                                              feature_stride=frc.FEATURE_STRIDE)

        # generate labels and bounding boxes to train rpn
        rpn_bbox_targets, rpn_labels = tf.py_func(generate_rpn_labels_py,
                                                  [image_anchors, batch_gt_bboxes, gt_batch_indices, batch_image_shape],
                                                  [tf.float32, tf.float32])
        rpn_labels = tf.to_int32(rpn_labels)
        rpn_labels = tf.reshape(rpn_labels, [-1])
        rpn_bbox_targets = tf.reshape(rpn_bbox_targets, [-1, 4])

        # Get RCNN rois
        with tf.control_dependencies([rpn_labels, rpn_bbox_targets]):
            # process rpn proposals, including clip, decode, nms
            rois, roi_scores, rois_batch_indices = process_rpn_proposals(image_anchors,
                                                                         rpn_cls_prob, rpn_bbox_pred, batch_image_shape)
            rois, rois_batch_indices, labels, bbox_targets = tf.py_func(process_proposal_targets_py,
                                                                        [rois, rois_batch_indices, batch_gt_bboxes, gt_batch_indices],
                                                                        [tf.float32, tf.int32, tf.int32, tf.float32])

            rois = tf.reshape(rois, [-1, 4])
            rois_batch_indices = tf.reshape(tf.to_int32(rois_batch_indices), [-1])
            labels = tf.reshape(tf.to_int32(labels), [-1])
            bbox_targets = tf.reshape(bbox_targets, [-1, 4 * (frc.NUM_CLS + 1)])

            if is_training:
                # rpn_losses
                rpn_cls_loss, rpn_cls_acc, rpn_bbox_loss = build_rpn_losses(rpn_cls_score, rpn_cls_prob,
                                                                            rpn_bbox_pred, rpn_bbox_targets,
                                                                            rpn_labels)

                return rpn_cls_loss, rpn_cls_acc, rpn_bbox_loss, rois, rois_batch_indices, labels, bbox_targets
            else:
                return rois, rois_batch_indices, labels, bboxe_targets


def build_rpn_losses(rpn_cls_score, rpn_cls_prob, rpn_bbox_pred, rpn_bbox_targets, rpn_labels):
    """

    :param rpn_cls_score:
    :param rpn_cls_prob:
    :param rpn_bbox_pred:
    :param rpn_bbox_targets:
    :param rpn_labels:
    :return: rpn_cls_loss, rpn_cls_acc, rpn_bbox_loss
    """
    print(rpn_cls_score)
    print(rpn_cls_prob)
    print(rpn_bbox_pred)
    print(rpn_bbox_targets)
    print(rpn_labels)
    with tf.variable_scope('rpn_losses'):
        # calculate class accuracy
        rpn_cls_category = tf.argmax(rpn_cls_prob, axis=1)  # get 0 or 1 to represent the background or foreground

        # exclude not care labels
        calculated_rpn_target_indexes = tf.reshape(tf.where(tf.not_equal(rpn_labels, -1)), [-1])

        rpn_cls_category = tf.cast(tf.gather(rpn_cls_category, calculated_rpn_target_indexes), dtype=tf.int32)
        gt_labels = tf.cast(tf.gather(rpn_labels, calculated_rpn_target_indexes), dtype=tf.int32)

        # rpn class loss
        rpn_cls_acc = tf.reduce_mean(tf.cast(tf.equal(rpn_cls_category, gt_labels), dtype=tf.float32))

        rpn_cls_score = tf.gather(rpn_cls_score, calculated_rpn_target_indexes)
        rpn_cls_loss = tf.reduce_mean(tf.nn.sparse_softmax_cross_entropy_with_logits(labels=gt_labels,
                                                                                     logits=rpn_cls_score,
                                                                                     name='rpn_cls_loss'))

        rpn_bbox_loss = smooth_l1_loss_rpn(rpn_bbox_pred, rpn_bbox_targets, rpn_labels)

    return rpn_cls_loss, rpn_cls_acc, rpn_bbox_loss


def process_rpn_proposals(image_anchors, rpn_cls_pred, rpn_bbox_pred, image_shape, scale_factor=None):
    slice_count = tf.shape(image_anchors)[0]
    rois = []
    rois_score = []
    rois_batch_indices = []
    for ind_in_batch in range(frc.IMAGE_BATCH_SIZE):
        image_rpn_bbox_pred = rpn_bbox_pred[slice_count * ind_in_batch:slice_count * (ind_in_batch+1), :]
        image_rpn_cls_pred = rpn_cls_pred[slice_count * ind_in_batch:slice_count * (ind_in_batch+1), :]
        # 1. Trans bboxes
        t_x, t_y, t_w, t_h = tf.unstack(image_rpn_bbox_pred, axis=1)

        if scale_factor:
            t_x /= scale_factor[0]
            t_y /= scale_factor[1]
            t_w /= scale_factor[2]
            t_h /= scale_factor[3]

        anchors_x_min, anchors_y_min, anchors_x_max, anchors_y_max = tf.unstack(image_anchors, axis=1)
        anchors_width = anchors_x_max - anchors_x_min
        anchors_height = anchors_y_max - anchors_y_min
        anchors_center_x = anchors_x_min + anchors_width / 2.0
        anchors_center_y = anchors_y_min + anchors_height / 2.0

        # According to equations in Faster-RCNN
        # t_x = (x - x_a) / w_a
        # t_y = (y - y_a) / h_a
        # t_w = log(w / w_a)
        # t_h = log(h / h_a)
        # Where (t_x, t_y, t_w, t_h) is the prediction by RPN, (x, y, w, h) is the prediction of bounding box,
        # (x_a, y_a, w_a, h_a) is the generated anchor box.
        # The RPN will be optimized to calculate the coordinates of (t_x, t_y, t_w, t_h).
        predict_center_x = t_x * anchors_width + anchors_center_x
        predict_center_y = t_y * anchors_height + anchors_center_y
        predict_width = tf.exp(t_w) * anchors_width
        predict_height = tf.exp(t_h) * anchors_height

        predict_x_min = predict_center_x - predict_width / 2
        predict_y_min = predict_center_y - predict_height / 2
        predict_x_max = predict_center_x + predict_width / 2
        predict_y_max = predict_center_y + predict_height / 2

        # 2. Clip bounding boxes, make all boxes in the bounding of image
        image_height, image_width = tf.to_float(image_shape[ind_in_batch, 0]), tf.to_float(image_shape[ind_in_batch, 1])

        predict_x_min = tf.maximum(0., tf.minimum(image_width - 1, predict_x_min))
        predict_y_min = tf.maximum(0., tf.minimum(image_height - 1, predict_y_min))

        predict_x_max = tf.maximum(0., tf.minimum(image_width - 1, predict_x_max))
        predict_y_max = tf.maximum(0., tf.minimum(image_height - 1, predict_y_max))

        predict_bboxes = tf.stack([predict_x_min, predict_y_min, predict_x_max, predict_y_max], axis=1)

        predict_targets_count = tf.minimum(frc.RPN_TOP_K_NMS_TRAIN, tf.shape(predict_bboxes)[0])
        sorted_rpn_cls_pred, sorted_pred_indeces = tf.nn.top_k(image_rpn_cls_pred[:, 1], predict_targets_count)
        sorted_bounding_boxes = tf.gather(predict_bboxes, sorted_pred_indeces)

        # 3. NMS
        selected_bboxes_indeces = tf.image.non_max_suppression(sorted_bounding_boxes, sorted_rpn_cls_pred,
                                                               max_output_size=frc.RPN_PROPOSAL_MAX_TRAIN,
                                                               iou_threshold=frc.RPN_NMS_IOU_THRESHOLD)

        selected_bboxes = tf.gather(sorted_bounding_boxes, selected_bboxes_indeces)
        selected_scores = tf.gather(sorted_rpn_cls_pred, selected_bboxes_indeces)

        rois.append(selected_bboxes)
        rois_score.append(selected_scores)
        rois_batch_indices.append(ind_in_batch * tf.ones(tf.shape(selected_bboxes_indeces)[0], dtype=tf.int32))

    rois = tf.concat(rois, axis=0)
    rois_score = tf.concat(rois_score, axis=0)
    rois_batch_indices = tf.concat(rois_batch_indices, axis=0)

    # print(rois)
    # print(rois_score)
    # print(rois_batch_indices)
    return rois, rois_score, rois_batch_indices


def make_anchors_in_image(anchor_base, feature_width, feature_height, feature_stride):
    _anchors = generate_anchors(original_anchor=[1, 1, anchor_base - 1, anchor_base - 1],
                                scales=frc.ANCHOR_SCALE, ratios=frc.ANCHOR_RATE)
    shift_x = tf.range(feature_width, dtype=tf.float32) * feature_stride
    shift_y = tf.range(feature_height, dtype=tf.float32) * feature_stride
    shift_x, shift_y = tf.meshgrid(shift_x, shift_y)

    shifts = tf.stack([tf.reshape(shift_x, [-1]), tf.reshape(shift_y, [-1]),
                       tf.reshape(shift_x, [-1]), tf.reshape(shift_y, [-1])],
                      axis=1)
    all_anchors = tf.reshape(_anchors[tf.newaxis, :, :] + shifts[:, tf.newaxis, :], [-1, 4])
    # all_anchors_batch = tf.reshape(tf.concat([all_anchors] * frc.IMAGE_BATCH_SIZE, axis=0), [-1, 4])
    #
    # # batch_ind = tf.reshape(tf.range(frc.IMAGE_BATCH_SIZE, dtype=tf.int32), [-1, 1])
    # # batch_ind = tf.tile(batch_ind, [1, frc.ANCHOR_NUM * tf.to_int32(feature_width) * tf.to_int32(feature_height)])
    # _, batch_ind = tf.meshgrid(tf.range(frc.ANCHOR_NUM * tf.to_int32(feature_width) * tf.to_int32(feature_height)),
    #                            tf.range(frc.IMAGE_BATCH_SIZE))
    # batch_ind = tf.reshape(batch_ind, [-1])
    return all_anchors


def process_proposal_targets_py(rpn_rois, batch_rois_indices, batch_gt_bboxes, batch_gt_indices):
    """
    Assign object detection proposals to ground truth. Produce proposal classification labels and
    bounding box regression targets.
    :param rpn_rois:
    :param batch_gt_bboxes:
    :return:
    """
    def _get_rois_and_labels(rois, gt_bboxes, fg_rois_per_image):
        # Sample rois with classification labels and bounding box regression.
        # ALGORITHM:
        # 1. Calculates overlaps between rois and ground truth.
        # 2. Get the maximum overlap area for each roi and set it label equal to the ground truth.
        overlaps = get_overlaps_py(rois, gt_bboxes[:, :-1])
        max_overlaps_gt_indices = np.argmax(overlaps, axis=1)
        max_overlaps = np.max(overlaps, axis=1)

        labels = gt_bboxes[max_overlaps_gt_indices, -1]

        # 3. Set overlap iou larger than iou threshold as positive sample, and less than threshold as negative samples.
        # IOU > POSITIVE_THRESHOLD = 0.5 => POSITIVE
        # 0 = NEGATIVE_THRESHOLD < IOU < POSITIVE_THRESHOLD = 0.5 => NEGATIVE
        # 4. Let the total numbers of rois equal to the
        # ROIS_PER_IMAGE = FOREGROUND_ROIS_PER_IMAGE + BACKGROUND_ROIS_PER_IMAGE

        # chose foreground indices
        fg_indices = np.where(max_overlaps >= frc.FASTER_RCNN_IOU_POSITIVE_THRESHOLD)[0]
        fg_rois_per_image = np.minimum(fg_rois_per_image, fg_indices.size)
        if fg_indices.size > 0:
            fg_indices = np.random.choice(fg_indices, size=int(fg_rois_per_image), replace=False)

        # chose background indices
        bg_indices = np.where((max_overlaps < frc.FASTER_RCNN_IOU_POSITIVE_THRESHOLD) &
                              (max_overlaps >= frc.FASTER_RCNN_IOU_NEGATIVE_THRESHOLD))[0]
        # bg_roi_per_image = fg_rois_per_image
        # bg_roi_per_image = np.ceil(0.5 * fg_rois_per_image) + 1
        bg_roi_per_image = rois_per_image - fg_rois_per_image
        bg_roi_per_image = np.minimum(bg_roi_per_image, bg_indices.size)

        if bg_indices.size > 0:
            bg_indices = np.random.choice(bg_indices, size=int(bg_roi_per_image), replace=False)

        keep_indices = np.append(fg_indices, bg_indices)
        labels = labels[keep_indices]

        # 5. Make the negative labels equal to 0.
        labels[int(fg_rois_per_image):] = 0
        rois = np.float32(all_rois[keep_indices])

        # 6. Encodes bounding boxes to targets coordinates for bounding box regression.
        bbox_targets_data = encode_bboxes(rois, gt_bboxes[max_overlaps_gt_indices[keep_indices], :-1])
        # bbox_targets_data = np.hstack([labels[:, np.newaxis], bbox_targets_data]).astype(np.float32, copy=False)

        # Make the bbox_targets as a sparse matrix. For example, 3 classes and 2 targets, the bbox_targets is like
        #              ||                               bbox_targets                              ||
        # Target class ||        Class 1        ||        Class 2        ||        Class 3        ||
        # ========================================================================================||
        #              || t_x | t_y | t_w | t_h || t_x | t_y | t_w | t_h || t_x | t_y | t_w | t_h ||
        #    Class 1   ||  x  |  y  |  w  |  h  ||  0  |  0  |  0  |  0  ||  0  |  0  |  0  |  0  ||
        #    Class 3   ||  0  |  0  |  0  |  0  ||  0  |  0  |  0  |  0  ||  x  |  y  |  w  |  h  ||
        bbox_targets = np.zeros((labels.size, 4 * (frc.NUM_CLS + 1)), dtype=np.float32)
        inds = np.where(labels > 0)[0]
        for i in inds:
            cls = labels[i]
            start = int(4 * cls)
            end = start + 4
            bbox_targets[i, start:end] = bbox_targets_data[i, :]

        return rois, labels, bbox_targets

    # rpn rois: [x1, y1, x2, y2]
    # ground truth: [x1, y1, x2, y2, label]

    if frc.ADD_GT_BOX_TO_TRAIN:
        # Add ground truth bboxes to train.
        all_rois = np.vstack([rpn_rois, batch_gt_bboxes[:, :-1]])
        all_rois_image_ind_in_batch = np.vstack([batch_rois_indices, batch_gt_indices])
    else:
        all_rois = rpn_rois
        all_rois_image_ind_in_batch = batch_rois_indices

    # use OHEM(Online Hard Example Mining) set rois_per_image = INF
    rois_per_image = np.inf if frc.FASTER_RCNN_MINIBATCH_SIZE == -1 else frc.FASTER_RCNN_MINIBATCH_SIZE

    fg_rois_per_image = np.round(frc.FASTER_RCNN_POSITIVE_RATE * rois_per_image)

    batch_rois = []
    batch_labels = []
    batch_bbox_targets = []
    batch_roi_in_image_indices = []

    for ind_in_batch in range(frc.IMAGE_BATCH_SIZE):
        selected_batch_indices = np.where(all_rois_image_ind_in_batch == ind_in_batch)[0]
        selected_gt_indices = np.where(batch_gt_indices == ind_in_batch)[0]

        rois = all_rois[selected_batch_indices, :]
        gt_bboxes = batch_gt_bboxes[selected_gt_indices, :]

        rois, labels, bbox_targets = _get_rois_and_labels(rois, gt_bboxes, fg_rois_per_image)

        batch_rois.append(rois)
        batch_labels.append(labels)
        batch_bbox_targets.append(bbox_targets)
        batch_roi_in_image_indices.append(ind_in_batch * np.ones(len(labels), dtype=np.int32))

    batch_rois = np.concatenate(batch_rois, axis=0)
    batch_labels = np.concatenate(batch_labels, axis=0)
    batch_bbox_targets = np.concatenate(batch_bbox_targets, axis=0)
    batch_roi_in_image_indices = np.concatenate(batch_roi_in_image_indices, axis=0)

    return batch_rois, batch_roi_in_image_indices, batch_labels, batch_bbox_targets


def generate_rpn_labels_py(image_anchors, batch_gt_bboxes, gt_batch_indices, batch_image_shape):
    """
    Generate samples and labels to train RPN.
    :param image_anchors:
    :param batch_gt_bboxes:
    :param batch_image_shape:
    :return:
    """
    def _unmap(data, count, indexes, fill=0):
        """
        Unmap a subset of item (data) back to the original set of items (of
        size count)
        Reference:
        https://github.com/DetectionTeamUCAS/Faster-RCNN_Tensorflow/blob/db355dc5e22f7e4f3106038e5e621d04df64c876/libs/detection_oprations/anchor_target_layer_without_boxweight.py#L97
        """
        if len(data.shape) == 1:
            ret = np.empty((count,), dtype=np.float32)
            ret.fill(fill)
            ret[indexes] = data
        else:
            ret = np.empty((count,) + data.shape[1:], dtype=np.float32)
            ret.fill(fill)
            ret[indexes, :] = data
        return ret

    def _check_anchors(all_anchors, image_shape, allowed_boader=0):
        """
        Guarantee all anchors inner the image.
        :param all_anchors:
        :param image_shape:
        :param allowed_boader:
        :return:
        """
        image_height, image_width = image_shape
        inside_boader_indeces = np.where(
            (all_anchors[:, 0] >= -allowed_boader) &
            (all_anchors[:, 1] >= -allowed_boader) &
            (all_anchors[:, 2] < image_width + allowed_boader) &
            (all_anchors[:, 3] < image_height + allowed_boader)
        )[0]
        return inside_boader_indeces

    def _foreground_background_limit(labels):
        """
        Make limitation for rpn labels.
        1 for foreground, 0 for background, -1 for not care
        """
        fg_num = int(frc.RPN_MINIBATCH_SIZE * frc.RPN_FOREGROUND_FRACTION)
        bg_num = int(frc.RPN_MINIBATCH_SIZE - fg_num)

        fg_indexes = np.where(labels == 1)[0]
        bg_indexes = np.where(labels == 0)[0]

        if len(fg_indexes) > fg_num:
            disable_indexes = np.random.choice(fg_indexes, len(fg_indexes) - fg_num, replace=False)
            labels[disable_indexes] = -1

        if len(bg_indexes) > bg_num:
            disable_indexes = np.random.choice(bg_indexes, len(bg_indexes) - bg_num, replace=False)
            labels[disable_indexes] = -1

        return labels

    def _get_targets_and_labels(anchors, gt_bboxes):
        """Get rpn tagets and labels for each image in one batch."""
        # labels: positive=1; negative=0; not_care=-1
        labels = np.empty((len(anchors),), dtype=np.float32)
        labels.fill(-1)

        overlaps = get_overlaps_py(anchors, gt_bboxes)
        # overlaps: cross ious for each anchors and gt_boxes
        # rows: Anchors Indexes
        # columns: Ground Truth Bounding Box Indexes
        # For example K anchors with N ground truth bbox
        # overlaps is matrix have shape of K * N

        # For each anchor, get gt_bbox indices have max overlap region.
        # Shape: K
        max_overlap_gt_indices = np.argmax(overlaps, axis=1)
        # Get the max overlap for each anchor
        max_overlaps_for_each_anchor = overlaps[np.arange(len(inside_boarder_indices), dtype=np.int32),
                                                max_overlap_gt_indices]

        # For each gt_bbox, get anchor indices have max overlap region.
        # Shape: N
        max_overlap_anchor_indices = np.argmax(overlaps, axis=0)
        # Get the max overlap for each gt_bbox
        max_overlaps_for_each_gt = overlaps[max_overlap_anchor_indices,
                                            np.arange(len(gt_bboxes), dtype=np.int32)]

        # Find anchors have max overlap region
        max_overlap_indices = np.where(overlaps == max_overlaps_for_each_gt)[0]

        # Set negative labels
        labels[max_overlaps_for_each_anchor < frc.RPN_IOU_NEGATIVE_THRESHOLD] = 0

        # Set positive labels
        labels[max_overlap_indices] = 1
        labels[max_overlaps_for_each_anchor >= frc.RPN_IOU_POSITIVE_THRESHOLD] = 1

        bbox_targets = encode_bboxes(anchors, gt_bboxes[max_overlap_gt_indices, :])

        labels = _unmap(labels, image_anchors.shape[0], inside_boarder_indices, fill=-1)
        bbox_targets = _unmap(bbox_targets, image_anchors.shape[0], inside_boarder_indices)

        return labels, bbox_targets

    assert frc.IMAGE_BATCH_SIZE > 0

    batch_labels = []
    batch_bbox_targets = []
    for ind_in_batch in range(frc.IMAGE_BATCH_SIZE):
        image_shape = batch_image_shape[ind_in_batch]
        inside_boarder_indices = _check_anchors(image_anchors, image_shape)
        anchors = image_anchors[inside_boarder_indices, :]

        selected_gt_in_batch = np.where(gt_batch_indices == ind_in_batch)[0]
        batch_gt = batch_gt_bboxes[selected_gt_in_batch, :]

        labels, bbox_targets = _get_targets_and_labels(anchors, batch_gt)
        batch_labels.append(labels)
        batch_bbox_targets.append(bbox_targets)

    batch_labels = np.vstack(batch_labels)
    batch_bbox_targets = np.vstack(batch_bbox_targets)

    # Set foreground and background limitation, disable foreground and background exceeding the rpn batch size.
    batch_labels = _foreground_background_limit(batch_labels)

    batch_bbox_targets = batch_bbox_targets.reshape([-1, 4])
    batch_labels = batch_labels.reshape([-1, 1])
    # print(batch_labels.shape)
    # print(batch_bbox_targets.shape)
    assert batch_bbox_targets.shape[0] == batch_labels.shape[0] == \
           frc.IMAGE_BATCH_SIZE * frc.ANCHOR_NUM * frc.IMAGE_SHAPE[0] * frc.IMAGE_SHAPE[1] / (frc.FEATURE_STRIDE * frc.FEATURE_STRIDE)

    return batch_bbox_targets, batch_labels


def get_overlaps_py(pred_bboxes, gt_bboxes):
    """
    Calculate overlap area of predicted acnchors and ground truth. Inputs K anchors and N ground truth boxes, returns
    K * N array of ious.
    :param pred_bboxes: Generated anchors.
    :param gt_bboxes: Ground truth bounding boxes.
    :return: Intersection of Union of anchors and ground truth bounding box.
    """
    len_pred_bboxes = len(pred_bboxes)
    len_gt_bboxes = len(gt_bboxes)

    # generate indices for calculating ious as broadcast
    # For example, have 3 pred bboxes and 2 gt bboxes, return 3 x 2 ious.
    # The indices as follow when calculating the ious as broadcast
    # pred index        gt index
    # 0                 0
    # 0                 1
    # 1                 0
    # 1                 1
    # 2                 0
    # 2                 1
    pred_map_indices = np.arange(len_pred_bboxes, dtype=np.int32)
    pred_map_indices = np.repeat(pred_map_indices, (len_gt_bboxes,))
    gt_map_indices = np.arange(len_gt_bboxes, dtype=np.int32)
    gt_map_indices = np.repeat(gt_map_indices[:, np.newaxis], (len_pred_bboxes,), axis=1).transpose().ravel()

    # Can also use np.meshgrid to realize it
    # gt_map_indices, pred_map_indices = np.meshgrid(gt_map_indices, pred_map_indices)
    # pred_map_indices = pred_map_indices.ravel()
    # gt_map_indices = gt_map_indices.ravel()

    # find overlaps
    intersection_boxes_x1 = np.maximum(gt_bboxes[gt_map_indices, 0], pred_bboxes[pred_map_indices, 0])
    intersection_boxes_y1 = np.maximum(gt_bboxes[gt_map_indices, 1], pred_bboxes[pred_map_indices, 1])
    intersection_boxes_x2 = np.minimum(gt_bboxes[gt_map_indices, 2], pred_bboxes[pred_map_indices, 2])
    intersection_boxes_y2 = np.minimum(gt_bboxes[gt_map_indices, 3], pred_bboxes[pred_map_indices, 3])

    iws = intersection_boxes_x2 - intersection_boxes_x1 + 1
    ihs = intersection_boxes_y2 - intersection_boxes_y1 + 1

    less_zero_indices = np.bitwise_or(iws < 0, ihs < 0)
    iws[less_zero_indices] = 0
    ihs[less_zero_indices] = 0
    gt_bboxes_areas = (gt_bboxes[gt_map_indices, 2] - gt_bboxes[gt_map_indices, 0] + 1) * \
                      (gt_bboxes[gt_map_indices, 3] - gt_bboxes[gt_map_indices, 1] + 1)
    pred_bboxes_areas = (pred_bboxes[pred_map_indices, 2] - pred_bboxes[pred_map_indices, 0] + 1) * \
                        (pred_bboxes[pred_map_indices, 3] - pred_bboxes[pred_map_indices, 1] + 1)
    intersection_areas = iws * ihs
    ious = intersection_areas / (gt_bboxes_areas + pred_bboxes_areas - intersection_areas)

    return ious.reshape([len_pred_bboxes, len_gt_bboxes])


if __name__ == '__main__':
    gt_bboxes = np.random.randint(0, 256, (10, 4))
    labels = np.random.randint(0, 3, 10)
    gt_bboxes = np.sort(gt_bboxes, axis=1)
    gt_bboxes = np.concatenate([gt_bboxes, labels[:, np.newaxis]], axis=1)
    pred_bboxes = np.random.randint(0, 256, (15, 4))
    pred_bboxes = np.sort(pred_bboxes, axis=1)

    rois, labels2, bboxe_targets = process_proposal_targets_py(pred_bboxes, gt_bboxes)
    print(gt_bboxes)
    print(pred_bboxes)
    print(rois)
    print(labels)
    print(labels2)

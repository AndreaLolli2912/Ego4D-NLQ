"""VSLNet Baseline for Ego4D Episodic Memory -- Natural Language Queries.
"""
import torch
import torch.nn as nn
from transformers import get_linear_schedule_with_warmup

from model.layers import (
    Embedding,
    VisualProjection,
    FeatureEncoder,
    CQAttention,
    CQConcatenate,
    ConditionedPredictor,
    HighLightLayer,
)


def build_optimizer_and_scheduler(model, configs):
    no_decay = [
        "bias",
        "layer_norm",
        "LayerNorm",
    ]  # no decay for parameters of layer norm and bias
    optimizer_grouped_parameters = [
        {
            "params": [
                p
                for n, p in model.named_parameters()
                if not any(nd in n for nd in no_decay)
            ],
            "weight_decay": 0.01,
        },
        {
            "params": [
                p
                for n, p in model.named_parameters()
                if any(nd in n for nd in no_decay)
            ],
            "weight_decay": 0.0,
        },
    ]
    optimizer = torch.optim.AdamW(optimizer_grouped_parameters, lr=configs.init_lr)
    scheduler = get_linear_schedule_with_warmup(
        optimizer,
        configs.num_train_steps * configs.warmup_proportion,
        configs.num_train_steps,
    )
    return optimizer, scheduler

class TeacherVSLNetCBDK(nn.Module):
    """TeacherVSLNetCBKD"""
    def __init__(self, configs, word_vectors):
        super(TeacherVSLNetCBDK, self).__init__()
        self.configs = configs

        # Block 1 layers
        self.block1 = nn.ModuleDict({
            "video_affine": VisualProjection(
                visual_dim=configs.video_feature_dim,
                dim=configs.dim,
                drop_rate=configs.drop_rate,
            ),
            "embedding_net":  Embedding(
                num_words=configs.word_size,
                num_chars=configs.char_size,
                out_dim=configs.dim,
                word_dim=configs.word_dim,
                char_dim=configs.char_dim,
                word_vectors=word_vectors,
                drop_rate=configs.drop_rate,
            ),
        })

        # Block 2 layers
        self.block2 = nn.ModuleDict({
            "feature_encoder": FeatureEncoder(
                dim=configs.dim,
                num_heads=configs.num_heads,
                kernel_size=7,
                num_layers=4,
                max_pos_len=configs.max_pos_len,
                drop_rate=configs.drop_rate,
            ),
        })

        # Block 3 layers
        self.block3 = nn.ModuleDict({
            "cq_attention":    CQAttention(dim=configs.dim, drop_rate=configs.drop_rate),
            "cq_concat":       CQConcatenate(dim=configs.dim),
            "highlight_layer": HighLightLayer(dim=configs.dim),
        })

        # Block 4 layers
        self.block4 = nn.ModuleDict({
            "predictor": ConditionedPredictor(
                dim=configs.dim,
                num_heads=configs.num_heads,
                drop_rate=configs.drop_rate,
                max_pos_len=configs.max_pos_len,
                predictor=configs.predictor,
            ),
        })

        self.init_parameters()

    def init_parameters(self):
        def init_weights(m):
            if (
                isinstance(m, nn.Conv2d)
                or isinstance(m, nn.Conv1d)
                or isinstance(m, nn.Linear)
            ):
                torch.nn.init.xavier_uniform_(m.weight)
                if m.bias is not None:
                    torch.nn.init.zeros_(m.bias)
            elif isinstance(m, nn.LSTM):
                m.reset_parameters()

        self.apply(init_weights)

    def forward(self, word_ids, char_ids, video_features, v_mask, q_mask):
        """Forward as it is"""

        # Block 1
        video_features = self.block1["video_affine"](video_features)
        query_features = self.block1["embedding_net"](word_ids, char_ids)
        
        # Block 2
        query_features = self.block2["feature_encoder"](query_features, mask=q_mask)
        video_features = self.block2["feature_encoder"](
            video_features, 
            mask=v_mask,
            query_feats=query_features,
            film_mode=self.configs.film_mode
        )

        # Block 3
        features = self.block3["cq_attention"](video_features, query_features, v_mask, q_mask)
        features = self.block3["cq_concat"](features, query_features, q_mask)
        h_score = self.block3["highlight_layer"](features, v_mask)
        features = features * h_score.unsqueeze(2)

        # Block 4
        start_logits, end_logits = self.block4["predictor"](features, mask=v_mask)
        return h_score, start_logits, end_logits

    def extract_index(self, start_logits, end_logits):
        return self.block4["predictor"].extract_index(
            start_logits=start_logits, end_logits=end_logits
        )

    def compute_highlight_loss(self, scores, labels, mask):
        return self.block3["highlight_layer"].compute_loss(
            scores=scores, labels=labels, mask=mask
        )

    def compute_loss(self, start_logits, end_logits, start_labels, end_labels):
        return self.block4["predictor"].compute_cross_entropy_loss(
            start_logits=start_logits,
            end_logits=end_logits,
            start_labels=start_labels,
            end_labels=end_labels,
        )

import torch
import torch.nn as nn


class NullPhraseLayer(nn.Module):
    """ Dummy phrase layer that does nothing. Exists solely for API compatibility. """

    def __init__(self, input_dim: int):
        super(NullPhraseLayer, self).__init__()
        self.input_dim = input_dim

    def get_input_dim(self):
        return self.input_dim

    def get_output_dim(self):
        return 0

    def forward(self, embs, mask):
        return None


class Pooler(nn.Module):
    """ Do pooling, possibly with a projection beforehand """

    def __init__(self, project=True, d_inp=512, d_proj=512, pool_type="max"):
        super(Pooler, self).__init__()
        self.project = nn.Linear(d_inp, d_proj) if project else lambda x: x
        self.pool_type = pool_type

    def forward(self, sequence, mask):
        if len(mask.size()) < 3:
            mask = mask.unsqueeze(dim=-1)
        pad_mask = mask == 0
        proj_seq = self.project(sequence)  # linear project each hid state
        if self.pool_type == "max":
            proj_seq = proj_seq.masked_fill(pad_mask, -float("inf"))
            seq_emb = proj_seq.max(dim=1)[0]
        elif self.pool_type == "mean":
            proj_seq = proj_seq.masked_fill(pad_mask, 0)
            seq_emb = proj_seq.sum(dim=1) / mask.sum(dim=1)
        elif self.pool_type == "final":
            idxs = mask.expand_as(proj_seq).sum(dim=1, keepdim=True).long() - 1
            seq_emb = proj_seq.gather(dim=1, index=idxs)
        elif self.pool_type == "first":
            seq_emb = proj_seq[:, 0]
        return seq_emb


class Classifier(nn.Module):
    """ Logistic regression or MLP classifier """

    # NOTE: Expects dropout to have already been applied to its input.

    def __init__(self, d_inp, n_classes, cls_type="mlp", dropout=0.2, d_hid=512):
        super(Classifier, self).__init__()
        if cls_type == "log_reg":
            classifier = nn.Linear(d_inp, n_classes)
        elif cls_type == "mlp":
            classifier = nn.Sequential(
                nn.Linear(d_inp, d_hid),
                nn.Tanh(),
                nn.LayerNorm(d_hid),
                nn.Dropout(dropout),
                nn.Linear(d_hid, n_classes),
            )
        elif cls_type == "fancy_mlp":  # What they did in Infersent.
            classifier = nn.Sequential(
                nn.Linear(d_inp, d_hid),
                nn.Tanh(),
                nn.LayerNorm(d_hid),
                nn.Dropout(dropout),
                nn.Linear(d_hid, d_hid),
                nn.Tanh(),
                nn.LayerNorm(d_hid),
                nn.Dropout(p=dropout),
                nn.Linear(d_hid, n_classes),
            )
        else:
            raise ValueError("Classifier type %s not found" % type)
        self.classifier = classifier

    def forward(self, seq_emb):
        logits = self.classifier(seq_emb)
        return logits

    @classmethod
    def from_params(cls, d_inp, n_classes, params):
        return cls(
            d_inp,
            n_classes,
            cls_type=params["cls_type"],
            dropout=params["dropout"],
            d_hid=params["d_hid"],
        )


class SingleClassifier(nn.Module):
    """ Thin wrapper around a set of modules. For single-sentence classification. """

    def __init__(self, pooler, classifier):
        super(SingleClassifier, self).__init__()
        self.pooler = pooler
        self.classifier = classifier

    def forward(self, sent, mask, idxs=[]):
        """ Assumes batch_size x seq_len x d_emb """
        emb = self.pooler(sent, mask)

        # append any specific token representations, e.g. for WiC task
        ctx_embs = []
        for idx in [i.long() for i in idxs]:
            if len(idx.shape) == 1:
                idx = idx.unsqueeze(-1)
            if len(idx.shape) == 2:
                idx = idx.unsqueeze(-1).expand([-1, -1, sent.size(-1)])
            ctx_emb = sent.gather(dim=1, index=idx)
            ctx_embs.append(ctx_emb.squeeze(dim=1))
        final_emb = torch.cat([emb] + ctx_embs, dim=-1)

        logits = self.classifier(final_emb)
        return logits


class PairClassifier(nn.Module):
    """ Thin wrapper around a set of modules.
    For sentence pair classification.
    Pooler specifies how to aggregate inputted sequence of vectors.
    Also allows for use of specific token representations to be addded to the overall
    representation
    """

    def __init__(self, pooler, classifier, attn=None):
        super(PairClassifier, self).__init__()
        self.pooler = pooler
        self.classifier = classifier
        self.attn = attn

    def forward(self, s1, s2, mask1, mask2, idx1=[], idx2=[]):
        """ s1, s2: sequences of hidden states corresponding to sentence 1,2
            mask1, mask2: binary mask corresponding to non-pad elements
            idx{1,2}: indexes of particular tokens to extract in sentence {1, 2}
                and append to the representation, e.g. for WiC
        """
        mask1 = mask1.squeeze(-1) if len(mask1.size()) > 2 else mask1
        mask2 = mask2.squeeze(-1) if len(mask2.size()) > 2 else mask2
        if self.attn is not None:
            s1, s2 = self.attn(s1, s2, mask1, mask2)
        emb1 = self.pooler(s1, mask1)
        emb2 = self.pooler(s2, mask2)

        s1_ctx_embs = []
        for idx in [i.long() for i in idx1]:
            if len(idx.shape) == 1:
                idx = idx.unsqueeze(-1)
            if len(idx.shape) == 2:
                idx = idx.unsqueeze(-1).expand([-1, -1, s1.size(-1)])
            s1_ctx_emb = s1.gather(dim=1, index=idx)
            s1_ctx_embs.append(s1_ctx_emb.squeeze(dim=1))
        emb1 = torch.cat([emb1] + s1_ctx_embs, dim=-1)

        s2_ctx_embs = []
        for idx in [i.long() for i in idx2]:
            if len(idx.shape) == 1:
                idx = idx.unsqueeze(-1)
            if len(idx.shape) == 2:
                idx = idx.unsqueeze(-1).expand([-1, -1, s2.size(-1)])
            s2_ctx_emb = s2.gather(dim=1, index=idx)
            s2_ctx_embs.append(s2_ctx_emb.squeeze(dim=1))
        emb2 = torch.cat([emb2] + s2_ctx_embs, dim=-1)

        pair_emb = torch.cat([emb1, emb2, torch.abs(emb1 - emb2), emb1 * emb2], 1)
        logits = self.classifier(pair_emb)
        return logits